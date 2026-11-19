"""SQLite store for connections and settings.

Connection configs (hosts, users, passwords, tokens) are encrypted with a
key kept next to the database, so a copied ferry.db alone leaks nothing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Optional

from cryptography.fernet import Fernet

from . import config

SECRET_FIELDS = {"password", "refresh_token"}
TYPES = {"local", "sftp", "smb", "ftp", "dropbox"}

_lock = threading.Lock()
_db: Optional[sqlite3.Connection] = None
_fernet: Optional[Fernet] = None


def _key_file(name: str, nbytes: int, fernet: bool = False) -> bytes:
    path = config.DATA / name
    if not path.exists():
        val = Fernet.generate_key() if fernet else base64.urlsafe_b64encode(secrets.token_bytes(nbytes))
        path.write_bytes(val)
        os.chmod(path, 0o600)
    return path.read_bytes().strip()


def init() -> None:
    global _db, _fernet
    config.ensure_dirs()
    _fernet = Fernet(_key_file("secret.key", 32, fernet=True))
    _db = sqlite3.connect(config.DATA / "ferry.db", check_same_thread=False)
    _db.row_factory = sqlite3.Row
    with _lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                config BLOB NOT NULL,
                position INTEGER DEFAULT 0,
                created REAL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, updated REAL, data TEXT NOT NULL);
            """
        )
        _db.commit()


def session_secret() -> str:
    return _key_file("session.key", 32).decode()


# settings -----------------------------------------------------------------
def get_setting(key: str, default=None):
    with _lock:
        row = _db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        _db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        _db.commit()


# password -----------------------------------------------------------------
def _hash(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def set_password(pw: str) -> None:
    salt = secrets.token_bytes(16)
    set_setting("password", {"salt": salt.hex(), "hash": _hash(pw, salt)})


def has_password() -> bool:
    return get_setting("password") is not None


def check_password(pw: str) -> bool:
    rec = get_setting("password")
    if not rec:
        return False
    return hmac.compare_digest(_hash(pw, bytes.fromhex(rec["salt"])), rec["hash"])


# connections --------------------------------------------------------------
def _enc(cfg: dict) -> bytes:
    return _fernet.encrypt(json.dumps(cfg).encode())


def _dec(blob: bytes) -> dict:
    return json.loads(_fernet.decrypt(blob))


def _row(row, secrets_too: bool) -> dict:
    cfg = _dec(row["config"])
    out = {"id": row["id"], "name": row["name"], "type": row["type"], "position": row["position"]}
    if secrets_too:
        out["config"] = cfg
    else:
        pub = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
        for k in SECRET_FIELDS:
            pub[f"has_{k}"] = bool(cfg.get(k))
        out["config"] = pub
    return out


def list_connections(secrets_too: bool = False) -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM connections ORDER BY position, created").fetchall()
    return [_row(r, secrets_too) for r in rows]


def get_connection(cid: str, secrets_too: bool = True) -> Optional[dict]:
    with _lock:
        row = _db.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
    return _row(row, secrets_too) if row else None


def create_connection(name: str, type_: str, cfg: dict) -> dict:
    if type_ not in TYPES:
        raise ValueError(f"Unknown connection type {type_}")
    cid = uuid.uuid4().hex[:12]
    with _lock:
        pos = _db.execute("SELECT COALESCE(MAX(position),0)+1 FROM connections").fetchone()[0]
        _db.execute("INSERT INTO connections(id,name,type,config,position,created) VALUES(?,?,?,?,?,?)",
                    (cid, name, type_, _enc(cfg), pos, time.time()))
        _db.commit()
    return get_connection(cid, secrets_too=False)


def update_connection(cid: str, name: Optional[str], cfg_patch: dict, replace: bool = False) -> dict:
    cur = get_connection(cid)
    if not cur:
        raise KeyError(cid)
    cfg = {} if replace else dict(cur["config"])
    for k, v in cfg_patch.items():
        # blank secret fields from the UI mean "keep what's saved"
        if k in SECRET_FIELDS and (v is None or v == "") and not replace:
            continue
        cfg[k] = v
    with _lock:
        _db.execute("UPDATE connections SET name=?, config=? WHERE id=?",
                    (name or cur["name"], _enc(cfg), cid))
        _db.commit()
    return get_connection(cid, secrets_too=False)


def save_config(cid: str, cfg: dict) -> None:
    with _lock:
        _db.execute("UPDATE connections SET config=? WHERE id=?", (_enc(cfg), cid))
        _db.commit()


def delete_connection(cid: str) -> None:
    with _lock:
        _db.execute("DELETE FROM connections WHERE id=?", (cid,))
        _db.commit()


def reorder(ids: list[str]) -> None:
    with _lock:
        for i, cid in enumerate(ids):
            _db.execute("UPDATE connections SET position=? WHERE id=?", (i, cid))
        _db.commit()


# transfer jobs --------------------------------------------------------------
def save_job(jid: str, state: dict) -> None:
    with _lock:
        _db.execute("INSERT INTO jobs(id,updated,data) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, data=excluded.data",
                    (jid, time.time(), json.dumps(state)))
        _db.commit()


def load_jobs() -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT data FROM jobs ORDER BY updated").fetchall()
    return [json.loads(r["data"]) for r in rows]


def delete_jobs(ids: list[str]) -> None:
    with _lock:
        _db.executemany("DELETE FROM jobs WHERE id=?", [(i,) for i in ids])
        _db.commit()
