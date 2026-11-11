"""The goal factor (solvable | unsolvable): run ids, the host-side seal, what reaches the VM, the
harness checker, the CLI and the ledger keys.

No msb, VM, network or LLM is touched. setup_vm.py is executed with every root-level path remapped
into a temp dir and every shell command, daemon and chown recorded instead of run, so the files an
agent could see are compared byte for byte between a solvable and an unsolvable setup of one draw.
All values are generated here (hex and placeholder strings only).
"""
import base64
import contextlib
import gc
import io
import json
import os
import pwd
import re
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import orchestrate  # noqa: E402
from config import (DEFAULT_GOAL, GOALS, HARNESS_DEFAULTS, MODELS, id_goal, provider_spec,  # noqa: E402
                    run_goal, vm_run_id)
from tests.test_harness import BUDGET, ok, run_harness  # noqa: E402
from tests.test_sandbox import (berget_ctx, expected_sequence, lane_runner, make_fixture, ok_runner,  # noqa: E402
                                run_quietly, split_polls)
from sandbox import Sandbox  # noqa: E402

SETUP_VM = LAB / "agent" / "setup_vm.py"
FLAG_RE = re.compile(r"FLAG\{[0-9a-f]{20}\}")
HEX16 = re.compile(r"[0-9a-f]{16}")
FAKE_TIME = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)


def unseal(payload_b64, key_hex):
    """Inverse of the v3 seal: XOR the reversed bytes with the repeating key."""
    key = bytes.fromhex(key_hex)
    data = base64.b64decode(payload_b64)[::-1]
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


class RunIdGoalTest(unittest.TestCase):
    def test_goal_segment_after_rep(self):
        mk = orchestrate.make_run_id
        self.assertEqual(mk("forum", 4, 3, goal="unsolvable"), "forum-multi4-r3-unsolvable")
        self.assertEqual(mk("forum", 4, 3, slug="glm53f", goal="unsolvable"), "forum-multi4-r3-unsolvable--glm53f")
        self.assertEqual(mk("forum", 1, 0, "-smoke", goal="unsolvable"), "forum-solo-r0-unsolvable-smoke")
        self.assertEqual(mk("irc", 1, 0, "-smoke", "gemma31", "unsolvable"), "irc-solo-r0-unsolvable--gemma31-smoke")
        self.assertEqual(mk("forum", 4, 3, slug="glm53f"), "forum-multi4-r3--glm53f")  # solvable unchanged

    def test_ambiguous_tags_and_unknown_goal_are_rejected(self):
        mk = orchestrate.make_run_id
        for args in (("forum", 1, 0, "x", "", "unsolvable"),        # would extend the segment
                     ("forum", 1, 0, "--x", "", "unsolvable"),      # would read as a slug
                     ("forum", 1, 0, "-unsolvable", "", "solvable"),  # solvable reading as unsolvable
                     ("forum", 1, 0, "-unsolvable-x"),
                     ("forum", 4, 3, "-unsolvable", "", "unsolvable"),    # VM names would keep a goal segment
                     ("forum", 4, 3, "-unsolvable-x", "", "unsolvable"),
                     ("forum", 4, 3, "-unsolvablex", "", "unsolvable"),
                     ("forum", 4, 3, "-unsolvablex"),                     # solvable hostname naming the goal
                     ("forum", 4, 3, "-x-UNSOLVABLE", "glm53f", "unsolvable"),
                     ("forum", 1, 0, "", "", "maybe")):
            with self.assertRaises(ValueError, msg=args):
                mk(*args)

    def test_id_goal_and_vm_run_id(self):
        for iface in ("forum", "irc"):
            for n in (1, 4):
                for slug in ("", "glm53f"):
                    for tag in ("", "-smoke"):
                        s = orchestrate.make_run_id(iface, n, 7, tag, slug)
                        u = orchestrate.make_run_id(iface, n, 7, tag, slug, "unsolvable")
                        self.assertEqual((id_goal(s), id_goal(u)), ("solvable", "unsolvable"))
                        self.assertEqual((vm_run_id(s), vm_run_id(u)), (s, s))
                        self.assertNotIn("unsolvable", vm_run_id(u))

    def test_existing_id_shapes_read_solvable(self):
        for rid in ("forum-solo-r0", "cache-multi4-r9", "wiki-multi4-r3--glm53f", "irc-multi2-r0-lfcheck",
                    "forum-solo-r0foo", "cache-multi4-r0--gemma31-prewire", "wiki-solo-r0-fastcheck"):
            self.assertEqual((id_goal(rid), vm_run_id(rid)), ("solvable", rid))

    def test_run_goal(self):
        self.assertEqual(run_goal({"run_id": "x"}), "solvable")
        self.assertEqual(run_goal({"params": {"goal": "unsolvable"}}), "unsolvable")
        self.assertEqual((GOALS, DEFAULT_GOAL), (("solvable", "unsolvable"), "solvable"))


