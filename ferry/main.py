"""Ferry web app: browse and move files between your machines."""
from __future__ import annotations

import asyncio
import json
import os
import posixpath
import shutil
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import archive, config, db, media, zipper
from .backends import make_backend, manager
from .backends.base import (AlreadyExists, BackendError, NotFound, PermissionDenied,
                            is_hidden, join, norm)
from .transfers import Job, engine, unique_name

VERSION = "0.4.0"

DEFAULT_TRASH = {"local": "@synology", "sftp": "/delete_me", "smb": "/delete_me",
                 "ftp": "/delete_me", "dropbox": ""}


# lifecycle ---------------------------------------------------------------------
def _seed() -> None:
    env_pw = os.environ.get("FERRY_PASSWORD")
    if env_pw and not db.has_password():
        db.set_password(env_pw)
    if not db.list_connections() and os.path.isdir(config.DEFAULT_LOCAL_ROOT):
        db.create_connection(config.DEFAULT_LOCAL_NAME, "local",
                             {"root": config.DEFAULT_LOCAL_ROOT, "trash": "@synology"})


async def _housekeeping():
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(archive.cleanup_cache)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _seed()
    engine.restore()
    shutil.rmtree(config.TMP, ignore_errors=True)
    config.TMP.mkdir(parents=True, exist_ok=True)
    config.ssh_private_key_path()
    tasks = [asyncio.create_task(manager.reap_idle()), asyncio.create_task(_housekeeping())]
    yield
    for t in tasks:
        t.cancel()
    await manager.close_all()


app = FastAPI(title="Ferry", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)


def _session_mw():
    db.init()
    return db.session_secret()


app.add_middleware(SessionMiddleware, secret_key=_session_mw(), session_cookie="ferry",
                   max_age=config.SESSION_DAYS * 86400, same_site="lax", https_only=False)


@app.exception_handler(BackendError)
async def backend_error(request: Request, e: BackendError):
    return JSONResponse({"error": str(e) or e.__class__.__name__}, status_code=e.status)


# auth ---------------------------------------------------------------------------
_fails: dict[str, list[float]] = {}


def _pw_version() -> int:
    return db.get_setting("pw_version", 1)


async def auth(request: Request):
    if request.session.get("auth") != _pw_version():
        raise HTTPException(401, "Sign in first")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-ferry") != "1":
        raise HTTPException(403, "Missing X-Ferry header")


class LoginBody(BaseModel):
    password: str


@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/me")
async def me(request: Request):
    return {"authed": request.session.get("auth") == _pw_version(),
            "setup": not db.has_password(), "version": VERSION}


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many tries, wait a few minutes")
    if not db.has_password():
        if len(body.password) < 8:
            raise HTTPException(400, "Use at least 8 characters")
        db.set_password(body.password)
    elif not db.check_password(body.password):
        recent.append(now)
        _fails[ip] = recent
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    _fails.pop(ip, None)
    request.session["auth"] = _pw_version()
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


class PasswordBody(BaseModel):
    old: str
    new: str


@app.post("/api/password", dependencies=[Depends(auth)])
async def change_password(body: PasswordBody, request: Request):
    if not db.check_password(body.old):
        raise HTTPException(401, "Current password is wrong")
    if len(body.new) < 8:
        raise HTTPException(400, "Use at least 8 characters")
    db.set_password(body.new)
    db.set_setting("pw_version", _pw_version() + 1)
    request.session["auth"] = _pw_version()
    return {"ok": True}


# connections ----------------------------------------------------------------------
class ConnBody(BaseModel):
    name: str
    type: str
    config: dict = {}


def _public_conn(c: dict) -> dict:
    c = dict(c)
    c["config"] = dict(c["config"])
    c["config"].setdefault("trash", DEFAULT_TRASH.get(c["type"], ""))
    c["live"] = c["id"] in manager.live
    return c


@app.get("/api/connections", dependencies=[Depends(auth)])
async def list_conns():
    return {"connections": [_public_conn(c) for c in db.list_connections()]}


