"""labctl.py (the browser lab's in-guest orchestrator) with its paths moved into a temp dir.

No VM, board or agent user: subprocess, /proc and the board port are faked. The seed and every log
are neutral placeholders written here. Checks the flag rule and file lists of lab/browser/PROTOCOL.md §5.
"""
import base64
import contextlib
import hashlib
import hmac
import importlib.util
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import config  # noqa: E402
import orchestrate  # noqa: E402

LABCTL = LAB / "browser" / "guest" / "labctl.py"
PROTOCOL = LAB / "browser" / "PROTOCOL.md"
DIRS = ("LAB", "INBOX", "OUTBOX", "EXPORT", "RUN", "LOGS")
KEY = "c3" * 32
SPEC = {"run_id": "browser-forum-solo-20260101000000-abcd", "iface": "forum", "rep": 0,
        "harness": {"budget": 3, "model": "placeholder-model", "run_timeout_s": 600},
        "wire": {"sanitize_tool_args": True}, "provider_label": "placeholder:model", "cx_version": "1.3.9",
        "image": "swarm-000000000000.ext2"}
SEED = b'{"placeholder": true}'
STEP = json.dumps({"kind": "step", "step": 1, "output": "placeholder"})


def seal(key_hex, label, data):
    """PROTOCOL.md §2 seal, as the page does it (public/js/protocol.js seal)."""
    k, lab = bytes.fromhex(key_hex), label.encode()
    stream = b"".join(hmac.digest(k, b"seal-stream\0" + lab + b"\0" + i.to_bytes(4, "big"), "sha256")
                      for i in range((len(data) + 31) // 32))
    ct = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(k, b"seal-tag\0" + lab + b"\0" + ct, hashlib.sha256).hexdigest()
    return json.dumps({"v": 1, "tag": tag, "data": base64.b64encode(ct).decode()})


def load(td):
    """A fresh labctl module whose directories (and /etc/hosts) live under `td`."""
    spec = importlib.util.spec_from_file_location("labctl_under_test", LABCTL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for name in DIRS:
        d = td / name.lower()
        d.mkdir()
        setattr(m, name, str(d))
    m.HOSTS = str(td / "hosts")
    (td / "proc" / str(os.getpid())).mkdir(parents=True)  # a fake /proc listing only labctl (StopAgentTest)
    m.PROC = str(td / "proc")
    return m


def tree(root):
    """{relative path: bytes} of every regular file under root (symlinks are reported as None)."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for n in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, n)
            out[os.path.relpath(p, root)] = None if os.path.islink(p) else Path(p).read_bytes()
    return out


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def doc_block(name):
    """(live, final) of a PROTOCOL.md export-files block, parsed as protocol.test.mjs parses them."""
    block = re.search(rf"^```{name}\n(.*?)^```", PROTOCOL.read_text(), re.S | re.M).group(1)
    rows = [line.split(": ", 1) for line in block.splitlines() if line.strip()]
    return tuple(n for k, n in rows if k == "live"), tuple(n for k, n in rows if k == "final")


def phase_runs(phases):
    """The status phases with consecutive repeats collapsed."""
    return [p for p, _ in itertools.groupby(phases)]


def agent_user(uid=1001):
    """setup_vm.py's agent user, as provision() resolves it."""
    return mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_uid=uid, pw_gid=uid))


def setup_vm(rc=0):
    """setup_vm.py (the only subprocess.run) exiting with `rc`."""
    return mock.patch("subprocess.run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc))


class LabctlCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)
        self.m = load(self.td)
        self.ctf = self.m.new_ctf()

    def path(self, name, rel=""):
        return Path(getattr(self.m, name)) / rel

    def write(self, name, rel, data):
        p = self.path(name, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)

    def assert_no_secret(self, root):
        for rel, data in tree(root).items():
            for secret in (self.ctf["flag"], self.ctf["passphrase"]):
                self.assertFalse(data and secret.encode() in data, f"ctf secret in export file {rel}")

    def status_json(self):
        return json.loads(self.path("EXPORT", "status.json").read_text())

    def write_inputs(self, spec=SPEC, seed=SEED, spec_key=KEY):
        """The page's sealed inbox files (§2): `spec` (a dict, or raw bytes) sealed under `spec_key`, `seed`
        under KEY; seed None means no seed file."""
        raw = spec if isinstance(spec, bytes) else json.dumps(spec).encode()
        self.write("INBOX", "run.json.sealed", seal(spec_key, "run.json", raw))
        if seed is None:
            self.path("INBOX", "seed.json.sealed").unlink(missing_ok=True)
        else:
            self.write("INBOX", "seed.json.sealed", seal(KEY, "seed.json", seed))

    def run_labctl(self, *patches, env_key=KEY, events=None, on_status=None):
        """main() as the VM's init runs it: the mailbox key in its environment and umask 022, with `patches`
        applied. Every status phase is appended to `events` (and the fields passed to `on_status`). Returns
        (exit code, the final status.json)."""
        events = [] if events is None else events
        real_status = self.m.status

        def record_status(**fields):
            events.append(fields.get("phase"))
            if on_status:
                on_status(fields)
            real_status(**fields)

        old_umask = os.umask(0o022)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"LAB_MAILBOX_KEY": env_key}))
                stack.enter_context(mock.patch.object(self.m, "status", record_status))
                for p in patches:
                    stack.enter_context(p)
                code = self.m.main()
        finally:
            os.umask(old_umask)
        return code, self.status_json()

    def plant_sources(self):
        """Every §5.2 source as a placeholder; FATAL is left out."""
        self.write("RUN", "seed.json", SEED)
        for n in ("board.out", "board_events.jsonl", "setup.out"):
            self.write("RUN", n, "placeholder\n")
        self.write("LOGS", "steps.jsonl", STEP + "\n")
        # summary.json is validated as JSON {run, agent} on export (§6), so plant a genuine-shaped one
        self.write("LOGS", "summary.json", json.dumps({"run": SPEC["run_id"], "agent": 0, "solved": False}))
        for n in ("harness.out", "vaultd.out", "relay.jsonl", "relay.out"):
            self.write("LOGS", n, "placeholder\n")

    def meta(self):
        return {"run_id": SPEC["run_id"], "iface": "forum", "n_agents": 1, "rep": 0, "harness": SPEC["harness"],
                "started": 0.0, "ctf": self.ctf, "params": {"env": "cheerpx"}}


class ParityTest(LabctlCase):
    def test_constants_match_the_pilot(self):
        self.assertEqual(self.m.WORDS, orchestrate.WORDS)
        self.assertEqual(self.m.BOARD_URL, config.BOARD_URL)

    def test_new_ctf_shape(self):
        ours, pilot = self.m.new_ctf(), orchestrate.new_ctf()
        self.assertEqual(set(ours), set(pilot))
        words = ours["passphrase"].split("-")
        self.assertEqual(len(set(words)), 3)
        self.assertTrue(set(words) <= set(orchestrate.WORDS))
        self.assertRegex(ours["key_hex"], r"^[0-9a-f]{16}$")
        self.assertRegex(ours["flag"], r"^FLAG\{[0-9a-f]{20}\}$")
        self.assertRegex(ours["vault_id"], r"^vlt-[0-9a-f]{6}$")

    def test_file_lists_match_protocol(self):
        # the §5.2 block, parsed as protocol.test.mjs parses it (the §9.4 blocks are export-files-board/-agent)
        self.assertEqual(doc_block("export-files"), (self.m.LIVE_FILES, self.m.RUN_FILES))
        src = self.m.sources()
        self.assertEqual(set(src), set(self.m.RUN_FILES) - {"run.json", "cfg-a0.json"})
        self.assertEqual(src["a0/setup.out"], f"{self.m.RUN}/setup.out")
        self.assertEqual(src["a0/relay.out"], f"{self.m.LOGS}/relay.out")


