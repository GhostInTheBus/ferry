"""FTP / FTPS via aioftp.

The FTP control channel handles one command at a time, so metadata calls
share one locked client and every data transfer opens its own login.
"""
from __future__ import annotations

import asyncio
import calendar
import posixpath
import ssl
import time
from typing import AsyncIterator, Optional

import aioftp

from .base import (CHUNK, Backend, BackendError, Disconnected, Entry, NotFound,
                   PermissionDenied, Writer, is_hidden, join, norm)


def _parse_modify(s: Optional[str]) -> float:
    if not s:
        return 0.0
    s = s.split(".")[0]
    try:
        return float(calendar.timegm(time.strptime(s[:14], "%Y%m%d%H%M%S")))
    except Exception:
        return 0.0


class _FTPWriter(Writer):
    def __init__(self, client, stream):
        self.client = client
        self.stream = stream

    async def write(self, data: bytes) -> None:
        await self.stream.write(data)

    async def close(self) -> None:
        await self.stream.finish()
        try:
            await self.client.quit()
        except Exception:
            self.client.close()

    async def abort(self) -> None:
        try:
            self.stream.close()
        except Exception:
            pass
        self.client.close()


class FTPBackend(Backend):
    kind = "ftp"

    def __init__(self, conn, on_config_update=None):
        super().__init__(conn, on_config_update)
        self.client: Optional[aioftp.Client] = None
        self.lock = asyncio.Lock()
        self.base = norm(self.cfg.get("root") or "/")

    def _p(self, path: str) -> str:
        return norm(posixpath.join(self.base, norm(path).lstrip("/")))

    async def _new_client(self) -> aioftp.Client:
        c = self.cfg
        host = (c.get("host") or "").strip()
        if not host:
            raise BackendError("Host is required")
        port = int(c.get("port") or 21)
        client = aioftp.Client(socket_timeout=60, connection_timeout=15, path_timeout=60,
                               encoding=c.get("encoding") or "utf-8")
        try:
            await client.connect(host, port)
            if c.get("tls"):
                ctx = ssl.create_default_context()
                if not c.get("verify_tls"):
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                await client.upgrade_to_tls(ctx)
            await client.login(c.get("username") or "anonymous", c.get("password") or "")
        except aioftp.StatusCodeError as e:
            client.close()
            if "530" in [str(x) for x in e.received_codes]:
                raise PermissionDenied("FTP login failed, check user and password") from e
            raise BackendError(f"FTP error: {e.info}") from e
        except (OSError, asyncio.TimeoutError) as e:
            client.close()
            raise Disconnected(f"Can't reach {host}:{port} ({e.__class__.__name__})") from e
        return client

    async def connect(self) -> None:
        self.client = await self._new_client()

    async def close(self) -> None:
        self.dead = True
        if self.client:
            try:
                await asyncio.wait_for(self.client.quit(), 3)
            except Exception:
                self.client.close()

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, BackendError):
            return e
        if isinstance(e, aioftp.StatusCodeError):
            codes = [str(x) for x in e.received_codes]
            info = " ".join(e.info) if isinstance(e.info, (list, tuple)) else str(e.info)
            if "550" in codes:
                low = info.lower()
                if "permission" in low or "denied" in low:
                    return PermissionDenied(info)
                return NotFound(info or "Not found")
            if any(c.startswith("4") for c in codes):
                return Disconnected(info)
            return BackendError(f"FTP {','.join(codes)}: {info}")
        if isinstance(e, (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError)):
            self.dead = True
            return Disconnected(str(e) or e.__class__.__name__)
        return super().translate(e)

    def _entry(self, path: str, info: dict) -> Entry:
        name = posixpath.basename(path) or "/"
        is_dir = info.get("type") == "dir"
        size = 0 if is_dir else int(info.get("size") or 0)
        return Entry(name=name, path=path, is_dir=is_dir, size=size,
                     mtime=_parse_modify(info.get("modify")), hidden=is_hidden(name))

    async def list(self, path: str) -> list[Entry]:
        path = norm(path)
        async with self.lock:
            items = await self.client.list(self._p(path))
        out = []
        for p, info in items:
            name = p.name
            if name in (".", ".."):
                continue
            out.append(self._entry(join(path, name), info))
        return out

    async def stat(self, path: str) -> Entry:
        path = norm(path)
        if self._p(path) == "/":
            return Entry(name="/", path="/", is_dir=True)
        async with self.lock:
            info = await self.client.stat(self._p(path))
        return self._entry(path, info)

    async def mkdir(self, path: str) -> None:
        async with self.lock:
            await self.client.make_directory(self._p(path), parents=False)

    async def remove(self, path: str) -> None:
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")
        st = await self.stat(path)
        async with self.lock:
            if st.is_dir:
                await self.client.remove_directory(self._p(path))
            else:
                await self.client.remove_file(self._p(path))

    async def rename(self, src: str, dst: str) -> None:
        async with self.lock:
            await self.client.rename(self._p(src), self._p(dst))

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        client = await self._new_client()
        stream = None
        finished = False
        try:
            stream = await client.download_stream(self._p(path), offset=offset)
            remaining = length
            async for block in stream.iter_by_block(CHUNK):
                if remaining is not None:
                    if remaining <= 0:
                        break
                    block = block[:remaining]
                    remaining -= len(block)
                yield block
                if remaining is not None and remaining <= 0:
                    break
            else:
                await stream.finish()
                finished = True
        finally:
            if not finished and stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                client.close()
            else:
                try:
                    await asyncio.wait_for(client.quit(), 3)
                except Exception:
                    client.close()

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        client = await self._new_client()
        try:
            stream = await client.upload_stream(self._p(path))
        except Exception:
            client.close()
            raise
        return _FTPWriter(client, stream)