@app.post("/api/connections", dependencies=[Depends(auth)])
async def create_conn(body: ConnBody):
    if body.type not in db.TYPES:
        raise HTTPException(400, "Unknown type")
    cfg = dict(body.config)
    cfg.setdefault("trash", DEFAULT_TRASH.get(body.type, ""))
    return _public_conn(db.create_connection(body.name.strip() or body.type.upper(), body.type, cfg))


@app.put("/api/connections/{cid}", dependencies=[Depends(auth)])
async def update_conn(cid: str, body: ConnBody):
    cur = db.get_connection(cid)
    if not cur:
        raise HTTPException(404, "No such connection")
    patch = dict(body.config)
    # host or port changed: forget the pinned SSH host key
    if cur["type"] == "sftp" and (patch.get("host", cur["config"].get("host")) != cur["config"].get("host")
                                  or str(patch.get("port", cur["config"].get("port"))) != str(cur["config"].get("port"))):
        patch["host_key"] = ""
    out = db.update_connection(cid, body.name, patch)
    await manager.drop(cid)
    return _public_conn(out)


@app.delete("/api/connections/{cid}", dependencies=[Depends(auth)])
async def delete_conn(cid: str):
    await manager.drop(cid)
    db.delete_connection(cid)
    return {"ok": True}


class OrderBody(BaseModel):
    ids: list[str]


@app.post("/api/connections/order", dependencies=[Depends(auth)])
async def order_conns(body: OrderBody):
    db.reorder(body.ids)
    return {"ok": True}


async def _test_backend(conn: dict) -> dict:
    b = make_backend(conn)
    try:
        await asyncio.wait_for(b.connect(), 25)
        entries = await b.list("/")
        usage = {}
        if hasattr(b, "disk_usage"):
            try:
                usage = await b.disk_usage("/")
            except Exception:
                pass
        return {"ok": True, "items": len(entries), "usage": usage,
                "host_key": b.cfg.get("host_key", "")}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timed out"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(b.translate(e))}
    finally:
        await b.close()


@app.post("/api/connections/{cid}/test", dependencies=[Depends(auth)])
async def test_saved(cid: str):
    conn = db.get_connection(cid)
    if not conn:
        raise HTTPException(404, "No such connection")
    res = await _test_backend(conn)
    if res.get("ok") and res.get("host_key") and not conn["config"].get("host_key"):
        cfg = conn["config"]
        cfg["host_key"] = res["host_key"]
        db.save_config(cid, cfg)
    return res


@app.post("/api/connections/test", dependencies=[Depends(auth)])
async def test_unsaved(body: ConnBody, cid: Optional[str] = None):
    cfg = dict(body.config)
    if cid:
        cur = db.get_connection(cid)
        if cur:
            for k in db.SECRET_FIELDS:
                if not cfg.get(k):
                    cfg[k] = cur["config"].get(k, "")
            if cfg.get("host") == cur["config"].get("host"):
                cfg.setdefault("host_key", cur["config"].get("host_key", ""))
    return await _test_backend({"id": "_test", "name": body.name, "type": body.type, "config": cfg})


@app.get("/api/ssh-key", dependencies=[Depends(auth)])
async def ssh_key():
    pub = config.ssh_public_key()
    from . import tailnet
    return {"public_key": pub,
            "authorized_keys_line": f'restrict,command="/usr/libexec/sftp-server" {pub}',
            "tailscale": tailnet.available()}


class DropboxStart(BaseModel):
    app_key: str


class DropboxFinish(BaseModel):
    flow_id: str
    code: str


@app.post("/api/dropbox/start", dependencies=[Depends(auth)])
async def dropbox_start(body: DropboxStart):
    from .backends import dropbox_
    flow_id, url = dropbox_.auth_start(body.app_key.strip())
    return {"flow_id": flow_id, "url": url}


@app.post("/api/dropbox/finish", dependencies=[Depends(auth)])
async def dropbox_finish(body: DropboxFinish):
    from .backends import dropbox_
    try:
        token = await asyncio.to_thread(dropbox_.auth_finish, body.flow_id, body.code)
    except BackendError:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Dropbox said no: {e}")
    return {"refresh_token": token}