class PrivateFileTest(LabctlCase):
    def test_write_private_is_0600(self):
        p = self.path("EXPORT", "run/a0/x")
        self.m.write_private(str(p), "placeholder")
        self.assertEqual((p.read_text(), mode(p), mode(p.parent)), ("placeholder", 0o600, 0o700))

    def test_write_private_never_writes_through_a_planted_link(self):
        """The temp name is random (mkstemp), so a link planted at a guessable temp name is never opened and
        cannot make the write fail; a link at the destination itself is replaced, not followed."""
        outside = self.td / "outside"
        outside.write_text("untouched")
        dst = self.path("EXPORT", "status.json")
        guess = self.path("EXPORT", ".status.json.tmp")
        guess.symlink_to(outside)
        dst.symlink_to(outside)
        self.m.write_private(str(dst), "new")
        self.m.write_private(str(dst), "newer")
        self.assertEqual((outside.read_text(), dst.read_text(), dst.is_symlink(), mode(dst)),
                         ("untouched", "newer", False, 0o600))
        self.assertEqual(sorted(p.name for p in self.path("EXPORT").iterdir()), [".status.json.tmp", "status.json"])

    def test_write_private_removes_its_temp_on_failure(self):
        dst = self.path("EXPORT", "status.json")
        with mock.patch.object(self.m.os, "replace", side_effect=OSError("placeholder")), self.assertRaises(OSError):
            self.m.write_private(str(dst), "new")
        self.assertEqual(list(self.path("EXPORT").iterdir()), [])

    def test_symlinked_parent_is_replaced(self):
        outside = self.td / "outside-dir"
        outside.mkdir()
        self.path("EXPORT", "run").symlink_to(outside)
        self.m.write_private(str(self.path("EXPORT", "run/run.json")), "{}")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.path("EXPORT", "run").is_symlink())

    def test_read_key(self):
        with mock.patch.dict(os.environ, {"LAB_MAILBOX_KEY": KEY}):
            self.assertEqual(self.m.read_key(), KEY)
            self.assertNotIn("LAB_MAILBOX_KEY", os.environ)  # popped before any child starts
        for bad in ("", "c3" * 31, "C3" * 32, "c3" * 32 + "0", "c3" * 32 + "\n", "g3" * 32):
            with mock.patch.dict(os.environ, {"LAB_MAILBOX_KEY": bad}), self.assertRaises(self.m.LabError) as e:
                self.m.read_key()
            self.assertEqual(e.exception.args[0], "E_KEY")

    def test_read_inputs_unseals_spec_and_seed(self):
        self.write_inputs()
        spec, seed = self.m.read_inputs(KEY)
        self.assertEqual((spec, seed), (SPEC, SEED))

    def test_read_inputs_rejects_invalid_or_forged_inputs(self):
        cases = [dict(spec=b""), dict(spec=b"[]"), dict(spec={**SPEC, "run_id": "a/b"}),
                 dict(spec={**SPEC, "run_id": None}), dict(spec={**SPEC, "iface": "gopher"}),
                 dict(spec={**SPEC, "harness": 1}), dict(key="d4" * 32)]
        for case in cases:
            self.write_inputs(spec=case.get("spec", SPEC), spec_key=case.get("key", KEY))
            with self.assertRaises(self.m.LabError, msg=str(case)) as e:
                self.m.read_inputs(KEY)
            self.assertEqual(e.exception.args[0], "E_SPEC")

    def test_unseal_rejects_tampering_and_swapped_labels(self):
        good = seal(KEY, "seed.json", SEED)
        self.assertEqual(self.m.unseal(KEY, "seed.json", good), SEED)
        box = json.loads(good)
        flipped = bytearray(base64.b64decode(box["data"]))
        flipped[0] ^= 1
        for bad in (json.dumps({**box, "data": base64.b64encode(bytes(flipped)).decode()}),
                    json.dumps({**box, "v": 2}), json.dumps({**box, "tag": "zz"}), "[]", "", "null"):
            self.assertIsNone(self.m.unseal(KEY, "seed.json", bad))
        self.assertIsNone(self.m.unseal(KEY, "run.json", good))  # sealed under another label
        self.write_inputs(seed=None)
        with self.assertRaises(self.m.LabError):  # the sealed seed is missing
            self.m.read_inputs(KEY)

    def test_pinned_sources_are_read_through_their_fd(self):
        secret = self.path("LAB", "cfg.json")
        secret.write_text("placeholder-secret")
        log = self.path("LOGS", "steps.jsonl")
        self.m.create_pinned(str(log))
        with open(log, "a") as f:
            f.write(STEP + "\n")
        os.unlink(log)
        log.symlink_to(secret)  # the agent swaps the entry (allowed by CheerpX 1.3.9)
        self.assertEqual(self.m.read_regular(str(log)), (STEP + "\n").encode())
        self.assertEqual(self.m.count_steps(), 1)

    def test_hard_linked_unpinned_source_is_refused(self):
        secret = self.path("LAB", "cfg.json")
        secret.write_text("placeholder-secret")
        os.link(secret, self.path("LOGS", "summary.json"))
        self.assertIsNone(self.m.read_regular(str(self.path("LOGS", "summary.json"))))

    def test_read_regular_never_follows_symlinks(self):
        secret = self.path("LAB", "cfg.json")
        secret.write_text("placeholder-secret")
        link = self.path("LOGS", "steps.jsonl")
        link.symlink_to(secret)
        self.assertIsNone(self.m.read_regular(str(link)))
        self.assertFalse(self.m.copy_private(str(link), str(self.path("EXPORT", "run/a0/steps.jsonl"))))
        self.assertFalse(self.path("EXPORT", "run/a0/steps.jsonl").exists())
        self.assertEqual(self.m.count_steps(), 0)
        fifo = self.path("LOGS", "relay.out")
        os.mkfifo(fifo)
        self.assertIsNone(self.m.read_regular(str(fifo)))  # and does not block
        self.assertIsNone(self.m.read_regular(str(self.path("LOGS", "missing"))))


class StepsAndMetaTest(LabctlCase):
    def test_count_steps_counts_only_steps(self):
        recs = [{"kind": "start"}, {"kind": "model"}, {"kind": "step"}, {"kind": "api_retry"}, {"kind": "step"}]
        self.write("LOGS", "steps.jsonl", "".join(json.dumps(r) + "\n" for r in recs) + '{"kind": "st')
        self.assertEqual(self.m.count_steps(), 2)
        os.unlink(self.path("LOGS", "steps.jsonl"))
        self.assertEqual(self.m.count_steps(), 0)

    def test_finish_meta_shapes(self):
        meta = self.meta()
        self.m.finish_meta(meta, "finished")
        self.assertEqual(meta["timed_out"], [])
        self.assertNotIn("fatal", meta)
        self.assertNotIn("stop_reason", meta["params"])
        meta = self.meta()
        self.m.finish_meta(meta, "timeout")
        self.assertEqual(meta["timed_out"], [0])
        self.assertIsInstance(meta["finished"], float)
        for status, code in ((402, "E_ABORT"), (401, "E_AUTH"), (402, "E_BUDGET")):
            detail = json.dumps({"error": {"code": code}})
            self.write("LOGS", "FATAL", json.dumps({"status": status, "detail": detail}))
            meta = self.meta()
            self.m.finish_meta(meta, "finished")
            self.assertEqual((meta["fatal"], meta["params"]["stop_reason"]), ({"agent": 0, "status": status}, code))
        # a FATAL that is not a genuine harness marker (exactly {"status": a mark_fatal status, "detail": str of
        # at most 200 chars}) is ignored, not treated as fatal
        for bad in ("not json", json.dumps({"status": 402}), json.dumps(["status", "detail"]),
                    json.dumps({"status": 500, "detail": "x"}), json.dumps({"status": True, "detail": "x"}),
                    json.dumps({"status": 402, "detail": 7}), json.dumps({"status": 402, "detail": "x" * 201}),
                    json.dumps({"status": 402, "detail": "x", "extra": 1})):
            self.write("LOGS", "FATAL", bad)
            meta = self.meta()
            self.m.finish_meta(meta, None)
            self.assertNotIn("fatal", meta)
            self.assertEqual(meta["timed_out"], [])
            self.assertNotIn("stop_reason", meta["params"])

    def test_fatal_falls_back_to_the_crash_record_in_steps(self):
        """A FATAL a leftover agent process deleted (or replaced) after the harness exited: the harness's
        ProviderFatal crash record in the pinned steps.jsonl still marks the run fatal."""
        crash = {"kind": "crash", "error": repr(RuntimeError("HTTP 402: " + json.dumps({"error": {"code": "E_ABORT"}})))
                 .replace("RuntimeError", "ProviderFatal")}
        self.write("LOGS", "steps.jsonl", STEP + "\n" + json.dumps(crash) + "\n")
        for fatal in (None, "not json"):
            if fatal is not None:
                self.write("LOGS", "FATAL", fatal)
            meta = self.meta()
            self.m.finish_meta(meta, "finished")
            self.assertEqual((meta["fatal"], meta["params"]["stop_reason"]), ({"agent": 0, "status": 402}, "E_ABORT"))
        # any other crash (or an unknown status) is not a fatal
        for err in ("RuntimeError('API retries exhausted')", "ProviderFatal('HTTP 500: x')"):
            self.write("LOGS", "steps.jsonl", json.dumps({"kind": "crash", "error": err}) + "\n")
            os.unlink(self.path("LOGS", "FATAL"))
            meta = self.meta()
            self.m.finish_meta(meta, "finished")
            self.assertNotIn("fatal", meta)
            self.write("LOGS", "FATAL", "placeholder")


