"""Read-only live tap of running microsandbox VMs (CONTRACT section 3.4).

Allowed host commands: `msb ls`, `msb ps`, `msb exec`. Inside the guest only `sh`, `wc`, `tail`, `head`, `test`
run (enforced below). Never create/stop/rm/cp/--replace, never write in the guest, never signal orchestrate.
Degrades to a no-op when msb is missing or nothing is running.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .ingest import LiveChunk
from .parse import VM_RE, vm_run_candidates

log = logging.getLogger("obs.livetap")

GUEST_CMDS = ("sh", "wc", "tail", "head", "test")
assert set(GUEST_CMDS) <= {"sh", "wc", "tail", "head", "test"}
HOST_SUBCOMMANDS = ("ls", "ps", "exec")
MAX_BYTES = 262144
EXEC_TIMEOUT_S = 15
MISS_LIMIT = 2

# Observer-effect hardening (CONTRACT 3.4): the guest process list is readable by the unprivileged agent user,
# so nothing in any argv names the tap or the log paths. The script and its arguments travel in env vars
# (/proc/<pid>/environ is owner-only), the guest argv is just `sh -c 'eval "$S"'`, and files are read through
# redirections, so the child processes show up as plain `wc -c`, `tail -c +N`, `head -c N`.
# $A = "<max bytes per file> <file> <offset> [<file> <offset> ...]". Output per file:
#   @@OBS <file> <size|-1> <offset> <n>\n<n raw bytes>\n
# then @@DONE if /root/logs/DONE exists, then @@END. Uses only sh builtins + wc/tail/head/test. One line, so it
# survives any env transport.
FRAMED_SCRIPT = (
    'set -- $A; max=$1; shift; '
    'while test $# -ge 2; do f=$1; off=$2; shift 2; '
    'if test -f "$f"; then sz=$(wc -c < "$f"); sz=$((sz + 0)); n=$((sz - off)); '
    'if test $n -lt 0; then n=0; fi; if test $n -gt $max; then n=$max; fi; '
    'echo "@@OBS $f $sz $off $n"; '
    'if test $n -gt 0; then tail -c +$((off + 1)) < "$f" | head -c $n; fi; echo; '
    'else echo "@@OBS $f -1 $off 0"; echo; fi; done; '
    'if test -f /root/logs/DONE; then echo "@@DONE"; fi; echo "@@END"'
)
GUEST_ARGV = ["sh", "-c", 'eval "$S"']

AGENT_FILES = [("/root/logs/steps.jsonl", "steps"), ("/root/logs/relay.jsonl", "relay"),
               ("/var/tmp/submissions", "subm")]
BOARD_FILES = [("/root/logs/events.jsonl", "board"), ("/root/logs/board.out", "diag:board.out")]
SUMMARY = ("/root/logs/summary.json", "summary")


def _msb_argv(msb: str, sub: str, *rest: str) -> list[str]:
    assert sub in HOST_SUBCOMMANDS, sub
    forbidden = {"create", "stop", "rm", "remove", "cp", "copy", "--replace", "restart", "kill"}
    assert not (set(rest[:1]) & forbidden)
    return [msb, sub, *rest]


def exec_env(files: list[tuple[str, int]], max_bytes: int = MAX_BYTES) -> dict[str, str]:
    args = [str(int(max_bytes))]
    for f, off in files:
        args += [f, str(int(off))]
    return {"S": FRAMED_SCRIPT, "A": " ".join(args)}


def exec_argv(msb: str, vm: str, files: list[tuple[str, int]], max_bytes: int = MAX_BYTES) -> list[str]:
    env = exec_env(files, max_bytes)
    argv = _msb_argv(msb, "exec", "--no-stdin", "-q", "--timeout", "10s", "-e", f"S={env['S']}",
                     "-e", f"A={env['A']}", vm, "--", *GUEST_ARGV)
    assert argv[argv.index("--") + 1] in GUEST_CMDS
    return argv


def parse_framed(out: bytes) -> tuple[list[dict], bool, bool]:
    """Returns ([{file,size,off,data}], done, complete)."""
    res, pos, done, complete = [], 0, False, False
    n_out = len(out)
    while pos < n_out:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        line = out[pos:nl]
        pos = nl + 1
        if line.startswith(b"@@OBS "):
            parts = line.decode("utf-8", "replace").split()
            try:
                f, sz, off, n = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
            except (IndexError, ValueError):
                break
            data = out[pos:pos + n]
            if len(data) < n:
                break
            pos += n
            if out[pos:pos + 1] == b"\n":
                pos += 1
            res.append({"file": f, "size": sz, "off": off, "data": data})
        elif line == b"@@DONE":
            done = True
        elif line == b"@@END":
            complete = True
            break
    return res, done, complete


class LiveTap:
    def __init__(self, cfg, submit, offsets_fn, run_done_fn=None):
        self.cfg = cfg
        self.submit = submit  # callable(item) -> worker queue
        self.offsets_fn = offsets_fn  # (run_id, vm) -> {guest_path: (byte_off, epoch)}
        self.run_done_fn = run_done_fn or (lambda rid: (cfg.runs_dir / rid / "run.json").exists())
        self.enabled = bool(cfg.live_tap and cfg.msb and Path(cfg.msb).exists())
        self.vms: dict[str, dict] = {}  # name -> {run, role, agent_id, created_at}
        self.last_discover_ts: float | None = None
        self.errors = 0
        self.backoff: dict[str, tuple[float, float]] = {}  # vm -> (next_ok_ts, delay)
        self.summary_seen: set[str] = set()
        self.misses: dict[str, int] = {}
        self.stop_ev = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="obs-tap")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._vm_run: dict[tuple[str, str], str] = {}  # (vm name, created_at) -> run id it was resolved to

    def status(self) -> dict:
        return {"enabled": self.enabled, "msb": self.cfg.msb, "live_vms": len(self.vms),
                "last_discover_ts": self.last_discover_ts, "errors": self.errors}

    # ------------------------------------------------------------------ discovery
    def resolve_run(self, vm_run: str) -> str:
        """Run id of a VM named after `vm_run`. VM names leave the goal segment out (lab config.vm_run_id), so
        'sb-forum-multi4-r3-a0' may belong to forum-multi4-r3 or forum-multi4-r3-unsolvable: the orchestrator
        creates runs/<run_id>/ before the VMs, so pick the candidate dir that exists without a run.json (the
        newest when several do). With none live, a VM outliving its run (failed `msb rm`) goes to the newest
        finished candidate dir, so run_done_fn skips it instead of inventing a goal-less run. With no candidate
        dir at all the VM id is the run id, as before goals existed."""
        cands = vm_run_candidates(vm_run)
        if len(cands) == 1:
            return vm_run
        live, done = [], []
        for c in cands:
            d = self.cfg.runs_dir / c
            try:
                if not d.is_dir():
                    continue
                age = (max([d.stat().st_mtime] + [q.stat().st_mtime for q in d.glob("cfg-a*.json")]), c)
                (done if (d / "run.json").exists() else live).append(age)
            except OSError:
                continue
        if live:
            return max(live)[1]
        return max(done)[1] if done else vm_run

    def discover(self) -> dict[str, set[str]] | None:
        argv = _msb_argv(self.cfg.msb, "ls", "--running", "--format", "json")
        try:
            r = subprocess.run(argv, capture_output=True, timeout=EXEC_TIMEOUT_S)
            items = json.loads(r.stdout or b"[]") if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            items = None
        self.last_discover_ts = time.time()
        if items is None:  # failed listing: keep the previous view (never declare VMs gone on a transient error)
            self.errors += 1
            return None
        vms, by_run = {}, {}
        present, present_keys = set(), set()
        for it in items if isinstance(items, list) else []:
            name = it.get("name") or ""
            if str(it.get("status", "Running")).lower() != "running":
                continue
            m = VM_RE.match(name)
            if not m:
                continue
            present.add(name)
            key = (name, str(it.get("created_at") or ""))
            present_keys.add(key)
            rid = self._vm_run.get(key)
            if rid is None:  # resolved once per VM instance: a finished run's run.json must not re-route it
                rid = self._vm_run[key] = self.resolve_run(m["run"])
            if self.run_done_fn(rid):
                continue  # never exec against a VM whose run already has run.json
            vms[name] = {"run": rid, "role": "board" if m["role"] == "board" else "agent",
                         "agent_id": -1 if m["role"] == "board" else int(m["idx"]),
                         "created_at": str(it.get("created_at") or "")}
            by_run.setdefault(rid, set()).add(name)
        # a VM is only gone after MISS_LIMIT consecutive successful listings without it
        for name, info in self.vms.items():
            if name in vms:
                continue
            miss = self.misses.get(name, 0) + 1
            if miss < MISS_LIMIT and not self.run_done_fn(info["run"]):
                self.misses[name] = miss
                vms[name] = info
                by_run.setdefault(info["run"], set()).add(name)
        for name in list(self.misses):
            if name not in vms or name in present:
                self.misses.pop(name, None)
        # keep a VM instance's resolved run while msb still lists it (done runs included: dropping a done VM's
        # entry would re-resolve it next cycle, possibly to another run); carried-over misses keep theirs too
        for key in [k for k in self._vm_run if k not in present_keys and k[0] not in vms]:
            del self._vm_run[key]
        self.vms = vms
        return by_run

    # ------------------------------------------------------------------ tail
    def tail_vm(self, name: str, dry: bool = False) -> list[LiveChunk]:
        info = self.vms.get(name)
        if not info or self.run_done_fn(info["run"]):
            return []
        offs = self.offsets_fn(info["run"], name)
        files = AGENT_FILES if info["role"] == "agent" else BOARD_FILES
        req = []
        for path, stream in files:
            off, ep = offs.get(path, (0, None))
            if ep is not None and ep != info["created_at"]:
                off = 0
            req.append((path, off, stream))
        if info["role"] == "agent" and name in self.summary_seen and SUMMARY[0] not in offs:
            req.append((SUMMARY[0], 0, SUMMARY[1]))
        argv = exec_argv(self.cfg.msb, name, [(p, o) for p, o, _ in req])
        try:
            r = subprocess.run(argv, capture_output=True, timeout=EXEC_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            self._fail(name)
            return []
        parsed, done, complete = parse_framed(r.stdout)
        if not complete:
            self._fail(name)
            return []
        self.backoff.pop(name, None)
        if done:
            self.summary_seen.add(name)
        stream_of = {p: s for p, _, s in req}
        out = []
        for f in parsed:
            out.append(LiveChunk(run_id=info["run"], vm=name, agent_id=info["agent_id"], stream=stream_of.get(f["file"], "?"),
                                 guest_path=f["file"], epoch=info["created_at"], size=f["size"], off=f["off"],
                                 data=f["data"], done=done))
        if not dry:
            for c in out:
                if c.size >= 0 and (c.data or c.stream == "summary"):
                    self.submit(c)
        return out

    def _fail(self, name):
        self.errors += 1
        _, delay = self.backoff.get(name, (0, 1.0))
        delay = min(delay * 2, 30.0)
        self.backoff[name] = (time.time() + delay, delay)

    def _tail_job(self, name):
        try:
            self.tail_vm(name)
        except Exception:
            log.exception("tail %s failed", name)
            self._fail(name)
        finally:
            with self._lock:
                self._inflight.discard(name)

    # ------------------------------------------------------------------ loops
    def start(self):
        if not self.enabled:
            return
        threading.Thread(target=self._loop, name="obs-livetap", daemon=True).start()

    def stop(self):
        self.stop_ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        next_disc = 0.0
        while not self.stop_ev.is_set():
            now = time.time()
            if now >= next_disc:
                try:
                    by_run = self.discover()
                    if by_run is not None:
                        self.submit(("vms", by_run))
                except Exception:
                    log.exception("discover failed")
                    self.errors += 1
                next_disc = now + self.cfg.tap_discover_s
            for name in list(self.vms):
                nb, _ = self.backoff.get(name, (0, 0))
                if nb > now:
                    continue
                with self._lock:
                    if name in self._inflight:
                        continue
                    self._inflight.add(name)
                self.pool.submit(self._tail_job, name)
            self.stop_ev.wait(self.cfg.tap_tail_s)


def tap_once(cfg, vm: str | None = None, write: bool = False, ingester=None) -> list[dict]:
    """Debug: one discovery + one read-only tail tick. Returns per-file byte counts."""
    pending = []
    offsets = ingester.live_offsets if (write and ingester) else (lambda rid, name: {})
    tap = LiveTap(cfg, submit=pending.append, offsets_fn=offsets)
    if not (cfg.msb and Path(cfg.msb).exists()):
        return [{"error": "msb not found"}]
    by_run = tap.discover()
    if by_run is None:
        return [{"error": "msb ls failed"}]
    report = []
    names = [vm] if vm else sorted(tap.vms)
    for name in names:
        if name not in tap.vms:
            report.append({"vm": name, "error": "not a running observatory VM (or its run is done)"})
            continue
        chunks = tap.tail_vm(name, dry=not write)
        for c in chunks:
            report.append({"vm": name, "run": c.run_id, "file": c.guest_path, "size": c.size, "off": c.off,
                           "bytes": len(c.data), "done": c.done})
    if write and ingester:
        ingester.set_live_vms(by_run)
        for item in pending:
            if isinstance(item, LiveChunk):
                ingester.apply_live(item)
        ingester.derive_dirty()
    return report