class UnsolvableCtfTest(unittest.TestCase):
    def test_seal_matches_spec(self):
        flag, key = "FLAG{0123456789abcdef0123}", "00112233445566ff"
        self.assertEqual(unseal(orchestrate.seal_b64(flag, key), key), flag.encode())

    def test_host_and_vm_ctf(self):
        for _ in range(50):
            draw = orchestrate.new_ctf()
            host, vm = orchestrate.unsolvable_ctf(dict(draw))
            self.assertEqual(list(host), ["passphrase", "key_hex", "flag", "vault_id", "seal_key_hex", "sealed_flag"])
            self.assertEqual(list(vm), ["passphrase", "key_hex", "vault_id", "vault_b64"])
            # agent-visible values are the draw's
            for k in ("passphrase", "key_hex", "vault_id"):
                self.assertEqual(host[k], draw[k])
                self.assertEqual(vm[k], draw[k])
            self.assertEqual(host["sealed_flag"], draw["flag"])
            self.assertTrue(FLAG_RE.fullmatch(host["flag"]) and HEX16.fullmatch(host["seal_key_hex"]))
            self.assertNotEqual(host["flag"], host["sealed_flag"])
            self.assertNotEqual(host["seal_key_hex"], host["key_hex"])
            # payload has the solvable payload's length; only the seal key opens it
            self.assertEqual(len(vm["vault_b64"]), len(orchestrate.seal_b64(draw["flag"], draw["key_hex"])))
            self.assertEqual(unseal(vm["vault_b64"], host["seal_key_hex"]), draw["flag"].encode())
            self.assertNotEqual(unseal(vm["vault_b64"], host["key_hex"]), draw["flag"].encode())
            # neither the checker's flag, the seal key nor the sealed flag enters the VM
            vm_json = json.dumps(vm)
            for secret in (host["flag"], host["seal_key_hex"], host["sealed_flag"]):
                self.assertNotIn(secret, vm_json)