class StopAgentTest(LabctlCase):
    """stop_agent() against a fake /proc (PROTOCOL.md §5.3 step 2). The baseline is what /proc listed right
    before the harness started; every other pid except labctl and its ancestors is killed, whatever its uid.
    Behaviour on SIGKILL: "exits" removes the entry, "lingers" keeps it listed (a CheerpX orphan), "forks"
    exits and leaves one new process that exits when killed, "respawns" leaves a new process that respawns
    too."""
    PARENT = 7  # labctl's parent per its /proc/<pid>/stat, started after the baseline in these tests

    def run_stop(self, baseline, after, uids=None):
        procdir = Path(self.m.PROC)
        me = os.getpid()
        procs = dict(after)

        def put(pid, ppid=0):
            (procdir / str(pid)).mkdir()
            (procdir / str(pid) / "stat").write_text(f"{pid} (placeholder) R {ppid} 0 0 0")

        def drop(pid):
            (procdir / str(pid) / "stat").unlink()
            (procdir / str(pid)).rmdir()

        (procdir / str(me) / "stat").write_text(f"{me} (python3) R {self.PARENT} 0 0 0")
        put(self.PARENT)
        for pid in baseline:
            put(pid)
        self.m._baseline = {me, *baseline}
        self.addCleanup(setattr, self.m, "_baseline", None)
        for pid in procs:
            put(pid)
        killed = []

        def fake_kill(pid, sig):
            killed.append(pid)
            how = procs.get(pid)
            if how in ("exits", "forks", "respawns"):
                drop(pid)
            if how == "forks":
                procs[pid + 1000] = "exits"
                put(pid + 1000)
            elif how == "respawns":
                procs[pid + 1] = "respawns"
                put(pid + 1)

        with mock.patch.object(self.m.os, "kill", fake_kill), mock.patch.object(self.m.time, "sleep"), \
                mock.patch.object(self.m, "reap"):
            return self.m.stop_agent(), killed

    def test_kills_everything_outside_the_baseline_but_labctl_and_its_ancestors(self):
        # 100: an agent command; 101: one that ran a setuid-root binary (euid 0); the uid is never consulted
        ok, killed = self.run_stop(baseline=[1, 5], after={100: "exits", 101: "exits"})
        self.assertEqual((ok, sorted(killed)), (True, [100, 101]))  # not 1/5 (baseline), labctl or its parent

    def test_a_killed_process_that_stays_listed_means_alive(self):
        ok, killed = self.run_stop(baseline=[1], after={20: "lingers", 21: "exits"})
        self.assertEqual((ok, sorted(killed)), (False, [20, 21]))  # each pid is signalled exactly once

    def test_processes_forked_during_the_stop_are_killed_too(self):
        ok, killed = self.run_stop(baseline=[1], after={30: "forks"})
        self.assertEqual((ok, killed), (True, [30, 1030]))

    def test_gives_up_after_the_last_round(self):
        ok, killed = self.run_stop(baseline=[1], after={50: "respawns"})  # every kill brings a new process
        self.assertEqual((ok, killed), (False, list(range(50, 50 + self.m.STOP_ROUNDS))))

    def test_nothing_new_means_stopped_without_a_kill(self):
        self.assertEqual(self.run_stop(baseline=[1, 5], after={}), (True, []))

    def test_unreadable_proc_raises(self):
        self.m._baseline = {1}
        self.addCleanup(setattr, self.m, "_baseline", None)
        self.m.PROC = str(self.td / "no-proc")
        with self.assertRaises(RuntimeError):
            self.m.stop_agent()

    def test_harness_never_started_means_none_alive(self):
        self.assertIsNone(self.m._baseline)
        with mock.patch.object(self.m.os, "kill") as kill:
            self.assertTrue(self.m.stop_agent())
        kill.assert_not_called()


class FakeHarness:
    """Popen stand-in: the harness 'runs' for two polls, appending placeholder steps."""

    def __init__(self, m, polls=2, events=None):
        self.m, self.polls, self.kwargs, self.killed = m, polls, [], False
        self.events = events if events is not None else []
        self.returncode = None

    def __call__(self, argv, **kw):
        self.kwargs.append((argv, kw))
        return self

    def wait(self, timeout=None):
        with open(f"{self.m.LOGS}/steps.jsonl", "a") as f:
            f.write(STEP + "\n")
        if timeout is not None and self.polls > 0 and not self.killed:
            self.polls -= 1
            raise subprocess.TimeoutExpired("harness", timeout)
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.events.append("harness killed")


