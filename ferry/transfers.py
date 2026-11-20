"""Transfer queue: copy, move and archive extraction between any two connections.

How a copy runs:
  - a scanner lists source folders, several at once, and feeds a queue
  - workers copy files off that queue as soon as they show up
  - every file is written under a hidden part name and renamed when complete
  - a file already at the destination with the same size and date is skipped,
    so running a job again only fills in what's missing
  - a move deletes each source file right after its copy is confirmed; on one
    connection it renames each file instead, which is instant
  - a merge is a move into an existing folder: duplicates go to the trash,
    different files with the same name are kept side by side
  - when a connection drops the job waits and retries instead of failing
  - jobs are saved, so a restart picks unfinished ones back up
"""
from __future__ import annotations

import asyncio
import filecmp
import posixpath
import shutil
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from . import config, db, trash
from .backends import manager
from .backends.base import (PART_SUFFIX, RESUME_ALIGN, Backend, BackendError, Disconnected,
                            Entry, NotFound, join, norm, part_name)

LIST_PARALLEL = 8          # folders listed at the same time
QUEUE_MAX = 2000           # files waiting for a worker
RESUME_MIN = 16 << 20      # only bigger files keep their part file for resuming
WAIT_MAX = 6 * 3600        # how long a job waits for a machine to come back
ACTIVE = ("queued", "running", "paused", "waiting")
FINISHED = ("done", "error", "canceled")


class JobCancelled(Exception):
    pass


async def unique_name(b: Backend, parent: str, name: str, taken: Optional[dict] = None) -> str:
    """'Photo.jpg' -> 'Photo (2).jpg' until nothing is in the way."""
    stem, ext = posixpath.splitext(name)
    if name.startswith(".") and not ext:
        stem, ext = name, ""
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if (taken is None or cand not in taken) and not await b.exists(join(parent, cand)):
            return join(parent, cand)
        n += 1


def same_file(dst: Entry, size: int, mtime: float) -> bool:
    """Same size and date, two seconds of slack for FAT and SMB clocks."""
    if dst.is_dir or dst.size != size:
        return False
    return not dst.mtime or not mtime or abs(dst.mtime - mtime) <= 2


@dataclass
class Item:
    kind: str  # "dir" or "file"
    src: str
    dst: str
    size: int = 0
    mtime: float = 0.0
    mode: int = 0
    renamed: bool = False  # moved by a rename on one connection


class Side:
    """One end of a transfer: how to get its backend and how to reset it."""

    def __init__(self, cid: str, getter: Optional[Callable[[], Awaitable[Backend]]] = None):
        self.cid = cid
        self.getter = getter or (lambda: manager.get(cid))

    async def get(self) -> Backend:
        return await self.getter()

    async def reset(self) -> None:
        if not self.cid.startswith("_"):
            await manager.drop(self.cid)


class Run:
    """Per job scratch state: folders made, destination listings, locks."""

    def __init__(self, src: Side, dst: Side):
        self.src = src
        self.dst = dst
        self.made: set[str] = {"/"}
        self.fresh: set[str] = set()     # created by this job, known empty
        self.index: dict[str, dict[str, Entry]] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.dirs: list[tuple[str, str]] = []


@dataclass
class Job:
    kind: str
    src_conn: str
    src_paths: list
    dst_conn: str
    dst_dir: str
    conflict: str = "rename"  # rename (keep both), overwrite, skip, resume, merge
    title: str = ""
    archive: Optional[dict] = None
    targets: dict = field(default_factory=dict)  # top level source -> destination
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"
    phase: str = ""
    error: str = ""
    errors: list = field(default_factory=list)
    bytes_total: int = 0
    bytes_done: int = 0
    files_total: int = 0
    files_done: int = 0
    files_skipped: int = 0
    counting: bool = False
    current: str = ""
    speed: float = 0.0
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancelled: bool = False
    parked: dict = field(default_factory=dict)  # bytes waiting in kept part files, shown as done

    SAVED = ("id", "kind", "title", "src_conn", "src_paths", "dst_conn", "dst_dir", "conflict",
             "archive", "targets", "status", "error", "errors", "bytes_total", "bytes_done",
             "files_total", "files_done", "files_skipped", "created", "started", "finished")

    def __post_init__(self):
        self.run_event = asyncio.Event()
        self.run_event.set()
        self._last_t = 0.0
        self._last_b = 0
        self._saved = 0.0

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.SAVED if k not in ("archive", "errors")}
        if self.status not in FINISHED:
            d["bytes_done"] += sum(self.parked.values())
        d.update(phase=self.phase, current=self.current, speed=self.speed, counting=self.counting,
                 errors=self.errors[-50:], error_count=len(self.errors))
        return d

    def state(self) -> dict:
        s = {k: getattr(self, k) for k in self.SAVED}
        s["errors"] = self.errors[-200:]
        if s["archive"]:
            s["archive"] = {k: v for k, v in s["archive"].items() if k != "password"}
        return s

    @classmethod
    def from_state(cls, s: dict) -> "Job":
        return cls(**{k: s[k] for k in cls.SAVED if k in s})


