"""Where deleted things go. Used by the Delete button and by merges for duplicate files."""
from __future__ import annotations

import posixpath
from typing import Optional

from .backends.base import Backend, PermissionDenied, join, norm

DEFAULT_TRASH = {"local": "@synology", "sftp": "/delete_me", "smb": "/delete_me",
                 "ftp": "/delete_me", "dropbox": ""}


def setting(conn: dict) -> str:
    """A connection's trash setting: a folder, @synology, or blank for delete for good."""
    return conn["config"].get("trash", DEFAULT_TRASH.get(conn["type"], ""))


def trash_dir(trash: str, path: str) -> Optional[str]:
    """Where a deleted item goes. None means delete for good."""
    if not trash:
        return None
    if trash == "@synology":
        parts = path.strip("/").split("/")
        if len(parts) < 2:
            raise PermissionDenied("Ferry won't delete a whole shared folder")
        return None if parts[1] in ("#recycle", "delete_me") else "@synology"
    t = norm(trash)
    if path == t or path.startswith(t.rstrip("/") + "/"):
        return None  # already in the trash: emptying it
    return t


async def discard(b: Backend, trash: str, path: str) -> Optional[str]:
    """Move path to the trash and return where it went, or delete it for good and return None."""
    from .transfers import unique_name  # transfers imports this module too

    path = norm(path)
    dest = trash_dir(trash, path)
    if dest is None:
        await b.rmtree(path)
        return None
    if dest == "@synology":
        parts = path.strip("/").split("/")
        share = "/" + parts[0]
        sub = "/".join(parts[1:-1])
        recycle = join(share, "#recycle")
        if not await b.exists(recycle):
            recycle = join(share, "delete_me")
        dest = join(recycle, sub) if sub else recycle
        try:
            await b.makedirs(dest)
        except Exception as e:
            if not isinstance(b.translate(e), PermissionDenied) or recycle.endswith("delete_me"):
                raise
            # no write access to #recycle: fall back to a delete_me folder in the share
            recycle = join(share, "delete_me")
            dest = join(recycle, sub) if sub else recycle
    await b.makedirs(dest)
    target = join(dest, posixpath.basename(path))
    if await b.exists(target):
        target = await unique_name(b, dest, posixpath.basename(path))
    await b.rename(path, target)
    return target