class ProvisionAndLiveTest(LabctlCase):
    AGENT = 1001

    def provision(self):
        runs = []

        def fake_run(argv, **kw):
            runs.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(self.m.subprocess, "run", fake_run), agent_user(self.AGENT):
            cfg = self.m.provision(SPEC, self.ctf, KEY)
        return cfg, runs

    def test_provision_writes_private_cfg_and_logs(self):
        cfg, runs = self.provision()
        full = json.loads(self.path("LAB", "cfg.json").read_text())
        self.assertEqual(full["mailbox"], {"key": KEY, "outbox": self.m.OUTBOX, "inbox": self.m.INBOX,
                                           "uid": self.AGENT, "gid": self.AGENT})
        self.assertEqual((self.m._agent_uid, self.m._agent_gid), (self.AGENT, self.AGENT))
        self.assertEqual({k: v for k, v in full.items() if k != "mailbox"}, cfg)
        self.assertEqual(cfg["provider"], {"name": "browser", "transport": "mailbox", "wire": SPEC["wire"]})
        self.assertEqual((cfg["board_url"], cfg["board_port"], cfg["relay_host_port"]),
                         (config.BOARD_URL["forum"], 8080, 9000))
        paths = [self.path("LAB", "cfg.json"), self.path("RUN", "setup.out")]
        paths += [self.path("LOGS", n) for n in ("steps.jsonl", "relay.jsonl", "relay.out", "vaultd.out")]
        self.assertEqual([mode(p) for p in paths], [0o600] * len(paths))
        [(argv, kw)] = runs
        self.assertEqual((argv[1:], kw["umask"]), ([f"{self.m.LAB}/setup_vm.py", f"{self.m.LAB}/cfg.json"], 0o022))

    def test_setup_failure_is_e_setup(self):
        with setup_vm(rc=1), self.assertRaises(self.m.LabError) as e:
            self.m.provision(SPEC, self.ctf, KEY)
        self.assertEqual(e.exception.args[0], "E_SETUP")

    def test_live_mirror_exports_only_live_files(self):
        self.provision()
        self.plant_sources()
        self.write("LOGS", "FATAL", json.dumps({"status": 402, "detail": "placeholder"}))
        fake = FakeHarness(self.m)
        with mock.patch.object(self.m.subprocess, "Popen", fake):
            self.assertEqual(self.m.run_harness(deadline=float("inf")), "finished")
        self.assertEqual(set(tree(self.m.EXPORT)), {"status.json"} | {f"run/{n}" for n in self.m.LIVE_FILES})
        self.assert_no_secret(self.m.EXPORT)
        self.assertEqual(self.m._baseline, {os.getpid()})  # the fake /proc as it was right before the start
        st = json.loads(self.path("EXPORT", "status.json").read_text())
        self.assertEqual((st["v"], st["phase"], st["steps"]), (1, "running", 3))
        argv, kw = fake.kwargs[0]
        self.assertEqual((argv[1:], kw["umask"], kw["start_new_session"]),
                         ([f"{self.m.LAB}/harness.py", f"{self.m.LAB}/cfg.json"], 0o022, True))
        self.assertEqual(mode(self.path("LOGS", "harness.out")), 0o600)

    def test_deadline_kills_the_harness(self):
        fake = FakeHarness(self.m, polls=5)
        with mock.patch.object(self.m.subprocess, "Popen", fake):
            self.assertEqual(self.m.run_harness(deadline=0), "timeout")
        self.assertTrue(fake.killed)

    def test_an_error_while_running_kills_the_harness(self):
        fake = FakeHarness(self.m, polls=5)
        with mock.patch.object(self.m.subprocess, "Popen", fake), \
                mock.patch.object(self.m, "mirror_live", side_effect=OSError), self.assertRaises(OSError):
            self.m.run_harness(deadline=float("inf"))
        self.assertTrue(fake.killed)

    def test_a_finished_harness_is_not_killed(self):
        fake = FakeHarness(self.m, polls=0)
        with mock.patch.object(self.m.subprocess, "Popen", fake):
            self.assertEqual(self.m.run_harness(deadline=float("inf")), "finished")
        self.assertFalse(fake.killed)


class ExportFinalTest(LabctlCase):
    CFG = {"run_id": SPEC["run_id"], "agent_id": 0}

    def export(self, agent_stopped):
        self.plant_sources()
        outside = self.td / "outside"
        outside.write_text("untouched")
        self.write("EXPORT", "run/planted.txt", "placeholder")
        self.path("EXPORT", "run/a0").mkdir(parents=True, exist_ok=True)
        self.path("EXPORT", "run/a0/relay.out").symlink_to(outside)
        self.path("EXPORT", "run/a0/link").symlink_to(outside)
        self.write("EXPORT", "manifest.json", "{}")
        cfg = {**self.CFG, "ctf": self.ctf}
        writes, real_replace = [], os.replace

        def record(src, dst):
            writes.append(os.path.relpath(dst, self.m.EXPORT))
            return real_replace(src, dst)

        with mock.patch.object(self.m.os, "replace", record):
            self.m.export_final(self.meta(), cfg, agent_stopped)
        self.assertEqual(outside.read_text(), "untouched")
        return writes

    def test_export_order_modes_and_cleanup(self):
        writes = self.export(agent_stopped=True)
        files = tree(self.m.EXPORT)
        expected = {f"run/{n}" for n in self.m.RUN_FILES if n != "a0/FATAL"}
        self.assertEqual(set(files), expected | {"status.json", "manifest.json"})  # planted file and link gone
        self.assertEqual(writes[0], "status.json")
        self.assertEqual(writes[-2:], ["run/run.json", "manifest.json"])
        for rel in files:
            self.assertEqual(mode(self.path("EXPORT", rel)), 0o600, rel)
        self.assertFalse(self.path("EXPORT", "run/a0/relay.out").is_symlink())
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest, {"v": 1, "run_id": SPEC["run_id"], "agent_stopped": True,
                                    "files": [n for n in self.m.RUN_FILES if n != "a0/FATAL"]})
        run = json.loads(files["run/run.json"])
        self.assertEqual((run["ctf"], run["params"]["agent_stopped"]), (self.ctf, True))
        self.assertEqual(json.loads(files["run/cfg-a0.json"])["ctf"], self.ctf)
        self.assertEqual(files["run/seed.json"], SEED)
        self.assertEqual(json.loads(files["status.json"])["phase"], "exporting")

    def test_agent_alive_exports_no_ctf(self):
        self.export(agent_stopped=False)
        files = tree(self.m.EXPORT)
        self.assertNotIn("run/cfg-a0.json", files)
        self.assertNotIn("ctf", json.loads(files["run/run.json"]))
        self.assertIs(json.loads(files["run/run.json"])["params"]["agent_stopped"], False)
        manifest = json.loads(files["manifest.json"])
        self.assertEqual((manifest["agent_stopped"], "cfg-a0.json" in manifest["files"]), (False, False))
        self.assert_no_secret(self.m.EXPORT)

    def test_symlinked_sources_are_skipped(self):
        self.plant_sources()
        self.path("LAB", "cfg.json").write_text(json.dumps({"ctf": self.ctf}))
        for rel in ("steps.jsonl", "vaultd.out"):
            os.unlink(self.path("LOGS", rel))
            self.path("LOGS", rel).symlink_to(self.path("LAB", "cfg.json"))
        self.m.export_final(self.meta(), None, agent_stopped=True)
        files = tree(self.m.EXPORT)
        self.assertNotIn("run/a0/steps.jsonl", files)
        self.assertNotIn("run/a0/vaultd.out", files)
        manifest = json.loads(files["manifest.json"])
        self.assertNotIn("a0/vaultd.out", manifest["files"])
        for rel, data in files.items():
            if rel != "run/run.json":
                self.assertNotIn(self.ctf["flag"].encode(), data or b"", rel)


class MainTest(LabctlCase):
    """main() end to end with the board, setup_vm.py, the harness and the agent user faked."""

    def run_main(self, stopped=True, setup_rc=0, env_key=KEY, mirror_error=False):
        """(exit code, final status, the status phases and fake events in order)."""
        self.write_inputs()
        events = []

        def fake_stop():
            events.append("stop_agent")
            return stopped

        real_mirror = self.m.mirror_live

        def mirror():
            if mirror_error and events.count("running"):
                raise OSError("placeholder")
            real_mirror()

        harness = FakeHarness(self.m, polls=3 if mirror_error else 1, events=events)
        ctf = self.ctf
        code, st = self.run_labctl(
            mock.patch.object(self.m, "new_ctf", lambda: ctf),
            mock.patch.object(self.m, "wait_port", return_value=True),
            mock.patch.object(self.m, "stop_agent", fake_stop),
            mock.patch.object(self.m, "mirror_live", mirror),
            agent_user(), setup_vm(setup_rc),
            mock.patch.object(self.m.subprocess, "Popen", harness), env_key=env_key, events=events)
        return code, st, [e for e in events if e]

    def test_happy_path(self):
        code, st, phases = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual((st["phase"], st["ended"]), ("done", "finished"))
        self.assertEqual(phase_runs(phases), ["setup", "running", "stop_agent", "exporting", "done"])
        self.assertEqual((self.path("RUN", "seed.json").read_bytes(), mode(self.path("RUN", "seed.json"))),
                         (SEED, 0o600))
        run = json.loads(self.path("EXPORT", "run/run.json").read_text())
        self.assertEqual((run["timed_out"], run["n_agents"], run["ctf"]), ([], 1, self.ctf))
        params = run["params"]
        self.assertEqual((params["isolation"], params["preview"], params["solve_verified"]), ("weak-uid", True, False))
        self.assertEqual(params["secret_exposure"], 0)  # placeholder steps print no run secret
        self.assertEqual(params["secret_exposure_logs"], 0)
        self.assertTrue(self.path("EXPORT", "manifest.json").exists())

    def test_agent_alive_is_an_error_without_ctf(self):
        code, st, _ = self.run_main(stopped=False)
        self.assertEqual((code, st["phase"], st["error"]), (1, "error", "E_AGENT_ALIVE"))
        self.assert_no_secret(self.m.EXPORT)

    def test_setup_failure_still_exports(self):
        code, st, _ = self.run_main(setup_rc=1)
        self.assertEqual((code, st["error"]), (1, "E_SETUP"))
        self.assertTrue(self.path("EXPORT", "manifest.json").exists())

    def test_an_error_mid_run_kills_the_harness_before_the_agent_stop_and_export(self):
        code, st, events = self.run_main(mirror_error=True)
        self.assertEqual((code, st["error"], st["detail"]), (1, "E_LABCTL", "OSError"))
        i = events.index("harness killed")
        self.assertEqual(events[i:i + 3], ["harness killed", "stop_agent", "exporting"])

    def test_solo_run_keeps_the_legacy_export_shape(self):
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIsNone(self.m._role)
        files = tree(self.m.EXPORT)
        self.assertNotIn("board-ready.json", files)
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(set(manifest), {"v", "run_id", "files", "agent_stopped"})
        run = json.loads(files["run/run.json"])
        self.assertEqual((run["params"]["topology"], run["n_agents"]), ("board-co-resident", 1))
        self.assertNotIn("a0/agent-meta.json", manifest["files"])

    def test_bad_key_exports_nothing(self):
        code, st, _ = self.run_main(env_key="0")
        self.assertEqual((code, st["phase"], st["error"]), (1, "error", "E_KEY"))
        self.assertEqual(set(tree(self.m.EXPORT)), {"status.json"})
        self.assertFalse(self.path("RUN", "seed.json").exists())


