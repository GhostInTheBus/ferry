"""Paths and settings. Everything Ferry keeps lives in FERRY_DATA (/data in Docker)."""
from __future__ import annotations

import os
import socket
from pathlib import Path

DATA = Path(os.environ.get("FERRY_DATA", "/data"))

# Docker can run Ferry as a bare uid with no passwd entry; asyncssh wants a local user name
if not any(os.environ.get(k) for k in ("LOGNAME", "USER", "LNAME", "USERNAME")):
    os.environ["USER"] = "ferry"
os.environ.setdefault("HOME", str(DATA))
WEB = Path(os.environ.get("FERRY_WEB", Path(__file__).resolve().parent.parent / "web"))
CACHE = DATA / "cache"
TMP = DATA / "tmp"
UPLOADS = DATA / "uploads"  # half sent uploads wait here, kept across restarts

MAX_JOBS = int(os.environ.get("FERRY_MAX_JOBS", "2"))
FILE_WORKERS = int(os.environ.get("FERRY_FILE_WORKERS", "6"))
# Remote archives get downloaded before browsing, cap that
ARCHIVE_FETCH_MAX = int(os.environ.get("FERRY_ARCHIVE_FETCH_MAX", str(4 << 30)))
THUMB_FETCH_MAX = int(os.environ.get("FERRY_THUMB_FETCH_MAX", str(120 << 20)))
DEFAULT_LOCAL_ROOT = os.environ.get("FERRY_LOCAL_ROOT", "/nas")
DEFAULT_LOCAL_NAME = os.environ.get("FERRY_LOCAL_NAME", "NAS")
SESSION_DAYS = int(os.environ.get("FERRY_SESSION_DAYS", "30"))


def ensure_dirs() -> None:
    for d in (DATA, CACHE, CACHE / "thumbs", CACHE / "archives", TMP, UPLOADS, DATA / "ssh"):
        d.mkdir(parents=True, exist_ok=True)


def ssh_private_key_path() -> str:
    """Ferry's own SSH key, made on first use. Its public half goes on the Mac."""
    key = DATA / "ssh" / "id_ed25519"
    if not key.exists():
        import asyncssh
        k = asyncssh.generate_private_key("ssh-ed25519", comment=f"ferry@{socket.gethostname()}")
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(k.export_private_key())
        os.chmod(key, 0o600)
        (key.parent / "id_ed25519.pub").write_bytes(k.export_public_key())
    return str(key)


def ssh_public_key() -> str:
    ssh_private_key_path()
    return (DATA / "ssh" / "id_ed25519.pub").read_text().strip()
