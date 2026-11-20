"""WebDAV (Nextcloud, ownCloud, Synology WebDAV, Apache and friends) over httpx."""
from __future__ import annotations

import asyncio
import email.utils
import posixpath
import xml.etree.ElementTree as ET
from typing import AsyncIterator, Optional
from urllib.parse import quote, unquote, urlsplit

import httpx

from .base import (CHUNK, AlreadyExists, Backend, BackendError, Disconnected, Entry,
                   NotFound, PermissionDenied, Writer, is_hidden, join, norm)

PROPFIND = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
            '<d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>')
D = "{DAV:}"


class _Upload(Writer):
    """Feeds one streaming PUT. Bytes go out as they are written, nothing is held in memory."""

    def __init__(self, backend: "WebDAVBackend", path: str):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.task = asyncio.create_task(backend.http.put(backend.url(path), content=self._feed()))

    async def _feed(self):
        while (data := await self.q.get()) is not None:
            yield data

    async def _send(self, item) -> None:
        put = asyncio.ensure_future(self.q.put(item))
        await asyncio.wait({put, self.task}, return_when=asyncio.FIRST_COMPLETED)
        if not put.done():  # the request ended before taking our bytes
            put.cancel()
            self.task.result().raise_for_status()
            raise Disconnected("The server closed the upload")

    async def write(self, data: bytes) -> None:
        await self._send(data)

    async def close(self) -> None:
        if not self.task.done():
            await self._send(None)
        (await self.task).raise_for_status()

    async def abort(self) -> None:
        self.task.cancel()


class WebDAVBackend(Backend):
    kind = "webdav"
    keeps_dates = False  # most servers stamp files with the upload time

    async def connect(self) -> None:
        raw = (self.cfg.get("url") or "").strip()
        if not raw:
            raise BackendError("Enter the WebDAV address")
        if "://" not in raw:
            raw = "https://" + raw
        u = urlsplit(raw)
        self.origin = f"{u.scheme}://{u.netloc}"
        self.base = norm(unquote(u.path) + "/" + (self.cfg.get("root") or "")).rstrip("/")
        auth = None
        if self.cfg.get("username"):
            auth = httpx.BasicAuth(self.cfg["username"], self.cfg.get("password") or "")
        self.http = httpx.AsyncClient(auth=auth, verify=self.cfg.get("verify", True) is not False,
                                      timeout=httpx.Timeout(30, read=120, write=120),
                                      follow_redirects=True)
        await self.stat("/")

    async def close(self) -> None:
        await self.http.aclose()

    def url(self, path: str) -> str:
        return self.origin + quote(self.base + norm(path))

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code == 404:
                return NotFound("Not found")
            if code in (401, 403):
                return PermissionDenied("The server refused that. Check the user name and password." if code == 401
                                        else "The server doesn't allow that here")
            if code in (405, 412):
                return AlreadyExists("Something with that name is already there")
            if code == 409:
                return NotFound("The folder it goes in doesn't exist")
            if code == 507:
                return BackendError("The server is out of space")
            return BackendError(f"The server answered {code}")
        if isinstance(e, (httpx.TransportError, httpx.StreamError)):
            self.dead = isinstance(e, httpx.ConnectError)
            return Disconnected(str(e) or e.__class__.__name__)
        return super().translate(e)

    async def _propfind(self, path: str, depth: int) -> list[Entry]:
        r = await self.http.request("PROPFIND", self.url(path), content=PROPFIND,
                                    headers={"Depth": str(depth), "Content-Type": "application/xml"})
        r.raise_for_status()
        out = []
        for resp in ET.fromstring(r.content).iter(D + "response"):
            href = unquote(urlsplit(resp.findtext(D + "href") or "").path).rstrip("/")
            rel = norm(href[len(self.base):]) if href.startswith(self.base) else None
            if rel is None:
                continue
            prop = next((ps.find(D + "prop") for ps in resp.iter(D + "propstat")
                         if " 200 " in (ps.findtext(D + "status") or " 200 ")), None)
            if prop is None:
                continue
            is_dir = prop.find(f"{D}resourcetype/{D}collection") is not None
            mtime = 0.0
            try:
                mtime = email.utils.parsedate_to_datetime(prop.findtext(D + "getlastmodified")).timestamp()
            except Exception:
                pass
            name = posixpath.basename(rel)
            out.append(Entry(name, rel, is_dir, 0 if is_dir else int(prop.findtext(D + "getcontentlength") or 0),
                             mtime, is_hidden(name)))
        return out

    async def list(self, path: str) -> list[Entry]:
        path = norm(path)
        return [e for e in await self._propfind(path, 1) if e.path != path]

    async def stat(self, path: str) -> Entry:
        path = norm(path)
        for e in await self._propfind(path, 0):
            if e.path == path:
                return e
        raise NotFound("Not found")

    async def mkdir(self, path: str) -> None:
        (await self.http.request("MKCOL", self.url(path))).raise_for_status()

    async def remove(self, path: str) -> None:
        (await self.http.delete(self.url(path))).raise_for_status()

    async def rmtree(self, path: str) -> None:
        await self.remove(path)  # DELETE on a folder takes everything in it

    async def rename(self, src: str, dst: str) -> None:
        r = await self.http.request("MOVE", self.url(src), headers={"Destination": self.url(dst), "Overwrite": "F"})
        r.raise_for_status()

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        headers = {}
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            headers["Range"] = f"bytes={offset}-{end}"
        async with self.http.stream("GET", self.url(path), headers=headers) as r:
            r.raise_for_status()
            if offset and r.status_code != 206:
                raise BackendError("This server can't start in the middle of a file")
            async for chunk in r.aiter_bytes(CHUNK):
                yield chunk

    async def _open_raw_write(self, path: str, offset: int = 0) -> Writer:
        return _Upload(self, path)
