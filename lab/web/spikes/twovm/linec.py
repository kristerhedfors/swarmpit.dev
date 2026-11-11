"""Client for lined.py: prints `lined ok <ms> banner=<server name>` or `lined fail <ExceptionClass> <ms>`."""
import socket
import sys
import time

t = time.time()
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)
    f = s.makefile("rwb")
    name = f.readline().split()[1].decode()
    f.write(b"AUTH placeholder-pass\n"); f.flush(); f.readline()
    f.write(b"KEY vlt-000000\n"); f.flush()
    print("lined", "ok" if f.readline().strip() == b"KEY 00112233" else "bad", round((time.time() - t) * 1000), f"banner={name}")
except OSError as e:
    print("lined fail", type(e).__name__, round((time.time() - t) * 1000))