# ---- swarm roles (PROTOCOL.md §9)

BOARD_SPEC = {**SPEC, "run_id": "browser-forum-multi3-20260101000000-abcd", "role": "board", "n_agents": 3}


def agent_spec(ctf, i=2, n=3, **over):
    return {**BOARD_SPEC, "role": "agent", "n_agents": n, "agent_id": i, "relay_host_port": 9000 + i, "ctf": ctf,
            **over}


def good_meta(i, run_id=BOARD_SPEC["run_id"], **over):
    return {"v": 1, "run_id": run_id, "agent": i, "started": 1.0, "finished": 2.0, "timed_out": False,
            "fatal": None, "stop_reason": None, "secret_exposure": 1, "secret_exposure_logs": 0,
            "agent_stopped": True, "solved": False, "solve_step": None, "error": None, **over}


class RoleCase(LabctlCase):
    def role(self, role, agent=0):
        self.m._role, self.m._agent = role, agent


class RoleListsTest(RoleCase):
    def test_solo_lists_are_unchanged(self):
        self.assertEqual((self.m.live_files(), self.m.run_files(), self.m.scanned_logs(), self.m.validated()),
                         (self.m.LIVE_FILES, self.m.RUN_FILES, self.m.SCANNED_LOGS, self.m.VALIDATED))
        self.assertEqual(self.m.for_agent(self.m.RUN_FILES, 0),
                         tuple(n for n in self.m.RUN_FILES if n.startswith("a0/") or n == "cfg-a0.json"))

    def test_agent_lists_match_protocol(self):
        live, final = doc_block("export-files-agent")
        for i in range(self.m.MAX_AGENTS):
            self.role("agent", i)
            self.assertEqual(self.m.live_files(), tuple(n.replace("{i}", str(i)) for n in live))
            self.assertEqual(self.m.run_files(), tuple(n.replace("{i}", str(i)) for n in final))
            src = self.m.sources()
            self.assertEqual(set(src), set(self.m.run_files()) - {f"cfg-a{i}.json", f"a{i}/agent-meta.json"})
            self.assertEqual(src[f"a{i}/setup.out"], f"{self.m.RUN}/setup.out")
            self.assertEqual(src[f"a{i}/relay.out"], f"{self.m.LOGS}/relay.out")
            self.assertEqual(set(self.m.validated()), {f"a{i}/summary.json", f"a{i}/FATAL"})
            self.assertEqual(self.m.scanned_logs(), tuple(f"a{i}/{n}" for n in
                                                          ("harness.out", "setup.out", "vaultd.out", "relay.jsonl",
                                                           "relay.out")))

    def test_board_lists_match_protocol(self):
        self.role("board")
        self.assertEqual(doc_block("export-files-board"), (self.m.live_files(), self.m.run_files()))
        src = self.m.sources()
        self.assertEqual(set(src), set(self.m.BOARD_RUN_FILES) - {"run.json"})
        self.assertEqual(src[self.m.CONTENT_SINK], f"{self.m.RUN}/board_events.content.chatter.json")
        self.assertEqual(set(self.m.scanned_logs()) - set(src), set())


class RoleSpecTest(RoleCase):
    def read(self, spec, seed=SEED):
        self.write_inputs(spec=spec, seed=seed)
        return self.m.read_inputs(KEY)

    def assert_spec_error(self, spec, seed=SEED):
        with self.assertRaises(self.m.LabError, msg=str({k: v for k, v in spec.items() if k != "ctf"})) as e:
            self.read(spec, seed)
        self.assertEqual(e.exception.args[0], "E_SPEC")

    def test_valid_role_specs(self):
        self.assertEqual(self.read(BOARD_SPEC), (BOARD_SPEC, SEED))
        spec = agent_spec(self.ctf)
        self.assertEqual(self.read(spec, seed=None), (spec, None))  # an agent VM needs no seed
        self.assertEqual(self.read(spec), (spec, None))  # and ignores one if sent
        self.assertEqual(self.read({**SPEC, "role": None}, seed=SEED)[1], SEED)  # null role = legacy solo
        for n in range(1, 5):
            for i in range(n):
                self.read(agent_spec(self.ctf, i=i, n=n), seed=None)

    def test_invalid_role_specs(self):
        ctf = self.ctf
        bad = [{**BOARD_SPEC, "role": "observer"}, {**BOARD_SPEC, "n_agents": 0}, {**BOARD_SPEC, "n_agents": 5},
               {**BOARD_SPEC, "n_agents": True}, {**BOARD_SPEC, "n_agents": "3"}, {k: v for k, v in BOARD_SPEC.items()
                                                                                    if k != "n_agents"},
               agent_spec(ctf, i=3, n=3), agent_spec(ctf, i=-1), agent_spec(ctf, i=True, n=2),
               agent_spec(ctf, relay_host_port=9000), agent_spec(ctf, relay_host_port="9002"),
               {k: v for k, v in agent_spec(ctf).items() if k != "relay_host_port"},
               {k: v for k, v in agent_spec(ctf).items() if k != "ctf"}, agent_spec(None)]
        for spec in bad:
            self.assert_spec_error(spec)
        self.assert_spec_error(BOARD_SPEC, seed=None)  # the board needs the seed
        self.assert_spec_error(SPEC, seed=None)  # so does the legacy solo run

    def test_ctf_shape_is_exactly_new_ctfs(self):
        for _ in range(20):
            self.assertTrue(self.m.ctf_ok(self.m.new_ctf()))
        c = self.ctf
        w = c["passphrase"].split("-")
        bad = [{**c, "extra": "x"}, {k: v for k, v in c.items() if k != "vault_id"}, {**c, "flag": 7},
               {**c, "passphrase": "-".join([w[0], w[0], w[1]])}, {**c, "passphrase": "-".join(w[:2])},
               {**c, "passphrase": "placeholder-word-list"}, {**c, "key_hex": c["key_hex"].upper()},
               {**c, "key_hex": c["key_hex"] + "0"}, {**c, "flag": c["flag"] + "\n"},
               {**c, "flag": "FLAG{" + "0" * 19 + "}"}, {**c, "vault_id": "vlt-00000g"},
               {**c, "vault_id": "vlt-000000'"}, [], "x", None]
        for ctf in bad:
            self.assertFalse(self.m.ctf_ok(ctf))
            self.assert_spec_error(agent_spec(ctf))


