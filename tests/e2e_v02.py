"""Checks for the streaming engine: reruns skip, permissions, moves, resume, waiting."""
import hashlib
import os
import subprocess
import sys
import time

import httpx

B = os.environ.get("FERRY_URL", "http://127.0.0.1:8420")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
STAGE = sys.argv[2] if len(sys.argv) > 2 else "main"
H = {"X-Ferry": "1"}
c = httpx.Client(base_url=B, timeout=120)
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def job(body, timeout=180):
    jid = c.post("/api/transfer", headers=H, json=body).json()["id"]
    return wait(jid, timeout)


def wait(jid, timeout=180, statuses=("done", "error", "canceled")):
    t = time.time()
    while time.time() - t < timeout:
        j = {x["id"]: x for x in c.get("/api/jobs").json()["jobs"]}.get(jid)
        if j and j["status"] in statuses:
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


c.post("/api/login", json={"password": "testpass123"})
conns = c.get("/api/connections").json()["connections"]
nas = next(x for x in conns if x["type"] == "local")["id"]
mac = next((x for x in conns if x["name"] == "Mac v02"), None)
if not mac:
    mac = c.post("/api/connections", headers=H, json={"name": "Mac v02", "type": "sftp", "config": {
        "host": "127.0.0.1", "port": 2222, "username": "demo", "auth": "key", "trash": "/.Trash", "via": "direct"}}).json()
mac = mac["id"]

if STAGE == "main":
    # a small repo-like tree with modes git cares about
    repo = f"{ROOT}/nas/Transfer Station/repo"
    os.makedirs(f"{repo}/.git/objects/ab", exist_ok=True)
    os.makedirs(f"{repo}/.git/hooks", exist_ok=True)
    os.makedirs(f"{repo}/empty-dir", exist_ok=True)
    for i in range(40):
        p = f"{repo}/.git/objects/ab/obj{i:03d}"
        open(p, "wb").write(os.urandom(200 + i))
        os.chmod(p, 0o444)
    open(f"{repo}/run.sh", "w").write("#!/bin/sh\necho hi\n")
    os.chmod(f"{repo}/run.sh", 0o755)
    open(f"{repo}/.git/hooks/pre-commit", "w").write("#!/bin/sh\n")
    os.chmod(f"{repo}/.git/hooks/pre-commit", 0o755)
    open(f"{repo}/README.md", "w").write("hello\n")

    j = job({"kind": "copy", "src_conn": nas, "paths": ["/Transfer Station/repo"], "dst_conn": mac, "dst_dir": "/v02"})
    check(j["status"] == "done" and j["files_done"] == 43 and not j["error_count"], f"stream copy {j['status']} {j['files_done']} {j['errors'][:2]}")
    m = os.stat(f"{ROOT}/sftp/v02/repo/run.sh").st_mode & 0o777
    check(m == 0o755, f"executable bit kept ({oct(m)})")
    m = os.stat(f"{ROOT}/sftp/v02/repo/.git/objects/ab/obj005").st_mode & 0o777
    check(m == 0o444, f"read only kept ({oct(m)})")
    check(os.path.isdir(f"{ROOT}/sftp/v02/repo/empty-dir"), "empty folder copied")

    # retry the same job: nothing copied twice, no "(2)" folders
    r = c.post(f"/api/jobs/{j['id']}/retry", headers=H).json()
    j2 = wait(r["id"])
    check(j2["status"] == "done" and j2["files_done"] == 0 and j2["files_skipped"] == 43,
          f"rerun skips everything ({j2['files_done']} copied, {j2['files_skipped']} skipped)")
    check(not os.path.exists(f"{ROOT}/sftp/v02/repo (2)"), "rerun made no second folder")

    # change one file, rerun: only that one comes over (as a kept copy, nothing overwritten)
    open(f"{repo}/README.md", "w").write("hello changed\n")
    j3 = wait(c.post(f"/api/jobs/{j['id']}/retry", headers=H).json()["id"])
    check(j3["files_done"] == 1 and j3["files_skipped"] == 42, f"rerun copies only the changed file ({j3['files_done']})")
    check(open(f"{ROOT}/sftp/v02/repo/README.md").read() == "hello\n" and os.path.exists(f"{ROOT}/sftp/v02/repo/README (2).md"),
          "changed file kept both, nothing overwritten")

    # move: sources go file by file, empty folders cleaned up
    j = job({"kind": "move", "src_conn": mac, "paths": ["/v02/repo"], "dst_conn": nas, "dst_dir": "/Transfer Station/moved"})
    check(j["status"] == "done" and not os.path.exists(f"{ROOT}/sftp/v02/repo"), f"move removed source tree {j['status']} {j['errors'][:2]}")
    check(oct(os.stat(f"{ROOT}/nas/Transfer Station/moved/repo/.git/hooks/pre-commit").st_mode & 0o777) == "0o755",
          "mode survives the trip back")

    # resume: a part file with a damaged tail gets fixed, not trusted
    big = f"{ROOT}/nas/Transfer Station/big.bin"
    st = os.stat(big)
    os.makedirs(f"{ROOT}/sftp/resume", exist_ok=True)
    part = f"{ROOT}/sftp/resume/.big.bin.{st.st_size}-{int(st.st_mtime)}.ferry-part"
    data = open(big, "rb").read()
    open(part, "wb").write(data[:40 << 20][:-(3 << 20)] + b"\0" * (3 << 20))  # 40MB with a 3MB hole at the end
    j = job({"kind": "copy", "src_conn": nas, "paths": ["/Transfer Station/big.bin"], "dst_conn": mac, "dst_dir": "/resume"})
    check(j["status"] == "done" and sha(f"{ROOT}/sftp/resume/big.bin") == sha(big), "resumed file is intact")
    check(not os.path.exists(part), "part file gone after resume")

    # top level guard message on a NAS whose root isn't writable is covered in deploy checks

    # waiting: stop the SFTP server, start a copy, bring it back
    subprocess.run(["pkill", "-f", "tests/servers.py"])
    time.sleep(1)
    c.post(f"/api/connections/{mac}/test", headers=H)  # drop the cached session
    jid = c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas, "paths": ["/Media/Photos"],
                                                   "dst_conn": mac, "dst_dir": "/waited"}).json()["id"]
    w = wait(jid, 60, statuses=("waiting", "error", "done"))
    check(w["status"] == "waiting", f"job waits while the Mac is gone ({w['status']}: {w['error'] or w['current']})")
    subprocess.Popen([sys.executable, "tests/servers.py", ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    j = wait(jid, 120)
    check(j["status"] == "done" and j["files_done"] == 15, f"job finishes after the Mac comes back ({j['status']} {j['files_done']})")

elif STAGE == "restored":
    # run after tests/seed_job.py put a 'running' job in the database and Ferry restarted
    jobs = c.get("/api/jobs").json()["jobs"]
    seeded = next(j for j in jobs if j["title"] == "seeded")
    j = wait(seeded["id"], 120)
    check(j["status"] == "done" and os.path.exists(f"{ROOT}/sftp/restored/Photos/2026 Roadtrip/IMG_4100.JPG"),
          f"job left running before a restart finishes on its own ({j['status']} {j['error']})")

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
