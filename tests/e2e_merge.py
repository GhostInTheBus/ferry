"""Merge checks: three folders on the NAS become one, duplicates go to the trash.

Run against a Ferry started on the test fixtures (see the README), after e2e.py's setup:
    python3 tests/e2e_merge.py /tmp/ferrytest
"""
import os
import shutil
import sys
import time

import httpx

BASE = os.environ.get("FERRY_URL", "http://127.0.0.1:8420")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
NAS = f"{ROOT}/nas"
H = {"X-Ferry": "1"}
c = httpx.Client(base_url=BASE, timeout=120)
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def put(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def wait_job(jid, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        j = {x["id"]: x for x in c.get("/api/jobs").json()["jobs"]}[jid]
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


c.post("/api/login", json={"password": os.environ.get("FERRY_PASSWORD", "testpass123")})
local = next(x for x in c.get("/api/connections").json()["connections"] if x["type"] == "local")["id"]

share = f"{NAS}/MergeShare"
shutil.rmtree(share, ignore_errors=True)
T = 1_700_000_000
put(f"{share}/A/a.txt", "alpha")
put(f"{share}/A/same.txt", "same", T)
put(f"{share}/A/clash.txt", "A version")
put(f"{share}/A/sub/deep.txt", "deep", T)
put(f"{share}/B/b.txt", "bravo")
put(f"{share}/B/same.txt", "same", T)                 # same size and date: duplicate
put(f"{share}/B/clash.txt", "B version")
put(f"{share}/B/sub/deep.txt", "deep", T + 5000)      # same bytes, other date: duplicate by content
put(f"{share}/B/sub/new/x.txt", "x")
put(f"{share}/C/clash.txt", "C version")
put(f"{share}/B/fresh.txt", "from B")                 # new to A, in two sources at once
put(f"{share}/C/fresh.txt", "from C!")
put(f"{share}/C/same.txt", "SAME", T)                 # same size and date, other bytes: kept
with open(f"{share}/C/big.bin", "wb") as f:
    f.write(os.urandom(3 << 20))
os.makedirs(f"{share}/C/emptydir")
big_inode = os.stat(f"{share}/C/big.bin").st_ino

r = c.post("/api/merge", headers=H, json={"conn": local, "paths": ["/MergeShare/A", "/MergeShare/A/sub"], "into": "/MergeShare/A"})
check(r.status_code == 400, "refuses to merge a folder with one inside it")
r = c.post("/api/merge", headers=H, json={"conn": local, "paths": ["/MergeShare/A/a.txt"], "into": "/MergeShare/A"})
check(r.status_code == 400, "needs at least two folders")

r = c.post("/api/merge", headers=H, json={"conn": local, "into": "/MergeShare/A",
                                           "paths": ["/MergeShare/A", "/MergeShare/B", "/MergeShare/C"]})
check(r.status_code == 200, f"merge accepted {r.status_code}")
j = wait_job(r.json()["id"])
check(j["status"] == "done", f"merge done ({j['status']} {j.get('error')}) errors={j.get('errors')}")
check(j["title"] == "Merge B and C into A", f"title: {j['title']}")

A = f"{share}/A"
names = sorted(os.listdir(A))
check({"a.txt", "b.txt", "big.bin", "same.txt", "sub", "emptydir"} <= set(names), f"everything landed in A: {names}")
clashes = sorted(n for n in names if n.startswith("clash"))
texts = sorted(open(f"{A}/{n}").read() for n in clashes)
check(texts == ["A version", "B version", "C version"], f"different files with one name all kept: {clashes}")
check(sorted(open(f"{A}/{n}").read() for n in names if n.startswith("fresh")) == ["from B", "from C!"],
      "two new files with one name both kept, neither overwrote the other")
check(open(f"{A}/same.txt").read() == "same", "A's own copy of same.txt stays")
sames = sorted(open(f"{A}/{n}").read() for n in names if n.startswith("same"))
check(sames == ["SAME", "same"], f"same size and date but other bytes is kept, not trashed: {sames}")
check(open(f"{A}/sub/deep.txt").read() == "deep" and len(os.listdir(f"{A}/sub")) == 2, f"sub merged: {os.listdir(A + '/sub')}")
check(os.path.exists(f"{A}/sub/new/x.txt"), "nested folder moved in")
check(os.stat(f"{A}/big.bin").st_ino == big_inode, "one connection: file renamed, not copied")
check(not os.path.exists(f"{share}/B") and not os.path.exists(f"{share}/C"), "emptied folders removed")
trash = f"{share}/delete_me"
check(os.path.exists(f"{trash}/B/same.txt"), "identical file went to the trash")
check(os.path.exists(f"{trash}/B/sub/deep.txt"), "same bytes with another date went to the trash")
check(j["files_skipped"] == 2, f"2 duplicates counted ({j['files_skipped']})")

print(f"\n{len(fails)} failed" if fails else "\nall merge checks passed")
sys.exit(1 if fails else 0)