# browsing ----------------------------------------------------------------------------
@app.get("/api/list", dependencies=[Depends(auth)])
async def list_dir(conn: str, path: str = "/"):
    path = norm(path)
    entries = await manager.run(conn, lambda b: b.list(path))
    entries = [e for e in entries if not e.name.endswith(".ferry-part")]
    media.annotate(entries)
    return {"path": path, "entries": [e.to_dict() for e in entries]}


@app.get("/api/stat", dependencies=[Depends(auth)])
async def stat(conn: str, path: str):
    e = await manager.run(conn, lambda b: b.stat(path))
    e.extra["kind"] = media.kind(e.name, e.is_dir)
    return e.to_dict()


@app.get("/api/usage", dependencies=[Depends(auth)])
async def usage(conn: str, path: str = "/"):
    b = await manager.get(conn)
    if not hasattr(b, "disk_usage"):
        return {}
    try:
        return await b.disk_usage(path)
    except Exception:
        return {}


class PathBody(BaseModel):
    conn: str
    path: str


class RenameBody(BaseModel):
    conn: str
    path: str
    name: str


class DeleteBody(BaseModel):
    conn: str
    paths: list[str]
    permanent: bool = False


def _check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "That name won't work")
    return name


@app.post("/api/mkdir", dependencies=[Depends(auth)])
async def mkdir(body: PathBody):
    parent, name = posixpath.split(norm(body.path))
    _check_name(name)

    async def op(b):
        if await b.exists(body.path):
            raise AlreadyExists("Something with that name is already there")
        await b.mkdir(body.path)

    await manager.run(body.conn, op)
    return {"ok": True, "path": norm(body.path)}


@app.post("/api/rename", dependencies=[Depends(auth)])
async def rename(body: RenameBody):
    src = norm(body.path)
    if src == "/":
        raise HTTPException(400, "Can't rename the root")
    name = _check_name(body.name)
    dst = join(posixpath.dirname(src), name)
    if dst == src:
        return {"ok": True, "path": dst}

    async def op(b):
        # case only renames (photo.JPG -> photo.jpg) look like a clash on Mac and Windows
        if dst.lower() != src.lower() and await b.exists(dst):
            raise AlreadyExists("Something with that name is already there")
        await b.rename(src, dst)

    await manager.run(body.conn, op, retry=False)
    return {"ok": True, "path": dst}


def _trash_dir(trash: str, path: str) -> Optional[str]:
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


@app.post("/api/delete", dependencies=[Depends(auth)])
async def delete(body: DeleteBody):
    conn = db.get_connection(body.conn)
    if not conn:
        raise HTTPException(404, "No such connection")
    trash = conn["config"].get("trash", DEFAULT_TRASH.get(conn["type"], ""))
    results = []
    b = await manager.get(body.conn)
    for p in body.paths:
        p = norm(p)
        if p == "/":
            results.append({"path": p, "error": "Can't delete the root"})
            continue
        try:
            dest = None if body.permanent else _trash_dir(trash, p)
            if dest is None:
                await b.rmtree(p)
                results.append({"path": p, "deleted": True})
                continue
            if dest == "@synology":
                parts = p.strip("/").split("/")
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
            target = join(dest, posixpath.basename(p))
            if await b.exists(target):
                target = await unique_name(b, dest, posixpath.basename(p))
            await b.rename(p, target)
            results.append({"path": p, "trashed": target})
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            results.append({"path": p, "error": str(b.translate(e))})
    return {"results": results}


# transfers --------------------------------------------------------------------------------
class TransferBody(BaseModel):
    kind: str = "copy"
    src_conn: str
    paths: list[str]
    dst_conn: str
    dst_dir: str
    conflict: str = "rename"


def _title(kind: str, paths: list[str], src: str, dst: str) -> str:
    conns = {c["id"]: c["name"] for c in db.list_connections()}
    first = posixpath.basename(norm(paths[0])) if paths else "?"
    more = f" and {len(paths) - 1} more" if len(paths) > 1 else ""
    verb = {"copy": "Copy", "move": "Move", "extract": "Extract"}.get(kind, kind)
    if src == dst:
        return f"{verb} {first}{more} within {conns.get(src, '?')}"
    return f"{verb} {first}{more} from {conns.get(src, '?')} to {conns.get(dst, '?')}"