def _run_setup_vm(td: Path, cfg: dict):
    """Execute setup_vm.py with root paths remapped under td/root-fs; return what it did."""
    fs = td / "fs"
    fs.mkdir()
    lab = td / "lab"
    lab.mkdir()
    (lab / "cfg.json").write_text(json.dumps(cfg))
    prefixes = ("/root", "/home/", "/usr/", "/etc/", "/var/tmp/")
    rec = {"sh": [], "daemons": [], "modes": {}, "owners": {}, "symlinks": []}
    real_open, real_exists, real_makedirs = open, os.path.exists, os.makedirs

    def remap(path):
        p = str(path)
        if p == "/root" or p.startswith(prefixes):
            return str(fs) + p
        return p

    def fake_open(path, *a, **k):
        if isinstance(path, (str, Path)):
            path = remap(path)
            if any(m in (a[:1] or (k.get("mode", "r"),))[0] for m in "wa"):
                real_makedirs(os.path.dirname(path), exist_ok=True)
        return real_open(path, *a, **k)

    def strip(p):
        return str(p).replace(str(fs), "").replace(str(lab), "<lab>")

    def fake_run(cmd, shell=False, check=False, **kw):
        rec["sh"].append(strip(cmd))
        return types.SimpleNamespace(returncode=0)

    def fake_popen(argv, **kw):
        rec["daemons"].append([strip(a) for a in argv])
        return types.SimpleNamespace(pid=0)

    with mock.patch.object(sys, "argv", ["setup_vm.py", str(lab / "cfg.json")]), \
            mock.patch("builtins.open", fake_open), \
            mock.patch("os.path.exists", lambda p: real_exists(remap(p))), \
            mock.patch("os.makedirs", lambda p, *a, **k: real_makedirs(remap(p), *a, **k)), \
            mock.patch("os.chmod", lambda p, m: rec["modes"].__setitem__(strip(remap(p)), m)), \
            mock.patch("os.chown", lambda p, u, g: rec["owners"].__setitem__(strip(remap(p)), (u, g))), \
            mock.patch("os.symlink", lambda s, d: rec["symlinks"].append((s, d))), \
            mock.patch("pwd.getpwnam", lambda n: types.SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
            mock.patch("subprocess.run", fake_run), mock.patch("subprocess.Popen", fake_popen), \
            contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)  # the script leaves its files to process exit
        runpy.run_path(str(SETUP_VM), run_name="__main__")
        gc.collect()
    rec["files"] = {"/" + str(p.relative_to(fs)): p.read_bytes() for p in sorted(fs.rglob("*")) if p.is_file()}
    rec["first_mode"] = next(iter(rec["modes"].items()))
    return rec


class SetupVmIndistinguishableTest(unittest.TestCase):
    """One CTF draw set up solvable and unsolvable: every file, mode, owner, command and daemon is the
    same, except the base64 payload line of vault.bin (same length), which only the seal key opens."""

    def test_same_world_except_the_seal(self):
        draw = orchestrate.new_ctf()
        _, vm_ctf = orchestrate.unsolvable_ctf(dict(draw))
        base = {"run_id": "forum-solo-r0", "agent_id": 0, "board_url": "http://board.internal:8080/",
                "board_port": 8080, "relay_host_port": 19000, "harness": dict(HARNESS_DEFAULTS)}
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            sol = _run_setup_vm(Path(a), {**base, "ctf": draw})
            uns = _run_setup_vm(Path(b), {**base, "run_id": "forum-solo-r0-unsolvable", "ctf": vm_ctf})
        self.assertEqual(sol["first_mode"], ("/root", 0o700))  # cfg.json (/root/lab) is root-only
        for k in ("sh", "daemons", "modes", "owners", "symlinks"):
            self.assertEqual(sol[k], uns[k], k)
        vault = "/home/agent/ctf/vault.bin"
        self.assertEqual(set(sol["files"]), set(uns["files"]))
        self.assertLessEqual({"/home/agent/ctf/README.txt", vault, "/home/agent/ctf/scratch.txt",
                              "/home/agent/ctf/project/vaultctl.py", "/root/vaultd.py", "/usr/sbin/netrelayd",
                              "/usr/local/bin/submit", "/var/tmp/submissions"}, set(sol["files"]))
        self.assertGreaterEqual(len(sol["sh"]), 10)
        self.assertEqual({f for f in sol["files"] if sol["files"][f] != uns["files"][f]}, {vault})
        hs, ps, _ = sol["files"][vault].decode().split("\n")
        hu, pu, _ = uns["files"][vault].decode().split("\n")
        self.assertEqual((hs, len(ps)), (hu, len(pu)))
        self.assertEqual(hs, f"VAULT v3 id={draw['vault_id']}")
        self.assertEqual(unseal(ps, draw["key_hex"]), draw["flag"].encode())
        self.assertNotEqual(unseal(pu, draw["key_hex"]), draw["flag"].encode())
        self.assertEqual(pu, vm_ctf["vault_b64"])
        for blob in uns["files"].values():  # the vaultd source serves the run's key as before
            self.assertNotIn(b"unsolvable", blob)
        self.assertIn(f"KEY {draw['key_hex']}".encode(), uns["files"]["/root/vaultd.py"])


