"""Dropbox via the official SDK.

Auth uses PKCE with an app key, so no app secret is stored. The user makes
a Dropbox app once (dropbox.com/developers/apps, scoped access, Full
Dropbox), pastes the app key into Ferry, and approves it in the browser.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import posixpath
from typing import AsyncIterator, Optional

import dropbox
import requests
from dropbox import exceptions as dbx_exc
from dropbox import files as dbx_files

from .base import (Backend, BackendError, Disconnected, Entry, NotFound,
                   PermissionDenied, Writer, is_hidden, join, norm)

UPLOAD_CHUNK = 8 << 20
SIMPLE_UPLOAD_MAX = 24 << 20

_flows: dict[str, dropbox.DropboxOAuth2FlowNoRedirect] = {}


def auth_start(app_key: str) -> tuple[str, str]:
    import secrets
    flow = dropbox.DropboxOAuth2FlowNoRedirect(app_key, use_pkce=True, token_access_type="offline")
    url = flow.start()
    flow_id = secrets.token_urlsafe(16)
    _flows[flow_id] = flow
    return flow_id, url


def auth_finish(flow_id: str, code: str) -> str:
    flow = _flows.pop(flow_id, None)
    if not flow:
        raise BackendError("That Dropbox sign in expired, start again")
    res = flow.finish(code.strip())
    return res.refresh_token


def _ts(d: Optional[dt.datetime]) -> float:
    if not d:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


class _DropboxWriter(Writer):
    def __init__(self, backend: "DropboxBackend", path: str, mtime: Optional[float]):
        self.b = backend
        self.path = path
        self.mtime = mtime
        self.buf = bytearray()
        self.session_id: Optional[str] = None
        self.offset = 0

    def _commit(self):
        cm = None
        if self.mtime:
            cm = dt.datetime.fromtimestamp(int(self.mtime), tz=dt.timezone.utc).replace(tzinfo=None)
        return dbx_files.CommitInfo(path=self.path, mode=dbx_files.WriteMode.overwrite,
                                    client_modified=cm, mute=True)

    async def write(self, data: bytes) -> None:
        self.buf += data
        while len(self.buf) >= UPLOAD_CHUNK and (self.session_id or self.offset + len(self.buf) > SIMPLE_UPLOAD_MAX):
            piece = bytes(self.buf[:UPLOAD_CHUNK])
            del self.buf[:UPLOAD_CHUNK]
            await self._send(piece)

    async def _send(self, piece: bytes):
        dbx = self.b.dbx
        if self.session_id is None:
            r = await asyncio.to_thread(dbx.files_upload_session_start, piece)
            self.session_id = r.session_id
        else:
            cur = dbx_files.UploadSessionCursor(session_id=self.session_id, offset=self.offset)
            await asyncio.to_thread(dbx.files_upload_session_append_v2, piece, cur)
        self.offset += len(piece)

    async def close(self) -> None:
        dbx = self.b.dbx
        commit = self._commit()
        if self.session_id is None:
            await asyncio.to_thread(
                dbx.files_upload, bytes(self.buf), self.path, mode=commit.mode,
                client_modified=commit.client_modified, mute=True)
        else:
            cur = dbx_files.UploadSessionCursor(session_id=self.session_id, offset=self.offset)
            await asyncio.to_thread(dbx.files_upload_session_finish, bytes(self.buf), cur, commit)
        self.buf = bytearray()

    async def abort(self, keep: bool = False) -> None:
        self.buf = bytearray()


class DropboxBackend(Backend):
    kind = "dropbox"

    def __init__(self, conn, on_config_update=None):
        super().__init__(conn, on_config_update)
        self.dbx: Optional[dropbox.Dropbox] = None
        self.base = norm(self.cfg.get("root") or "/")

    def _p(self, path: str) -> str:
        p = norm(posixpath.join(self.base, norm(path).lstrip("/")))
        return "" if p == "/" else p

    async def connect(self) -> None:
        c = self.cfg
        if not c.get("app_key") or not c.get("refresh_token"):
            raise PermissionDenied("Dropbox isn't authorized yet. Edit the connection and sign in.")
        self.dbx = dropbox.Dropbox(oauth2_refresh_token=c["refresh_token"], app_key=c["app_key"],
                                   timeout=60)
        try:
            await asyncio.to_thread(self.dbx.users_get_current_account)
        except Exception as e:
            raise self.translate(e) from e

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, BackendError):
            return e
        if isinstance(e, dbx_exc.AuthError):
            return PermissionDenied("Dropbox rejected the login, sign in again")
        if isinstance(e, dbx_exc.ApiError):
            s = str(e.error)
            if "not_found" in s:
                return NotFound("Not found in Dropbox")
            if "conflict" in s:
                from .base import AlreadyExists
                return AlreadyExists("Already exists in Dropbox")
            return BackendError(f"Dropbox: {e.user_message_text or s}")
        if isinstance(e, (requests.ConnectionError, requests.Timeout)):
            return Disconnected(str(e))
        return super().translate(e)

    def _entry(self, path: str, md) -> Entry:
        name = md.name
        if isinstance(md, dbx_files.FolderMetadata):
            return Entry(name=name, path=path, is_dir=True, hidden=is_hidden(name))
        mtime = _ts(getattr(md, "client_modified", None) or getattr(md, "server_modified", None))
        return Entry(name=name, path=path, is_dir=False, size=int(md.size or 0),
                     mtime=mtime, hidden=is_hidden(name))

    async def list(self, path: str) -> list[Entry]:
        path = norm(path)
        res = await asyncio.to_thread(self.dbx.files_list_folder, self._p(path), limit=2000)
        out = []
        while True:
            for md in res.entries:
                if isinstance(md, dbx_files.DeletedMetadata):
                    continue
                out.append(self._entry(join(path, md.name), md))
            if not res.has_more:
                break
            res = await asyncio.to_thread(self.dbx.files_list_folder_continue, res.cursor)
        return out

    async def stat(self, path: str) -> Entry:
        path = norm(path)
        if self._p(path) == "":
            return Entry(name="/", path="/", is_dir=True)
        md = await asyncio.to_thread(self.dbx.files_get_metadata, self._p(path))
        e = self._entry(path, md)
        e.name = posixpath.basename(path)
        return e

    async def mkdir(self, path: str) -> None:
        await asyncio.to_thread(self.dbx.files_create_folder_v2, self._p(path))

    async def makedirs(self, path: str) -> None:
        # Dropbox creates parent folders on its own
        if norm(path) == "/":
            return
        try:
            await self.mkdir(path)
        except dbx_exc.ApiError as e:
            if "conflict" not in str(e.error):
                raise

    async def remove(self, path: str) -> None:
        if norm(path) == "/":
            raise PermissionDenied("Refusing to remove the connection root")
        await asyncio.to_thread(self.dbx.files_delete_v2, self._p(path))

    async def rmtree(self, path: str) -> None:
        await self.remove(path)  # Dropbox deletes folders recursively

    async def rename(self, src: str, dst: str) -> None:
        await asyncio.to_thread(self.dbx.files_move_v2, self._p(src), self._p(dst))

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        link = await self.stream_url(path)
        headers = {}
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            headers["Range"] = f"bytes={offset}-{end}"
        r = await asyncio.to_thread(requests.get, link, headers=headers, stream=True, timeout=60)
        try:
            r.raise_for_status()
            it = r.iter_content(1 << 20)
            while True:
                chunk = await asyncio.to_thread(next, it, None)
                if not chunk:
                    break
                yield chunk
        finally:
            r.close()

    async def stream_url(self, path: str) -> Optional[str]:
        res = await asyncio.to_thread(self.dbx.files_get_temporary_link, self._p(path))
        return res.link

    async def open_write(self, path: str, mtime: Optional[float] = None, mode=None, part=None,
                         offset: int = 0, replace=None) -> Writer:
        # Dropbox commits uploads atomically, no temp file needed
        return _DropboxWriter(self, self._p(path), mtime)
