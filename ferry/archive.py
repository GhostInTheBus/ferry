"""Browse and extract RAR, ZIP, 7z and tar archives with libarchive."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import posixpath
import re
import time
from typing import Callable, Optional

import libarchive

from . import config
from .backends import manager
from .backends.base import BackendError

ARCHIVE_EXT = (".rar", ".zip", ".7z", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2",
               ".tar.xz", ".txz", ".cbz", ".cbr", ".iso", ".cab", ".lzh", ".xar")


def is_archive(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(x) for x in ARCHIVE_EXT)


def default_folder_name(path: str) -> str:
    name = posixpath.basename(path)
    low = name.lower()
    for ext in sorted(ARCHIVE_EXT, key=len, reverse=True):
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    # Movie.part1 -> Movie
    if name.lower().endswith((".part1", ".part01", ".part001")):
        name = name.rsplit(".", 1)[0]
    return name or "Extracted"


def _safe(pathname: str) -> Optional[str]:
    parts = [p for p in pathname.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) if parts else None


def _err(e: Exception) -> BackendError:
    msg = str(e)
    low = msg.lower()
    if "passphrase" in low or "encrypt" in low or "password" in low:
        return BackendError("This archive is password protected. Enter the password and try again.")
    if "multivolume" in low or "multi-volume" in low or "volume" in low:
        return BackendError("This is a split archive. Keep all its parts in one folder on the NAS and open part 1.")
    return BackendError(f"Couldn't read the archive: {msg}")


async def ensure_local(cid: str, path: str, progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Path to the archive on this machine, downloading a cached copy if needed."""
    b = await manager.get(cid)
    lp = await b.local_path(path)
    if lp and os.path.isfile(lp):
        return lp
    st = await b.stat(path)
    if st.size > config.ARCHIVE_FETCH_MAX:
        raise BackendError("That archive is too big to open over the network. Copy it to the NAS first.")
    key = hashlib.sha1(f"{cid}|{path}|{st.size}|{st.mtime}".encode()).hexdigest()
    ext = os.path.splitext(path)[1][:10]
    out = config.CACHE / "archives" / f"{key}{ext}"
    if out.exists() and out.stat().st_size == st.size:
        os.utime(out)
        return str(out)
    tmp = out.with_suffix(out.suffix + ".part")
    done = 0
    with open(tmp, "wb") as f:
        async for chunk in b.read(path):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, st.size)
    os.replace(tmp, out)
    return str(out)


# unar / lsar ---------------------------------------------------------------------
# libarchive can't do RAR4 solid, encrypted RAR5 or split RARs. unar can.
_UNAR = shutil.which("unar")
_LSAR = shutil.which("lsar")


_SPLIT = re.compile(r"\.(part0*1\.rar|r00|rar\.001)$", re.I)


def _use_unar(local: str) -> bool:
    """Split RARs go straight to unar. Everything else tries libarchive first."""
    return bool(_UNAR and _LSAR) and bool(_SPLIT.search(local))


def _lsar(local: str, password: Optional[str]) -> list[dict]:
    cmd = [_LSAR, "-j"]
    if password:
        cmd += ["-p", password]
    cmd.append(local)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    out = r.stdout.strip()
    if "password" in (out + r.stderr).lower() and not out.startswith("{"):
        raise BackendError("This archive is password protected. Enter the password and try again.")
    try:
        data = json.loads(out)
    except ValueError:
        raise BackendError(f"Couldn't read the archive: {(r.stderr or out)[:200]}")
    if data.get("lsarError"):
        raise BackendError(f"Couldn't read the archive: {data.get('lsarError')}")
    return data.get("lsarContents") or []


def _lsar_time(s: Optional[str]) -> float:
    if not s:
        return 0.0
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return 0.0


def _lsar_entries(local: str, password: Optional[str]) -> list[dict]:
    files, dirs = {}, set()
    for i, e in enumerate(_lsar(local, password)):
        p = _safe(e.get("XADFileName") or "")
        if not p:
            continue
        if e.get("XADIsDirectory"):
            dirs.add(p)
        else:
            files[p] = {"path": p, "size": int(e.get("XADFileSize") or 0),
                        "mtime": _lsar_time(e.get("XADLastModificationDate")), "is_dir": False,
                        "encrypted": bool(e.get("XADIsEncrypted")), "index": e.get("XADIndex", i)}
        parent = posixpath.dirname(p)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    out = [{"path": d, "size": 0, "mtime": 0, "is_dir": True} for d in sorted(dirs)]
    return out + sorted(files.values(), key=lambda x: x["path"])


