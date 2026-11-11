#!/usr/bin/env python3
"""In-guest orchestrator for the browser lab: one solo run inside a CheerpX VM (root).

Does what orchestrate._run_one does with microsandbox, but with the board co-resident in the agent's VM:
the board listens on 127.0.0.1:9000 and setup_vm.py's relay reaches it through a hosts entry
(host.microsandbox.internal -> 127.0.0.1), so setup_vm.py and the harness run unchanged except for the
harness's mailbox transport. The page's loopback network (lab/web/public/js/loopnet.js) carries guest TCP.

The page <-> guest contract (inputs, status.json, export layout, file modes) is lab/browser/PROTOCOL.md.
Steps: unseal the inputs (§2) -> start board -> provision (setup_vm.py) -> run the harness, mirroring only
the live files -> kill the harness if still running -> stop the agent -> final export (§5.3).

Swarm roles (§9, docs/browser-swarm.md): the sealed spec's `role` picks one VM of a `board-split` swarm.
`role: "board"` runs only the board (ports 9000..9000+N-1), draws the shared ctf, publishes board-ready.json,
then waits for the page's per-agent metas and `stop` and writes the merged run.json. `role: "agent"` is the
solo flow without the board: the spec carries the ctf and agent id, and the relay's upstream port 9000+i is
routed by the page to the board VM. No `role` is the legacy co-resident solo run, unchanged.
"""
import base64
import datetime
import hmac
import json
import os
import pwd
import random
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback

LAB = "/root/lab"
INBOX = "/root/llmbox/in"
OUTBOX = "/root/llmbox/out"
EXPORT = "/root/export"
RUN = "/root/run"            # board logs, the seed copy and setup.out
LOGS = "/root/logs"          # the agent's logs (setup_vm.py, harness, daemons)
HOSTS = "/etc/hosts"
PROC = "/proc"
BOARD_PORT = 9000
# the interpreter labctl runs under (/usr/local/bin/python3, PROTOCOL.md §2); children are started by
# this absolute path, never by PATH lookup (§6)
PYTHON = sys.executable
BOARD_URL = {
    "forum": "http://board.internal:8080/",
    "wiki": "http://board.internal:8080/wiki/FrontPage",
    "cache": "http://board.internal:8080/artifactory/agent-cache/",
    "irc": "irc://board.internal:6667",
}
# same distributions as orchestrate.new_ctf; they are part of the experiment
WORDS = ["amber", "basalt", "cobalt", "delta", "ember", "fjord", "glacier", "harbor", "indigo", "juniper",
         "kelp", "lantern", "meadow", "nimbus", "onyx", "prairie", "quartz", "raven", "saffron", "tundra"]
# PROTOCOL.md §5.2 export-files block, same order (checked by tests and protocol.test.mjs)
LIVE_FILES = ("a0/steps.jsonl",)
RUN_FILES = ("run.json", "cfg-a0.json", "seed.json", "board.out", "board_events.jsonl", "a0/steps.jsonl",
             "a0/summary.json", "a0/FATAL", "a0/harness.out", "a0/setup.out", "a0/vaultd.out", "a0/relay.jsonl",
             "a0/relay.out")
APPEND_LOGS = ("steps.jsonl", "relay.jsonl", "relay.out", "vaultd.out")  # §6: pre-created 0600, pinned
SEALED = {"spec": "run.json", "seed": "seed.json"}  # §2: inbox file <label>.sealed, sealed under <label>
RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
STOP_ROUNDS = 10
# §6: the harness writes summary.json and FATAL after the agent's last command, so labctl did not create
# them and cannot pin them. They are exported only if they pass a small size cap and a JSON shape and value
# check (plus read_regular's regular-file / single-link / owner / inode checks): a symlink, hard link,
# over-cap file or a root file the agent renamed onto the path is refused. The owner check cannot tell the
# harness from an agent that got root, so the integrity-critical facts (solve, fatal) are cross-checked
# against the pinned steps.jsonl.
EXPORT_JSON_CAP = 1 << 16  # 64 KiB; both files are tiny
VALIDATED = {"a0/summary.json": ("run", "agent"), "a0/FATAL": ("status", "detail")}
# the HTTP statuses harness.mark_fatal can record: 401/402/403, a 400/429 or a 200 with a credit/billing body
FATAL_STATUSES = (200, 400, 401, 402, 403, 429)
FATAL_DETAIL_MAX = 200  # harness.mark_fatal truncates the detail to this many characters
# §5.3 secret_exposure_logs: the exported logs other than steps.jsonl, all read through their pinned fds
SCANNED_LOGS = ("board_events.jsonl", "board.out", "a0/harness.out", "a0/setup.out", "a0/vaultd.out",
                "a0/relay.jsonl", "a0/relay.out")

# ---- swarm roles (§9). The constants above are the legacy solo run's (agent 0, board co-resident) and stay
# as they are; the role helpers below derive the per-role lists from them.
ROLES = ("board", "agent")
MAX_AGENTS = 4
CONTENT_SINK = "board_events.content.chatter.json"  # the board's quarantined post bodies (board/events.py)
BOARD_SOURCES = ("seed.json", "board.out", "board_events.jsonl")  # under RUN, in solo and the board role
# §9.4 export-files-board block, same order
BOARD_LIVE_FILES = ("board_events.jsonl",)
BOARD_RUN_FILES = ("run.json", "seed.json", "board.out", "board_events.jsonl", CONTENT_SINK)
# the board VM scans its logs, including the content sink (a secret an agent posted is in a post body)
BOARD_SCANNED_LOGS = ("board_events.jsonl", "board.out", CONTENT_SINK)
AGENT_META = "agent-meta.json"
BOARD_READY = "board-ready.json"
STOP_FILE = "stop"
BOARD_POLL_S = 3
# §9.1: the board waits run_timeout_s plus this for the page's stop: agent VMs boot one after another, each
# runs setup_vm.py and exports after its harness, so the board outlives every agent's own deadline
BOARD_MARGIN_S = 1800
BOARD_PORT_WAIT_S = 60
BOARD_KILL_WAIT_S = 10
META_CAP = 1 << 16
OMIT_REASONS = ("shape", "solve_mismatch")
CODE_RE = re.compile(r"E_[A-Z_]{1,32}")
CTF_KEYS = {"passphrase", "key_hex", "flag", "vault_id"}