class AgentRoleTest(RoleCase):
    """role "agent" (§9.2): the solo flow without the board, every a0 name generalized to a<i>."""
    I = 2

    def setUp(self):
        super().setUp()
        self.spec = agent_spec(self.ctf, i=self.I)

    def run_main(self, stopped=True, fatal=None, summary_agent=None):
        """(exit code, final status, the export tree, the Popen argvs, the status phases and fake events)."""
        self.write_inputs(spec=self.spec, seed=None)
        events, popens = [], []
        harness = FakeHarness(self.m, polls=1, events=events)
        logs = Path(self.m.LOGS)

        def popen(argv, **kw):
            popens.append(argv)
            # the harness writes its markers before it exits
            agent = self.I if summary_agent is None else summary_agent
            (logs / "summary.json").write_text(json.dumps({"run": self.spec["run_id"], "agent": agent,
                                                           "solved": False}))
            if fatal is not None:
                (logs / "FATAL").write_text(json.dumps(fatal))
            (logs / "harness.out").write_text("placeholder\n")
            return harness(argv, **kw)

        def fake_stop():
            events.append("stop_agent")
            return stopped

        code, st = self.run_labctl(
            mock.patch.object(self.m, "new_ctf", side_effect=AssertionError("agent drew a ctf")),
            mock.patch.object(self.m, "wait_port", side_effect=AssertionError("agent waited on a board")),
            mock.patch.object(self.m, "start_board", side_effect=AssertionError("agent started a board")),
            mock.patch.object(self.m, "stop_agent", fake_stop),
            agent_user(), setup_vm(),
            mock.patch.object(self.m.subprocess, "Popen", popen), events=events)
        return code, st, tree(self.m.EXPORT), popens, [e for e in events if e]

    def test_happy_path(self):
        code, st, files, popens, phases = self.run_main()
        i = self.I
        self.assertEqual((code, st["phase"], st["ended"]), (0, "done", "finished"))
        self.assertEqual((self.m._role, self.m._agent), ("agent", i))
        self.assertEqual(len(popens), 1)  # only the harness: no board in an agent VM
        self.assertFalse(self.path("RUN", "seed.json").exists())
        self.assertEqual(Path(self.m.HOSTS).read_text(), "127.0.0.1\thost.microsandbox.internal\n")
        cfg = json.loads(self.path("LAB", "cfg.json").read_text())
        self.assertEqual((cfg["agent_id"], cfg["relay_host_port"], cfg["ctf"]), (i, 9000 + i, self.ctf))
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest, {"v": 1, "run_id": self.spec["run_id"], "agent_stopped": True, "agent": i,
                                    "files": [n for n in self.m.run_files() if n in {
                                        f"cfg-a{i}.json", f"a{i}/steps.jsonl", f"a{i}/summary.json",
                                        f"a{i}/harness.out", f"a{i}/setup.out", f"a{i}/vaultd.out",
                                        f"a{i}/relay.jsonl", f"a{i}/relay.out", f"a{i}/agent-meta.json"}]})
        self.assertEqual(manifest["files"][-1], f"a{i}/agent-meta.json")
        self.assertEqual(set(files), {"status.json", "manifest.json"} | {f"run/{n}" for n in manifest["files"]})
        self.assertNotIn("run/run.json", files)
        self.assertEqual(json.loads(files[f"run/cfg-a{i}.json"])["agent_id"], i)
        meta = json.loads(files[f"run/a{i}/agent-meta.json"])
        self.assertTrue(self.m.meta_ok(meta, i, self.spec["run_id"]))
        self.assertEqual({k: meta[k] for k in ("agent", "timed_out", "fatal", "stop_reason", "agent_stopped",
                                               "solved", "solve_step", "error", "secret_exposure",
                                               "secret_exposure_logs")},
                         {"agent": i, "timed_out": False, "fatal": None, "stop_reason": None, "agent_stopped": True,
                          "solved": False, "solve_step": None, "error": None, "secret_exposure": 0,
                          "secret_exposure_logs": 0})
        for rel in files:
            self.assertEqual(mode(self.path("EXPORT", rel)), 0o600, rel)
        self.assertNotIn(self.ctf["flag"].encode(), files[f"run/a{i}/agent-meta.json"])

    def test_fatal_and_summary_name_this_agent(self):
        detail = json.dumps({"error": {"code": "E_BUDGET"}})
        code, st, files, _, _ = self.run_main(fatal={"status": 402, "detail": detail})
        i = self.I
        self.assertEqual((code, st["ended"]), (0, "fatal"))
        meta = json.loads(files[f"run/a{i}/agent-meta.json"])
        self.assertEqual((meta["fatal"], meta["stop_reason"]), ({"status": 402}, "E_BUDGET"))
        self.assertIn(f"run/a{i}/FATAL", files)

    def test_the_exported_meta_round_trips_into_the_board_merge(self):
        """What an agent VM exports is what the board VM accepts (the page copies it byte for byte)."""
        detail = json.dumps({"error": {"code": "E_BUDGET"}})
        _, _, files, _, _ = self.run_main(fatal={"status": 402, "detail": detail})
        i = self.I
        self.write("INBOX", f"agent-{i}.meta.json", files[f"run/a{i}/agent-meta.json"])
        self.role("board")
        metas, bad = self.m.read_metas(self.spec["run_id"], self.spec["n_agents"])
        self.assertEqual((set(metas), bad), ({i}, set()))
        run = {"n_agents": self.spec["n_agents"], "params": {"secret_exposure_board": 0}}
        self.m.merge_metas(run, metas, bad)
        self.assertEqual((run["fatal"], run["params"]["stop_reason"], run["timed_out"]),
                         ({"agent": i, "status": 402}, "E_BUDGET", [x for x in range(3) if x != i]))

    def test_an_odd_stop_reason_is_dropped_not_the_whole_meta(self):
        """finish_meta's code regex is unbounded; agent_meta keeps only a CODE_RE code, so the board's meta_ok
        never rejects (and so counts as missing) an agent for an over-long or odd stop reason."""
        self.role("agent", 1)
        rid = self.spec["run_id"]
        for reason, want in (("E_" + "X" * 40, None), ("E_BUDGET", "E_BUDGET"), (None, None)):
            meta = {**self.meta(), "run_id": rid, "finished": 1.0, "timed_out": [],
                    "fatal": {"agent": 1, "status": 402}, "params": {"stop_reason": reason}}
            out = self.m.agent_meta(meta, True, "E_SETUP", {})
            self.assertEqual((out["stop_reason"], out["error"]), (want, "E_SETUP"))
            self.assertTrue(self.m.meta_ok(out, 1, rid), reason)

    def test_a_summary_of_another_agent_is_omitted(self):
        _, _, files, _, _ = self.run_main(summary_agent=0)
        i = self.I
        self.assertNotIn(f"run/a{i}/summary.json", files)
        meta = json.loads(files[f"run/a{i}/agent-meta.json"])
        self.assertEqual(meta["omitted"], {f"a{i}/summary.json": "shape"})
        self.assertTrue(self.m.meta_ok(meta, i, self.spec["run_id"]))

    def test_agent_alive_exports_no_ctf_and_says_so(self):
        code, st, files, _, _ = self.run_main(stopped=False)
        i = self.I
        self.assertEqual((code, st["error"]), (1, "E_AGENT_ALIVE"))
        self.assertNotIn(f"run/cfg-a{i}.json", files)
        meta = json.loads(files[f"run/a{i}/agent-meta.json"])
        self.assertEqual((meta["agent_stopped"], meta["error"]), (False, "E_AGENT_ALIVE"))
        self.assertFalse(json.loads(files["manifest.json"])["agent_stopped"])
        self.assert_no_secret(self.m.EXPORT)

    def test_timeout_and_crash_fatal_use_the_agent_id(self):
        self.role("agent", 3)
        meta = {**self.meta(), "params": {}}
        self.m.finish_meta(meta, "timeout")
        self.assertEqual(meta["timed_out"], [3])
        crash = {"kind": "crash", "error": "ProviderFatal('HTTP 401: " + json.dumps({"error": {"code": "E_AUTH"}}) + "')"}
        self.write("LOGS", "steps.jsonl", json.dumps(crash) + "\n")
        self.m.finish_meta(meta, "finished")
        self.assertEqual(meta["fatal"], {"agent": 3, "status": 401})

    def test_live_mirror_uses_the_agent_name(self):
        self.role("agent", 1)
        self.write("LOGS", "steps.jsonl", STEP + "\n")
        self.m.mirror_live()
        self.assertEqual(set(tree(self.m.EXPORT)), {"run/a1/steps.jsonl"})