@app.post("/api/transfer", dependencies=[Depends(auth)])
async def transfer(body: TransferBody):
    if body.kind not in ("copy", "move"):
        raise HTTPException(400, "kind must be copy or move")
    if body.conflict not in ("rename", "overwrite", "skip"):
        raise HTTPException(400, "Bad conflict mode")
    if not body.paths:
        raise HTTPException(400, "Nothing selected")
    for cid in (body.src_conn, body.dst_conn):
        if not db.get_connection(cid, secrets_too=False):
            raise HTTPException(404, "No such connection")
    job = Job(kind=body.kind, src_conn=body.src_conn, src_paths=[norm(p) for p in body.paths],
              dst_conn=body.dst_conn, dst_dir=norm(body.dst_dir), conflict=body.conflict,
              title=_title(body.kind, body.paths, body.src_conn, body.dst_conn))
    engine.submit(job)
    return job.to_dict()


@app.get("/api/jobs", dependencies=[Depends(auth)])
async def jobs():
    return {"jobs": engine.snapshot(), "version": engine.version}


@app.post("/api/jobs/{jid}/{action}", dependencies=[Depends(auth)])
async def job_action(jid: str, action: str):
    if jid not in engine.jobs:
        raise HTTPException(404, "No such job")
    if action == "pause":
        engine.pause(jid)
    elif action == "resume":
        engine.resume(jid)
    elif action == "cancel":
        engine.cancel(jid)
    elif action == "retry":
        return engine.retry(jid).to_dict()
    else:
        raise HTTPException(400, "Unknown action")
    return {"ok": True}


@app.post("/api/jobs/clear", dependencies=[Depends(auth)])
async def jobs_clear():
    engine.clear_finished()
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(auth)])
async def events(request: Request):
    async def gen():
        last = -1
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            if engine.version != last:
                last = engine.version
                yield f"data: {json.dumps({'jobs': engine.snapshot(), 'version': last})}\n\n"
            else:
                yield ": ping\n\n"
            await engine.wait_change(15)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# file contents ---------------------------------------------------------------------------
def _disposition(name: str, attachment: bool) -> str:
    kind = "attachment" if attachment else "inline"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


def _parse_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    if not header or not header.startswith("bytes=") or size == 0:
        return None
    spec = header[6:].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            n = int(end_s)
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        raise HTTPException(416, "Range not satisfiable")
    return start, min(end, size - 1)


@app.get("/api/raw", dependencies=[Depends(auth)])
async def raw(request: Request, conn: str, path: str, dl: int = 0):
    path = norm(path)
    b = await manager.get(conn)
    st = await manager.run(conn, lambda b: b.stat(path))
    if st.is_dir:
        raise HTTPException(400, "That's a folder, use zip download")
    name = st.name
    lp = await b.local_path(path)
    if lp:
        resp = FileResponse(lp, media_type=media.mime(name), stat_result=None)
        resp.headers["content-disposition"] = _disposition(name, bool(dl))
        resp.headers["cache-control"] = "private, max-age=300"
        return resp
    url = await b.stream_url(path)
    if url:
        return RedirectResponse(url, status_code=302)
    size = st.size
    rng = _parse_range(request.headers.get("range"), size)
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": _disposition(name, bool(dl)),
               "Cache-Control": "private, max-age=300"}
    if rng:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(b.read(path, start, end - start + 1), status_code=206,
                                 media_type=media.mime(name), headers=headers)
    headers["Content-Length"] = str(size)
    return StreamingResponse(b.read(path), media_type=media.mime(name), headers=headers)


@app.get("/api/thumb", dependencies=[Depends(auth)])
async def thumb(conn: str, path: str, s: int = 320):
    box = 320 if s <= 320 else (640 if s <= 640 else 2048)
    out = await media.thumbnail(conn, norm(path), box)
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.get("/api/text", dependencies=[Depends(auth)])
async def text(conn: str, path: str):
    limit = 512 * 1024
    data = await media.read_bytes(conn, norm(path), limit + 1)
    truncated = len(data) > limit
    if b"\x00" in data[:4096]:
        raise HTTPException(415, "That looks like a binary file")
    return {"text": media._decode(data[:limit]), "truncated": truncated}