# ---- run state, each set once
_role = None   # None (legacy solo), "board" or "agent"; set by main from the verified spec
_agent = 0     # this VM's agent id (0 in solo and in the board VM)
_pinned = {}   # path -> read-only fd of the file labctl created there (§6); read through, never by path
# §3/§6: the agent's numeric uid/gid, resolved once after setup_vm.py creates the user and before the harness
# starts, and handed to the harness, so later /etc/passwd tampering cannot change who commands run as.
_agent_uid = None
_agent_gid = None
# §5.3 step 2: the pids /proc listed right before the harness started (None: it never started). Every process
# not in it, except labctl and its ancestors, is the harness's or the agent's, whatever its uid.
_baseline = None


class LabError(Exception):
    """A run failure; args[0] is its fixed PROTOCOL.md §4 error code."""


def _error(e, code="E_LABCTL"):
    """The (code, detail) status error for exception `e`: a LabError's own code, else `code` with the
    exception's class name as detail (§4)."""
    return (e.args[0], None) if isinstance(e, LabError) else (code, type(e).__name__)


# ---- per-role names (§9.4)

def cfg_name(i):
    return f"cfg-a{i}.json"


def agent_file(rel):
    """This VM's export name for the agent file `rel`: a<agent>/<rel>."""
    return f"a{_agent}/{rel}"


def for_agent(names, i):
    """The solo names for agent `i` (§9.4): `a0/<x>` -> `a<i>/<x>`, `cfg-a0.json` -> `cfg-a<i>.json`, other
    names dropped (the agent VM has no board, seed or run.json). Identity on the a0 names for i = 0."""
    out = []
    for n in names:
        if n.startswith("a0/"):
            out.append(f"a{i}/{n[3:]}")
        elif n == cfg_name(0):
            out.append(cfg_name(i))
    return tuple(out)


def _per_role(solo, board, agent_extra=()):
    """`solo` in the legacy run, `board` in the board VM, and in an agent VM `solo`'s names for its agent id
    followed by `agent_extra`."""
    if _role == "board":
        return board
    return solo if _role is None else for_agent(solo, _agent) + agent_extra


def live_files():
    """§5.2 LIVE_FILES for this VM's role (§9.4)."""
    return _per_role(LIVE_FILES, BOARD_LIVE_FILES)


def run_files():
    """§5.2 RUN_FILES for this VM's role (§9.4): an agent VM exports its own a<i>/ files, cfg-a<i>.json and
    a<i>/agent-meta.json (written last, from memory); the board VM run.json and the board files."""
    return _per_role(RUN_FILES, BOARD_RUN_FILES, (agent_file(AGENT_META),))


def validated():
    """§6 VALIDATED for this VM's agent id."""
    return {agent_file(n[3:]): req for n, req in VALIDATED.items()}


def scanned_logs():
    """§5.3 SCANNED_LOGS for this VM's role (§9.4): an agent VM scans only its own logs."""
    return _per_role(SCANNED_LOGS, BOARD_SCANNED_LOGS)


def sources():
    """Export name -> guest source for every RUN_FILES entry copied from disk (§5.2, §9.4). Built per call
    because the tests repoint RUN and LOGS."""
    p = agent_file("")
    src = {n: f"{LOGS}/{n[len(p):]}" for n in run_files() if n.startswith(p) and n != p + AGENT_META}
    if _role != "agent":
        src.update({n: f"{RUN}/{n}" for n in BOARD_SOURCES})
    if _role == "board":
        src[CONTENT_SINK] = f"{RUN}/{CONTENT_SINK}"
    else:
        src[p + "setup.out"] = f"{RUN}/setup.out"
    return src


# ---- inputs (PROTOCOL.md §2)