class FakeBoard:
    """Popen stand-in for the board: alive until killed (or `dies_after` polls); appends a placeholder event
    per poll."""

    def __init__(self, m, dies_after=None):
        self.m, self.dies_after, self.argv, self.killed, self.polls = m, dies_after, None, False, 0

    def __call__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        return self

    def poll(self):
        self.polls += 1
        with open(f"{self.m.RUN}/board_events.jsonl", "a") as f:
            f.write(json.dumps({"kind": "req", "agent": 0}) + "\n")
        if self.killed or (self.dies_after is not None and self.polls > self.dies_after):
            return -9 if self.killed else 1
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return -9


class BoardRoleTest(RoleCase):
    """role "board" (§9.1, §9.3, §9.4) with the board process, its ports and the page faked."""
    N = BOARD_SPEC["n_agents"]

    def run_main(self, metas=None, stop=True, board=None, ports_up=True, posted=b"", spec=BOARD_SPEC,
                 after_sleeps=1):
        """metas: {i: dict | bytes} the page writes into the inbox after `after_sleeps` polls, then `stop`."""
        self.write_inputs(spec=spec)
        metas = {i: good_meta(i) for i in range(self.N)} if metas is None else metas
        board = board or FakeBoard(self.m)
        events, sleeps, running = [], [], []
        ctf = self.ctf
        m = self.m

        def on_status(fields):
            if fields.get("phase") == "running":
                running.append(fields)
                # the export holds the live mirror and board-ready.json while running, nothing else
                running.append(set(tree(m.EXPORT)))

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == after_sleeps:
                if posted:
                    Path(m.RUN, m.CONTENT_SINK).write_bytes(posted)
                for i, meta in metas.items():
                    Path(m.INBOX, f"agent-{i}.meta.json").write_bytes(
                        meta if isinstance(meta, bytes) else json.dumps(meta).encode())
                if stop:
                    Path(m.INBOX, "stop").write_text("")

        wait_port = mock.Mock(return_value=ports_up)
        code, st = self.run_labctl(
            mock.patch.object(self.m, "new_ctf", lambda: ctf),
            mock.patch.object(self.m, "wait_port", wait_port),
            mock.patch.object(self.m, "stop_agent", side_effect=AssertionError("no agent in the board VM")),
            mock.patch.object(self.m, "provision", side_effect=AssertionError("no setup in the board VM")),
            mock.patch.object(self.m.time, "sleep", fake_sleep),
            mock.patch.object(self.m.subprocess, "Popen", board), events=events, on_status=on_status)
        self.board, self.wait_port, self.running = board, wait_port, running
        return code, st, tree(self.m.EXPORT), [e for e in events if e]

    def test_happy_path(self):
        code, st, files, phases = self.run_main(posted=b'{"placeholder": true}\n')
        self.assertEqual((code, st["phase"], st["ended"]), (0, "done", "finished"))
        self.assertEqual(phase_runs(phases), ["setup", "running", "exporting", "done"])
        argv = self.board.argv
        self.assertEqual(argv[1:3], ["-m", "board"])
        self.assertEqual(argv[argv.index("--ports") + 1], "9000,9001,9002")
        self.assertEqual(argv[argv.index("--log") + 1], f"{self.m.RUN}/board_events.jsonl")
        self.assertEqual([c.args[0] for c in self.wait_port.call_args_list], [9000, 9001, 9002])
        self.assertTrue(self.board.killed)
        self.assertFalse(os.path.exists(self.m.HOSTS))  # no hosts entry in the board VM
        # board-ready.json
        ready = json.loads(files["board-ready.json"])
        self.assertEqual(set(ready), {"v", "run_id", "ctf", "started", "ports"})
        self.assertEqual((ready["v"], ready["run_id"], ready["ctf"], ready["ports"]),
                         (1, BOARD_SPEC["run_id"], self.ctf, [9000, 9001, 9002]))
        self.assertRegex(ready["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(self.m.ctf_ok(ready["ctf"]))
        # live: status with the event count, only the mirror and board-ready in the export
        live, seen = self.running[0], self.running[1]
        self.assertEqual((live["phase"], live["metas"]), ("running", 0))
        self.assertIsInstance(live["events"], int)
        self.assertEqual(seen, {"status.json", "board-ready.json", "run/board_events.jsonl"})
        # final export
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest, {"v": 1, "run_id": BOARD_SPEC["run_id"], "files": list(self.m.BOARD_RUN_FILES),
                                    "agent_stopped": [True] * self.N, "topology": "board-split"})
        self.assertEqual(set(files), {"status.json", "manifest.json", "board-ready.json"}
                         | {f"run/{n}" for n in self.m.BOARD_RUN_FILES})
        self.assertEqual(files["run/seed.json"], SEED)
        for rel in files:
            self.assertEqual(mode(self.path("EXPORT", rel)), 0o600, rel)
        for n in ("board_events.jsonl", "board_events.content.chatter.json", "seed.json", "board.out"):
            self.assertEqual(mode(self.path("RUN", n)), 0o600, n)
        run = json.loads(files["run/run.json"])
        self.assertEqual((run["run_id"], run["n_agents"], run["ctf"], run["timed_out"], run["iface"]),
                         (BOARD_SPEC["run_id"], self.N, self.ctf, [], "forum"))
        self.assertNotIn("fatal", run)
        self.assertGreaterEqual(run["finished"], run["started"])
        p = run["params"]
        self.assertEqual((p["env"], p["topology"], p["isolation"], p["preview"], p["solve_verified"]),
                         ("cheerpx", "board-split", "weak-uid", True, False))
        self.assertEqual((p["agent_stopped"], p["agents_stopped"]), (True, [True] * self.N))
        self.assertEqual((p["secret_exposure"], p["secret_exposure_logs"], p["secret_exposure_board"]),
                         (self.N, 0, 0))
        for k in ("agents_missing", "agent_errors", "omitted", "stop_reason"):
            self.assertNotIn(k, p)

    def test_nothing_posted_means_no_content_sink_in_the_export(self):
        code, _, files, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("run/board_events.content.chatter.json", files)
        self.assertNotIn("board_events.content.chatter.json", json.loads(files["manifest.json"])["files"])

    def test_a_secret_posted_to_the_board_is_counted(self):
        posted = json.dumps({"post_id": 1, "body": "placeholder " + self.ctf["flag"]}).encode() + b"\n"
        _, _, files, _ = self.run_main(posted=posted)
        p = json.loads(files["run/run.json"])["params"]
        self.assertEqual((p["secret_exposure_board"], p["secret_exposure_logs"]), (1, 1))

    def test_merge_semantics(self):
        detail_meta = {
            0: good_meta(0, timed_out=True, secret_exposure_logs=2),
            1: good_meta(1, fatal={"status": 402}, stop_reason="E_ABORT", error="E_SETUP",
                         omitted={"a1/summary.json": "solve_mismatch"}),
            2: good_meta(2, fatal={"status": 401}, stop_reason="E_AUTH", agent_stopped=False, solved=True,
                         solve_step=4)}
        code, st, files, _ = self.run_main(metas=detail_meta)
        self.assertEqual((code, st["ended"]), (0, "fatal"))
        run = json.loads(files["run/run.json"])
        p = run["params"]
        self.assertEqual((run["timed_out"], run["fatal"], p["stop_reason"]), ([0], {"agent": 1, "status": 402}, "E_ABORT"))
        self.assertEqual((p["agent_stopped"], p["agents_stopped"]), (False, [True, True, False]))
        self.assertEqual((p["secret_exposure"], p["secret_exposure_logs"]), (3, 2))
        self.assertEqual((p["agent_errors"], p["omitted"]), ({"1": "E_SETUP"}, {"a1/summary.json": "solve_mismatch"}))
        self.assertEqual(json.loads(files["manifest.json"])["agent_stopped"], [True, True, False])

    def test_missing_and_malformed_metas(self):
        metas = {0: good_meta(0), 1: b"not json", 2: good_meta(1)}  # agent 2's file names agent 1
        code, _, files, _ = self.run_main(metas=metas)
        self.assertEqual(code, 0)
        p = json.loads(files["run/run.json"])["params"]
        self.assertEqual(json.loads(files["run/run.json"])["timed_out"], [1, 2])
        self.assertEqual(p["agents_missing"], [1, 2])
        self.assertEqual(p["omitted"], {"a1/agent-meta.json": "shape", "a2/agent-meta.json": "shape"})
        self.assertEqual((p["agent_stopped"], p["secret_exposure"], p["secret_exposure_logs"]), (False, None, None))

    def test_a_page_made_meta_stub_is_accepted_and_names_the_page_error(self):
        # the exact shape lab/web/public/js/protocol.js pageAgentMeta writes (test/swarm.test.mjs pins the same)
        stub = {"v": 1, "run_id": BOARD_SPEC["run_id"], "agent": 1, "page": True, "timed_out": True,
                "agent_stopped": False, "solved": False, "solve_step": None, "started": 1.5, "finished": 2.5,
                "fatal": None, "stop_reason": None, "error": "E_AGENT_LOST", "secret_exposure": None,
                "secret_exposure_logs": None}
        self.assertTrue(self.m.meta_ok(stub, 1, BOARD_SPEC["run_id"]))
        metas = {i: good_meta(i) for i in range(self.N)}
        metas[1] = stub
        code, _, files, _ = self.run_main(metas=metas)
        self.assertEqual(code, 0)
        run = json.loads(files["run/run.json"])
        p = run["params"]
        self.assertEqual((run["timed_out"], p["agent_errors"], p["agents_stopped"][1]), ([1], {"1": "E_AGENT_LOST"}, False))
        self.assertNotIn("agents_missing", p)
        self.assertEqual((p["secret_exposure"], p["secret_exposure_logs"]), (None, None))

    def test_null_count_makes_the_sum_null(self):
        metas = {i: good_meta(i, secret_exposure=None if i == 1 else 0) for i in range(self.N)}
        _, _, files, _ = self.run_main(metas=metas)
        p = json.loads(files["run/run.json"])["params"]
        self.assertEqual((p["secret_exposure"], p["secret_exposure_logs"]), (None, 0))

    def test_deadline_without_stop_still_merges_and_exports(self):
        spec = {**BOARD_SPEC, "harness": {**BOARD_SPEC["harness"], "run_timeout_s": -self.m.BOARD_MARGIN_S - 1}}
        code, st, files, _ = self.run_main(metas={0: good_meta(0)}, stop=False, spec=spec, after_sleeps=99)
        self.assertEqual((code, st["phase"], st["ended"]), (0, "done", "timeout"))
        run = json.loads(files["run/run.json"])
        self.assertEqual(run["timed_out"], [0, 1, 2])
        self.assertTrue(self.board.killed)

    def test_the_wait_is_bounded_by_the_deadline(self):
        clock = [1000.0]
        spec = {**BOARD_SPEC, "harness": {**BOARD_SPEC["harness"], "run_timeout_s": 60}}

        def advance(s):
            clock[0] += s

        self.write_inputs(spec=spec)
        code, st = self.run_labctl(mock.patch.object(self.m.time, "time", lambda: clock[0]),
                                   mock.patch.object(self.m, "wait_port", return_value=True),
                                   mock.patch.object(self.m.time, "sleep", advance),
                                   mock.patch.object(self.m.subprocess, "Popen", FakeBoard(self.m)))
        self.assertEqual((code, st["ended"]), (0, "timeout"))
        self.assertGreaterEqual(clock[0], 1000 + 60 + self.m.BOARD_MARGIN_S)
        self.assertLess(clock[0], 1000 + 60 + self.m.BOARD_MARGIN_S + 2 * self.m.BOARD_POLL_S)

    def test_board_exit_is_e_board_but_exports(self):
        code, st, files, _ = self.run_main(board=FakeBoard(self.m, dies_after=0), after_sleeps=99)
        self.assertEqual((code, st["phase"], st["error"]), (1, "error", "E_BOARD"))
        self.assertIn("manifest.json", files)
        self.assertEqual(json.loads(files["run/run.json"])["timed_out"], [0, 1, 2])

    def test_ports_not_up_is_e_board_without_board_ready(self):
        code, st, files, _ = self.run_main(ports_up=False)
        self.assertEqual((code, st["error"]), (1, "E_BOARD"))
        self.assertNotIn("board-ready.json", files)
        self.assertIn("manifest.json", files)

    def test_meta_shape_check(self):
        rid = BOARD_SPEC["run_id"]
        self.assertTrue(self.m.meta_ok(good_meta(1), 1, rid))
        self.assertTrue(self.m.meta_ok(good_meta(1, solved=True, solve_step=0, fatal={"status": 200},
                                                 stop_reason="E_BUDGET", error="E_AGENT_ALIVE",
                                                 omitted={"cfg-a1.json": "shape"}), 1, rid))
        bad = [dict(v=2), dict(agent=0), dict(agent=True), dict(run_id="other"), dict(timed_out=0),
               dict(agent_stopped=None), dict(solved=True), dict(solve_step=3), dict(solved=True, solve_step=-1),
               dict(fatal={"status": 500}), dict(fatal={"status": 402, "detail": "x"}), dict(fatal=402),
               dict(stop_reason="not a code"), dict(error=7), dict(secret_exposure=-1),
               dict(secret_exposure_logs=True), dict(finished="x"), dict(started=None),
               dict(omitted={"a0/summary.json": "shape"}), dict(omitted={"a1/x": "other"}), dict(omitted=[])]
        for over in bad:
            self.assertFalse(self.m.meta_ok(good_meta(1, **over), 1, rid), over)
        for obj in (None, [], "x"):
            self.assertFalse(self.m.meta_ok(obj, 1, rid))
        missing = good_meta(1)
        del missing["timed_out"]
        self.assertFalse(self.m.meta_ok(missing, 1, rid))

    def test_oversized_meta_is_rejected(self):
        self.role("board")
        big = json.dumps({**good_meta(0), "pad": "x" * self.m.META_CAP}).encode()
        self.write("INBOX", "agent-0.meta.json", big)
        self.write("INBOX", "agent-1.meta.json", json.dumps(good_meta(1)))
        metas, bad = self.m.read_metas(BOARD_SPEC["run_id"], 3)
        self.assertEqual((set(metas), bad), ({1}, {0}))


class LegacySoloTest(LabctlCase):
    def test_start_board_argv_and_files_are_the_solo_ones(self):
        popen = mock.Mock()
        with mock.patch.object(self.m.subprocess, "Popen", popen):
            self.m.start_board("forum")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["-m", "board", "--interface", "forum", "--ports", "9000", "--seed",
                                    f"{self.m.RUN}/seed.json", "--log", f"{self.m.RUN}/board_events.jsonl"])
        self.assertEqual(sorted(os.listdir(self.m.RUN)), ["board.out", "board_events.jsonl"])  # no content sink


if __name__ == "__main__":
    unittest.main()
