"""Appends a byte to argv[1] every 0.2 s, forever (one process; never exits on its own, so killing it never
hits CheerpX 1.3.9's exited-orphan SIGKILL hang, S2k)."""
import sys
import time

while True:
    with open(sys.argv[1], "a") as f:
        f.write(".")
    time.sleep(0.2)
