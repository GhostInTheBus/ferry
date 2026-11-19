"""Files on the machine Ferry runs on (the NAS volume mounted into Docker)."""
from __future__ import annotations

import asyncio
import errno
import os
import posixpath
import shutil
from typing import AsyncIterator, Optional

from .base import (CHUNK, Backend, Entry, NotFound, PermissionDenied, Writer,
                   is_hidden, join, norm)


class _LocalWriter(Writer):
    def __init__(self, real: str, offset: int = 0):
        self.real = real
        self.offset = offset
        self.f = None

    async def _open(self):
        def op():
            if self.offset and os.path.exists(self.real):
                f = open(self.real, "r+b")
                f.seek(self.offset)
                f.truncate()
                return f
            return open(self.real, "wb")
        self.f = await asyncio.to_thread(op)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self.f.write, data)

    async def close(self) -> None:
        await asyncio.to_thread(self.f.close)

    async def abort(self) -> None:
        try:
            await asyncio.to_thread(self.f.close)
        except Exception:
            pass


TOP_LEVEL = ("The top level of this NAS only holds shared folders. "
             "Open a shared folder first, then copy into it.")


class LocalBackend(Backend):
    kind = "local"
    resumable = True

    def __init__(self, conn, on_config_update=None):
        super().__init__(conn, on_config_update)
        self.root = os.path.realpath(self.cfg.get("root") or "/nas")

    def _real(self, path: str) -> str:
        rel = norm(path).lstrip("/")
        real = os.path.realpath(os.path.join(self.root, rel))
        if real != self.root and not real.startswith(self.root.rstrip(os.sep) + os.sep):
            raise PermissionDenied("Path is outside this connection")
        return real

    async def connect(self) -> None:
        if not os.path.isdir(self.root):
            raise NotFound(f"Folder {self.root} does not exist inside the container")

    def _entry(self, path: str, name: str, st, is_dir: bool) -> Entry:
        return Entry(name=name, path=path, is_dir=is_dir,
                     size=0 if is_dir else st.st_size, mtime=st.st_mtime,
                     hidden=is_hidden(name), mode=st.st_mode & 0o7777)

    async def list(self, path: str) -> list[Entry]:
        real = self._real(path)
        path = norm(path)

        def scan():
            out = []
            with os.scandir(real) as it:
                for d in it:
                    try:
                        st = d.stat(follow_symlinks=True)
                        is_dir = d.is_dir(follow_symlinks=True)
                    except OSError:
                        # broken symlink, show it as an empty file
                        try:
                            st = d.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        is_dir = False
                    out.append(self._entry(join(path, d.name), d.name, st, is_dir))
            return out

        return await asyncio.to_thread(scan)

    async def stat(self, path: str) -> Entry:
        real = self._real(path)
        path = norm(path)
        st = await asyncio.to_thread(os.stat, real)
        import stat as _s
        name = os.path.basename(path) or "/"
        return self._entry(path, name, st, _s.S_ISDIR(st.st_mode))

    def _top_level_guard(self, path: str, e: Exception) -> None:
        if isinstance(e, PermissionError) and posixpath.dirname(norm(path)) == "/":
            raise PermissionDenied(TOP_LEVEL) from e

    async def mkdir(self, path: str) -> None:
        try:
            await asyncio.to_thread(os.mkdir, self._real(path))
        except PermissionError as e:
            self._top_level_guard(path, e)
            raise

    async def remove(self, path: str) -> None:
        real = self._real(path)
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")

        def rm():
            if os.path.isdir(real) and not os.path.islink(real):
                os.rmdir(real)
            else:
                os.remove(real)

        await asyncio.to_thread(rm)

    async def rmtree(self, path: str) -> None:
        real = self._real(path)
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")

        def rm():
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)

        await asyncio.to_thread(rm)

    async def rename(self, src: str, dst: str) -> None:
        rs, rd = self._real(src), self._real(dst)

        def mv():
            try:
                os.rename(rs, rd)
            except OSError as e:
                # Synology shares are separate btrfs subvolumes
                if e.errno == errno.EXDEV:
                    shutil.move(rs, rd)
                else:
                    raise

        await asyncio.to_thread(mv)

    async def set_mtime(self, path: str, mtime: float) -> None:
        real = self._real(path)
        await asyncio.to_thread(os.utime, real, (mtime, mtime))

    async def set_mode(self, path: str, mode: int) -> None:
        await asyncio.to_thread(os.chmod, self._real(path), mode & 0o7777)

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        real = self._real(path)
        f = await asyncio.to_thread(open, real, "rb")
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
            await asyncio.to_thread(f.close)

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        w = _LocalWriter(self._real(path), offset)
        try:
            await w._open()
        except PermissionError as e:
            self._top_level_guard(path, e)
            raise
        return w

    async def local_path(self, path: str) -> Optional[str]:
        return self._real(path)

    async def disk_usage(self, path: str = "/") -> dict:
        u = await asyncio.to_thread(shutil.disk_usage, self._real(path))
        return {"total": u.total, "used": u.used, "free": u.free}
