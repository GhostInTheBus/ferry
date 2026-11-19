"""End to end checks against a running Ferry plus tests/servers.py."""
import hashlib
import io
import os
import sys
import time
import zipfile

import httpx

BASE = os.environ.get("FERRY_URL", "http://127.0.0.1:8420")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
H = {"X-Ferry": "1"}
c = httpx.Client(base_url=BASE, timeout=120)
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def wait_job(jid, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        jobs = {j["id"]: j for j in c.get("/api/jobs").json()["jobs"]}
        j = jobs[jid]
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


# auth
r = c.get("/api/list", params={"conn": "x", "path": "/"})
check(r.status_code == 401, "unauthenticated list is rejected")
r = c.post("/api/login", json={"password": "nope"})
check(r.status_code == 401, "wrong password rejected")
r = c.post("/api/login", json={"password": "testpass123"})
check(r.status_code == 200, "login works")
r = c.post("/api/mkdir", json={"conn": "x", "path": "/a"})
check(r.status_code == 403, "POST without X-Ferry header is blocked (CSRF guard)")

conns = c.get("/api/connections").json()["connections"]
nas = next(x for x in conns if x["type"] == "local")
check(nas["config"]["trash"] == "@synology", "seeded NAS connection uses #recycle")

key = c.get("/api/ssh-key").json()
check(key["public_key"].startswith("ssh-ed25519"), "ferry ssh key exists")

mac = c.post("/api/connections", headers=H, json={"name": "MacBook", "type": "sftp", "config": {
    "host": "127.0.0.1", "port": 2222, "username": "demo", "auth": "key", "root": "/", "trash": "/.Trash"}}).json()
t = c.post(f"/api/connections/{mac['id']}/test", headers=H).json()
check(t.get("ok"), f"sftp key login test: {t}")
macpw = c.post("/api/connections/test", headers=H, json={"name": "pw", "type": "sftp", "config": {
    "host": "127.0.0.1", "port": 2222, "username": "demo", "auth": "password", "password": "sftp-pw"}}).json()
check(macpw.get("ok"), f"sftp password login test: {macpw}")
bad = c.post("/api/connections/test", headers=H, json={"name": "pw", "type": "sftp", "config": {
    "host": "127.0.0.1", "port": 2222, "username": "demo", "auth": "password", "password": "wrong"}}).json()
check(not bad.get("ok") and "rejected" in bad.get("error", ""), f"bad sftp password gives a clear error: {bad.get('error')}")
ftp = c.post("/api/connections", headers=H, json={"name": "FTP box", "type": "ftp", "config": {
    "host": "127.0.0.1", "port": 2121, "username": "ftpuser", "password": "ftp-pw"}}).json()
t = c.post(f"/api/connections/{ftp['id']}/test", headers=H).json()
check(t.get("ok"), f"ftp test: {t}")
listed = c.get("/api/connections").json()["connections"]
check(all("password" not in x["config"] for x in listed), "passwords never come back from the API")
check(next(x for x in listed if x["id"] == ftp["id"])["config"]["has_password"], "has_password flag set")

# browse
r = c.get("/api/list", params={"conn": nas["id"], "path": "/Media/Photos/2026 Roadtrip"}).json()
names = {e["name"]: e for e in r["entries"]}
check(len(names) == 15, f"photo folder lists 15 files ({len(names)})")
check(names["IMG_5001.HEIC"].get("live") == "IMG_5001.MOV", "Live Photo pair detected")
check(names["IMG_5001.MOV"].get("live_of") == "IMG_5001.HEIC", "Live Photo video marked")
check(names["IMG_4100.JPG"]["kind"] == "image", "kind=image")
r = c.get("/api/list", params={"conn": nas["id"], "path": "/../../etc"})
check(r.status_code in (200, 404) and "passwd" not in r.text, "path traversal stays inside root")
r = c.get("/api/list", params={"conn": mac["id"], "path": "/Documents"}).json()
check(r["entries"][0]["name"] == "résumé – final.txt", "unicode names over sftp")

# thumbnails
r = c.get("/api/thumb", params={"conn": nas["id"], "path": "/Media/Photos/2026 Roadtrip/IMG_4100.JPG"})
check(r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and len(r.content) > 1000, "jpeg thumbnail")
r = c.get("/api/thumb", params={"conn": nas["id"], "path": "/Media/Photos/2026 Roadtrip/IMG_5001.HEIC", "s": 2048})
check(r.status_code == 200 and len(r.content) > 1000, "heic preview renders to jpeg")
r = c.get("/api/thumb", params={"conn": mac["id"], "path": "/Pictures/mac_0.jpg"})
check(r.status_code == 200, "thumbnail over sftp")

# raw with ranges
p = "/Media/Movies/Big Buck Test/Big Buck Test (2026).mp4"
full = open(f"{ROOT}/nas{p}", "rb").read()
r = c.get("/api/raw", params={"conn": nas["id"], "path": p}, headers={"Range": "bytes=100-199"})
check(r.status_code == 206 and r.content == full[100:200], "local range request")
c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"], "paths": ["/Media/Movies"],
                                          "dst_conn": mac["id"], "dst_dir": "/"})
