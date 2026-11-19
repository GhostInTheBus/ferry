"""SFTP over SSH. Used for the Mac (System Settings > Sharing > Remote Login)."""
from __future__ import annotations

import asyncio
import posixpath
import stat as _stat
from typing import AsyncIterator, Optional

import asyncssh

from .. import config, tailnet
from .base import (Backend, BackendError, Disconnected, Entry, NotFound,
                   PermissionDenied, Unsupported, Writer, is_hidden, join, norm)

SFTP_CHUNK = 4 << 20  # bigger reads let asyncssh pipeline requests over high latency links


class _Client(asyncssh.SSHClient):
    def __init__(self, backend: "SFTPBackend"):
        self.backend = backend

    def connection_lost(self, exc):
        self.backend.dead = True


class _SFTPWriter(Writer):
    def __init__(self, f, pos: int = 0):
        self.f = f
        self.pos = pos

    async def write(self, data: bytes) -> None:
        await self.f.write(data, self.pos)
        self.pos += len(data)

    async def close(self) -> None:
        await self.f.close()

    async def abort(self) -> None:
        try:
            await self.f.close()
        except Exception:
            pass


class SFTPBackend(Backend):
    kind = "sftp"
    resumable = True

    def __init__(self, conn, on_config_update=None):
        super().__init__(conn, on_config_update)
        self.ssh = None
        self.sftp = None
        self.root = "/"

    # lifecycle -----------------------------------------------------------
    async def connect(self) -> None:
        c = self.cfg
        host = c.get("host", "").strip()
        port = int(c.get("port") or 22)
        if not host:
            raise BackendError("Host is required")
        pattern = host if port == 22 else f"[{host}]:{port}"
        known = None
        if c.get("host_key"):
            known = asyncssh.import_known_hosts(f"{pattern} {c['host_key']}\n")
        kw = dict(host=host, port=port, username=c.get("username") or None,
                  known_hosts=known, connect_timeout=15,
                  keepalive_interval=30, keepalive_count_max=4,
                  agent_path=None, client_factory=lambda: _Client(self))
        if tailnet.wanted(c):
            kw["proxy_command"] = tailnet.nc_command(host, port)
        if (c.get("auth") or "key") == "password":
            kw.update(password=c.get("password") or "", client_keys=None)
        else:
            kw.update(client_keys=[config.ssh_private_key_path()], password=None)
        try:
            self.ssh = await asyncssh.connect(**kw)
        except asyncssh.HostKeyNotVerifiable as e:
            raise BackendError(
                "The SSH host key changed since Ferry first connected. If you reinstalled "
                "or replaced that machine, clear the saved host key in the connection settings."
            ) from e
        except asyncssh.PermissionDenied as e:
            raise PermissionDenied(
                "SSH login was rejected. For key login, add Ferry's public key "
                "(Connections > Ferry SSH key) to ~/.ssh/authorized_keys on that machine."
            ) from e
        except (OSError, asyncio.TimeoutError) as e:
            raise Disconnected(f"Can't reach {host}:{port} ({e.__class__.__name__}: {e})") from e

        if not c.get("host_key"):
            key = self.ssh.get_server_host_key()
            if key is not None:
                self.cfg["host_key"] = key.export_public_key("openssh").decode().strip()
                if self.on_config_update:
                    self.on_config_update(self.cfg)
        self.sftp = await self.ssh.start_sftp_client()
        self.root = await self.sftp.realpath(c.get("root") or ".")

    async def close(self) -> None:
        self.dead = True
        try:
            if self.sftp:
                self.sftp.exit()
            if self.ssh:
                self.ssh.close()
                await asyncio.wait_for(self.ssh.wait_closed(), 5)
        except Exception:
            pass

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, BackendError):
            return e
        if isinstance(e, asyncssh.SFTPError):
            if e.code == asyncssh.FX_NO_SUCH_FILE or e.code == getattr(asyncssh, "FX_NO_SUCH_PATH", -1):
                return NotFound(e.reason or "No such file")
            if e.code == asyncssh.FX_PERMISSION_DENIED:
                return PermissionDenied(
                    (e.reason or "Permission denied") +
                    ". On a Mac, folders like Documents and Desktop need "
                    "'Allow full disk access for remote users' in Remote Login settings.")
            if e.code in (asyncssh.FX_CONNECTION_LOST, asyncssh.FX_NO_CONNECTION):
                self.dead = True
                return Disconnected(e.reason or "SFTP connection lost")
            if e.code == asyncssh.FX_OP_UNSUPPORTED:
                return Unsupported(e.reason or "Not supported by the server")
            return BackendError(e.reason or str(e))
        if isinstance(e, (asyncssh.DisconnectError, asyncssh.ConnectionLost,
                          asyncssh.ChannelOpenError, BrokenPipeError, ConnectionError)):
            self.dead = True
            return Disconnected(str(e) or "SSH connection lost")
        return super().translate(e)

    # helpers -------------------------------------------------------------
    def _p(self, path: str) -> str:
        rel = norm(path).lstrip("/")
        return posixpath.join(self.root, rel) if rel else self.root

    @staticmethod
    def _is_dir(attrs) -> bool:
        if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
            return True
        return attrs.permissions is not None and _stat.S_ISDIR(attrs.permissions)

    @staticmethod
    def _is_link(attrs) -> bool:
        if attrs.type == asyncssh.FILEXFER_TYPE_SYMLINK:
            return True
        return attrs.permissions is not None and _stat.S_ISLNK(attrs.permissions)

    def _entry(self, path: str, name: str, attrs) -> Entry:
        is_dir = self._is_dir(attrs)
        return Entry(name=name, path=path, is_dir=is_dir,
                     size=0 if is_dir else int(attrs.size or 0),
                     mtime=float(attrs.mtime or 0), hidden=is_hidden(name),
                     mode=(attrs.permissions or 0) & 0o7777)

    # metadata ------------------------------------------------------------
    async def list(self, path: str) -> list[Entry]:
        path = norm(path)
        names = await self.sftp.readdir(self._p(path))
        out = []
        for n in names:
            name = n.filename
            if name in (".", ".."):
                continue
            attrs = n.attrs
            child = join(path, name)
            if self._is_link(attrs):
                try:
                    attrs = await self.sftp.stat(self._p(child))
                except asyncssh.SFTPError:
                    pass
            out.append(self._entry(child, name, attrs))
        return out

    async def stat(self, path: str) -> Entry:
        path = norm(path)
        attrs = await self.sftp.stat(self._p(path))
        return self._entry(path, posixpath.basename(path) or "/", attrs)

    async def mkdir(self, path: str) -> None:
        await self.sftp.mkdir(self._p(path))

    async def remove(self, path: str) -> None:
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")
        p = self._p(path)
        attrs = await self.sftp.lstat(p)
        if self._is_dir(attrs):
            await self.sftp.rmdir(p)
        else:
            await self.sftp.remove(p)

    async def rename(self, src: str, dst: str) -> None:
        await self.sftp.rename(self._p(src), self._p(dst))

    async def set_mtime(self, path: str, mtime: float) -> None:
        await self.sftp.utime(self._p(path), (mtime, mtime))

    async def set_mode(self, path: str, mode: int) -> None:
        await self.sftp.chmod(self._p(path), mode & 0o7777)

    async def set_attrs(self, path, mtime, mode) -> None:
        # one round trip instead of two, it adds up over a slow link
        attrs = asyncssh.SFTPAttrs()
        if mtime:
            attrs.atime = attrs.mtime = int(mtime)
        if mode:
            attrs.permissions = mode & 0o7777
        if mtime or mode:
            await self.sftp.setstat(self._p(path), attrs)

    # data ----------------------------------------------------------------
    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        f = await self.sftp.open(self._p(path), "rb")
        try:
            pos = offset
            remaining = length
            while True:
                n = SFTP_CHUNK if remaining is None else min(SFTP_CHUNK, remaining)
                if n <= 0:
                    break
                data = await f.read(n, pos)
                if not data:
                    break
                pos += len(data)
                if remaining is not None:
                    remaining -= len(data)
                yield data
        finally:
            try:
                await f.close()
            except Exception:
                pass

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        if offset:
            try:
                f = await self.sftp.open(self._p(path), "r+b")
                await f.truncate(offset)
                return _SFTPWriter(f, offset)
            except asyncssh.SFTPError:
                pass
        f = await self.sftp.open(self._p(path), "wb")
        return _SFTPWriter(f)

    async def disk_usage(self, path: str = "/") -> dict:
        try:
            v = await self.sftp.statvfs(self._p(path))
            total = v.f_blocks * v.f_frsize
            free = v.f_bavail * v.f_frsize
            return {"total": total, "used": total - free, "free": free}
        except Exception:
            return {}