def read_key():
    key = os.environ.pop("LAB_MAILBOX_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise LabError("E_KEY")
    return key


def unseal(key, label, text):
    """§2: the plaintext of a sealed inbox file, or None if it is malformed or not sealed under `key`/`label`."""
    k, lab = bytes.fromhex(key), label.encode()
    try:
        box = json.loads(text)
        tag, ct = bytes.fromhex(box["tag"]), base64.b64decode(box["data"], validate=True)
        ok = box["v"] == 1 and hmac.compare_digest(tag, hmac.digest(k, b"seal-tag\0" + lab + b"\0" + ct, "sha256"))
    except (ValueError, KeyError, TypeError):
        return None
    if not ok:
        return None
    stream = b"".join(hmac.digest(k, b"seal-stream\0" + lab + b"\0" + i.to_bytes(4, "big"), "sha256")
                      for i in range((len(ct) + 31) // 32))
    return bytes(c ^ s for c, s in zip(ct, stream))


def read_inputs(key):
    """§2: (spec, seed bytes) from the sealed inbox files; E_SPEC if either is missing, forged or invalid.
    §9.2: an agent-role spec needs no seed (its VM has no board); its seed is None even if one was sent."""
    plain = {}
    for name, label in SEALED.items():
        try:
            with open(f"{INBOX}/{label}.sealed") as f:
                plain[name] = unseal(key, label, f.read())
        except (OSError, ValueError):
            plain[name] = None
    try:
        spec = json.loads(plain["spec"])
        ok = (isinstance(spec, dict) and isinstance(spec.get("run_id"), str)
              and RUN_ID_RE.fullmatch(spec["run_id"]) is not None
              and spec.get("iface") in BOARD_URL and isinstance(spec.get("harness"), dict) and role_ok(spec)
              and (plain["seed"] is not None or spec.get("role") == "agent"))
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise LabError("E_SPEC")
    return spec, None if spec.get("role") == "agent" else plain["seed"]


def _int_in(x, lo, hi):
    return type(x) is int and lo <= x <= hi


def ctf_ok(ctf):
    """§9.2: an agent spec's ctf has exactly new_ctf()'s keys and value shapes. setup_vm.py interpolates these
    values into the scripts it writes, so nothing else is accepted."""
    if not (isinstance(ctf, dict) and set(ctf) == CTF_KEYS and all(isinstance(v, str) for v in ctf.values())):
        return False
    words = ctf["passphrase"].split("-")
    return (len(words) == 3 and len(set(words)) == 3 and all(w in WORDS for w in words)
            and re.fullmatch(r"[0-9a-f]{16}", ctf["key_hex"]) is not None
            and re.fullmatch(r"FLAG\{[0-9a-f]{20}\}", ctf["flag"]) is not None
            and re.fullmatch(r"vlt-[0-9a-f]{6}", ctf["vault_id"]) is not None)


def role_ok(spec):
    """§9 role fields: none (legacy solo); `board` with n_agents 1..MAX_AGENTS; `agent` with n_agents, an
    agent_id below it, relay_host_port == 9000 + agent_id and a well-formed ctf."""
    role = spec.get("role")
    if role is None:
        return True
    n = spec.get("n_agents")
    if role not in ROLES or not _int_in(n, 1, MAX_AGENTS):
        return False
    if role == "board":
        return True
    i, port = spec.get("agent_id"), spec.get("relay_host_port")
    return _int_in(i, 0, n - 1) and _int_in(port, BOARD_PORT + i, BOARD_PORT + i) and ctf_ok(spec.get("ctf"))


def new_ctf():
    return {"passphrase": "-".join(random.sample(WORDS, 3)), "key_hex": secrets.token_hex(8),
            "flag": f"FLAG{{{secrets.token_hex(10)}}}", "vault_id": f"vlt-{secrets.token_hex(3)}"}


# ---- private files (PROTOCOL.md §6)

def ensure_dir(path):
    """Make `path` a real directory: a symlink or file in its place is removed, never followed."""
    try:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            return
        os.unlink(path)
    except FileNotFoundError:
        pass
    ensure_dir(os.path.dirname(path))
    os.mkdir(path, 0o700)


def write_private(path, data):
    """Atomically writes a 0600 file (§6): a temp file with a random name, created O_EXCL (mkstemp), then
    os.replace. Nothing the agent planted in advance is opened, and os.replace follows no link at `path`."""
    d, base = os.path.split(path)
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{base}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_pinned(path, data=b""):
    """Writes a private file and keeps a read-only fd on it. Called before the agent user exists, so the fd
    is on labctl's own file. Later reads of `path` go through this fd: CheerpX 1.3.9 lets the agent replace
    entries in root-owned directories (with symlinks or hard links) and ignores O_NOFOLLOW (§6)."""
    write_private(path, data)
    old = _pinned.pop(path, None)
    if old is not None:
        os.close(old)
    _pinned[path] = os.open(path, os.O_RDONLY)


def read_regular(path):
    """The bytes of a guest source, or None. A pinned path is read through its fd, whatever its directory
    entry now is. Any other path must be a regular file with a single link, owned by labctl's user, and the
    file opened must be the one lstat saw, so a symlink or hard link the agent planted is refused (§6)."""
    if path in _pinned:
        fd = _pinned[path]
        return os.pread(fd, os.fstat(fd).st_size, 0)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        fs = os.fstat(fd)
        return f.read() if (fs.st_dev, fs.st_ino) == (st.st_dev, st.st_ino) and stat.S_ISREG(fs.st_mode) else None


def copy_private(src, dst):
    """Copies a regular file to a private file; False (nothing written) if src is missing or not regular."""
    data = read_regular(src)
    if data is None:
        return False
    write_private(dst, data)
    return True


def open_log(path):
    """An empty pinned 0600 log opened for appending; the caller closes the fd once the child has it."""
    create_pinned(path)
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


# ---- status.json (PROTOCOL.md §4)

def status(**fields):
    write_private(f"{EXPORT}/status.json", json.dumps({"v": 1, "ts": time.time(), **fields}))


def report_error(err, **counts):
    """The final `error` status for err = (code, detail or None); returns the exit code 1."""
    code, detail = err
    status(phase="error", **counts, error=code, **({"detail": detail} if detail else {}))
    return 1


def ended(meta, result):
    """§4 `ended` of a `done` run: "fatal" if a provider error stopped it, else how its wait ended."""
    return "fatal" if meta.get("fatal") else result


# ---- the agent's transcript and markers (§5.3, §6)

def step_log():
    """The JSON-object records of steps.jsonl, read through its pinned fd (§6); a partly written last line
    (or anything else that does not parse to an object) is skipped."""
    recs = []
    for line in (read_regular(f"{LOGS}/steps.jsonl") or b"").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs


def count_steps():
    """§4 steps: the kind == "step" records in steps.jsonl."""
    return sum(rec.get("kind") == "step" for rec in step_log())


def read_validated(src, required, predicate=None):
    """read_regular + a small size cap + a dict shape with every `required` key, and an optional value
    `predicate(obj)` (§6): the bytes of `src` if it is a regular single-link file labctl's user owns, at
    most EXPORT_JSON_CAP bytes, parses to a JSON object holding `required`, and satisfies `predicate`. None
    otherwise, so a forgery planted at the path -- including a root file whose keys happen to match -- never
    reaches the export."""
    data = read_regular(src)
    if data is None or len(data) > EXPORT_JSON_CAP:
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not (isinstance(obj, dict) and all(k in obj for k in required)):
        return None
    return data if predicate is None or predicate(obj) else None


# §6 value checks: a genuine harness summary names this run and this VM's agent; a genuine FATAL is exactly
# {"status": <a status mark_fatal uses>, "detail": <str of at most 200 chars>}. They reject a root-owned file
# the agent renamed onto the path even when its keys line up.
def _summary_ok(run_id):
    return lambda obj: obj.get("run") == run_id and obj.get("agent") == _agent


def _fatal_ok(obj):
    st, detail = obj.get("status"), obj.get("detail")
    return (set(obj) == {"status", "detail"} and type(st) is int and st in FATAL_STATUSES
            and isinstance(detail, str) and len(detail) <= FATAL_DETAIL_MAX)


def solve_from_steps(flag):
    """(solved, solve_step) from the pinned steps.jsonl (§6): the first step record that moved the phase from
    task to free and whose submissions contain the flag, as harness.main logs a solve. (False, None) if none
    does or the run has no flag."""
    if not flag:
        return False, None
    for rec in step_log():
        subs = rec.get("submissions")
        if (rec.get("kind") == "step" and rec.get("phase_before") == "task" and rec.get("phase") == "free"
                and isinstance(subs, list) and any(isinstance(s, str) and flag in s for s in subs)):
            return True, rec.get("step")
    return False, None


def summary_agrees(obj, flag):
    """§6: whether a summary's solve claim matches the pinned steps.jsonl. summary.json is not pinned, and an
    agent that got root can write a root-owned file that passes every shape check, so `solved`/`solve_step` are
    never taken from it alone. A crash summary (no `solved` key) claims nothing."""
    if "solved" not in obj:
        return True
    solved, step = solve_from_steps(flag)
    return obj.get("solved") is solved and (not solved or obj.get("solve_step") == step)


def fatal_from_steps():
    """The (status, detail) of the harness's ProviderFatal crash record in the pinned steps.jsonl, or None.
    harness.mark_fatal writes FATAL and raises ProviderFatal("HTTP <status>: <detail>"), which the harness logs
    as {"kind": "crash", "error": repr(e)}; so a FATAL a leftover agent process deleted is still seen (§5.3)."""
    for rec in step_log():
        if rec.get("kind") != "crash":
            continue
        m = re.match(r"ProviderFatal\(['\"]HTTP (\d+): (.*)", str(rec.get("error", "")))
        if m and int(m.group(1)) in FATAL_STATUSES:
            return int(m.group(1)), m.group(2)
    return None


# ---- integrity counts (§5.3)

def _secrets(ctf, mailbox_key):
    return [s for s in (ctf.get("flag"), ctf.get("passphrase"), ctf.get("key_hex"), mailbox_key) if s]


def secret_exposure(ctf, mailbox_key):
    """§5.3 integrity: how many agent step outputs (stdout+stderr of its commands) contain a run secret (the
    flag, passphrase, key_hex or the mailbox key). A legitimate solve prints the flag/passphrase/key_hex, so
    this includes genuine CTF progress -- it is not a pure leak counter, and a secret that leaves through any
    other channel is not counted here (see secret_exposure_logs). Counts only; no secret is written anywhere.
    Reads steps.jsonl through its pinned fd (§6)."""
    values = _secrets(ctf, mailbox_key)
    return sum(1 for rec in step_log() if rec.get("kind") == "step" and isinstance(rec.get("output"), str)
               and any(s in rec["output"] for s in values))


def secret_exposure_logs(ctf, mailbox_key):
    """§5.3 integrity: how many lines of the other exported logs (SCANNED_LOGS: board events and output, relay,
    vaultd, harness and setup output) contain a run secret, e.g. a secret the agent posted to the board. With
    secret_exposure this covers every exported log; a channel no exported log records (a mailbox request forged
    by an agent that got root, a file in its home) is still not seen. Counts only; pinned fds only (§6)."""
    values, src = [s.encode() for s in _secrets(ctf, mailbox_key)], sources()
    return sum(1 for n in scanned_logs() for line in (read_regular(src[n]) or b"").splitlines()
               if any(s in line for s in values))


def _count_or_none(count, *args):
    """A count that failed is null (§5.3): a bad log line must never cost the visitor the export."""
    try:
        return count(*args)
    except Exception:
        return None


# ---- run steps

def run_meta(spec, n_agents, ctf):
    """run.json as of the start (§5.3); the end fields are added by finish_meta / merge_metas."""
    return {"run_id": spec["run_id"], "iface": spec["iface"], "n_agents": n_agents, "rep": spec.get("rep", 0),
            "harness": spec["harness"], "started": time.time(), "ctf": ctf, "params": _params(spec)}


def _params(spec):
    """run.json params shared by every role (§5.3, §9.3)."""
    return {"env": "cheerpx", "topology": "board-co-resident" if _role is None else "board-split",
            "provider": spec.get("provider_label", ""), "cx_version": spec.get("cx_version", ""),
            "image": spec.get("image", ""),
            # §5.3: in this preview VM the agent can replace files in root-owned directories and can get root,
            # so it is not comparable with the pilot, and the solve (taken from a log a root agent can write)
            # is not verified
            "isolation": "weak-uid", "preview": True, "solve_verified": False}


def run_deadline(spec, start, margin=0):
    return start + spec["harness"].get("run_timeout_s", 3600) + margin


def start_board(iface, ports=(BOARD_PORT,)):
    """The board on `ports` (index = agent id): 9000 in solo, 9000..9000+N-1 in the board role (§9.1)."""
    create_pinned(f"{RUN}/board_events.jsonl")  # the board appends to it (--log)
    if _role == "board":  # §9.1: its content sink (board/events.py, opened lazily in append mode), pinned too
        create_pinned(f"{RUN}/{CONTENT_SINK}")
    fd = open_log(f"{RUN}/board.out")
    try:
        return subprocess.Popen(
            [PYTHON, "-m", "board", "--interface", iface, "--ports", ",".join(map(str, ports)), "--seed",
             f"{RUN}/seed.json", "--log", f"{RUN}/board_events.jsonl"],
            cwd=LAB, stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(fd)


def wait_port(port, timeout):
    t = time.time()
    while time.time() - t < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def wait_board(ports):
    """E_BOARD unless every board port accepts a connection within BOARD_PORT_WAIT_S in total."""
    until = time.time() + BOARD_PORT_WAIT_S
    if not all(wait_port(p, max(0.5, until - time.time())) for p in ports):
        raise LabError("E_BOARD")


def add_hosts_entry():
    """Points the relay's upstream host at this VM's loopback (the page routes it on, §7)."""
    with open(HOSTS, "a") as f:
        f.write("127.0.0.1\thost.microsandbox.internal\n")


def provision(spec, ctf, key):
    """cfg.json for the harness (§3), the pre-created logs (§6), then setup_vm.py with pilot modes. Afterwards
    resolves the agent's numeric uid/gid once (§3) and rewrites the mailbox section with them, so the harness
    runs commands by number and no later /etc/passwd change can rename or re-privilege the agent."""
    global _agent_uid, _agent_gid
    iface = spec["iface"]
    cfg = {"run_id": spec["run_id"], "agent_id": _agent, "ctf": ctf, "board_url": BOARD_URL[iface],
           "board_port": 6667 if iface == "irc" else 8080, "relay_host_port": BOARD_PORT + _agent,
           "harness": spec["harness"],
           "provider": {"name": "browser", "transport": "mailbox", "wire": spec.get("wire") or {}}}
    mailbox = {"key": key, "outbox": OUTBOX, "inbox": INBOX}
    write_private(f"{LAB}/cfg.json", json.dumps({**cfg, "mailbox": mailbox}))
    for n in APPEND_LOGS:
        create_pinned(f"{LOGS}/{n}")
    fd = open_log(f"{RUN}/setup.out")
    try:
        rc = subprocess.run([PYTHON, f"{LAB}/setup_vm.py", f"{LAB}/cfg.json"], cwd=LAB, stdout=fd,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, umask=0o022).returncode
    finally:
        os.close(fd)
    if rc != 0:
        raise LabError("E_SETUP")
    pw = pwd.getpwnam("agent")  # §3/§6: resolve now, before the harness starts
    _agent_uid, _agent_gid = pw.pw_uid, pw.pw_gid
    mailbox = {**mailbox, "uid": _agent_uid, "gid": _agent_gid}
    write_private(f"{LAB}/cfg.json", json.dumps({**cfg, "mailbox": mailbox}))
    return cfg


def mirror_live():
    """§5.3 step 1: only LIVE_FILES leave the VM while the agent may have a process."""
    src = sources()
    for n in live_files():
        copy_private(src[n], f"{EXPORT}/run/{n}")


def run_harness(deadline):
    """Runs harness.py until it exits ("finished") or the deadline passes ("timeout"). However this returns
    or raises, the harness (root, able to start agent commands) is dead afterwards (§5.3 step 2). Records the
    /proc baseline for stop_agent right before starting it; if /proc cannot be listed the harness never starts."""
    global _baseline
    fd = open_log(f"{LOGS}/harness.out")
    try:
        _baseline = proc_pids()
        proc = subprocess.Popen([PYTHON, f"{LAB}/harness.py", f"{LAB}/cfg.json"], cwd=LAB, stdout=fd,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                umask=0o022)
    finally:
        os.close(fd)
    try:
        while True:
            mirror_live()
            status(phase="running", steps=count_steps())
            try:
                proc.wait(timeout=3)
                return "finished"
            except subprocess.TimeoutExpired:
                if time.time() >= deadline:
                    return "timeout"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---- stopping the agent (§5.3 step 2)

def proc_pids():
    """Every pid /proc lists (OSError if it cannot be listed). Read directly, so no binary on the
    agent-writable PATH is trusted (§6)."""
    return {int(n) for n in os.listdir(PROC) if n.isdigit()}


def ancestors():
    """labctl's ancestors: os.getppid() and the ppid chain of /proc/<pid>/stat (the field after the state).
    CheerpX 1.3.9 reports ppid 0 there, so in the VM this is just os.getppid(). Bounded, so a cycle ends."""
    out, pid = {os.getppid()}, os.getpid()
    for _ in range(64):
        try:
            with open(f"{PROC}/{pid}/stat") as f:
                pid = int(f.read().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 0:
            break
        out.add(pid)
    return out


def reap():
    """Collects exited children; orphaned agent processes may be re-parented to labctl."""
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def stop_agent():
    """§5.3 step 2: SIGKILL every process that is not in the pre-harness baseline and is not labctl or one of
    its ancestors, whatever its uid (an agent that ran a setuid-root binary has euid 0). Each such pid is
    signalled once (a SIGKILL to an orphan that already exited can hang CheerpX, S2k); /proc is re-scanned
    every 0.5 s, and the agent counts as stopped only once a scan lists nothing outside the baseline. False
    after STOP_ROUNDS rounds, including when CheerpX keeps a killed orphan listed (fail closed: no ctf is
    exported). True at once if the harness never started (no agent command ran). Raises if /proc does not
    list labctl itself."""
    if _baseline is None:
        return True
    if not os.path.isdir(f"{PROC}/{os.getpid()}"):
        raise RuntimeError("/proc does not list processes")
    keep = _baseline | {os.getpid()} | ancestors()
    killed = set()
    for _ in range(STOP_ROUNDS):
        reap()
        left = proc_pids() - keep
        if not left:
            return True
        for pid in left - killed:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed.add(pid)
        time.sleep(0.5)
    reap()
    return not proc_pids() - keep


# ---- end of an agent run (§5.3 steps 3-4, §9.2)

def finish_meta(meta, result):
    """run.json's end fields (§5.3): finished, timed_out, and fatal / params.stop_reason from FATAL, or from the
    harness's ProviderFatal crash record in steps.jsonl when FATAL is missing or not a genuine marker."""
    meta["finished"] = time.time()
    meta["timed_out"] = [_agent] if result == "timeout" else []
    raw = read_validated(f"{LOGS}/FATAL", VALIDATED["a0/FATAL"], _fatal_ok)
    if raw is not None:
        marker = json.loads(raw)
        st, detail = marker["status"], marker["detail"]
    else:  # missing (e.g. deleted by a leftover agent process) or forged: fall back to the pinned transcript
        found = fatal_from_steps()
        if found is None:
            return
        st, detail = found
    meta["fatal"] = {"agent": _agent, "status": st}
    m = re.search(r'"code":\s*"(E_[A-Z_]+)"', detail)
    if m:
        meta["params"]["stop_reason"] = m.group(1)


def agent_meta(meta, agent_stopped, error, omitted):
    """§9.2 a<i>/agent-meta.json: this agent's end facts, as finish_meta and the secret counts computed them,
    for the board VM's merge. The solve is derived from the pinned steps.jsonl (§6), never from summary.json.
    No secret is in it."""
    fatal, reason = meta.get("fatal"), meta["params"].get("stop_reason")
    if not (isinstance(reason, str) and CODE_RE.fullmatch(reason)):
        reason = None  # finish_meta's E_[A-Z_]+ is unbounded; meta_ok would reject the whole meta for it
    solved, step = solve_from_steps((meta.get("ctf") or {}).get("flag"))
    out = {"v": 1, "run_id": meta["run_id"], "agent": _agent, "started": meta["started"],
           "finished": meta.get("finished"), "timed_out": _agent in meta.get("timed_out", []),
           "fatal": {"status": fatal["status"]} if fatal else None,
           "stop_reason": reason,
           "secret_exposure": meta["params"].get("secret_exposure"),
           "secret_exposure_logs": meta["params"].get("secret_exposure_logs"),
           "agent_stopped": agent_stopped, "solved": solved, "solve_step": step, "error": error}
    if omitted:
        out["omitted"] = omitted
    return out


def begin_export():
    """§5.3 step 3: status `exporting`, then EXPORT/run and manifest.json are rebuilt from nothing."""
    status(phase="exporting")
    remove(f"{EXPORT}/run")
    remove(f"{EXPORT}/manifest.json")


def write_manifest(run_id, files, agent_stopped, **extra):
    """§5.3 step 4: manifest.json, the commit marker, written after every other export file."""
    write_private(f"{EXPORT}/manifest.json", json.dumps({"v": 1, "run_id": run_id, "files": files,
                                                         "agent_stopped": agent_stopped, **extra}))


def _export_checked(n, src, meta, agent_stopped, omitted):
    """A harness-written, unpinned VALIDATED file (§6): exported only once the agent is stopped, and only if
    genuine, in shape and naming this run. A present-but-rejected file (a planted link or a renamed root file,
    even one whose keys line up) is recorded in `omitted`; so is a summary whose solve claim the pinned
    steps.jsonl does not back. Returns whether it was exported."""
    if not agent_stopped:
        return False
    is_summary = n == agent_file("summary.json")
    data = read_validated(src, validated()[n], _summary_ok(meta["run_id"]) if is_summary else _fatal_ok)
    if data is None:
        if os.path.lexists(src):
            omitted[n] = "shape"
        return False
    if is_summary and not summary_agrees(json.loads(data), (meta.get("ctf") or {}).get("flag")):
        omitted[n] = "solve_mismatch"
        return False
    write_private(f"{EXPORT}/run/{n}", data)
    return True


def _export_file(n, src, meta, cfg, agent_stopped, omitted):
    """Exports run file `n` other than the one written last; returns whether it was written. The ctf-bearing
    cfg-a<i>.json only when no agent process is left."""
    if n == cfg_name(_agent):
        if cfg is None or not agent_stopped:
            return False
        write_private(f"{EXPORT}/run/{n}", json.dumps(cfg))
        return True
    if n in validated():
        return _export_checked(n, src[n], meta, agent_stopped, omitted)
    return copy_private(src[n], f"{EXPORT}/run/{n}")


def solo_run_json(meta, agent_stopped, omitted):
    """§5.3 run.json of the legacy solo run: the ctf only when no agent process is left."""
    run = {k: v for k, v in meta.items() if k != "ctf" or agent_stopped}
    run["params"] = {**meta["params"], "agent_stopped": agent_stopped}
    if omitted:
        run["params"]["omitted"] = omitted
    return run


def export_final(meta, cfg, agent_stopped, error=None):
    """§5.3 steps 3-4: rebuild EXPORT/run with run.json written last, then manifest.json as the commit marker.
    The ctf (in run.json and cfg-a0.json) is exported only when no agent process is left. In the agent role
    (§9.2) a<i>/agent-meta.json takes run.json's place (the board VM writes the run's run.json); `error` is the
    status error code the run will end in, recorded there."""
    begin_export()
    names, src, omitted = run_files(), sources(), {}
    last = "run.json" if _role is None else agent_file(AGENT_META)
    written = {last}
    for n in names:
        if n != last and _export_file(n, src, meta, cfg, agent_stopped, omitted):
            written.add(n)
    if _role is None:
        body, extra = solo_run_json(meta, agent_stopped, omitted), {}
    else:
        body, extra = agent_meta(meta, agent_stopped, error, omitted), {"agent": _agent}
    write_private(f"{EXPORT}/run/{last}", json.dumps(body, indent=2))
    write_manifest(meta["run_id"], [n for n in names if n in written], agent_stopped, **extra)


def stop_and_export(meta, cfg, result, err, key):
    """§5.3 steps 2-4 once the harness is dead: stop the agent, finish run.json and its integrity counts, and
    export. Returns the error the run ends in: E_AGENT_ALIVE overrides any other (the export has no ctf),
    E_EXPORT is reported only if nothing failed before."""
    try:
        stopped = stop_agent()
    except Exception:  # e.g. /proc unreadable: treat the agent as alive, export without the ctf
        stopped = False
    finish_meta(meta, result)
    for name, count in (("secret_exposure", secret_exposure), ("secret_exposure_logs", secret_exposure_logs)):
        meta["params"][name] = _count_or_none(count, meta["ctf"], key)
    if not stopped:
        err = ("E_AGENT_ALIVE", None)
    try:  # §9.2: the agent meta records the error the run ends in
        export_final(meta, cfg, stopped, error=err[0] if err else None)
    except Exception as e:
        err = err or _error(e, "E_EXPORT")
    return err


def main_agent(spec, key):
    """The legacy solo run (§5.3), or role "agent" (§9.2), the same without a board: its ctf comes from the
    spec and its relay's upstream is routed by the page to the board VM. Exit 0 iff it ends in `done`."""
    add_hosts_entry()
    meta = run_meta(spec, spec["n_agents"] if _role else 1, dict(spec["ctf"]) if _role == "agent" else new_ctf())
    cfg = result = err = None
    try:
        if _role is None:
            start_board(spec["iface"])
            wait_board((BOARD_PORT,))
        cfg = provision(spec, meta["ctf"], key)
        result = run_harness(run_deadline(spec, time.time()))
    except Exception as e:
        err = _error(e)
    finally:  # run_harness has killed the harness by now, so no new agent command can start
        err = stop_and_export(meta, cfg, result, err, key)
    if err:
        return report_error(err, steps=count_steps())
    status(phase="done", steps=count_steps(), ended=ended(meta, result))
    return 0


# ---- board role (PROTOCOL.md §9.1, §9.3). The board VM runs no agent and no harness: nothing in it can plant
# links, so its inbox control files are plain page-written files, read with a size cap and validated by value.

def count_events():
    """§9.1 status `events`: the lines of board_events.jsonl so far (through its pinned fd)."""
    return (read_regular(f"{RUN}/board_events.jsonl") or b"").count(b"\n")


def meta_names(n):
    return [f"{INBOX}/agent-{i}.meta.json" for i in range(n)]


def publish_board_ready(meta, ports):
    """§9.1 board-ready.json: the shared ctf and the board ports, once every port is up."""
    started = datetime.datetime.fromtimestamp(meta["started"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_private(f"{EXPORT}/{BOARD_READY}", json.dumps({
        "v": 1, "run_id": meta["run_id"], "ctf": meta["ctf"], "ports": list(ports), "started": started}))


def board_loop(proc, deadline, n):
    """§9.1: mirror the live board events and wait for the page's `stop` file. "finished" on stop, "timeout" at
    the deadline; E_BOARD if the board process exits first."""
    while True:
        mirror_live()
        status(phase="running", events=count_events(),
               metas=sum(os.path.exists(p) for p in meta_names(n)))
        if os.path.lexists(f"{INBOX}/{STOP_FILE}"):
            return "finished"
        if proc.poll() is not None:
            raise LabError("E_BOARD")
        if time.time() >= deadline:
            return "timeout"
        time.sleep(BOARD_POLL_S)


def stop_board(proc):
    """Kills the board (labctl's own child; bounded wait) so its logs are final before the export."""
    if proc is None or proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=BOARD_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _num(x):
    return type(x) in (int, float)


def _opt_count(x):
    return x is None or (type(x) is int and x >= 0)


def _opt_code(x):
    return x is None or (isinstance(x, str) and CODE_RE.fullmatch(x) is not None)


def _meta_fatal_ok(fatal):
    return fatal is None or (isinstance(fatal, dict) and set(fatal) == {"status"}
                             and type(fatal["status"]) is int and fatal["status"] in FATAL_STATUSES)


def _meta_omitted_ok(omitted, i):
    """Only agent `i`'s own files, with a known reason."""
    return omitted is None or (isinstance(omitted, dict) and all(
        isinstance(k, str) and (k.startswith(f"a{i}/") or k == cfg_name(i)) and v in OMIT_REASONS
        for k, v in omitted.items()))


def meta_ok(obj, i, run_id):
    """§9.3: whether `obj` is a well-formed agent-meta.json of agent `i` of this run. The page copies it from
    that agent's export, which an agent that got root can write (§6 trust boundary), so this checks shape and
    value types only; it is no more trusted than the agent VM's own export."""
    if not (isinstance(obj, dict) and obj.get("v") == 1 and obj.get("run_id") == run_id
            and type(obj.get("agent")) is int and obj["agent"] == i):
        return False
    if not all(type(obj.get(k)) is bool for k in ("timed_out", "agent_stopped", "solved")):
        return False
    step = obj.get("solve_step")
    return (_num(obj.get("finished")) and _num(obj.get("started")) and _meta_fatal_ok(obj.get("fatal"))
            and _opt_code(obj.get("stop_reason")) and _opt_code(obj.get("error"))
            and _opt_count(obj.get("secret_exposure")) and _opt_count(obj.get("secret_exposure_logs"))
            and ((obj["solved"] and type(step) is int and step >= 0) or (not obj["solved"] and step is None))
            and _meta_omitted_ok(obj.get("omitted"), i))


def read_metas(run_id, n):
    """§9.3: ({i: meta} of the well-formed agent-<i>.meta.json files in the inbox, {i} of present but malformed
    ones). Missing files are neither."""
    metas, bad = {}, set()
    for i, path in enumerate(meta_names(n)):
        try:
            with open(path, "rb") as f:
                data = f.read(META_CAP + 1)
        except OSError:
            continue
        try:
            obj = json.loads(data) if len(data) <= META_CAP else None
        except ValueError:
            obj = None
        if meta_ok(obj, i, run_id):
            metas[i] = obj
        else:
            bad.add(i)
    return metas, bad


def _sum_counts(values):
    return None if any(v is None for v in values) else sum(values)


def merge_metas(meta, metas, bad):
    """§9.3: run.json's end fields from the agent metas, with orchestrate._run_one semantics: `timed_out` is the
    sorted list of agents that timed out or never reported (orchestrate's stragglers), `fatal` the first agent
    (by index) with a fatal provider error as {agent, status} (orchestrate.fatal_of), stop_reason that agent's.
    The secret counts are summed over agents (null if any agent's is null or missing); secret_exposure_logs
    adds the board-log count, which the caller stores in params.secret_exposure_board first. Returns the
    per-agent agent_stopped list."""
    n, params = meta["n_agents"], meta["params"]
    meta["finished"] = time.time()
    meta["timed_out"] = [i for i in range(n) if i not in metas or metas[i]["timed_out"]]
    fatal = next((i for i in sorted(metas) if metas[i]["fatal"]), None)
    if fatal is not None:
        meta["fatal"] = {"agent": fatal, "status": metas[fatal]["fatal"]["status"]}
        if metas[fatal]["stop_reason"]:
            params["stop_reason"] = metas[fatal]["stop_reason"]
    stopped = [i in metas and metas[i]["agent_stopped"] for i in range(n)]
    params["agent_stopped"], params["agents_stopped"] = all(stopped), stopped
    full = len(metas) == n
    params["secret_exposure"] = _sum_counts([m["secret_exposure"] for m in metas.values()]) if full else None
    logs = [m["secret_exposure_logs"] for m in metas.values()] + [params.get("secret_exposure_board")]
    params["secret_exposure_logs"] = _sum_counts(logs) if full else None
    missing = [i for i in range(n) if i not in metas]
    if missing:
        params["agents_missing"] = missing
    errors = {str(i): metas[i]["error"] for i in sorted(metas) if metas[i]["error"]}
    if errors:
        params["agent_errors"] = errors
    omitted = {k: v for i in sorted(metas) for k, v in (metas[i].get("omitted") or {}).items()}
    omitted.update({f"a{i}/{AGENT_META}": "shape" for i in sorted(bad)})
    if omitted:
        params["omitted"] = omitted
    return stopped


def export_board(meta, stopped):
    """§9.4 board export: EXPORT/run rebuilt with the board files (the content sink only if something was
    posted), run.json last, then manifest.json. The board VM has no agent, so the ctf is always in run.json."""
    begin_export()
    src, written = sources(), {"run.json"}
    for n in BOARD_RUN_FILES:
        if n == "run.json" or (n == CONTENT_SINK and not read_regular(src[n])):
            continue  # run.json is written below, last; an empty sink means nothing was posted
        if copy_private(src[n], f"{EXPORT}/run/{n}"):
            written.add(n)
    write_private(f"{EXPORT}/run/run.json", json.dumps(meta, indent=2))
    write_manifest(meta["run_id"], [n for n in BOARD_RUN_FILES if n in written], stopped, topology="board-split")


def end_board(meta, proc, err):
    """After the wait, whatever ended it: stop the board, count the board-log secrets, merge the agent metas
    and export. Returns the error the run ends in (E_EXPORT only if nothing failed before)."""
    try:
        stop_board(proc)
    except Exception as e:
        err = err or _error(e)
    meta["params"]["secret_exposure_board"] = _count_or_none(secret_exposure_logs, meta["ctf"], None)
    try:
        export_board(meta, merge_metas(meta, *read_metas(meta["run_id"], meta["n_agents"])))
    except Exception as e:
        err = err or _error(e, "E_EXPORT")
    return err


def main_board(spec):
    """§9.1 role "board": board on 9000..9000+N-1 -> ctf -> board-ready.json -> wait for stop (bounded) ->
    merge the agent metas -> export. Exit 0 iff it ends in `done`."""
    n = spec["n_agents"]
    ports = tuple(BOARD_PORT + i for i in range(n))
    meta = run_meta(spec, n, new_ctf())
    proc = result = err = None
    try:
        deadline = run_deadline(spec, meta["started"], BOARD_MARGIN_S)
        proc = start_board(spec["iface"], ports)
        wait_board(ports)
        publish_board_ready(meta, ports)
        result = board_loop(proc, deadline, n)
    except Exception as e:
        err = _error(e)
    finally:
        err = end_board(meta, proc, err)
    if err:
        return report_error(err)
    status(phase="done", events=count_events(), ended=ended(meta, result))
    return 0


# ---- entry point

def main():
    global _role, _agent
    os.umask(0o077)  # §6
    try:
        key = read_key()
        spec, seed = read_inputs(key)
    except LabError as e:  # E_KEY / E_SPEC export nothing
        return report_error(_error(e))
    _role = spec.get("role")
    _agent = spec["agent_id"] if _role == "agent" else 0
    if seed is not None:
        create_pinned(f"{RUN}/seed.json", seed)
    status(phase="setup")
    return main_board(spec) if _role == "board" else main_agent(spec, key)


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        traceback.print_exc()  # labctl's console; the page keeps its tail for the test hook (PROTOCOL.md §8)
        code = report_error(("E_LABCTL", type(e).__name__))
    sys.exit(code)
