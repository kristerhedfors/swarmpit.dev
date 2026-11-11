"""Cross-VM exec daemon prototype (docs/browser-two-vm.md). Runs as root in the agent VM; the harness in the
control VM sends one command per request and gets its output back.

    EXECD_KEY=<64 hex> python3 execd.py <port> <allowed uid,...>

Wire: one JSON line per message, both directions, on a persistent TCP connection:
    {"payload": "<JSON string>", "mac": "<hex HMAC-SHA256(key, payload)>"}
request payload  {"seq": n, "cmd": str, "uid": int, "gid": int, "timeout": s}
response payload {"seq": n, "output": str, "exit": int, "timed_out": bool, "killed": int, "ms": int,
                  "submissions": [new lines of /var/tmp/submissions]}
seq must increase strictly (no replay). A timed-out command is stopped by walking /proc: every process of the
command's uid that did not exist before the command started is sent SIGKILL, in rounds, until a round finds
no new one (os.killpg fails in CheerpX 1.3.9: no process groups, and /proc has no session or parent ids).
Stdlib only.
"""
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

KEY = bytes.fromhex(os.environ.pop("EXECD_KEY"))
PORT = int(sys.argv[1])
ALLOWED = {int(u) for u in sys.argv[2].split(",")}
OUT_CAP = 64 * 1024
SUBS = "/var/tmp/submissions"
DEBUG = os.environ.pop("EXECD_DEBUG", "") == "1"
lock = threading.Lock()


def dbg(*a):
    if DEBUG:
        print("dbg", round(time.time() % 1000, 2), *a, flush=True)
state = {"seq": 0, "subs_off": 0}


def mac(payload):
    return hmac.new(KEY, payload.encode(), hashlib.sha256).hexdigest()


def uid_pids(uid):
    pids = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            owner = os.stat(f"/proc/{name}").st_uid
        except OSError:
            continue
        if owner == uid:
            pids.add(int(name))
    return pids


def kill_new(uid, before, first):
    killed = set()
    for _ in range(10):
        new = (uid_pids(uid) | {first}) - before - killed
        if not new:
            break
        for pid in new:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed.add(pid)
        time.sleep(0.2)
    return len(killed)


def new_submissions():
    try:
        with open(SUBS, "rb") as f:
            f.seek(state["subs_off"])
            data = f.read(OUT_CAP)
    except OSError:
        return []
    end = data.rfind(b"\n") + 1
    state["subs_off"] += end
    return data[:end].decode(errors="replace").splitlines()


def run(req):
    uid, gid, timeout = int(req["uid"]), int(req["gid"]), float(req.get("timeout", 30))
    if uid not in ALLOWED:
        return {"error": "E_UID"}
    env = {"HOME": "/home/agent", "USER": "agent", "LOGNAME": "agent", "SHELL": "/bin/bash", "TERM": "dumb",
           "LANG": "C.UTF-8", "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"}
    t0 = time.time()
    dbg("run start")
    before = uid_pids(uid)
    dbg("pids", len(before))
    killed = 0
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(["/bin/bash", "-c", req["cmd"]], user=uid, group=gid, extra_groups=[],
                             cwd="/home/agent" if os.path.isdir("/home/agent") else "/", env=env,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        dbg("spawned")
        try:
            code, timed_out = p.wait(timeout=timeout), False
            dbg("exited", code)
        except subprocess.TimeoutExpired:
            killed = kill_new(uid, before, p.pid)
            for _ in range(50):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            code, timed_out = -9, True
        out.seek(0)
        text = out.read(OUT_CAP).decode(errors="replace")
    return {"output": text, "exit": code, "timed_out": timed_out, "killed": killed,
            "ms": round((time.time() - t0) * 1000), "submissions": new_submissions()}


def serve(conn):
    dbg("conn")
    f = conn.makefile("rwb")
    for line in f:
        dbg("line", len(line))
        req = {}
        try:
            msg = json.loads(line)
            payload = msg["payload"]
            if not hmac.compare_digest(mac(payload), str(msg.get("mac", ""))):
                resp = {"error": "E_MAC"}
            else:
                req = json.loads(payload)
                with lock:
                    if not isinstance(req.get("seq"), int) or req["seq"] <= state["seq"]:
                        resp = {"error": "E_SEQ"}
                    else:
                        state["seq"] = req["seq"]
                        resp = run(req)
                resp["seq"] = req.get("seq")
        except Exception as e:  # noqa: BLE001 - report the class only
            resp = {"error": "E_REQ", "detail": type(e).__name__}
        body = json.dumps(resp)
        f.write((json.dumps({"payload": body, "mac": mac(body)}) + "\n").encode())
        f.flush()
        dbg("replied")
    dbg("eof")
    # close explicitly: a CheerpX client's close() waits until this side has closed too
    f.close()
    conn.close()


def main():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PORT))
    s.listen(4)
    print("execd listening", PORT, flush=True)
    while True:
        conn, _ = s.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


main()
