"""Thumbnails, HEIC/RAW previews, subtitle conversion and Live Photo pairing."""
from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import os
import posixpath
import re
from typing import Optional

from . import config
from .backends import manager
from .backends.base import BackendError, Entry

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
BROWSER_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"}
RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf",
           ".rw2", ".pef", ".srw", ".3fr", ".iiq", ".rwl", ".x3f", ".erf", ".kdc", ".mef", ".mos"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg",
             ".ts", ".m2ts", ".3gp", ".ogv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".aiff", ".aif",
             ".alac", ".wma", ".ape"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
            ".log", ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".sh",
            ".zsh", ".bash", ".rb", ".go", ".rs", ".swift", ".c", ".h", ".cpp", ".hpp", ".java",
            ".kt", ".sql", ".env", ".srt", ".vtt", ".ass", ".ssa", ".nfo", ".cue", ".m3u", ".m3u8",
            ".gitignore", ".dockerfile", ".plist", ".properties", ".bat", ".ps1"}
LIVE_STILL_EXT = {".heic", ".heif", ".jpg", ".jpeg"}
LIVE_VIDEO_EXT = {".mov", ".mp4"}

_EXTRA_TYPES = {
    ".mkv": "video/x-matroska", ".m4v": "video/mp4", ".mov": "video/quicktime", ".ts": "video/mp2t",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".webp": "image/webp", ".md": "text/markdown",
    ".vtt": "text/vtt", ".srt": "text/plain", ".ass": "text/plain", ".ssa": "text/plain",
    ".nfo": "text/plain", ".log": "text/plain", ".py": "text/plain", ".sh": "text/plain",
    ".yaml": "text/plain", ".yml": "text/plain", ".toml": "text/plain", ".ini": "text/plain",
    ".conf": "text/plain", ".env": "text/plain",
}

_thumb_sem = asyncio.Semaphore(3)


def ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def mime(name: str) -> str:
    e = ext(name)
    if e in _EXTRA_TYPES:
        return _EXTRA_TYPES[e]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def kind(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "dir"
    e = ext(name)
    from .archive import is_archive
    if e in RAW_EXT:
        return "raw"
    if e in IMAGE_EXT:
        return "image"
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in SUB_EXT:
        return "subtitle"
    if e == ".pdf":
        return "pdf"
    if is_archive(name):
        return "archive"
    if e in TEXT_EXT or name.lower() in ("dockerfile", "makefile", "readme", "license"):
        return "text"
    return "file"


def can_thumb(name: str) -> bool:
    e = ext(name)
    return e in IMAGE_EXT or e in RAW_EXT


def annotate(entries: list[Entry]) -> list[Entry]:
    """Add kind and pair Live Photos (IMG_1234.HEIC + IMG_1234.MOV)."""
    by_stem: dict[str, list[Entry]] = {}
    for e in entries:
        e.extra["kind"] = kind(e.name, e.is_dir)
        if not e.is_dir:
            by_stem.setdefault(os.path.splitext(e.name)[0].lower(), []).append(e)
    for group in by_stem.values():
        if len(group) < 2:
            continue
        stills = [e for e in group if ext(e.name) in LIVE_STILL_EXT]
        vids = [e for e in group if ext(e.name) in LIVE_VIDEO_EXT]
        if not stills or len(vids) != 1:
            continue
        v = vids[0]
        stills.sort(key=lambda e: 0 if ext(e.name) in (".heic", ".heif") else 1)
        s = stills[0]
        # a poster image next to a movie is not a Live Photo
        if ext(s.name) in (".jpg", ".jpeg") and (ext(v.name) != ".mov" or v.size > 40 << 20):
            continue
        if v.size > 80 << 20:
            continue
        s.extra["live"] = v.name
        v.extra["live_of"] = s.name
    return entries


# thumbnails ------------------------------------------------------------------
def _cache_path(cid: str, path: str, size: int, mtime: float, box: int) -> str:
    key = hashlib.sha1(f"{cid}|{path}|{size}|{mtime}|{box}".encode()).hexdigest()
    d = config.CACHE / "thumbs" / key[:2]
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{key}.jpg")


def _render(src, name: str, out: str, box: int) -> None:
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    e = ext(name)
    img = None
    if e in RAW_EXT:
        import rawpy
        with rawpy.imread(src) as raw:
            try:
                th = raw.extract_thumb()
                if th.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(th.data))
                else:
                    img = Image.fromarray(th.data)
            except Exception:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True)
                img = Image.fromarray(rgb)
    else:
        img = Image.open(src)
        if img.format == "JPEG":
            img.draft("RGB", (box * 2, box * 2))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (20, 22, 26))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    img.thumbnail((box, box), Image.LANCZOS)
    tmp = out + ".tmp"
    img.convert("RGB").save(tmp, "JPEG", quality=82 if box <= 512 else 88, optimize=True)
    os.replace(tmp, out)


