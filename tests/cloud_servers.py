"""Local WebDAV (port 8081) and S3 (port 5005) servers for the tests. Usage: cloud_servers.py <fixture dir>"""
import os
import sys
import threading

from cheroot import wsgi
from moto.server import ThreadedMotoServer
from wsgidav.wsgidav_app import WsgiDAVApp

root = sys.argv[1]
dav = os.path.join(root, "webdav")
os.makedirs(dav, exist_ok=True)
app = WsgiDAVApp({"host": "127.0.0.1", "port": 8081, "provider_mapping": {"/dav": dav}, "verbose": 0,
                  "simple_dc": {"user_mapping": {"*": {"davuser": {"password": "dav-pw"}}}},
                  "http_authenticator": {"accept_basic": True, "accept_digest": False, "default_to_digest": False}})
server = wsgi.Server(("127.0.0.1", 8081), app)
threading.Thread(target=server.start, daemon=True).start()
moto = ThreadedMotoServer(ip_address="127.0.0.1", port=5005)
moto.start()
import boto3
boto3.client("s3", endpoint_url="http://127.0.0.1:5005", region_name="us-east-1",
             aws_access_key_id="test", aws_secret_access_key="test").create_bucket(Bucket="ferry-test")
print("cloud servers up", flush=True)
threading.Event().wait()
