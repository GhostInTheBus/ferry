"""SMB (Windows shares, Mac file sharing, other NAS boxes) via smbprotocol."""
from __future__ import annotations

import asyncio
import posixpath
from typing import AsyncIterator, Optional

import smbclient
from smbprotocol import exceptions as smbexc

from .. import tailnet
from .base import (CHUNK, Backend, BackendError, Disconnected, Entry, NotFound,
                   PermissionDenied, AlreadyExists, Writer, is_hidden, join, norm)


class _SMBWriter(Writer):
    def __init__(self, f):
        self.f = f

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self.f.write, data)

    async def close(self) -> None:
        await asyncio.to_thread(self.f.close)

    async def abort(self) -> None:
        try:
            await asyncio.to_thread(self.f.close)
        except Exception:
            pass


class SMBBackend(Backend):
    kind = "smb"

    def __init__(self, conn, on_config_update=None):
        super().__init__(conn, on_config_update)
        self.cache: dict = {}
        self.fwd = None
        c = self.cfg
        self.host = (c.get("host") or "").strip()
        self.share = (c.get("share") or "").strip().strip("/\\")
        self.base = norm(c.get("root") or "/")
        self.kw = dict(username=c.get("username") or None,
                       password=c.get("password") or None,
                       port=int(c.get("port") or 445),
                       connection_cache=self.cache)

    def _p(self, path: str) -> str:
        full = norm(posixpath.join(self.base, norm(path).lstrip("/")))
        tail = full.replace("/", "\\").rstrip("\\")
        return f"\\\\{self.host}\\{self.share}{tail}"

    async def connect(self) -> None:
        if not self.host or not self.share:
            raise BackendError("Host and share are required")
        if tailnet.wanted(self.cfg):
            self.fwd = tailnet.Forwarder(self.host, self.kw["port"])
            self.kw["port"] = await self.fwd.start()
            self.host = "127.0.0.1"
        try:
            await asyncio.to_thread(
                smbclient.register_session, self.host,
                username=self.kw["username"], password=self.kw["password"],
                port=self.kw["port"], connection_timeout=15,
                connection_cache=self.cache)
            await asyncio.to_thread(smbclient.stat, self._p("/"), **self.kw)
        except Exception as e:
            raise self.translate(e) from e

    async def close(self) -> None:
        self.dead = True
        try:
            await asyncio.to_thread(smbclient.reset_connection_cache, connection_cache=self.cache)
        except Exception:
            pass
        if self.fwd:
            await self.fwd.close()

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, BackendError):
            return e
        if isinstance(e, smbexc.SMBOSError):
            import errno
            if e.errno == errno.ENOENT:
                return NotFound(str(e))
            if e.errno == errno.EACCES:
                return PermissionDenied(str(e))
            if e.errno == errno.EEXIST:
                return AlreadyExists(str(e))
            return BackendError(str(e))
        if isinstance(e, smbexc.LogonFailure):
            return PermissionDenied("SMB login failed, check user and password")
        if isinstance(e, (smbexc.SMBConnectionClosed, smbexc.SMBException, ValueError)) and \
                "connect" in str(e).lower():
            self.dead = True
            return Disconnected(str(e))
        if isinstance(e, (OSError, TimeoutError)) and not isinstance(e, (FileNotFoundError, PermissionError, FileExistsError)):
            self.dead = True
            return Disconnected(f"{e.__class__.__name__}: {e}")
        return super().translate(e)

    def _entry(self, path: str, name: str, is_dir: bool, size: int, mtime: float) -> Entry:
        return Entry(name=name, path=path, is_dir=is_dir, size=0 if is_dir else size,
                     mtime=mtime, hidden=is_hidden(name))

    async def list(self, path: str) -> list[Entry]:
        path = norm(path)

        def scan():
            out = []
            for d in smbclient.scandir(self._p(path), **self.kw):
                # the listing already carries size and date, stat() would cost a round trip each
                info = d.smb_info
                out.append(self._entry(join(path, d.name), d.name, d.is_dir(),
                                       int(info.end_of_file), info.last_write_time.timestamp()))
            return out

        return await asyncio.to_thread(scan)

    async def stat(self, path: str) -> Entry:
        import stat as _s
        path = norm(path)
        st = await asyncio.to_thread(smbclient.stat, self._p(path), **self.kw)
        return self._entry(path, posixpath.basename(path) or "/", _s.S_ISDIR(st.st_mode),
                           st.st_size, st.st_mtime)

    async def mkdir(self, path: str) -> None:
        await asyncio.to_thread(smbclient.mkdir, self._p(path), **self.kw)

    async def remove(self, path: str) -> None:
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")
        st = await self.stat(path)
        fn = smbclient.rmdir if st.is_dir else smbclient.remove
        await asyncio.to_thread(fn, self._p(path), **self.kw)

    async def rename(self, src: str, dst: str) -> None:
        await asyncio.to_thread(smbclient.rename, self._p(src), self._p(dst), **self.kw)

    async def set_mtime(self, path: str, mtime: float) -> None:
        await asyncio.to_thread(smbclient.utime, self._p(path), times=(mtime, mtime), **self.kw)

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        f = await asyncio.to_thread(smbclient.open_file, self._p(path), "rb", **self.kw)
        try:
            if offset:
                await asyncio.to_thread(f.seek, offset)
            remaining = length
            while True:
                n = CHUNK if remaining is None else min(CHUNK, remaining)
                if n <= 0:
                    break
                data = await asyncio.to_thread(f.read, n)
                if not data:
                    break
                if remaining is not None:
                    remaining -= len(data)
                yield data
        finally:
            try:
                await asyncio.to_thread(f.close)
            except Exception:
                pass

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        f = await asyncio.to_thread(smbclient.open_file, self._p(path), "wb", **self.kw)
        return _SMBWriter(f)