async def thumbnail(cid: str, path: str, box: int) -> str:
    b = await manager.get(cid)
    st = await manager.run(cid, lambda b: b.stat(path))
    out = _cache_path(cid, path, st.size, st.mtime, box)
    if os.path.exists(out):
        return out
    if not can_thumb(st.name):
        raise BackendError("No preview for this file type")
    async with _thumb_sem:
        if os.path.exists(out):
            return out
        lp = await b.local_path(path)
        if lp:
            src = lp
        else:
            if st.size > config.THUMB_FETCH_MAX:
                raise BackendError("File too large to preview over the network")
            buf = bytearray()
            async for chunk in b.read(path):
                buf += chunk
            src = io.BytesIO(bytes(buf))
        try:
            await asyncio.to_thread(_render, src, st.name, out, box)
        except Exception as e:
            raise BackendError(f"Couldn't render a preview: {e}") from e
    return out


# subtitles --------------------------------------------------------------------
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return data.decode("latin-1")


_SRT_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def srt_to_vtt(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    out = ["WEBVTT", ""]
    for line in text.split("\n"):
        if "-->" in line:
            line = _SRT_TS.sub(lambda m: f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}.{m.group(4).ljust(3, '0')[:3]}", line)
            # drop SRT coordinates like X1:...
            line = re.sub(r"\s+X1:.*$", "", line)
        out.append(line)
    return "\n".join(out) + "\n"


def _ass_time(t: str) -> str:
    h, m, rest = t.strip().split(":")
    s, cs = (rest.split(".") + ["0"])[:2]
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{(cs + '00')[:2]}0"


def ass_to_vtt(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fmt = None
    cues = []
    in_events = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("["):
            in_events = s.lower() == "[events]"
            continue
        if not in_events:
            continue
        if s.lower().startswith("format:"):
            fmt = [x.strip().lower() for x in s[7:].split(",")]
        elif s.lower().startswith("dialogue:") and fmt:
            parts = s[9:].split(",", len(fmt) - 1)
            if len(parts) < len(fmt):
                continue
            row = dict(zip(fmt, parts))
            body = re.sub(r"\{[^}]*\}", "", row.get("text", ""))
            body = body.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()
            if not body:
                continue
            try:
                cues.append((_ass_time(row["start"]), _ass_time(row["end"]), body))
            except Exception:
                continue
    cues.sort(key=lambda c: c[0])
    out = ["WEBVTT", ""]
    for a, b, body in cues:
        out += [f"{a} --> {b}", body, ""]
    return "\n".join(out)


def to_vtt(name: str, data: bytes) -> str:
    text = _decode(data)
    e = ext(name)
    if e == ".vtt":
        return text if text.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + text
    if e in (".ass", ".ssa"):
        return ass_to_vtt(text)
    return srt_to_vtt(text)


def subtitle_label(video: str, sub: str) -> str:
    vstem = os.path.splitext(video)[0]
    sstem = os.path.splitext(sub)[0]
    if sstem.lower().startswith(vstem.lower()):
        rest = sstem[len(vstem):].strip(" ._-")
        return rest or "Default"
    return sstem


async def sibling_subtitles(cid: str, path: str) -> list[dict]:
    parent = posixpath.dirname(path) or "/"
    name = posixpath.basename(path)
    vstem = os.path.splitext(name)[0].lower()
    entries = await manager.run(cid, lambda b: b.list(parent))
    subs = [e for e in entries if not e.is_dir and ext(e.name) in SUB_EXT]
    # Subs/ or Subtitles/ folder next to the movie, a common release layout
    for d in entries:
        if d.is_dir and d.name.lower() in ("subs", "subtitles", "sub"):
            try:
                subs += [e for e in await manager.run(cid, lambda b: b.list(d.path))
                         if not e.is_dir and ext(e.name) in SUB_EXT]
            except Exception:
                pass
    out = []
    for s in subs:
        matched = os.path.splitext(s.name)[0].lower().startswith(vstem)
        out.append({"path": s.path, "name": s.name, "label": subtitle_label(name, s.name),
                    "matched": matched})
    out.sort(key=lambda x: (not x["matched"], x["name"].lower()))
    return out


async def read_bytes(cid: str, path: str, limit: int) -> bytes:
    b = await manager.get(cid)
    buf = bytearray()
    agen = b.read(path, 0, limit)
    try:
        async for chunk in agen:
            buf += chunk
            if len(buf) >= limit:
                break
    finally:
        await agen.aclose()
    return bytes(buf[:limit])
