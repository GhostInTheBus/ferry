"""Backend factory plus a manager that keeps one live session per connection."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Awaitable, Callable, TypeVar

from .. import db
from .base import Backend, BackendError, Disconnected, NotFound

T = TypeVar("T")


def make_backend(conn: dict, on_update=None) -> Backend:
    t = conn["type"]
    if t == "local":
        from .local import LocalBackend as B
    elif t == "sftp":
        from .sftp import SFTPBackend as B
    elif t == "smb":
        from .smb import SMBBackend as B
    elif t == "ftp":
        from .ftp import FTPBackend as B
    elif t == "webdav":
        from .webdav import WebDAVBackend as B
    elif t == "s3":
        from .s3 import S3Backend as B
    elif t == "dropbox":
        from .dropbox_ import DropboxBackend as B
    else:
        raise BackendError(f"Unknown connection type {t}")
    return B(conn, on_update)


class Manager:
    IDLE_CLOSE = 15 * 60

    def __init__(self):
        self.live: dict[str, Backend] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, cid: str) -> Backend:
        async with self.locks[cid]:
            b = self.live.get(cid)
            if b is not None and b.alive():
                b.last_used = time.time()
                return b
            if b is not None:
                self.live.pop(cid, None)
                asyncio.create_task(b.close())
            conn = db.get_connection(cid)
            if not conn:
                raise NotFound("That connection no longer exists")
            b = make_backend(conn, on_update=lambda cfg, cid=cid: db.save_config(cid, cfg))
            try:
                await asyncio.wait_for(b.connect(), 25)
            except asyncio.TimeoutError as e:
                raise Disconnected(f"Timed out connecting to {conn['name']}") from e
            except Exception as e:
                raise b.translate(e) from e
            self.live[cid] = b
            return b

    async def drop(self, cid: str) -> None:
        b = self.live.pop(cid, None)
        if b is not None:
            try:
                await asyncio.wait_for(b.close(), 5)
            except Exception:
                pass

    async def run(self, cid: str, fn: Callable[[Backend], Awaitable[T]], retry: bool = True) -> T:
        """Run fn against the connection, reconnecting once if the link dropped."""
        for attempt in (0, 1):
            b = await self.get(cid)
            try:
                return await fn(b)
            except Exception as e:
                err = b.translate(e)
                if isinstance(err, Disconnected):
                    await self.drop(cid)
                    if retry and attempt == 0:
                        continue
                raise err from e
        raise Disconnected("Connection lost")  # pragma: no cover

    async def reap_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for cid, b in list(self.live.items()):
                if now - b.last_used > self.IDLE_CLOSE and not getattr(b, "busy", 0):
                    await self.drop(cid)

    async def close_all(self) -> None:
        for cid in list(self.live):
            await self.drop(cid)


manager = Manager()
