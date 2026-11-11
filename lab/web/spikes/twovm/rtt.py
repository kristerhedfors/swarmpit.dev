"""Echo round trips over loopback: `rtt.py serve <port>` or `rtt.py client <host> <port> <n>`; the client
prints `rtt median=<ms> p90=<ms> max=<ms>`. `rtt.py cpu <n>` prints the time of a fixed pure-Python loop."""
import socket
import statistics
import sys
import time

if sys.argv[1] == "serve":
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(sys.argv[2]))); s.listen(4)
    while True:
        c, _ = s.accept()
        while True:
            b = c.recv(4096)
            if not b:
                break
            c.sendall(b)
        c.close()
elif sys.argv[1] == "client":
    c = socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=30)
    xs = []
    for _ in range(int(sys.argv[4])):
        t = time.time(); c.sendall(b"x" * 64); c.recv(4096); xs.append((time.time() - t) * 1000)
    xs.sort()
    print(f"rtt median={statistics.median(xs):.1f} p90={xs[int(0.9 * (len(xs) - 1))]:.1f} max={xs[-1]:.1f}")
else:
    t = time.time(); x = 0
    for i in range(int(sys.argv[2])):
        x += i * i
    print(f"cpu ms={(time.time() - t) * 1000:.0f}")
