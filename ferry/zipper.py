"""Stream a ZIP of files and folders from any connection, no temp files.

zipstream-ng is synchronous, so it runs in a thread and pulls file data
from the async backends through the event loop.
"""
from __future__ import annotations

import asyncio
import posixpath
import threading
from typing import AsyncIterator

import zipstream

from .backends import manager
from .backends.base import Entry, norm

_END = object()


async def collect(cid: str, paths: list[str]) -> list[tuple[str, Entry]]:
    """(name inside zip, entry) for every file, walking folders."""
    out: list[tuple[str, Entry]] = []
    for p in paths:
        p = norm(p)
        st = await manager.run(cid, lambda b: b.stat(p))
        base = posixpath.basename(p) or "files"
        if not st.is_dir:
            out.append((base, st))
            continue
        stack = [(p, base)]
        while stack:
            d, arc = stack.pop()
            entries = await manager.run(cid, lambda b, d=d: b.list(d))
            if not entries:
                out.append((arc + "/", Entry(name=arc, path=d, is_dir=True)))
            for e in entries:
                if e.name.endswith(".ferry-part") or e.name in ("@eaDir", ".DS_Store"):
                    continue
                if e.is_dir:
                    stack.append((e.path, f"{arc}/{e.name}"))
                else:
                    out.append((f"{arc}/{e.name}", e))
    return out


async def stream_zip(cid: str, items: list[tuple[str, Entry]]) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    b = await manager.get(cid)
    stop = threading.Event()

    def file_iter(entry: Entry):
        agen = b.read(entry.path)
        try:
            while not stop.is_set():
                try:
                    chunk = asyncio.run_coroutine_threadsafe(agen.__anext__(), loop).result()
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            asyncio.run_coroutine_threadsafe(agen.aclose(), loop)

    def run():
        try:
            zs = zipstream.ZipStream(compress_type=zipstream.ZIP_STORED, sized=False)
            for arc, e in items:
                if e.is_dir:
                    zs.mkdir(arc.rstrip("/"))
                else:
                    zs.add(file_iter(e), arc, size=e.size)
            for chunk in zs:
                if stop.is_set():
                    break
                asyncio.run_coroutine_threadsafe(q.put(chunk), loop).result()
        except Exception as ex:  # noqa: BLE001
            asyncio.run_coroutine_threadsafe(q.put(ex), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(q.put(_END), loop)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    b.busy = getattr(b, "busy", 0) + 1
    try:
        while True:
            chunk = await q.get()
            if chunk is _END:
                break
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
    finally:
        stop.set()
        b.busy -= 1
        # drain so the worker thread can finish
        while not q.empty():
            q.get_nowait()