time.sleep(2)
r = c.get("/api/raw", params={"conn": mac["id"], "path": "/Movies/Big Buck Test/Big Buck Test (2026).mp4"},
          headers={"Range": "bytes=5000-"})
check(r.status_code == 206 and r.content == full[5000:], "sftp open ended range")
r = c.get("/api/raw", params={"conn": mac["id"], "path": "/Movies/Big Buck Test/Big Buck Test (2026).mp4"},
          headers={"Range": "bytes=-500"})
check(r.status_code == 206 and r.content == full[-500:], "sftp suffix range")

# subtitles
r = c.get("/api/subs", params={"conn": nas["id"], "path": p}).json()["subtitles"]
check([s["label"] for s in r] == ["en", "es"], f"subtitle labels {[s['label'] for s in r]}")
vtt = c.get("/api/subtitle", params={"conn": nas["id"], "path": r[0]["path"]}).text
check(vtt.startswith("WEBVTT") and "00:00:01.000 --> 00:00:04.000" in vtt, "srt -> vtt")
vtt = c.get("/api/subtitle", params={"conn": nas["id"], "path": r[1]["path"]}).text
check("00:00:02.000 --> 00:00:06.500" in vtt and "Hola desde Ferry\nsegunda linea" in vtt, "ass -> vtt")

# big transfer NAS -> SFTP
j = c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                              "paths": ["/Transfer Station/big.bin", "/Media/Photos"],
                                              "dst_conn": mac["id"], "dst_dir": "/Inbox"}).json()
j = wait_job(j["id"])
check(j["status"] == "done" and j["files_done"] == 16, f"copy NAS->Mac {j['status']} {j['files_done']} {j['error']}")
check(sha(f"{ROOT}/sftp/Inbox/big.bin") == sha(f"{ROOT}/nas/Transfer Station/big.bin"), "64MB file intact over sftp")
src_m = os.stat(f"{ROOT}/nas/Media/Photos/2026 Roadtrip/IMG_4100.JPG").st_mtime
dst_m = os.stat(f"{ROOT}/sftp/Inbox/Photos/2026 Roadtrip/IMG_4100.JPG").st_mtime
check(abs(src_m - dst_m) < 2, "modified date preserved")
check(not [f for f in os.listdir(f"{ROOT}/sftp/Inbox") if f.endswith('.ferry-part')], "no part files left")

# conflict: rename
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                                       "paths": ["/Transfer Station/notes.md"], "dst_conn": mac["id"],
                                                       "dst_dir": "/Inbox"}).json()["id"])
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                                       "paths": ["/Transfer Station/notes.md"], "dst_conn": mac["id"],
                                                       "dst_dir": "/Inbox"}).json()["id"])
check(os.path.exists(f"{ROOT}/sftp/Inbox/notes (2).md"), "rename on conflict -> notes (2).md")
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                                       "paths": ["/Transfer Station/notes.md"], "dst_conn": mac["id"],
                                                       "dst_dir": "/Inbox", "conflict": "skip"}).json()["id"])
check(j["files_skipped"] == 1 and not os.path.exists(f"{ROOT}/sftp/Inbox/notes (3).md"), "skip on conflict")

