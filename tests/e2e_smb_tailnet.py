"""SMB backend plus routing through `tailscale nc` (faked by tests/fake_tailscale.py)."""
import hashlib, os, sys, time
import httpx
B = os.environ.get("FERRY_URL", "http://127.0.0.1:8420")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
SMB_DIR = "/tmp/smbshare"
H = {"X-Ferry": "1"}
c = httpx.Client(base_url=B, timeout=120)
fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)
def wait_job(jid):
    for _ in range(600):
        j = {x["id"]: x for x in c.get("/api/jobs").json()["jobs"]}[jid]
        if j["status"] in ("done", "error", "canceled"): return j
        time.sleep(0.3)
c.post("/api/login", json={"password": "testpass123"})
nas = next(x for x in c.get("/api/connections").json()["connections"] if x["type"] == "local")
check(c.get("/api/ssh-key").json()["tailscale"], "tailscale reported available")
for via in ("direct", "tailscale"):
    smb = c.post("/api/connections", headers=H, json={"name": f"SMB {via}", "type": "smb", "config": {
        "host": "127.0.0.1", "port": 4445, "share": "Media", "username": "smbuser", "password": "smb-pw", "via": via}}).json()
    t = c.post(f"/api/connections/{smb['id']}/test", headers=H).json()
    check(t.get("ok"), f"smb {via} test {t}")
    r = c.get("/api/list", params={"conn": smb["id"], "path": "/"}).json()
    check(any(e["name"] == "readme.txt" for e in r["entries"]), f"smb {via} list")
    j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
        "paths": ["/Transfer Station/big.bin", "/Media/Photos"], "dst_conn": smb["id"], "dst_dir": f"/in-{via}"}).json()["id"])
    check(j["status"] == "done" and not j["error_count"], f"copy NAS->SMB {via}: {j['status']} {j['errors'][:2]}")
    a = hashlib.sha256(open(f"{ROOT}/nas/Transfer Station/big.bin", "rb").read()).hexdigest()
    b = hashlib.sha256(open(f"{SMB_DIR}/in-{via}/big.bin", "rb").read()).hexdigest()
    check(a == b, f"64MB intact over smb {via}")
    m1 = os.stat(f"{ROOT}/nas/Media/Photos/2026 Roadtrip/IMG_4100.JPG").st_mtime
    m2 = os.stat(f"{SMB_DIR}/in-{via}/Photos/2026 Roadtrip/IMG_4100.JPG").st_mtime
    check(abs(m1 - m2) < 2, f"smb {via} keeps dates")
    r = c.get("/api/raw", params={"conn": smb["id"], "path": f"/in-{via}/big.bin"}, headers={"Range": "bytes=1000-1999"})
    check(r.status_code == 206 and r.content == open(f"{ROOT}/nas/Transfer Station/big.bin", "rb").read()[1000:2000], f"smb {via} range")
    r = c.post("/api/rename", headers=H, json={"conn": smb["id"], "path": f"/in-{via}/big.bin", "name": "big2.bin"})
    check(r.status_code == 200 and os.path.exists(f"{SMB_DIR}/in-{via}/big2.bin"), f"smb {via} rename")
    r = c.post("/api/delete", headers=H, json={"conn": smb["id"], "paths": [f"/in-{via}/big2.bin"]}).json()
    check(os.path.exists(f"{SMB_DIR}/delete_me/big2.bin") or os.path.exists(f"{SMB_DIR}/delete_me/big2 (2).bin"), f"smb {via} delete to delete_me {r}")
    j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "move", "src_conn": smb["id"],
        "paths": [f"/in-{via}/Photos"], "dst_conn": nas["id"], "dst_dir": f"/Transfer Station/back-{via}"}).json()["id"])
    check(j["status"] == "done" and os.path.exists(f"{ROOT}/nas/Transfer Station/back-{via}/Photos/2026 Roadtrip/IMG_5001.MOV")
          and not os.path.exists(f"{SMB_DIR}/in-{via}/Photos"), f"move SMB->NAS {via}: {j['status']} {j['error']}")
mac = c.post("/api/connections", headers=H, json={"name": "Mac via TS", "type": "sftp", "config": {
    "host": "127.0.0.1", "port": 2222, "username": "demo", "auth": "key", "via": "tailscale"}}).json()
t = c.post(f"/api/connections/{mac['id']}/test", headers=H).json()
check(t.get("ok"), f"sftp through tailscale nc {t}")
j = wait_job(c.post("/api/transfer", headers=H, json={"kind": "copy", "src_conn": nas["id"],
    "paths": ["/Transfer Station/big.bin"], "dst_conn": mac["id"], "dst_dir": "/ts-in"}).json()["id"])
check(j["status"] == "done", f"copy over tailscale proxied sftp {j['status']} {j['error']}")
print(f"\n{len(fails)} failures"); sys.exit(1 if fails else 0)
