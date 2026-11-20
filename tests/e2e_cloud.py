"""WebDAV, S3, search, scheduled sync and resumable upload checks.

Needs Ferry on the test fixtures plus tests/cloud_servers.py:
    python3 tests/cloud_servers.py /tmp/ferrytest &
    python3 tests/e2e_cloud.py /tmp/ferrytest
"""
import hashlib
import os
import shutil
import sys
import time

import httpx

BASE = os.environ.get("FERRY_URL", "http://127.0.0.1:8420")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
NAS = f"{ROOT}/nas"
H = {"X-Ferry": "1"}
c = httpx.Client(base_url=BASE, timeout=180)
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def wait_job(jid, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        j = {x["id"]: x for x in c.get("/api/jobs").json()["jobs"]}[jid]
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


def transfer(kind, src, paths, dst, dst_dir, conflict="rename"):
    r = c.post("/api/transfer", headers=H, json={"kind": kind, "src_conn": src, "paths": paths,
                                                  "dst_conn": dst, "dst_dir": dst_dir, "conflict": conflict})
    assert r.status_code == 200, r.text
    return wait_job(r.json()["id"])


def names(conn, path):
    return sorted(e["name"] for e in c.get("/api/list", params={"conn": conn, "path": path}).json()["entries"])


def sha(b):
    return hashlib.sha256(b).hexdigest()


c.post("/api/login", json={"password": os.environ.get("FERRY_PASSWORD", "testpass123")})
conns = c.get("/api/connections").json()["connections"]
local = next(x for x in conns if x["type"] == "local")["id"]
for x in conns:
    if x["name"] in ("Test WebDAV", "Test S3"):
        c.delete(f"/api/connections/{x['id']}", headers=H)

# fixtures
for n in os.listdir(f"{ROOT}/webdav"):
    shutil.rmtree(f"{ROOT}/webdav/{n}", ignore_errors=True)
import boto3
_b = boto3.resource("s3", endpoint_url="http://127.0.0.1:5005", region_name="us-east-1",
                    aws_access_key_id="test", aws_secret_access_key="test").Bucket("ferry-test")
_b.objects.all().delete()
src = f"{NAS}/CloudSrc"
shutil.rmtree(src, ignore_errors=True)
os.makedirs(f"{src}/sub dir")
open(f"{src}/hello.txt", "w").write("hello cloud")
open(f"{src}/sub dir/ünï côde.txt", "w").write("nested")
big = os.urandom(40 << 20)
open(f"{src}/big.bin", "wb").write(big)
shutil.rmtree(f"{NAS}/CloudBack", ignore_errors=True)
os.makedirs(f"{NAS}/CloudBack")

cfgs = {
    "webdav": ("Test WebDAV", {"url": "http://127.0.0.1:8081/dav", "username": "davuser", "password": "dav-pw"}),
    "s3": ("Test S3", {"bucket": "ferry-test", "endpoint": "http://127.0.0.1:5005", "username": "test",
                       "password": "test", "root": "/ferry"}),
}
for kind, (name, cfg) in cfgs.items():
    r = c.post("/api/connections", headers=H, json={"name": name, "type": kind, "config": cfg})
    check(r.status_code == 200, f"{kind}: connection saved ({r.status_code} {r.text[:80]})")
    cid = r.json()["id"]
    t = c.post(f"/api/connections/{cid}/test", headers=H).json()
    check(t.get("ok"), f"{kind}: connects ({t})")
    bad = c.post("/api/connections/test", headers=H, json={"type": kind, "config": {**cfg, "password": "wrong"}}).json()
    check(not bad.get("ok") or kind == "s3", f"{kind}: wrong password is refused ({str(bad)[:70]})")
    check(names(cid, "/") == [], f"{kind}: starts empty")
    r = c.post("/api/mkdir", headers=H, json={"conn": cid, "path": "/Made here"})
    check(r.status_code == 200 and names(cid, "/") == ["Made here"], f"{kind}: new folder")

    j = transfer("copy", local, ["/CloudSrc"], cid, "/")
    check(j["status"] == "done" and j["files_done"] == 3, f"{kind}: copy up, 3 files ({j['status']} {j['files_done']} {j.get('errors')})")
    check(names(cid, "/CloudSrc") == ["big.bin", "hello.txt", "sub dir"], f"{kind}: listing {names(cid, '/CloudSrc')}")
    check(names(cid, "/CloudSrc/sub dir") == ["ünï côde.txt"], f"{kind}: unicode and spaces in names")
    check(not any(".ferry-part" in n for n in names(cid, "/CloudSrc")), f"{kind}: no part files left")
    e = next(x for x in c.get("/api/list", params={"conn": cid, "path": "/CloudSrc"}).json()["entries"] if x["name"] == "big.bin")
    check(e["size"] == len(big), f"{kind}: 40 MB file has the right size")

    j = transfer("copy", local, ["/CloudSrc"], cid, "/", conflict="skip")
    check(j["files_done"] == 0 and j["files_skipped"] == 3, f"{kind}: second run copies nothing ({j['files_done']} done, {j['files_skipped']} skipped)")

    r = c.get("/api/raw", params={"conn": cid, "path": "/CloudSrc/big.bin"}, headers={"Range": "bytes=1000000-1000099"})
    check(r.status_code == 206 and r.content == big[1000000:1000100], f"{kind}: reads from the middle of a file ({r.status_code})")

    r = c.post("/api/rename", headers=H, json={"conn": cid, "path": "/CloudSrc/hello.txt", "name": "hi.txt"})
    check(r.status_code == 200 and "hi.txt" in names(cid, "/CloudSrc"), f"{kind}: rename a file ({r.status_code} {r.text[:60]})")
    r = c.post("/api/rename", headers=H, json={"conn": cid, "path": "/CloudSrc/sub dir", "name": "sub2"})
    check(r.status_code == 200 and names(cid, "/CloudSrc/sub2") == ["ünï côde.txt"], f"{kind}: rename a folder ({r.status_code} {r.text[:60]})")

    j = transfer("copy", cid, ["/CloudSrc"], local, f"/CloudBack/{kind}")
    back = f"{NAS}/CloudBack/{kind}/CloudSrc"
    check(j["status"] == "done", f"{kind}: copy back down ({j['status']} {j.get('error')})")
    check(os.path.exists(f"{back}/big.bin") and sha(open(f"{back}/big.bin", "rb").read()) == sha(big), f"{kind}: 40 MB file comes back byte for byte")
    check(open(f"{back}/sub2/ünï côde.txt").read() == "nested", f"{kind}: nested file comes back")

    r = c.post("/api/delete", headers=H, json={"conn": cid, "paths": ["/CloudSrc/hi.txt"]}).json()
    check(r["results"][0].get("trashed") == "/delete_me/hi.txt", f"{kind}: delete goes to delete_me ({r['results'][0]})")
    j = transfer("move", cid, ["/CloudSrc"], local, f"/CloudBack/{kind}-moved")
    check(j["status"] == "done" and "CloudSrc" not in names(cid, "/"), f"{kind}: move down removes the source ({j['status']}, left: {names(cid, '/')})")

    s = c.get("/api/search", params={"conn": cid, "path": "/", "q": "made"}).json()
    check([e["path"] for e in s["entries"]] == ["/Made here"], f"{kind}: search finds a folder")

# search on the NAS
s = c.get("/api/search", params={"conn": local, "path": "/CloudSrc", "q": "CÔDE ünï"}).json()
check([e["path"] for e in s["entries"]] == ["/CloudSrc/sub dir/ünï côde.txt"], f"search: words in any order, any case, in subfolders ({[e['path'] for e in s['entries']]})")
check(c.get("/api/search", params={"conn": local, "path": "/", "q": " "}).status_code == 400, "search: empty query refused")

# scheduled sync: one way, changed files replace, nothing is deleted
shutil.rmtree(f"{NAS}/SyncDst", ignore_errors=True)
os.makedirs(f"{NAS}/SyncDst")
open(f"{NAS}/SyncDst/only here.txt", "w").write("keep me")
for s in c.get("/api/syncs").json()["syncs"]:
    c.delete(f"/api/syncs/{s['id']}", headers=H)
r = c.post("/api/syncs", headers=H, json={"src_conn": local, "src_dir": "/CloudSrc", "dst_conn": local, "dst_dir": "/CloudSrc/sub dir", "hours": 24})
check(r.status_code == 400, "sync: refuses a folder inside the other")
r = c.post("/api/syncs", headers=H, json={"src_conn": local, "src_dir": "/CloudSrc", "dst_conn": local, "dst_dir": "/SyncDst", "hours": 24})
check(r.status_code == 200, f"sync: saved ({r.text[:80]})")
sid = r.json()["id"]
j = wait_job(c.post(f"/api/syncs/{sid}/run", headers=H).json()["last_job"])
check(j["status"] == "done" and sorted(os.listdir(f"{NAS}/SyncDst")) == ["big.bin", "hello.txt", "only here.txt", "sub dir"],
      f"sync: contents land in the folder itself ({sorted(os.listdir(NAS + '/SyncDst'))})")
time.sleep(1.1)
open(f"{src}/hello.txt", "w").write("hello again, longer")
open(f"{src}/new.txt", "w").write("new")
os.remove(f"{src}/sub dir/ünï côde.txt")
j = wait_job(c.post(f"/api/syncs/{sid}/run", headers=H).json()["last_job"])
check(j["files_done"] == 2, f"sync: second run copies only the new and the changed file ({j['files_done']})")
check(open(f"{NAS}/SyncDst/hello.txt").read() == "hello again, longer" and not os.path.exists(f"{NAS}/SyncDst/hello (2).txt"), "sync: changed file replaced, no second copy")
check(os.path.exists(f"{NAS}/SyncDst/sub dir/ünï côde.txt") and os.path.exists(f"{NAS}/SyncDst/only here.txt"), "sync: nothing deleted at the destination")
check(c.get("/api/syncs").json()["syncs"][0]["status"] == "done", "sync: list shows the last run")
c.delete(f"/api/syncs/{sid}", headers=H)
check(c.get("/api/syncs").json()["syncs"] == [], "sync: removed")

# uploads that continue after a drop
data = os.urandom(5 << 20)
uid = sha(data)[:32]
shutil.rmtree(f"{NAS}/UpTest", ignore_errors=True)
check(c.get("/api/upload/chunk", params={"id": uid}).json()["have"] == 0, "upload: nothing staged yet")
r = c.put("/api/upload/chunk", params={"id": uid, "offset": 0}, content=data[:2 << 20], headers=H)
check(r.json()["have"] == 2 << 20, "upload: first piece stored")
r = c.put("/api/upload/chunk", params={"id": uid, "offset": 3 << 20}, content=data[3 << 20:], headers=H)
check(r.status_code == 409 and r.json()["have"] == 2 << 20, "upload: a piece from the wrong place is refused and says where to go on")
fin = {"id": uid, "conn": local, "path": "/UpTest/phone video.mp4", "size": len(data), "mtime": 1_700_000_000_000}
check(c.post("/api/upload/finish", headers=H, json=fin).status_code == 409, "upload: can't finish while pieces are missing")
c2 = httpx.Client(base_url=BASE, timeout=60, cookies=c.cookies)  # a new connection, as after a drop
have = c2.get("/api/upload/chunk", params={"id": uid}).json()["have"]
r = c2.put("/api/upload/chunk", params={"id": uid, "offset": have}, content=data[have:], headers=H)
check(r.json()["have"] == len(data), "upload: carries on from where it stopped")
r = c2.post("/api/upload/finish", headers=H, json=fin)
out = f"{NAS}/UpTest/phone video.mp4"
check(r.status_code == 200 and open(out, "rb").read() == data, f"upload: file arrives whole ({r.status_code})")
check(abs(os.stat(out).st_mtime - 1_700_000_000) < 2, "upload: keeps the file's date")
check(c.get("/api/upload/chunk", params={"id": uid}).json()["have"] == 0, "upload: staging cleaned up")
check(c.get("/api/upload/chunk", params={"id": "../../etc"}).status_code == 400, "upload: odd ids refused")

print(f"\n{len(fails)} failed" if fails else "\nall cloud checks passed")
sys.exit(1 if fails else 0)
