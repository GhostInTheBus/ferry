#!/usr/bin/env python3
"""Stands in for `tailscale --socket=X nc host port` in tests: stdin/stdout <-> TCP."""
import socket, sys, threading
args = [a for a in sys.argv[1:] if not a.startswith("--socket")]
assert args[0] == "nc"
s = socket.create_connection((args[1], int(args[2])))
def up():
    while True:
        d = sys.stdin.buffer.read1(65536)
        if not d:
            s.shutdown(socket.SHUT_WR); return
        s.sendall(d)
threading.Thread(target=up, daemon=True).start()
while True:
    d = s.recv(65536)
    if not d:
        break
    sys.stdout.buffer.write(d); sys.stdout.buffer.flush()