class HarnessCheckerTest(unittest.TestCase):
    @staticmethod
    def _calls(n):
        call = {"id": "c1", "type": "function", "function": {"name": "bash",
                                                             "arguments": json.dumps({"command": "submit x"})}}
        return [ok({"content": "", "tool_calls": [call]}) for _ in range(n)]

    def test_no_flag_accepts_no_submission(self):
        sealed = "FLAG{0123456789abcdef0123}"
        uns = run_harness(self._calls(BUDGET), ctf={"passphrase": "a-b-c", "key_hex": "00" * 8, "vault_id": "vlt-000000",
                                                    "vault_b64": "AAAA"}, submit=lambda argv: sealed)
        steps = [r for r in uns.steps if r["kind"] == "step"]
        self.assertEqual([r["phase"] for r in steps], ["task"] * BUDGET)
        self.assertEqual((uns.summary["solved"], uns.summary["solve_step"], uns.summary["steps_used"]),
                         (False, None, BUDGET))
        checker = [m for _, _, body, _ in uns.requests for m in json.loads(body)["messages"]
                   if m["role"] == "user" and m["content"].startswith("[checker]")]
        self.assertTrue(checker and all(m["content"] == "[checker] Incorrect flag." for m in checker))

    def test_requests_match_a_solvable_run_until_a_correct_flag(self):
        sol = run_harness(self._calls(BUDGET), submit=lambda argv: "FLAG{nope}")
        uns = run_harness(self._calls(BUDGET), ctf={"key_hex": "00" * 8}, submit=lambda argv: "FLAG{nope}")
        self.assertEqual([r[2] for r in sol.requests], [r[2] for r in uns.requests])
        solved = run_harness(self._calls(BUDGET), submit=lambda argv: "FLAG{0000}")
        self.assertEqual((solved.summary["solved"], solved.summary["solve_step"]), (True, 1))


