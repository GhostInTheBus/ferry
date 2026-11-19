"""Common storage backend interface.

Every connection type (local disk, SFTP, SMB, FTP, Dropbox) implements the
same small async API. Paths given to a backend are always POSIX style and
relative to the connection root, e.g. "/Photos/2026". The root is "/".
"""
from __future__ import annotations

import hashlib
import posixpath
import time
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Callable, Optional

CHUNK = 1 << 20  # 1 MiB
# Writers never have more than this much in flight, so resuming a part file
# from one alignment step before its end can't leave a hole
RESUME_ALIGN = 4 << 20

HIDDEN_NAMES = {
    "@eaDir", "#recycle", "#snapshot", "@tmp", "@sharebin",
    "Thumbs.db", "desktop.ini", ".DS_Store", "$RECYCLE.BIN",
    "System Volume Information",
}
PART_SUFFIX = ".ferry-part"


def norm(path: Optional[str]) -> str:
    """Normalize a client supplied path to '/a/b'. Never escapes the root."""
    if not path:
        return "/"
    p = posixpath.normpath("/" + path.replace("\\", "/").lstrip("/"))
    return "/" if p in (".", "//") else p


def join(parent: str, name: str) -> str:
    return norm(posixpath.join(norm(parent), name))


def is_hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("@") or name in HIDDEN_NAMES


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    hidden: bool = False
    mode: int = 0  # permission bits when the backend knows them
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        extra = d.pop("extra")
        d.update(extra)
        return d


class BackendError(Exception):
    status = 500


class NotFound(BackendError):
    status = 404


class AlreadyExists(BackendError):
    status = 409


class PermissionDenied(BackendError):
    status = 403


class Disconnected(BackendError):
    """Connection level failure. The manager reconnects and may retry."""
    status = 503


class Unsupported(BackendError):
    status = 501


class Writer:
    async def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def abort(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def part_name(name: str, size: int = 0, mtime: float = 0) -> str:
    """Hidden temp name. Size and date are in it so a leftover part is only
    ever resumed from the same version of the source file."""
    stem = name if len(name.encode()) < 180 else hashlib.sha1(name.encode()).hexdigest()[:16]
    return f".{stem}.{size}-{int(mtime)}{PART_SUFFIX}" if size else f".{stem}{PART_SUFFIX}"


class PartWriter(Writer):
    """Writes to a hidden .ferry-part file, renames into place on close.

    A cancelled or failed transfer never leaves a half written file under
    the real name.
    """

    def __init__(self, backend: "Backend", final: str, raw: Writer, tmp: str,
                 mtime: Optional[float], mode: Optional[int], replace: Optional[bool]):
        self.backend = backend
        self.final = final
        self.raw = raw
        self.tmp = tmp
        self.mtime = mtime
        self.mode = mode
        self.replace = replace
        self.done = False

    async def write(self, data: bytes) -> None:
        await self.raw.write(data)

    async def close(self) -> None:
        await self.raw.close()
        try:
            await self.backend.set_attrs(self.tmp, self.mtime, self.mode)
        except Exception:
            pass
        replace = self.replace
        if replace is None:
            replace = await self.backend.exists(self.final)
        if replace:
            try:
                await self.backend.remove(self.final)
            except Exception as e:
                if not isinstance(self.backend.translate(e), NotFound):
                    raise
        await self.backend.rename(self.tmp, self.final)
        self.done = True

    async def abort(self, keep: bool = False) -> None:
        """Drop the part file, or keep it so a later run can resume."""
        try:
            await self.raw.abort()
        except Exception:
            pass
        if keep:
            return
        try:
            await self.backend.remove(self.tmp)
        except Exception:
            pass


class Backend:
    kind = "base"
    # True when ranged reads are cheap (video seeking works well)
    seekable = True
    # True when a part file can be reopened and continued at an offset
    resumable = False

    def __init__(self, conn: dict, on_config_update: Optional[Callable[[dict], None]] = None):
        self.conn = conn
        self.id = conn.get("id")
        self.cfg: dict = dict(conn.get("config") or {})
        self.on_config_update = on_config_update
        self.dead = False
        self.last_used = time.time()

    # lifecycle -----------------------------------------------------------
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def alive(self) -> bool:
        return not self.dead

    def translate(self, e: Exception) -> BackendError:
        """Map a library specific exception to a BackendError."""
        if isinstance(e, BackendError):
            return e
        if isinstance(e, FileNotFoundError):
            return NotFound(str(e))
        if isinstance(e, FileExistsError):
            return AlreadyExists(str(e))
        if isinstance(e, PermissionError):
            return PermissionDenied(str(e))
        if isinstance(e, (ConnectionError, TimeoutError, EOFError)):
            return Disconnected(str(e) or e.__class__.__name__)
        return BackendError(f"{e.__class__.__name__}: {e}")

    # metadata ------------------------------------------------------------
    async def list(self, path: str) -> list[Entry]:
        raise NotImplementedError

    async def stat(self, path: str) -> Entry:
        raise NotImplementedError

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except Exception as e:
            if isinstance(self.translate(e), NotFound):
                return False
            raise

    async def mkdir(self, path: str) -> None:
        raise NotImplementedError

    async def makedirs(self, path: str) -> None:
        path = norm(path)
        if path == "/":
            return
        parts = path.strip("/").split("/")
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            try:
                st = await self.stat(cur)
                if not st.is_dir:
                    raise AlreadyExists(f"{cur} exists and is a file")
                continue
            except Exception as e:
                err = self.translate(e)
                if not isinstance(err, NotFound):
                    raise err
            try:
                await self.mkdir(cur)
            except Exception as e:
                # lost a race with another worker creating the same folder
                if not await self.exists(cur):
                    raise self.translate(e)

    async def remove(self, path: str) -> None:
        """Remove a file or an empty folder."""
        raise NotImplementedError

    async def rmtree(self, path: str) -> None:
        st = await self.stat(path)
        if st.is_dir:
            for child in await self.list(path):
                await self.rmtree(child.path)
        await self.remove(path)

    async def rename(self, src: str, dst: str) -> None:
        raise NotImplementedError

    async def set_mtime(self, path: str, mtime: float) -> None:
        pass

    async def set_mode(self, path: str, mode: int) -> None:
        pass

    async def set_attrs(self, path: str, mtime: Optional[float], mode: Optional[int]) -> None:
        if mtime:
            await self.set_mtime(path, mtime)
        if mode:
            try:
                await self.set_mode(path, mode)
            except Exception:
                pass  # shares with ACLs often refuse chmod, the copy still counts

    # data ----------------------------------------------------------------
    def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        raise NotImplementedError

    async def resume_offset(self, part_path: str) -> int:
        """Where a leftover part file can safely be continued from."""
        if not self.resumable:
            return 0
        try:
            size = (await self.stat(part_path)).size
        except Exception:
            return 0
        return max(0, (size // RESUME_ALIGN - 1) * RESUME_ALIGN)

    async def open_write(self, path: str, mtime: Optional[float] = None, mode: Optional[int] = None,
                         part: Optional[str] = None, offset: int = 0,
                         replace: Optional[bool] = None) -> Writer:
        path = norm(path)
        parent, name = posixpath.split(path)
        tmp = join(parent, part or part_name(name))
        raw = await self._open_raw_write(tmp, offset if self.resumable else 0)
        return PartWriter(self, path, raw, tmp, mtime, mode, replace)

    async def stream_url(self, path: str) -> Optional[str]:
        """A direct URL the browser can stream from, if the service has one."""
        return None

    async def local_path(self, path: str) -> Optional[str]:
        """Real filesystem path when the file is on this machine."""
        return None