def _unar_extract(local: str, out_dir: str, members: Optional[list], password: Optional[str]) -> int:
    os.makedirs(out_dir, exist_ok=True)
    cmd = [_UNAR, "-q", "-D", "-f", "-k", "skip", "-o", out_dir]
    if password:
        cmd += ["-p", password]
    sel = {m.strip("/") for m in members} if members else None
    indexes = []
    if sel:
        for e in _lsar_entries(local, password):
            if not e["is_dir"] and _selected(e["path"], sel):
                indexes.append(str(e["index"]))
        if not indexes:
            raise BackendError("Nothing matched the selection")
        cmd += ["-i", local] + indexes
    else:
        cmd.append(local)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "password" in msg.lower():
            raise BackendError("Wrong or missing password for this archive.")
        raise BackendError(f"Couldn't extract: {msg[:300]}")
    # never let a link inside an archive point outside the output folder
    real_out = os.path.realpath(out_dir)
    count = 0
    for root, ds, fs in os.walk(out_dir):
        for n in ds + fs:
            p = os.path.join(root, n)
            if os.path.islink(p) or not os.path.realpath(p).startswith(real_out):
                os.unlink(p)
            elif n in fs:
                count += 1
    return count


def list_entries(local: str, password: Optional[str] = None) -> list[dict]:
    if _use_unar(local):
        return _lsar_entries(local, password)
    try:
        return _libarchive_entries(local, password)
    except BackendError:
        if _UNAR and _LSAR:
            return _lsar_entries(local, password)
        raise


def _libarchive_entries(local: str, password: Optional[str] = None) -> list[dict]:
    files: dict[str, dict] = {}
    dirs: set[str] = set()
    try:
        with libarchive.file_reader(local, passphrase=password) as arc:
            for e in arc:
                p = _safe(e.pathname)
                if not p:
                    continue
                if e.isdir:
                    dirs.add(p)
                else:
                    files[p] = {"path": p, "size": e.size or 0, "mtime": e.mtime or 0,
                                "is_dir": False, "encrypted": False}
                parent = posixpath.dirname(p)
                while parent:
                    dirs.add(parent)
                    parent = posixpath.dirname(parent)
    except libarchive.ArchiveError as e:
        raise _err(e) from e
    out = [{"path": d, "size": 0, "mtime": 0, "is_dir": True} for d in sorted(dirs)]
    out += sorted(files.values(), key=lambda x: x["path"])
    return out


def _selected(p: str, members: Optional[set]) -> bool:
    if not members:
        return True
    for m in members:
        if p == m or p.startswith(m + "/"):
            return True
    return False


def extract(local: str, out_dir: str, members: Optional[list] = None, password: Optional[str] = None) -> int:
    """Extract (selected) entries into out_dir. Symlinks are skipped on purpose."""
    if _use_unar(local):
        return _unar_extract(local, out_dir, members, password)
    try:
        return _libarchive_extract(local, out_dir, members, password)
    except BackendError as e:
        if _UNAR and _LSAR and "Nothing matched" not in str(e):
            shutil.rmtree(out_dir, ignore_errors=True)
            return _unar_extract(local, out_dir, members, password)
        raise


def _libarchive_extract(local: str, out_dir: str, members: Optional[list] = None, password: Optional[str] = None) -> int:
    sel = {m.strip("/") for m in members} if members else None
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    try:
        with libarchive.file_reader(local, passphrase=password) as arc:
            for e in arc:
                p = _safe(e.pathname)
                if not p or not _selected(p, sel):
                    continue
                dest = os.path.join(out_dir, p)
                if not os.path.realpath(dest).startswith(os.path.realpath(out_dir)):
                    continue
                if e.isdir:
                    os.makedirs(dest, exist_ok=True)
                    continue
                if not e.isfile:
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    for block in e.get_blocks():
                        f.write(block)
                if e.mtime:
                    try:
                        os.utime(dest, (e.mtime, e.mtime))
                    except OSError:
                        pass
                count += 1
    except libarchive.ArchiveError as e:
        raise _err(e) from e
    if count == 0 and sel:
        raise BackendError("Nothing matched the selection")
    return count


def cleanup_cache(max_age: float = 24 * 3600) -> None:
    now = time.time()
    for d in (config.CACHE / "archives",):
        for f in d.glob("*"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
            except OSError:
                pass