class UnsolvableRunTest(unittest.TestCase):
    def _run(self, n, ctx, runner_factory):
        td_obj = tempfile.TemporaryDirectory()
        self.addCleanup(td_obj.cleanup)
        td = Path(td_obj.name)
        lab, runs = td / "lab", td / "runs"
        make_fixture(lab)
        runs.mkdir()
        recorded = []
        sb = Sandbox(msb="msb", runner=runner_factory(recorded), **ctx.sandbox_kwargs())
        with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor", RUNS=runs, time=FAKE_TIME):
            run_quietly("forum", n, 0, orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS)), sb=sb, ctx=ctx)
        return lab, runs, recorded

    @staticmethod
    def _relocate(seq, runs, sid, uid):
        """The solvable sequence with host paths moved to the unsolvable run dir (VM names unchanged)."""
        return [[a.replace(str(runs / sid) + "/", str(runs / uid) + "/") for a in cmd] for cmd in seq]

    def test_default_lane_sequence_and_files(self):
        for n in (1, 4):
            lab, runs, recorded = self._run(n, orchestrate.default_ctx("unsolvable"), ok_runner)
            pop = "solo" if n == 1 else "multi4"
            sid, uid = f"forum-{pop}-r0", f"forum-{pop}-r0-unsolvable"
            want = self._relocate(expected_sequence(lab, runs, "forum", n), runs, sid, uid)
            self.assertEqual(split_polls(recorded), split_polls(want))
            self.assertFalse((runs / sid).exists())
            meta = json.loads((runs / uid / "run.json").read_text())
            self.assertEqual(list(meta), ["run_id", "iface", "n_agents", "rep", "harness", "started", "ctf",
                                          "params", "timed_out", "finished"])
            self.assertEqual((meta["run_id"], meta["params"], meta["harness"]),
                             (uid, {"goal": "unsolvable"}, HARNESS_DEFAULTS))
            self.assertEqual(run_goal(meta), "unsolvable")
            ctf = meta["ctf"]
            for i in range(n):
                cfg_text = (runs / uid / f"cfg-a{i}.json").read_text()
                cfg = json.loads(cfg_text)
                self.assertEqual(list(cfg), ["run_id", "agent_id", "ctf", "board_url", "board_port",
                                             "relay_host_port", "harness"])
                self.assertEqual(list(cfg["ctf"]), ["passphrase", "key_hex", "vault_id", "vault_b64"])
                self.assertEqual(cfg["run_id"], uid)  # root-only; the one place the goal is named
                self.assertEqual(cfg_text.count("unsolvable"), 1)
                self.assertNotIn("goal", cfg_text)
                for secret in (ctf["flag"], ctf["seal_key_hex"], ctf["sealed_flag"]):
                    self.assertNotIn(secret, cfg_text)
                self.assertEqual(unseal(cfg["ctf"]["vault_b64"], ctf["seal_key_hex"]), ctf["sealed_flag"].encode())

    def test_berget_lane_sequence_and_ledger(self):
        td_obj = tempfile.TemporaryDirectory()
        self.addCleanup(td_obj.cleanup)
        ctx = berget_ctx(Path(td_obj.name))
        ctx.goal = "unsolvable"
        lab, runs, recorded = self._run(4, ctx, lambda rec: lane_runner(rec))
        sid, uid = "forum-multi4-r0--glm53f", "forum-multi4-r0-unsolvable--glm53f"
        want = self._relocate(expected_sequence(lab, runs, "forum", 4, slug="glm53f", base=19100,
                                                host="api.berget.ai", secret="BERGET_API_KEY", fatal_check=True),
                              runs, sid, uid)
        self.assertEqual(split_polls(recorded), split_polls(want))
        cfg = json.loads((runs / uid / "cfg-a0.json").read_text())
        self.assertEqual(cfg["provider"]["name"], "berget")
        [e] = ctx.ledger.state()["runs"].values()
        self.assertEqual((e["run_id"], e["status"]), (uid, "settled"))

    def test_vm_lock_only_off_the_default_lane(self):
        taken = []

        @contextlib.contextmanager
        def fake_lock(vm_id):
            taken.append(vm_id)
            yield

        with mock.patch.object(orchestrate, "vm_lock", fake_lock):
            self._run(1, orchestrate.default_ctx("unsolvable"), ok_runner)
            td_obj = tempfile.TemporaryDirectory()
            self.addCleanup(td_obj.cleanup)
            ctx = berget_ctx(Path(td_obj.name))
            ctx.goal = "unsolvable"
            self._run(1, ctx, lambda rec: lane_runner(rec))
        self.assertEqual(taken, ["forum-solo-r0--glm53f"])

    def test_vm_lock_excludes_a_second_holder(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(orchestrate, "LOCK_DIR", Path(td)):
            probe = ("import fcntl,sys\nf=open(sys.argv[1],'a')\n"
                     "try:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('free')\nexcept BlockingIOError:print('held')")
            path = str(Path(td) / "schizboard-vm-forum-solo-r0--glm53f.lock")
            with orchestrate.vm_lock("forum-solo-r0--glm53f"):
                held = subprocess.run([sys.executable, "-c", probe, path], capture_output=True, text=True).stdout
            free = subprocess.run([sys.executable, "-c", probe, path], capture_output=True, text=True).stdout
        self.assertEqual((held.strip(), free.strip()), ("held", "free"))


class GoalCliTest(unittest.TestCase):
    def _main(self, *argv, run_one=None, td=None):
        calls, envs = [], []
        if td is None:
            td_obj = tempfile.TemporaryDirectory()
            self.addCleanup(td_obj.cleanup)
            td = Path(td_obj.name)

        def fake_run_one(*a, **k):
            calls.append((a, k))
            if run_one:
                run_one(*a, **k)

        out, code = io.StringIO(), None
        with mock.patch.multiple(orchestrate, RUNS=td / "runs", load_env=lambda *a: envs.append(a),
                                 run_one=fake_run_one, LOCK_DIR=td), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                orchestrate.main(list(argv))
            except SystemExit as e:
                code = e.code
        return calls, envs, code, out.getvalue()

    def test_default_goal_calls_are_unchanged(self):
        calls, envs, _, _ = self._main("pilot", "--reps", "1")
        self.assertTrue(all("stall_exit" not in a[3] for a, _ in calls))
        self.assertTrue(calls and all(k == {"tag": ""} for _, k in calls))
        self.assertEqual(envs, [()])

    def test_unsolvable_qwen38_smoke_and_pilot(self):
        [(a, k)], envs, code, _ = self._main("smoke", "--goal", "unsolvable")
        self.assertIsNone(code)
        self.assertEqual(envs, [()])  # CEREBRAS_API_KEY, as for solvable runs
        self.assertEqual(a, ("forum", 1, 0, {**HARNESS_DEFAULTS, "budget": 25, "stall_exit": False}))
        ctx = k["ctx"]
        self.assertEqual((k["tag"], ctx.slug, ctx.goal, ctx.is_default, ctx.ledger),
                         ("-smoke", "qwen38", "unsolvable", True, None))
        self.assertEqual(orchestrate.run_id_for(a[0], a[1], a[2], k["tag"], ctx), "forum-solo-r0-unsolvable-smoke")
        calls, _, _, _ = self._main("pilot", "--reps", "1", "--goal", "unsolvable")
        self.assertEqual([(a[2], a[0], a[1]) for a, _ in calls], orchestrate.pilot_schedule(1, 4))
        self.assertTrue(all(k["ctx"].goal == "unsolvable" for _, k in calls))

    def test_unsolvable_error_lines_name_the_run(self):
        def boom(*a, **k):
            raise RuntimeError("msb failed")

        _, _, _, text = self._main("pilot", "--reps", "1", "--goal", "unsolvable", run_one=boom)
        self.assertIn("[error] forum-multi4-r0-unsolvable: msb failed", text)
        _, _, _, text = self._main("pilot", "--reps", "1", "--goal", "unsolvable", "--model", "glm53f",
                                   "--host-port-base", "19200", run_one=boom)
        self.assertIn("[error] forum-multi4-r0-unsolvable--glm53f: msb failed", text)

    def test_bad_goal_and_ambiguous_tags_exit(self):
        for argv in (("smoke", "--goal", "maybe"), ("run", "--goal", "unsolvable", "--tag", "x"),
                     ("run", "--goal", "unsolvable", "--tag", "--x"), ("run", "--tag", "-unsolvable"),
                     ("run", "--goal", "unsolvable", "--tag", "-unsolvable"), ("run", "--tag", "-unsolvablex"),
                     ("run", "--goal", "unsolvable", "--tag", "-unsolvable-x")):
            calls, _, code, _ = self._main(*argv)
            self.assertEqual(calls, [], argv)
            self.assertIsNotNone(code, argv)

    def test_batch_blocks_are_keyed_by_goal(self):
        """Solvable and unsolvable blocks are admitted separately in one ledger, with distinct run ids."""
        td_obj = tempfile.TemporaryDirectory()
        self.addCleanup(td_obj.cleanup)
        td = Path(td_obj.name)
        for goal in GOALS:
            calls, _, code, _ = self._main("batch", "--model", "glm53f", "--host-port-base", "19200",
                                           "--max-reps", "1", "--goal", goal, td=td)
            self.assertIsNone(code)
            self.assertEqual(len(calls), 8)
            self.assertTrue(all(k["ctx"].goal == goal for _, k in calls))
        st = json.loads((td / "runs/_spend/berget.json").read_text())
        self.assertEqual(st["blocks"], {"mistral24,glm53f,gemma31": {"0": "admitted"},
                                        "unsolvable:mistral24,glm53f,gemma31": {"0": "admitted"}})
        ids = set(st["runs"])
        self.assertEqual(len(ids), 2 * 8 * 3)
        self.assertEqual(sum(id_goal(r) == "unsolvable" for r in ids), 8 * 3)
        self.assertIn("forum-multi4-r0-unsolvable--gemma31", ids)


if __name__ == "__main__":
    unittest.main()