@app.get("/api/subs", dependencies=[Depends(auth)])
async def subs(conn: str, path: str):
    return {"subtitles": await media.sibling_subtitles(conn, norm(path))}


@app.get("/api/subtitle", dependencies=[Depends(auth)])
async def subtitle(conn: str, path: str):
    data = await media.read_bytes(conn, norm(path), 8 << 20)
    vtt = media.to_vtt(posixpath.basename(path), data)
    return Response(vtt, media_type="text/vtt; charset=utf-8")


@app.put("/api/upload", dependencies=[Depends(auth)])
async def upload(request: Request, conn: str, path: str, mtime: float = 0, conflict: str = "rename"):
    path = norm(path)
    parent, name = posixpath.split(path)
    _check_name(name)
    b = await manager.get(conn)
    await b.makedirs(parent)
    if await b.exists(path):
        if conflict == "skip":
            return {"skipped": True, "path": path}
        if conflict == "rename":
            path = await unique_name(b, parent, name)
    w = await b.open_write(path, mtime=(mtime / 1000.0) if mtime else None)
    buf = bytearray()
    total = 0
    b.busy = getattr(b, "busy", 0) + 1
    try:
        async for chunk in request.stream():
            buf += chunk
            if len(buf) >= (1 << 20):
                await w.write(bytes(buf))
                total += len(buf)
                buf.clear()
        if buf:
            await w.write(bytes(buf))
            total += len(buf)
        await w.close()
    except BaseException:
        await w.abort()
        raise
    finally:
        b.busy -= 1
    return {"ok": True, "path": path, "size": total}


@app.get("/api/zip", dependencies=[Depends(auth)])
async def zip_download(conn: str, path: list[str] = Query(...), name: str = ""):
    items = await zipper.collect(conn, path)
    if not name:
        name = posixpath.basename(norm(path[0])) if len(path) == 1 else "Ferry"
    name = (name or "Ferry") + ".zip"
    return StreamingResponse(zipper.stream_zip(conn, items), media_type="application/zip",
                             headers={"Content-Disposition": _disposition(name, True)})


# archives ------------------------------------------------------------------------------------
@app.get("/api/archive/list", dependencies=[Depends(auth)])
async def archive_list(conn: str, path: str, password: str = ""):
    local = await archive.ensure_local(conn, norm(path))
    entries = await asyncio.to_thread(archive.list_entries, local, password or None)
    return {"entries": entries, "folder": archive.default_folder_name(path)}


class ExtractBody(BaseModel):
    conn: str
    path: str
    dst_conn: str
    dst_dir: str
    members: list[str] = []
    password: str = ""
    into_folder: bool = True
    folder: str = ""
    conflict: str = "rename"


@app.post("/api/archive/extract", dependencies=[Depends(auth)])
async def archive_extract(body: ExtractBody):
    folder = body.folder.strip() or archive.default_folder_name(body.path)
    _check_name(folder)
    job = Job(kind="extract", src_conn=body.conn, src_paths=[norm(body.path)], dst_conn=body.dst_conn,
              dst_dir=norm(body.dst_dir), conflict=body.conflict,
              title=_title("extract", [body.path], body.conn, body.dst_conn),
              archive={"members": body.members, "password": body.password,
                       "into_folder": body.into_folder, "folder": folder})
    engine.submit(job)
    return job.to_dict()


# web app --------------------------------------------------------------------------------------
_index_cache: dict = {}


@app.get("/", response_class=HTMLResponse)
async def index():
    p = config.WEB / "index.html"
    mt = p.stat().st_mtime
    if _index_cache.get("mt") != mt:
        _index_cache["mt"] = mt
        _index_cache["html"] = p.read_text().replace("{{v}}", f"{VERSION}-{int(mt)}")
    return HTMLResponse(_index_cache["html"], headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker():
    return FileResponse(config.WEB / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(config.WEB / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=config.WEB), name="static")