class Engine:
    KEEP_FINISHED = 60

    def __init__(self):
        self.jobs: "OrderedDict[str, Job]" = OrderedDict()
        self.sem = asyncio.Semaphore(config.MAX_JOBS)
        self.version = 0
        self._changed = asyncio.Event()
        self._last_bump = 0.0

    # change notification and saving --------------------------------------
    def bump(self, force: bool = True) -> None:
        now = time.time()
        if not force and now - self._last_bump < 0.3:
            return
        self._last_bump = now
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_change(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def snapshot(self) -> list[dict]:
        return [j.to_dict() for j in reversed(self.jobs.values())]

    def save(self, job: Job, force: bool = True) -> None:
        now = time.time()
        if not force and now - job._saved < 5:
            return
        job._saved = now
        try:
            db.save_job(job.id, job.state())
        except Exception:
            pass  # a full disk shouldn't stop a transfer

    def _status(self, job: Job, status: str) -> None:
        job.status = status
        self.save(job)
        self.bump()

    def restore(self) -> None:
        """Bring back saved jobs. Unfinished ones carry on where they were."""
        for s in db.load_jobs():
            try:
                job = Job.from_state(s)
            except Exception:
                continue
            if job.status in FINISHED:
                self.jobs[job.id] = job
                continue
            paused = job.status == "paused"
            job.conflict = "merge" if job.conflict == "merge" else "resume"
            job.status = "queued"
            job.bytes_done = job.files_done = job.files_skipped = 0
            job.bytes_total = job.files_total = 0
            if paused:
                job.run_event.clear()
                job.status = "paused"
            self.submit(job)

    # job control ----------------------------------------------------------
    def submit(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self._trim()
        self.save(job)
        asyncio.create_task(self._wrap(job))
        self.bump()
        return job

    def _trim(self):
        done = [j for j in self.jobs.values() if j.status in FINISHED]
        old = done[:-self.KEEP_FINISHED]
        for j in old:
            self.jobs.pop(j.id, None)
        if old:
            db.delete_jobs([j.id for j in old])

    def pause(self, jid: str) -> None:
        j = self.jobs[jid]
        if j.status in ("queued", "running", "waiting"):
            j.run_event.clear()
            if j.status == "queued":
                self._status(j, "paused")
        self.bump()

    def resume(self, jid: str) -> None:
        j = self.jobs[jid]
        j.run_event.set()
        if j.status == "paused":
            self._status(j, "running" if j.started else "queued")

    def cancel(self, jid: str) -> None:
        j = self.jobs[jid]
        j.cancelled = True
        j.run_event.set()
        self.bump()

    def clear_finished(self) -> None:
        ids = [j.id for j in self.jobs.values() if j.status in FINISHED]
        for jid in ids:
            self.jobs.pop(jid, None)
        db.delete_jobs(ids)
        self.bump()

    def retry(self, jid: str) -> Job:
        """Run the same job again. Finished files are skipped, the rest fill in."""
        j = self.jobs[jid]
        return self.submit(Job(kind=j.kind, src_conn=j.src_conn, src_paths=j.src_paths,
                               dst_conn=j.dst_conn, dst_dir=j.dst_dir,
                               conflict="merge" if j.conflict == "merge" else "resume",
                               title=j.title, archive=j.archive, targets=dict(j.targets)))

    async def _gate(self, job: Job) -> None:
        if job.cancelled:
            raise JobCancelled()
        if not job.run_event.is_set():
            prev = job.status
            self._status(job, "paused")
            await job.run_event.wait()
            if job.cancelled:
                raise JobCancelled()
            self._status(job, prev if prev != "paused" else "running")

    def _tick(self, job: Job) -> None:
        now = time.time()
        if now - job._last_t >= 0.5:
            if job._last_t:
                rate = (job.bytes_done - job._last_b) / (now - job._last_t)
                job.speed = rate if job.speed == 0 else job.speed * 0.6 + rate * 0.4
            job._last_t, job._last_b = now, job.bytes_done
            self.save(job, force=False)
            self.bump(force=False)

    async def _wrap(self, job: Job) -> None:
        try:
            await job.run_event.wait()
            async with self.sem:
                if job.cancelled:
                    raise JobCancelled()
                await job.run_event.wait()
                job.started = job.started or time.time()
                self._status(job, "running")
                if job.kind == "extract":
                    await self._extract(job)
                else:
                    await self._copy(job, Side(job.src_conn))
                if job.errors and job.files_done == 0 and job.files_skipped == 0 and job.files_total:
                    job.error = job.errors[0]
                    job.status = "error"
                else:
                    job.status = "done"
        except (JobCancelled, asyncio.CancelledError):
            job.status = "canceled"
        except BackendError as e:
            job.status = "error"
            job.error = str(e)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{e.__class__.__name__}: {e}"
        finally:
            job.finished = time.time()
            job.current = ""
            job.speed = 0
            job.counting = False
            self.save(job)
            self.bump()

    # waiting out a dropped connection ----------------------------------------
    async def _wait(self, job: Job, err: BackendError, since: float, delay: float) -> float:
        if time.time() - since > WAIT_MAX:
            raise err
        job.current = f"Connection dropped, trying again in {int(delay)}s ({err})"
        if job.status != "waiting":
            self._status(job, "waiting")
        end = time.time() + delay
        while time.time() < end:
            await self._gate(job)
            await asyncio.sleep(min(1, end - time.time()))
        return min(delay * 2, 60)

    async def _net(self, job: Job, side: Side, fn: Callable[[Backend], Awaitable]):
        """Run fn against one side, waiting out dropped connections."""
        since, delay = 0.0, 2.0
        while True:
            await self._gate(job)
            b = None
            try:
                b = await side.get()
                out = await fn(b)
                if job.status == "waiting":
                    self._status(job, "running")
                return out
            except (JobCancelled, asyncio.CancelledError):
                raise
            except Exception as e:
                err = b.translate(e) if b else (e if isinstance(e, BackendError) else BackendError(str(e)))
                if not isinstance(err, Disconnected):
                    raise err from e
                await side.reset()
                since = since or time.time()
                delay = await self._wait(job, err, since, delay)

    # copy / move ----------------------------------------------------------
    async def _tops(self, job: Job, run: Run, paths: list) -> list[tuple[Entry, str]]:
        """Pick the destination for each selected item, handling name clashes."""
        same = run.src.cid == run.dst.cid
        out = []
        for sp in paths:
            sp = norm(sp)
            try:
                st = await self._net(job, run.src, lambda b, sp=sp: b.stat(sp))
            except NotFound:
                if job.conflict == "resume":
                    continue  # moved on an earlier run
                raise
            if sp in job.targets:
                out.append((st, job.targets[sp]))
                continue
            name = posixpath.basename(sp)
            target = join(job.dst_dir, name)
            ddir = norm(job.dst_dir)
            if same and st.is_dir and (ddir == sp or ddir.startswith(sp.rstrip("/") + "/")):
                raise BackendError(f"Can't put {name} inside itself")
            if same and target == sp:
                if job.kind == "move":
                    continue
                target = await self._net(job, run.dst, lambda b: unique_name(b, ddir, name))
            elif job.conflict == "rename" and await self._net(job, run.dst, lambda b: b.exists(target)):
                target = await self._net(job, run.dst, lambda b: unique_name(b, ddir, name))
            if same and job.kind == "move":
                try:  # one connection: a plain rename is instant
                    if not await self._net(job, run.dst, lambda b: b.exists(target)):
                        await self._net(job, run.src, lambda b: b.rename(sp, target))
                        job.targets[sp] = target
                        job.files_total += 1
                        job.files_done += 1
                        continue
                except BackendError:
                    pass
            job.targets[sp] = target
            out.append((st, target))
        self.save(job)
        return out

    async def _scan(self, job: Job, run: Run, tops, queue: asyncio.Queue) -> None:
        sem = asyncio.Semaphore(LIST_PARALLEL)

        async def add(e: Entry, dst: str):
            if e.is_dir:
                run.dirs.append((e.path, dst))
                await queue.put(Item("dir", e.path, dst))
                await walk(e.path, dst)
            elif not e.name.endswith(PART_SUFFIX):
                job.files_total += 1
                job.bytes_total += e.size
                await queue.put(Item("file", e.path, dst, e.size, e.mtime, e.mode))

        async def walk(sdir: str, ddir: str):
            async with sem:
                try:
                    entries = await self._net(job, run.src, lambda b: b.list(sdir))
                except BackendError as e:  # an unreadable folder shouldn't sink the whole job
                    job.errors.append(f"{sdir}: {e}")
                    return
                job.current = f"Counting {sdir}"
                self.bump(force=False)
            await asyncio.gather(*(add(e, join(ddir, e.name)) for e in sorted(entries, key=lambda x: x.name)))

        await asyncio.gather(*(add(st, target) for st, target in tops))

    async def _ensure_dir(self, run: Run, b: Backend, path: str) -> None:
        """mkdir with a cache: one round trip per new folder on a slow link."""
        path = norm(path)
        if path in run.made:
            return
        await self._ensure_dir(run, b, posixpath.dirname(path))
        async with run.locks[path]:
            if path in run.made:
                return
            try:
                await b.mkdir(path)
                run.fresh.add(path)
            except Exception as e:
                if not await b.exists(path):
                    raise b.translate(e) from e
            run.made.add(path)

    async def _listing(self, run: Run, b: Backend, path: str) -> dict[str, Entry]:
        """What's already in a destination folder, fetched once per job."""
        if path in run.fresh:
            return run.index.setdefault(path, {})
        async with run.locks["ls:" + path]:
            if path not in run.index:
                try:
                    run.index[path] = {e.name: e for e in await b.list(path)}
                except Exception as e:
                    if not isinstance(b.translate(e), NotFound):
                        raise
                    run.index[path] = {}
            return run.index[path]

    async def _copy(self, job: Job, src: Side, paths: Optional[list] = None) -> None:
        run = Run(src, Side(job.dst_conn))
        job.phase = "Moving" if job.kind == "move" else "Copying"
        await self._net(job, run.dst, lambda b: b.makedirs(job.dst_dir))
        tops = await self._tops(job, run, paths or job.src_paths)
        top_dirs = {t for _, t in tops}
        queue: asyncio.Queue = asyncio.Queue(QUEUE_MAX)
        nworkers = max(1, config.FILE_WORKERS)

        async def worker():
            while (it := await queue.get()) is not None:
                if it.kind == "file":
                    await self._copy_file(job, run, it)
                    continue
                try:
                    await self._net(job, run.dst, lambda b: self._ensure_dir(run, b, it.dst))
                except BackendError as e:
                    if it.dst in top_dirs:
                        raise  # can't even make the top folder, nothing will work
                    job.errors.append(f"{it.dst}: {e}")

        async def producer():
            await self._scan(job, run, tops, queue)
            job.counting = False
            self.bump()
            for _ in range(nworkers):
                await queue.put(None)

        job.counting = True
        try:
            async with asyncio.TaskGroup() as tg:  # one failure stops the rest, no stuck queue
                for _ in range(nworkers):
                    tg.create_task(worker())
                tg.create_task(producer())
        except BaseExceptionGroup as eg:
            real = [e for e in eg.exceptions if not isinstance(e, asyncio.CancelledError)]
            raise (real or eg.exceptions)[0]
        finally:
            job.counting = False
        if job.kind == "move" and not job.cancelled:
            await self._remove_empty_dirs(job, run, tops)

    async def _remove_empty_dirs(self, job: Job, run: Run, tops) -> None:
        job.phase = "Cleaning up"
        self.bump()
        dirs = [s for s, _ in run.dirs]
        b = await run.src.get()
        for d in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
            try:
                if not await b.list(d):  # never remove a folder that still has anything in it
                    await b.remove(d)
            except Exception:
                pass

    async def _copy_file(self, job: Job, run: Run, it: Item) -> None:
        since, delay = 0.0, 2.0
        while True:
            await self._gate(job)
            try:
                await self._copy_once(job, run, it)
                if job.status == "waiting":
                    self._status(job, "running")
                return
            except (JobCancelled, asyncio.CancelledError):
                raise
            except Exception as e:
                err = e if isinstance(e, BackendError) else BackendError(f"{e.__class__.__name__}: {e}")
                if isinstance(err, Disconnected):
                    await run.src.reset()
                    await run.dst.reset()
                    since = since or time.time()
                    try:
                        delay = await self._wait(job, err, since, delay)
                        continue
                    except BackendError:
                        pass
                job.errors.append(f"{it.src}: {err}")
                self.save(job, force=False)
                self.bump()
                return

    async def _copy_once(self, job: Job, run: Run, it: Item) -> None:
        sb = await run.src.get()
        db_ = await run.dst.get()

        def classify(e: Exception) -> BackendError:
            for b in (sb, db_):
                err = b.translate(e)
                if isinstance(err, Disconnected):
                    return err
            return sb.translate(e) if not isinstance(e, BackendError) else e

        try:
            parent, name = posixpath.split(it.dst)
            await self._ensure_dir(run, db_, parent)
            have = await self._listing(run, db_, parent)
        except BackendError:
            raise
        except Exception as e:
            raise classify(e) from e
        # Names are picked under a lock and held in the folder listing while the file is on
        # its way, so two files going to one folder (a merge) never pick the same name.
        lock = run.locks["name:" + parent]
        target, replace, mine = it.dst, False, None
        async with lock:
            existing = have.get(name)
            if existing is None:
                mine = have[name] = Entry(name, it.dst, False, it.size, it.mtime, extra={"pending": True})
        if mine is None:
            pending = existing.extra.get("pending", False)
            if not pending:
                dup = same_file(existing, it.size, it.mtime)
                if job.conflict == "merge" and not existing.is_dir and existing.size == it.size:
                    same = await self._same_content(sb, it.src, db_, existing.path or join(parent, name))
                    dup = dup if same is None else same  # bytes beat dates when they can be read
                if dup:
                    await self._done(job, run, it, sb, copied=False)
                    return
            if existing.is_dir or pending or job.conflict in ("rename", "resume", "merge"):
                async with lock:
                    target = await unique_name(db_, parent, name, have)
                    tn = posixpath.basename(target)
                    mine = have[tn] = Entry(tn, target, False, it.size, it.mtime, extra={"pending": True})
            elif job.conflict == "skip":
                job.files_skipped += 1
                job.bytes_done += it.size
                return
            else:
                replace = True
        tn = posixpath.basename(target)
        try:
            await self._put(job, run, it, sb, db_, target, replace, classify)
        except BaseException:
            if mine is not None and have.get(tn) is mine:
                del have[tn]  # free the name again, nothing landed there
            raise
        have[tn] = Entry(tn, target, False, it.size, it.mtime)
        await self._done(job, run, it, sb, copied=True)

    async def _put(self, job: Job, run: Run, it: Item, sb: Backend, db_: Backend, target: str,
                   replace: bool, classify) -> None:
        """Get one file to target: a rename on one connection, otherwise a copy through a part file."""
        parent = posixpath.dirname(target)
        if job.kind == "move" and run.src.cid == run.dst.cid and not replace:
            job.current = it.src
            try:  # one connection: rename the file instead of copying its bytes
                await sb.rename(it.src, target)
            except Exception as e:
                if isinstance(classify(e), Disconnected):
                    raise classify(e) from e
            else:
                job.files_done += 1
                job.bytes_done += it.size
                self.bump(force=False)
                it.renamed = True
                return
        part = part_name(posixpath.basename(target), it.size, it.mtime)
        offset = 0
        if it.size >= RESUME_MIN and db_.resumable:
            offset = min(await db_.resume_offset(join(parent, part)), it.size)
        job.current = it.src
        w = await db_.open_write(target, mtime=it.mtime or None, mode=it.mode or None,
                                 part=part, offset=offset, replace=replace)
        written = offset
        job.parked.pop(it.src, None)
        job.bytes_done += offset
        agen = sb.read(it.src, offset)
        try:
            async for chunk in agen:
                await self._gate(job)
                await w.write(chunk)
                written += len(chunk)
                job.bytes_done += len(chunk)
                if job.status == "waiting":  # bytes are flowing again
                    self._status(job, "running")
                sb.last_used = db_.last_used = time.time()
                self._tick(job)
            if written != it.size:
                raise BackendError(f"Size changed while copying ({written} of {it.size} bytes)")
            await w.close()
        except BaseException as e:
            job.bytes_done -= written
            err = e if isinstance(e, (JobCancelled, asyncio.CancelledError)) else classify(e)
            keep = isinstance(err, Disconnected) and it.size >= RESUME_MIN
            if keep:
                job.parked[it.src] = written - written % RESUME_ALIGN
            await w.abort(keep=keep)
            if err is e:
                raise
            raise err from e
        finally:
            await agen.aclose()

    async def _done(self, job: Job, run: Run, it: Item, sb: Backend, copied: bool) -> None:
        if it.renamed:
            return  # moved by rename: counted already, nothing left behind
        if copied:
            job.files_done += 1
        else:
            job.files_skipped += 1
            job.bytes_done += it.size
        if job.kind == "move" and not run.src.cid.startswith("_"):
            try:  # the copy is confirmed (or was already there), now the original can go
                if job.conflict == "merge" and not copied:
                    await trash.discard(sb, self._trash_of(run.src.cid), it.src)
                else:
                    await sb.remove(it.src)
            except Exception as e:
                job.errors.append(f"Couldn't remove {it.src}: {sb.translate(e)}")
        self.bump(force=False)

    @staticmethod
    def _trash_of(cid: str) -> str:
        conn = db.get_connection(cid, secrets_too=False)
        return trash.setting(conn) if conn else ""

    @staticmethod
    async def _same_content(sb: Backend, a: str, db_: Backend, b: str) -> Optional[bool]:
        """Byte for byte check where both files are on this machine's disks, else None."""
        try:
            la, lb = await sb.local_path(a), await db_.local_path(b)
            if not la or not lb:
                return None
            return await asyncio.to_thread(filecmp.cmp, la, lb, False)
        except Exception:
            return None

    # archives ---------------------------------------------------------------
    async def _extract(self, job: Job) -> None:
        from . import archive
        from .backends.local import LocalBackend

        path = job.src_paths[0]
        opts = job.archive or {}
        job.phase = "Fetching archive"
        self.bump()

        def progress(done, total):
            job.bytes_done, job.bytes_total = done, total
            self._tick(job)

        local = await archive.ensure_local(job.src_conn, path, progress)
        await self._gate(job)
        tmpdir = config.TMP / f"extract-{job.id}"
        tmpdir.mkdir(parents=True, exist_ok=True)
        try:
            job.phase = "Extracting"
            job.bytes_done = job.bytes_total = 0
            self.bump()
            out_name = opts.get("folder") or archive.default_folder_name(path)
            out_root = tmpdir / "out"
            await asyncio.to_thread(archive.extract, local, str(out_root / out_name),
                                    opts.get("members") or None, opts.get("password") or None)
            await self._gate(job)
            tmp = LocalBackend({"id": f"_tmp_{job.id}", "config": {"root": str(out_root)}})

            async def get_tmp():
                return tmp

            paths = [f"/{out_name}"] if opts.get("into_folder", True) else \
                [e.path for e in await tmp.list(f"/{out_name}")]
            job.bytes_done = job.bytes_total = 0
            await self._copy(job, Side(f"_tmp_{job.id}", get_tmp), paths)
        finally:
            await asyncio.to_thread(shutil.rmtree, tmpdir, True)


engine = Engine()
