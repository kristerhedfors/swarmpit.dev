"""Client for execd.py, run in the control VM (docs/browser-two-vm.md). Prints one line
`RESULT <json>` with numbers and booleans only.

    EXECD_KEY=<64 hex> python3 execc.py <host> <port> <uid> <gid> bench|timeout|isolation|forge [args]
"""
import hashlib
import hmac
import json
import os
import socket
import statistics
import sys
import time

KEY = bytes.fromhex(os.environ.pop("EXECD_KEY"))
HOST, PORT, UID, GID, MODE = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
ARGS = sys.argv[6:]


def prog(msg):
    if os.environ.get("EXECD_DEBUG") == "1":
        with open("/tmp/execc.prog", "a") as f:
            f.write(f"{round(time.time() % 1000, 2)} {msg}\n")


class Exec:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=120)
        self.f = self.s.makefile("rwb")
        self.seq = int(time.time() * 1000)  # strictly increasing across client restarts

    def raw(self, payload, mac=None):
        mac = mac or hmac.new(KEY, payload.encode(), hashlib.sha256).hexdigest()
        self.f.write((json.dumps({"payload": payload, "mac": mac}) + "\n").encode())
        self.f.flush()
        prog("sent")
        msg = json.loads(self.f.readline(1 << 20))
        prog("got")
        ok = hmac.compare_digest(hmac.new(KEY, msg["payload"].encode(), hashlib.sha256).hexdigest(), msg["mac"])
        return json.loads(msg["payload"]), ok

    def run(self, cmd, timeout=30, uid=UID, gid=GID, seq=None):
        if seq is None:
            self.seq += 1
            seq = self.seq
        return self.raw(json.dumps({"seq": seq, "cmd": cmd, "uid": uid, "gid": gid, "timeout": timeout}))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def main():
    t = time.time()
    e = Exec()
    connect_ms = round((time.time() - t) * 1000)
    r = {"mode": MODE, "connect_ms": connect_ms}
    if MODE == "bench":
        n = int(ARGS[0]) if ARGS else 30
        lat, srv, okc = [], [], 0
        for i in range(n):
            t = time.time()
            resp, macok = e.run(f"echo n{i}")
            lat.append((time.time() - t) * 1000)
            srv.append(resp.get("ms", -1))
            okc += macok and resp.get("exit") == 0 and resp.get("output", "").strip() == f"n{i}"
        r.update(n=n, ok=okc, min=round(min(lat)), median=round(statistics.median(lat)), p90=round(pct(lat, 0.9)),
                 max=round(max(lat)), server_median=round(statistics.median(srv)))
    elif MODE == "timeout":
        t = time.time()
        resp, _ = e.run("nohup python3 /spike/ticker.py /tmp/keep-tick >/dev/null 2>&1 & echo started")
        r["bg_started"] = resp.get("output", "").strip() == "started"
        time.sleep(1)
        t = time.time()
        resp, _ = e.run("python3 /spike/ticker.py /tmp/cmd-tick & (while :; do echo . >> /tmp/cmd-busy; done) & wait",
                        timeout=float(ARGS[0]) if ARGS else 3)
        r.update(timed_out=resp.get("timed_out"), killed=resp.get("killed"), exit=resp.get("exit"),
                 timeout_wall_ms=round((time.time() - t) * 1000))
        resp, _ = e.run("s(){ stat -c %s $1 2>/dev/null || echo 0; }; a=$(s /tmp/cmd-tick); b=$(s /tmp/cmd-busy); "
                        "k=$(s /tmp/keep-tick); sleep 2; echo $a $(s /tmp/cmd-tick) $b $(s /tmp/cmd-busy) $k $(s /tmp/keep-tick)")
        v = [int(x) for x in resp.get("output", "0 0 0 0 0 0").split()[:6]]
        r.update(cmd_children_stopped=v[0] == v[1] and v[2] == v[3] and v[1] > 0 and v[3] > 0,
                 earlier_bg_survives=v[5] > v[4])
        resp, _ = e.run("pkill -KILL -f '/tmp/keep[-]tick'; echo done")  # [-]: the pattern must not match this shell
        r["cleanup"] = resp.get("output", "").strip().endswith("done")
    elif MODE == "isolation":
        private_port, own_port = int(ARGS[0]), PORT
        probe = ("import socket,sys\nfor p in sys.argv[1:]:\n"
                 "    try:\n        socket.create_connection(('127.0.0.1', int(p)), timeout=5).close(); print('open', p)\n"
                 "    except OSError: print('refused', p)\n")
        resp, _ = e.run("id -u; test -e /root/b-secret && echo B-SECRET-VISIBLE; test -e /tmp/b-only && echo B-TMP-VISIBLE; "
                        f"python3 -c \"{probe}\" {private_port} {own_port}")
        out = resp.get("output", "")
        r.update(agent_uid=out.split("\n")[0].strip() == str(UID), b_files_visible="VISIBLE" in out,
                 b_private_port_open=f"open {private_port}" in out, execd_port_open_from_a=f"open {own_port}" in out)
        resp, _ = e.run("id -u", uid=0, gid=0)
        r["root_refused"] = resp.get("error") == "E_UID"
    elif MODE == "forge":
        resp, _ = e.raw(json.dumps({"seq": e.seq + 10, "cmd": "id", "uid": UID, "gid": GID}), mac="00" * 32)
        r["bad_mac_refused"] = resp.get("error") == "E_MAC"
        resp, _ = e.run("true")
        resp, _ = e.run("true", seq=e.seq)  # replay of the last seq
        r["replay_refused"] = resp.get("error") == "E_SEQ"
    print("RESULT " + json.dumps(r), flush=True)
    prog("result printed")
    if os.environ.get("EXECC_CLOSE", "1") == "1":
        e.f.close()
        e.s.close()
    prog("closed")


main()