# move SFTP -> FTP
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "move", "src_conn": mac["id"],
                                                       "paths": ["/Inbox/Photos"], "dst_conn": ftp["id"],
                                                       "dst_dir": "/incoming"}).json()["id"])
check(j["status"] == "done", f"move Mac->FTP {j['status']} {j['error']} {j['errors'][:2]}")
check(os.path.exists(f"{ROOT}/ftp/incoming/Photos/2026 Roadtrip/IMG_5001.HEIC"), "files arrived on FTP")
check(not os.path.exists(f"{ROOT}/sftp/Inbox/Photos"), "source removed after move")
r = c.get("/api/list", params={"conn": ftp["id"], "path": "/incoming/Photos/2026 Roadtrip"}).json()
check(len(r["entries"]) == 15, f"ftp listing ({len(r['entries'])})")
r = c.get("/api/raw", params={"conn": ftp["id"], "path": "/incoming/Photos/2026 Roadtrip/IMG_4101.JPG"},
          headers={"Range": "bytes=10-29"})
orig = open(f"{ROOT}/nas/Media/Photos/2026 Roadtrip/IMG_4101.JPG", "rb").read()
check(r.status_code == 206 and r.content == orig[10:30], "ftp range read")

# same connection move = rename
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "move", "src_conn": mac["id"],
                                                       "paths": ["/Inbox/notes (2).md"], "dst_conn": mac["id"],
                                                       "dst_dir": "/Documents"}).json()["id"])
check(os.path.exists(f"{ROOT}/sftp/Documents/notes (2).md"), "move within sftp")
r = c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"], "paths": ["/Media"],
                                              "dst_conn": nas["id"], "dst_dir": "/Media/Photos"}).json()
j = wait_job(r["id"])
check(j["status"] == "error" and "inside itself" in j["error"], "can't copy a folder into itself")

# archives
r = c.get("/api/archive/list", params={"conn": nas["id"], "path": "/Media/Archives/Scans.zip"}).json()
paths = [e["path"] for e in r["entries"]]
check("Scans/deep/page2.txt" in paths and "Scans/deep" in paths, "zip listing with folders")
j = wait_job(c.post("/api/archive/extract", headers=H, json={
    "conn": nas["id"], "path": "/Media/Archives/Scans.zip", "dst_conn": mac["id"], "dst_dir": "/Extracted",
    "members": ["Scans/deep"]}).json()["id"])
check(j["status"] == "done" and open(f"{ROOT}/sftp/Extracted/Scans/Scans/deep/page2.txt").read() == "page two",
      f"extract selected zip folder to sftp {j['status']} {j['error']}")
for rar in ("rar5-solid.rar", "rar3-solid.rar"):
    r = c.get("/api/archive/list", params={"conn": nas["id"], "path": f"/Media/Archives/{rar}"})
    ok = r.status_code == 200 and len(r.json()["entries"]) > 0
    check(ok, f"list {rar}: {r.status_code} {r.text[:120]}")
    j = wait_job(c.post("/api/archive/extract", headers=H, json={
        "conn": nas["id"], "path": f"/Media/Archives/{rar}", "dst_conn": nas["id"],
        "dst_dir": "/Transfer Station"}).json()["id"])
    check(j["status"] == "done", f"extract {rar}: {j['status']} {j['error']}")
r = c.get("/api/archive/list", params={"conn": nas["id"], "path": "/Media/Archives/rar5-hpsw.rar"})
check(r.status_code == 500 and "password" in r.text.lower(), f"encrypted rar asks for password: {r.text[:100]}")
r = c.get("/api/archive/list", params={"conn": nas["id"], "path": "/Media/Archives/rar5-hpsw.rar",
                                        "password": "password"})
check(r.status_code == 200, f"encrypted rar with password: {r.text[:100]}")
# remote archive (download then list)
wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                                   "paths": ["/Media/Archives/Scans.zip"], "dst_conn": ftp["id"],
                                                   "dst_dir": "/"}).json()["id"])
r = c.get("/api/archive/list", params={"conn": ftp["id"], "path": "/Scans.zip"})
check(r.status_code == 200 and len(r.json()["entries"]) == 5, "archive over ftp lists")

