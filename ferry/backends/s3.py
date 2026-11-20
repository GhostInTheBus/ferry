"""Amazon S3 and anything that speaks its API (Backblaze B2, Wasabi, MinIO, Cloudflare R2).

S3 has no real folders: a "folder" is just the part of a key before a slash.
boto3 is a blocking library, so every call runs in a thread.
"""
from __future__ import annotations

import asyncio
import posixpath
from typing import AsyncIterator, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .base import (CHUNK, AlreadyExists, Backend, BackendError, Disconnected, Entry, NotFound,
                   PermissionDenied, Writer, is_hidden, join, norm)

PART = 16 << 20  # 16 MiB parts: files up to 160 GB


class _Upload(Writer):
    """A multipart upload. The object only appears once every part is in."""

    def __init__(self, b: "S3Backend", key: str):
        self.b, self.key = b, key
        self.buf = bytearray()
        self.parts: list[dict] = []
        self.upload_id: Optional[str] = None

    async def _flush(self) -> None:
        if self.upload_id is None:
            r = await self.b.call("create_multipart_upload", Key=self.key)
            self.upload_id = r["UploadId"]
        n = len(self.parts) + 1
        r = await self.b.call("upload_part", Key=self.key, UploadId=self.upload_id, PartNumber=n, Body=bytes(self.buf))
        self.parts.append({"ETag": r["ETag"], "PartNumber": n})
        self.buf.clear()

    async def write(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) >= PART:
            await self._flush()

    async def close(self) -> None:
        if self.upload_id is None:  # small file: one plain upload
            await self.b.call("put_object", Key=self.key, Body=bytes(self.buf))
            return
        if self.buf:
            await self._flush()
        await self.b.call("complete_multipart_upload", Key=self.key, UploadId=self.upload_id,
                          MultipartUpload={"Parts": self.parts})

    async def abort(self, keep: bool = False) -> None:
        if self.upload_id:
            try:
                await self.b.call("abort_multipart_upload", Key=self.key, UploadId=self.upload_id)
            except Exception:
                pass


class S3Backend(Backend):
    kind = "s3"
    keeps_dates = False  # objects are stamped with the upload time

    async def connect(self) -> None:
        self.bucket = (self.cfg.get("bucket") or "").strip()
        if not self.bucket:
            raise BackendError("Enter the bucket name")
        self.prefix = norm(self.cfg.get("root") or "/").strip("/")
        endpoint = (self.cfg.get("endpoint") or "").strip() or None
        if endpoint and "://" not in endpoint:
            endpoint = "https://" + endpoint
        self.s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name=(self.cfg.get("region") or "").strip() or "us-east-1",
            aws_access_key_id=self.cfg.get("username") or None, aws_secret_access_key=self.cfg.get("password") or None,
            config=Config(connect_timeout=15, read_timeout=120, retries={"max_attempts": 3},
                          s3={"addressing_style": "path"} if endpoint else {}))
        await self.call("list_objects_v2", MaxKeys=1, Prefix=self._key("/", True))

    def _key(self, path: str, folder: bool = False) -> str:
        k = "/".join(x for x in (self.prefix, norm(path).strip("/")) if x)
        return k + "/" if folder and k else k

    async def call(self, op: str, **kw):
        return await asyncio.to_thread(getattr(self.s3, op), Bucket=self.bucket, **kw)

    def translate(self, e: Exception) -> BackendError:
        if isinstance(e, ClientError):
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return NotFound("Not found")
            if code == "NoSuchBucket":
                return BackendError("There's no bucket with that name")
            if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                return PermissionDenied("S3 refused that. Check the access key, the secret and what the key may do.")
            return BackendError(e.response.get("Error", {}).get("Message") or code)
        if isinstance(e, BotoCoreError):
            return Disconnected(str(e))
        return super().translate(e)

    async def _pages(self, **kw):
        token = None
        while True:
            r = await self.call("list_objects_v2", **kw, **({"ContinuationToken": token} if token else {}))
            yield r
            token = r.get("NextContinuationToken")
            if not token:
                return

    async def list(self, path: str) -> list[Entry]:
        path = norm(path)
        pre = self._key(path, True)
        out: dict[str, Entry] = {}
        async for r in self._pages(Prefix=pre, Delimiter="/"):
            for cp in r.get("CommonPrefixes", []):
                name = cp["Prefix"][len(pre):].rstrip("/")
                out[name] = Entry(name, join(path, name), True, hidden=is_hidden(name))
            for o in r.get("Contents", []):
                name = o["Key"][len(pre):]
                if name and name not in out:
                    out[name] = Entry(name, join(path, name), False, o["Size"], o["LastModified"].timestamp(),
                                      is_hidden(name))
        return list(out.values())

    async def stat(self, path: str) -> Entry:
        path = norm(path)
        name = posixpath.basename(path)
        if path == "/":
            return Entry("", "/", True)
        try:
            o = await self.call("head_object", Key=self._key(path))
            return Entry(name, path, False, o["ContentLength"], o["LastModified"].timestamp(), is_hidden(name))
        except ClientError as e:
            if not isinstance(self.translate(e), NotFound):
                raise
        r = await self.call("list_objects_v2", Prefix=self._key(path, True), MaxKeys=1)
        if r.get("KeyCount"):
            return Entry(name, path, True, hidden=is_hidden(name))
        raise NotFound("Not found")

    async def mkdir(self, path: str) -> None:
        if await self.exists(path):
            raise AlreadyExists("Something with that name is already there")
        await self.call("put_object", Key=self._key(path, True), Body=b"")  # the usual empty folder marker

    async def remove(self, path: str) -> None:
        st = await self.stat(path)
        await self.call("delete_object", Key=self._key(path, st.is_dir))

    async def rmtree(self, path: str) -> None:
        st = await self.stat(path)
        if not st.is_dir:
            return await self.remove(path)
        async for r in self._pages(Prefix=self._key(path, True)):
            keys = [{"Key": o["Key"]} for o in r.get("Contents", [])]
            if keys:
                await self.call("delete_objects", Delete={"Objects": keys, "Quiet": True})

    async def _move_key(self, src: str, dst: str) -> None:
        # the managed copy also handles objects over 5 GB
        await asyncio.to_thread(self.s3.copy, {"Bucket": self.bucket, "Key": src}, self.bucket, dst)
        await self.call("delete_object", Key=src)

    async def rename(self, src: str, dst: str) -> None:
        st = await self.stat(src)
        if not st.is_dir:
            return await self._move_key(self._key(src), self._key(dst))
        a, b = self._key(src, True), self._key(dst, True)
        async for r in self._pages(Prefix=a):  # no real folders: every object moves on its own
            for o in r.get("Contents", []):
                await self._move_key(o["Key"], b + o["Key"][len(a):])

    async def read(self, path: str, offset: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        kw = {}
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            kw["Range"] = f"bytes={offset}-{end}"
        body = (await self.call("get_object", Key=self._key(path), **kw))["Body"]
        try:
            while chunk := await asyncio.to_thread(body.read, CHUNK):
                yield chunk
        finally:
            body.close()

    async def open_write(self, path: str, mtime=None, mode=None, part=None, offset: int = 0, replace=None) -> Writer:
        return _Upload(self, self._key(path))  # already all or nothing, no part file needed
