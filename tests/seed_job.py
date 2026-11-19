"""Put a half done job in the database, as if Ferry stopped mid transfer."""
import sys
from ferry import db
from ferry.transfers import Job
db.init()
conns = db.list_connections()
nas = next(c for c in conns if c["type"] == "local")["id"]
mac = next(c for c in conns if c["name"] == "Mac v02")["id"]
j = Job(kind="copy", src_conn=nas, src_paths=["/Media/Photos"], dst_conn=mac, dst_dir="/restored", title="seeded")
s = j.state(); s["status"] = "running"; s["bytes_done"] = 12345
db.save_job(j.id, s)
print("seeded", j.id)