# upload with mtime
data = os.urandom(3 << 20)
r = c.put("/api/upload", headers=H, params={"conn": mac["id"], "path": "/Uploads/from phone/clip.bin",
                                            "mtime": 1700000000000}, content=data)
check(r.status_code == 200 and open(f"{ROOT}/sftp/Uploads/from phone/clip.bin", "rb").read() == data, "upload to sftp")
check(abs(os.stat(f"{ROOT}/sftp/Uploads/from phone/clip.bin").st_mtime - 1700000000) < 2, "upload keeps phone date")
r = c.put("/api/upload", headers=H, params={"conn": mac["id"], "path": "/Uploads/from phone/clip.bin"}, content=b"x")
check(r.json()["path"].endswith("clip (2).bin"), "upload renames on conflict")

# zip download of a folder from sftp
r = c.get("/api/zip", params={"conn": mac["id"], "path": "/Extracted"})
z = zipfile.ZipFile(io.BytesIO(r.content))
check(z.testzip() is None and "Extracted/Scans/Scans/deep/page2.txt" in z.namelist(), f"zip stream valid {z.namelist()}")

# rename + mkdir + delete to trash
r = c.post("/api/mkdir", headers=H, json={"conn": mac["id"], "path": "/Uploads/New Folder"})
check(r.status_code == 200, "mkdir")
r = c.post("/api/mkdir", headers=H, json={"conn": mac["id"], "path": "/Uploads/New Folder"})
check(r.status_code == 409, "mkdir clash is 409")
r = c.post("/api/rename", headers=H, json={"conn": mac["id"], "path": "/Uploads/New Folder", "name": "Renamed"})
check(r.status_code == 200 and os.path.isdir(f"{ROOT}/sftp/Uploads/Renamed"), "rename")
r = c.post("/api/delete", headers=H, json={"conn": mac["id"], "paths": ["/Uploads/Renamed"]}).json()
check(os.path.isdir(f"{ROOT}/sftp/.Trash/Renamed"), f"delete moves to Mac trash {r}")
r = c.post("/api/delete", headers=H, json={"conn": nas["id"], "paths": ["/Media/Archives/Scans.zip"]}).json()
check(os.path.exists(f"{ROOT}/nas/Media/#recycle/Archives/Scans.zip"), f"NAS delete goes to share #recycle {r}")
r = c.post("/api/delete", headers=H, json={"conn": nas["id"], "paths": ["/Media"]}).json()
check("error" in r["results"][0] and os.path.isdir(f"{ROOT}/nas/Media"), "won't delete a whole share")

# pause / resume / cancel (pause right away so it is still queued)
j = c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                              "paths": ["/Transfer Station/big.bin"], "dst_conn": ftp["id"],
                                              "dst_dir": "/cancelme"}).json()
c.post(f"/api/jobs/{j['id']}/pause", headers=H)
time.sleep(1)
s = {x["id"]: x for x in c.get("/api/jobs").json()["jobs"]}[j["id"]]
check(s["status"] == "paused" and s["bytes_done"] == 0, f"pause holds the job ({s['status']})")
c.post(f"/api/jobs/{j['id']}/resume", headers=H)
j = wait_job(j["id"])
check(j["status"] == "done", f"resume finishes it ({j['status']})")
j = c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
                                              "paths": ["/Transfer Station/big.bin"], "dst_conn": ftp["id"],
                                              "dst_dir": "/cancelme2"}).json()
c.post(f"/api/jobs/{j['id']}/pause", headers=H)
c.post(f"/api/jobs/{j['id']}/cancel", headers=H)
j = wait_job(j["id"])
check(j["status"] == "canceled", f"cancel works ({j['status']})")
left = os.listdir(f"{ROOT}/ftp/cancelme2") if os.path.exists(f"{ROOT}/ftp/cancelme2") else []
check(not left, f"cancel leaves nothing behind {left}")

# sse
with c.stream("GET", "/api/events") as s:
    got = False
    for line in s.iter_lines():
        if line.startswith("data:"):
            got = True
            break
check(got, "SSE stream sends job snapshot")

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
