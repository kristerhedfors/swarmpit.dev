"""Host-side tests: the exact ordered `msb` command list a run issues, with a fake runner.

No msb binary, VM, or network is touched. The test pins the full command sequence (network-policy
flags especially) so an accidental change to isolation or copy layout fails loudly, and pins the
pilot cell order and smoke-mode harness arguments the CLI derives. Non-default models (Berget on
their own port lane) get the same treatment, plus fatal-error aborts and the spend ledger hookup.
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import orchestrate  # noqa: E402
from config import HARNESS_DEFAULTS, IMAGE, MODELS, provider_spec  # noqa: E402
from sandbox import Sandbox  # noqa: E402
from spend import BudgetCapReached, FatalProviderError, Ledger  # noqa: E402

FATAL = ["test", "-f", "/root/logs/FATAL"]


def ok_runner(recorded=None):
    """Fake msb runner: records argv and reports success (so DONE is seen on the first poll)."""
    def run(argv, *, check, timeout):
        if recorded is not None:
            recorded.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def run_quietly(*args, **kwargs):
    """orchestrate.run_one with its progress lines kept out of the unittest output."""
    with contextlib.redirect_stdout(io.StringIO()):
        orchestrate.run_one(*args, **kwargs)


def make_fixture(root: Path):
    """A minimal, stable lab tree so the asserted sequence does not depend on sibling edits."""
    (root / "board").mkdir(parents=True)
    (root / "board/__init__.py").write_text("")
    (root / "board/forum.py").write_text("# forum\n")
    (root / "agent").mkdir()
    (root / "agent/harness.py").write_text("# harness\n")
    (root / "agent/setup_vm.py").write_text("# setup\n")
    (root / "agent/.DS_Store").write_text("")      # stray files that must never reach a VM
    (root / "agent/harness.py~").write_text("")
    (root / "agent/__pycache__").mkdir()
    (root / "seeds").mkdir()
    (root / "seeds/seed.json").write_text("{}")
    (root / "vendor").mkdir()
    (root / "vendor/busybox").write_text("bb")  # already present -> ensure_busybox is a no-op


def expected_sequence(lab: Path, runs: Path, iface: str, n: int, rep: int = 0, *, slug="", base=19000,
                      host="api.cerebras.ai", secret="CEREBRAS_API_KEY", fatal_check=False):
    """Independent spec of the command list for a run, mirroring the orchestrator contract.

    The keyword arguments describe a non-default lane; their defaults give the Cerebras sequence.
    """
    run_id = f"{iface}-{'solo' if n == 1 else f'multi{n}'}-r{rep}" + (f"--{slug}" if slug else "")
    out = runs / run_id
    board = f"sb-{run_id}-board"
    agents = [f"sb-{run_id}-a{i}" for i in range(n)]
    gports = ",".join(str(9000 + i) for i in range(n))
    seq = []

    # board VM: deny egress, publish each agent's guest port on its own host loopback port
    ports = []
    for i in range(n):
        ports += ["-p", f"127.0.0.1:{base + i}:{9000 + i}"]
    seq.append(["msb", "create", IMAGE, "--name", board, "--replace",
                "--net-default-egress", "deny", *ports])
    seq.append(["msb", "exec", board, "--", "mkdir", "-p", "/root/lab/board", "/root/logs"])
    for name in ("__init__.py", "forum.py"):  # sorted board/*.py
        seq.append(["msb", "cp", str(lab / "board" / name), f"{board}:/root/lab/board/{name}"])
    seq.append(["msb", "cp", str(lab / "seeds/seed.json"), f"{board}:/root/lab/seed.json"])
    seq.append(["msb", "exec", board, "--", "sh", "-c",
                f"cd /root/lab; nohup python3 -m board --interface {iface} --ports {gports} "
                f"--seed /root/lab/seed.json --log /root/logs/events.jsonl "
                f"> /root/logs/board.out 2>&1 &"])

    # agent VMs: default-deny; egress only to DNS, inference API, and the agent's own board port
    for i, name in enumerate(agents):
        seq.append(["msb", "create", IMAGE, "--name", name, "--replace", "--no-net",
                    "--net-rule", "allow@dns",
                    "--net-rule", f"allow@{host}:tcp:443",
                    "--net-rule", f"allow@host:tcp:{base + i}",
                    "--secret", f"{secret}@{host}",
                    "--secret-violation-action", "block"])
        seq.append(["msb", "exec", name, "--", "mkdir", "-p", "/root/lab", "/root/logs"])
        seq.append(["msb", "cp", str(out / f"cfg-a{i}.json"), f"{name}:/root/lab/cfg.json"])
        for fn in ("harness.py", "setup_vm.py"):  # sorted agent/*
            seq.append(["msb", "cp", str(lab / "agent" / fn), f"{name}:/root/lab/{fn}"])
        seq.append(["msb", "cp", str(lab / "vendor/busybox"), f"{name}:/root/lab/busybox"])
        seq.append(["msb", "exec", name, "--", "python3", "/root/lab/setup_vm.py", "/root/lab/cfg.json"])
    for name in agents:
        seq.append(["msb", "exec", name, "--", "sh", "-c",
                    "nohup python3 /root/lab/harness.py /root/lab/cfg.json > /root/logs/harness.out 2>&1 &"])
    # one poll per agent (fake runner reports DONE immediately)
    for name in agents:
        seq.append(["msb", "exec", name, "--", "test", "-f", "/root/logs/DONE"])
        if fatal_check:  # non-default lanes: one FATAL check per agent at DONE
            seq.append(["msb", "exec", name, "--", *FATAL])
    # collection + teardown
    logs = ("steps.jsonl", "summary.json", "relay.jsonl", "harness.out", "vaultd.out") + (("FATAL",) if fatal_check else ())
    for name in agents:
        for f in logs:
            i = agents.index(name)
            seq.append(["msb", "cp", f"{name}:/root/logs/{f}", str(out / f"a{i}" / f)])
    seq.append(["msb", "cp", f"{board}:/root/logs/events.jsonl", str(out / "board_events.jsonl")])
    seq.append(["msb", "cp", f"{board}:/root/logs/events.content.chatter.json",
                str(out / "board_events.content.chatter.json")])
    seq.append(["msb", "cp", f"{board}:/root/logs/board.out", str(out / "board.out")])
    seq.append(["msb", "rm", "-f", board, *agents])
    return seq


def split_polls(seq):
    """The DONE (and FATAL) polls iterate a set, so their order is undefined; compare them as a
    multiset and the rest in order."""
    polls = (["test", "-f", "/root/logs/DONE"], FATAL)
    return [c for c in seq if c[-3:] not in polls], sorted(c for c in seq if c[-3:] in polls)


def lane_runner(recorded=None, fatal_status=None, summary=None, every_agent=False):
    """Fake msb runner for a non-default lane: DONE at once, no FATAL marker unless `fatal_status`.

    `fatal_status` makes agent 0's FATAL check succeed and its FATAL copy hold that status;
    `summary` is written as agent 0's (with `every_agent`, every agent's) collected summary.json
    (numbers and flags only).
    """
    summary_re = re.compile(r".*-a\d+:/root/logs/summary\.json" if every_agent else r".*-a0:/root/logs/summary\.json")

    def run(argv, *, check, timeout):
        if recorded is not None:
            recorded.append(list(argv))
        rc = 0
        if argv[-3:] == FATAL:
            rc = 0 if fatal_status is not None and argv[2].endswith("-a0") else 1
        elif argv[1] == "cp" and ":" in argv[2] and argv[2].endswith("-a0:/root/logs/FATAL"):
            if fatal_status is None:
                rc = 1
            else:
                Path(argv[3]).write_text(json.dumps({"status": fatal_status, "detail": "x"}))
        elif argv[1] == "cp" and summary_re.fullmatch(argv[2]) and summary is not None:
            Path(argv[3]).write_text(json.dumps(summary))
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    return run


def berget_ctx(td: Path, slug="glm53f", base=19100, cap=None):
    return orchestrate.RunContext(slug, MODELS[slug], provider_spec(slug), base,
                                  Ledger(td / "runs/_spend/berget.json", cap=cap, runs_dir=td / "runs"))


class SandboxSequenceTest(unittest.TestCase):
    def _run(self, n, ctx=None):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            recorded = []
            sb = Sandbox(msb="msb", runner=ok_runner(recorded))
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                     RUNS=runs, time=fake_time):
                run_quietly("forum", n, 0, dict(HARNESS_DEFAULTS), sb=sb, **({"ctx": ctx} if ctx else {}))
            # The DONE poll iterates a set, so its order is undefined in both baseline and refactor;
            # compare those commands as a multiset and the rest in order.
            self.assertEqual(split_polls(recorded), split_polls(expected_sequence(lab, runs, "forum", n)))

    def test_solo(self):
        self._run(1)

    def test_multi(self):
        self._run(4)

    def test_default_ctx_is_identical(self):
        """Passing the default context (qwen38, lane 19000) changes nothing in the command list."""
        self._run(4, ctx=orchestrate.default_ctx())

    def test_berget_lane_sequence(self):
        for n in (1, 4):
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                lab, runs = td / "lab", td / "runs"
                make_fixture(lab)
                runs.mkdir()
                ctx = berget_ctx(td)
                recorded = []
                sb = Sandbox(msb="msb", runner=lane_runner(recorded), **ctx.sandbox_kwargs())
                fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
                with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                         RUNS=runs, time=fake_time):
                    run_quietly("forum", n, 0, orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS)),
                                sb=sb, ctx=ctx)
                want = expected_sequence(lab, runs, "forum", n, slug="glm53f", base=19100,
                                         host="api.berget.ai", secret="BERGET_API_KEY", fatal_check=True)
                self.assertEqual(split_polls(recorded), split_polls(want))
                [e] = ctx.ledger.state()["runs"].values()
                self.assertEqual((e["run_id"], e["status"], e["usage_source"]),
                                 (f"forum-{'solo' if n == 1 else 'multi4'}-r0--glm53f", "settled", "projection"))

    def test_cfg_written_and_run_json(self):
        """run.json carries the expected schema and ctf fields, and cfg.json points at the relay."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            sb = Sandbox(msb="msb", runner=ok_runner())
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                     RUNS=runs, time=fake_time):
                run_quietly("wiki", 2, 3, dict(HARNESS_DEFAULTS), sb=sb)
            meta = json.loads((runs / "wiki-multi2-r3/run.json").read_text())
            self.assertEqual(list(meta), ["run_id", "iface", "n_agents", "rep", "harness",
                                          "started", "ctf", "timed_out", "finished"])
            self.assertEqual(meta["run_id"], "wiki-multi2-r3")
            self.assertEqual(set(meta["ctf"]), {"passphrase", "key_hex", "flag", "vault_id"})
            cfg = json.loads((runs / "wiki-multi2-r3/cfg-a1.json").read_text())
            self.assertEqual(list(cfg), ["run_id", "agent_id", "ctf", "board_url",
                                         "board_port", "relay_host_port", "harness"])
            self.assertEqual(cfg["relay_host_port"], 19001)
            self.assertEqual(cfg["board_port"], 8080)

    def test_berget_cfg_and_run_json(self):
        """A Berget run: cfg.json gains a trailing provider block and its own relay port; the harness
        names model and provider; the run.json key list is unchanged."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            ctx = berget_ctx(td)
            sb = Sandbox(msb="msb", runner=lane_runner(), **ctx.sandbox_kwargs())
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            harness = orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS))
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                     RUNS=runs, time=fake_time):
                run_quietly("wiki", 2, 3, harness, sb=sb, ctx=ctx)
            meta = json.loads((runs / "wiki-multi2-r3--glm53f/run.json").read_text())
            self.assertEqual(list(meta), ["run_id", "iface", "n_agents", "rep", "harness",
                                          "started", "ctf", "timed_out", "finished"])
            self.assertEqual(meta["harness"], {**HARNESS_DEFAULTS, "model": "zai-org/GLM-5.3-Flash",
                                               "provider": "berget"})
            cfg = json.loads((runs / "wiki-multi2-r3--glm53f/cfg-a1.json").read_text())
            self.assertEqual(list(cfg), ["run_id", "agent_id", "ctf", "board_url",
                                         "board_port", "relay_host_port", "harness", "provider"])
            self.assertEqual(cfg["relay_host_port"], 19101)
            self.assertEqual(cfg["provider"], {"name": "berget", "host": "api.berget.ai",
                                               "path": "/v1/chat/completions", "secret_env": "BERGET_API_KEY",
                                               "wire": {"sanitize_tool_args": True}})

    def test_cerebras_harness_unchanged(self):
        base = dict(HARNESS_DEFAULTS)
        self.assertIs(orchestrate.harness_for(orchestrate.default_ctx(), base), base)


class FatalAbortTest(unittest.TestCase):
    """A fatal provider error voids the run: archived under _aborted/, ledger aborted and halted."""

    def _run(self, **runner_kw):
        td_obj = tempfile.TemporaryDirectory()
        self.addCleanup(td_obj.cleanup)
        td = Path(td_obj.name)
        lab, runs = td / "lab", td / "runs"
        make_fixture(lab)
        runs.mkdir()
        ctx = berget_ctx(td)
        sb = Sandbox(msb="msb", runner=lane_runner(**runner_kw), **ctx.sandbox_kwargs())
        fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
        with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor", RUNS=runs, time=fake_time), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(FatalProviderError):
            run_quietly("forum", 4, 0, orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS)), sb=sb, ctx=ctx)
        return runs, ctx, err.getvalue()

    def _check(self, runs, ctx, err, status):
        rid = "forum-multi4-r0--glm53f"
        self.assertFalse((runs / rid).exists())
        meta = json.loads((runs / "_aborted" / f"{rid}-{status}" / "run.json").read_text())
        self.assertEqual(meta["fatal"], {"agent": 0, "status": status})
        st = ctx.ledger.state()
        [(key, e)] = st["runs"].items()
        self.assertTrue(key.startswith(rid + "@"))
        self.assertEqual(e["status"], "aborted")
        self.assertEqual(st["halted"]["reason"], f"{rid}: provider HTTP {status}")
        self.assertIn(f"[abort] {rid}: provider HTTP {status}", err)
        with self.assertRaises(BudgetCapReached):  # every lane now refuses new runs
            ctx.ledger.reserve("forum-solo-r1--glm53f", "glm53f", 1, 1)

    def test_marker_aborts(self):
        self._check(*self._run(fatal_status=402), 402)

    def test_summary_fallback_aborts(self):
        """No FATAL marker (the poll missed it), but the crash summary says fatal."""
        runs, ctx, err = self._run(summary={"run": "x", "agent": 0, "crashed": "ProviderFatal('HTTP 402: x')",
                                            "tokens": {"prompt": 10, "completion": 2, "cached": 0}, "fatal": True})
        self._check(runs, ctx, err, 402)

    def test_all_crashed_run_is_void_and_stops_only_this_lane(self):
        crash = {"run": "x", "agent": 0, "crashed": "RuntimeError('HTTP 404: x')",
                 "tokens": {"prompt": 0, "completion": 0, "cached": 0}}
        runs, ctx, err = self._run(summary=crash, every_agent=True)
        rid = "forum-multi4-r0--glm53f"
        self.assertFalse((runs / rid).exists())
        meta = json.loads((runs / "_aborted" / f"{rid}-404" / "run.json").read_text())
        self.assertEqual(meta["fatal"], {"agent": 0, "status": 404, "all_crashed": True})
        st = ctx.ledger.state()
        [e] = st["runs"].values()
        self.assertEqual(e["status"], "aborted")
        self.assertIsNone(st["halted"])  # other lanes go on
        self.assertIn(f"[abort] {rid}: every agent crashed (HTTP 404)", err)

    def test_partly_crashed_run_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            ctx = berget_ctx(td)
            crash = {"run": "x", "agent": 0, "crashed": "RuntimeError('HTTP 400: x')", "tokens": {}}
            sb = Sandbox(msb="msb", runner=lane_runner(summary=crash), **ctx.sandbox_kwargs())
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor", RUNS=runs, time=fake_time):
                run_quietly("forum", 4, 0, orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS)), sb=sb, ctx=ctx)
            self.assertNotIn("fatal", json.loads((runs / "forum-multi4-r0--glm53f/run.json").read_text()))

    def test_default_run_records_fatal_in_place(self):
        """Cerebras qwen38: a fatal crash summary is recorded in run.json; no abort, no raise."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            crash = {"run": "x", "agent": 0, "crashed": "ProviderFatal('HTTP 401: x')", "tokens": {}, "fatal": True}
            sb = Sandbox(msb="msb", runner=lane_runner(summary=crash, every_agent=True))
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor", RUNS=runs, time=fake_time):
                run_quietly("forum", 1, 0, dict(HARNESS_DEFAULTS), sb=sb)
            meta = json.loads((runs / "forum-solo-r0/run.json").read_text())
            self.assertEqual(meta["fatal"], {"agent": 0, "status": 401})
            self.assertFalse((runs / "_aborted").exists())

    def test_wait_done_stops_at_first_fatal(self):
        class FakeSb:
            def is_done(self, name, marker="/root/logs/DONE"):
                return marker == "/root/logs/DONE" or name == "a1"

        fatal = []
        with mock.patch.object(orchestrate, "POLL_INTERVAL_S", 0), contextlib.redirect_stdout(io.StringIO()):
            left = orchestrate.wait_done(FakeSb(), "x", ["a1"], time.time() + 5, fatal=fatal)
        self.assertEqual((left, fatal), ([], ["a1"]))


class RunIdTest(unittest.TestCase):
    def test_slug_and_tag(self):
        self.assertEqual(orchestrate.make_run_id("forum", 4, 3, slug="glm53f"), "forum-multi4-r3--glm53f")
        self.assertEqual(orchestrate.make_run_id("forum", 1, 0, "-smoke", "glm53f"), "forum-solo-r0--glm53f-smoke")
        self.assertEqual(orchestrate.make_run_id("forum", 1, 0, "-smoke"), "forum-solo-r0-smoke")

    def test_rejects_bad_slug_and_long_id(self):
        for bad in ("GLM", "g", "glm-5", "a" * 13):
            with self.assertRaises(ValueError):
                orchestrate.make_run_id("forum", 1, 0, slug=bad)
        with self.assertRaises(ValueError):
            orchestrate.make_run_id("forum", 1, 0, "-" + "x" * 90, "glm53f")

    def test_tag_after_slug_needs_dash(self):
        with self.assertRaises(ValueError):
            orchestrate.make_run_id("forum", 1, 0, "foo", "glm53f")
        self.assertEqual(orchestrate.make_run_id("forum", 1, 0, "foo"), "forum-solo-r0foo")  # legacy: unchecked

    def test_default_slug_has_no_segment_on_any_lane(self):
        ctx = orchestrate.RunContext("qwen38", MODELS["qwen38"], provider_spec("qwen38"), 19100)
        self.assertFalse(ctx.is_default)
        self.assertEqual(orchestrate.run_id_for("irc", 4, 2, "", ctx), "irc-multi4-r2")
        self.assertEqual(orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS)), HARNESS_DEFAULTS)

    def test_vm_names_differ_per_model(self):
        ids = {orchestrate.run_id_for("forum", 1, 0, "", c) for c in
               (orchestrate.default_ctx(), *(orchestrate.RunContext(s, MODELS[s], provider_spec(s), 19100)
                                             for s in ("mistral24", "glm53f", "gemma31")))}
        self.assertEqual(len({f"sb-{r}-a0" for r in ids}), 4)


class SandboxPolicyTest(unittest.TestCase):
    """The agent-VM network policy is the security contract; pin its exact flags here too."""

    def test_agent_policy_flags(self):
        recorded = []
        Sandbox(msb="msb", runner=ok_runner(recorded)).create_agent_vm("sb-x-a2", 2)
        self.assertEqual(recorded, [[
            "msb", "create", IMAGE, "--name", "sb-x-a2", "--replace", "--no-net",
            "--net-rule", "allow@dns",
            "--net-rule", "allow@api.cerebras.ai:tcp:443",
            "--net-rule", "allow@host:tcp:19002",
            "--secret", "CEREBRAS_API_KEY@api.cerebras.ai",
            "--secret-violation-action", "block",
        ]])

    def test_board_publishes_only_loopback(self):
        recorded = []
        Sandbox(msb="msb", runner=ok_runner(recorded)).create_board_vm("sb-x-board", 2)
        self.assertEqual(recorded, [[
            "msb", "create", IMAGE, "--name", "sb-x-board", "--replace",
            "--net-default-egress", "deny",
            "-p", "127.0.0.1:19000:9000", "-p", "127.0.0.1:19001:9001",
        ]])

    def test_berget_lane_flags(self):
        recorded = []
        sb = Sandbox(msb="msb", runner=ok_runner(recorded), host_port_base=19100,
                     inference_host="api.berget.ai", secret_env="BERGET_API_KEY")
        sb.create_board_vm("sb-y-board", 2)
        sb.create_agent_vm("sb-y-a1", 1)
        self.assertEqual(recorded, [[
            "msb", "create", IMAGE, "--name", "sb-y-board", "--replace",
            "--net-default-egress", "deny",
            "-p", "127.0.0.1:19100:9000", "-p", "127.0.0.1:19101:9001",
        ], [
            "msb", "create", IMAGE, "--name", "sb-y-a1", "--replace", "--no-net",
            "--net-rule", "allow@dns",
            "--net-rule", "allow@api.berget.ai:tcp:443",
            "--net-rule", "allow@host:tcp:19101",
            "--secret", "BERGET_API_KEY@api.berget.ai",
            "--secret-violation-action", "block",
        ]])

    def test_ctx_sandbox_kwargs(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(berget_ctx(Path(td), "gemma31", 19300).sandbox_kwargs(),
                             {"host_port_base": 19300, "inference_host": "api.berget.ai",
                              "secret_env": "BERGET_API_KEY"})
        self.assertEqual(orchestrate.default_ctx().sandbox_kwargs(),
                         {"host_port_base": 19000, "inference_host": "api.cerebras.ai",
                          "secret_env": "CEREBRAS_API_KEY"})


class TeardownTimeoutTest(unittest.TestCase):
    """Polling, collection and teardown run without a timeout (as before the refactor), so a hung
    msb blocks instead of raising out of the finally block and skipping `msb rm` and run.json."""

    def test_best_effort_calls_have_no_timeout(self):
        calls = []
        sb = Sandbox(msb="msb", runner=lambda argv, *, check, timeout:
                     calls.append((argv[1], check, timeout)) or types.SimpleNamespace(returncode=1))
        self.assertFalse(sb.is_done("a"))
        sb.collect("a", "/root/logs/x", "/tmp/x")
        sb.rm("a", "b")
        self.assertEqual(calls, [("exec", False, None), ("cp", False, None), ("rm", False, None)])

    def test_setup_calls_keep_timeout(self):
        calls = []
        sb = Sandbox(msb="msb", runner=lambda argv, *, check, timeout:
                     calls.append((argv[1], check, timeout)) or types.SimpleNamespace(returncode=0))
        sb.exec("a", "true")
        sb.cp("src", "a:/dst")
        self.assertEqual(calls, [("exec", True, 600), ("cp", True, 600)])


class WaitDoneTest(unittest.TestCase):
    """A harness that dies without DONE ends the wait after DEAD_POLLS polls and is a straggler."""

    def test_dead_harness_is_straggler(self):
        class FakeSb:
            def __init__(self):
                self.polls = 0

            def is_done(self, name):
                self.polls += name == "a0"
                return name == "a0" and self.polls >= 3

            def harness_dead(self, name):
                return name == "a1"

        with mock.patch.object(orchestrate, "POLL_INTERVAL_S", 0), contextlib.redirect_stdout(io.StringIO()):
            left = orchestrate.wait_done(FakeSb(), "x", ["a0", "a1", "a2"], time.time() + 0.5)
        self.assertEqual(left, ["a1", "a2"])

    def test_harness_dead_reads_msb_failure_as_alive(self):
        for rc, dead in ((0, False), (1, True), (2, False)):
            sb = Sandbox(msb="msb", runner=lambda argv, *, check, timeout: types.SimpleNamespace(returncode=rc))
            self.assertIs(sb.harness_dead("a"), dead)


class CliTest(unittest.TestCase):
    # Baseline: random.Random(1234), one shuffle per rep of [(iface, n) for iface in INTERFACES
    # for n in (1, multi)]. Matches the start order of the pilot runs in runs/.
    PILOT_3x4 = [
        (0, "forum", 4), (0, "wiki", 4), (0, "wiki", 1), (0, "cache", 1),
        (0, "cache", 4), (0, "irc", 1), (0, "forum", 1), (0, "irc", 4),
        (1, "cache", 1), (1, "cache", 4), (1, "wiki", 4), (1, "forum", 1),
        (1, "irc", 4), (1, "wiki", 1), (1, "irc", 1), (1, "forum", 4),
        (2, "forum", 4), (2, "wiki", 1), (2, "forum", 1), (2, "irc", 1),
        (2, "wiki", 4), (2, "cache", 1), (2, "irc", 4), (2, "cache", 4),
    ]

    def test_pilot_schedule(self):
        self.assertEqual(orchestrate.pilot_schedule(3, 4), self.PILOT_3x4)

    def _main(self, *argv):
        calls = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.multiple(orchestrate, RUNS=Path(td) / "runs", load_env=lambda: None,
                                    run_one=lambda *a, **k: calls.append((a, k)), LOCK_DIR=Path(td)):
            orchestrate.main(list(argv))
        return calls

    def test_pilot_runs_schedule_in_order(self):
        calls = self._main("pilot")
        self.assertEqual([(a[2], a[0], a[1]) for a, _ in calls], self.PILOT_3x4)
        self.assertTrue(all(a[3] == HARNESS_DEFAULTS and k == {"tag": ""} for a, k in calls))

    def test_smoke_caps_budget_and_tags(self):
        [(a, k)] = self._main("smoke", "--budget", "40")
        self.assertEqual(a, ("forum", 1, 0, {**HARNESS_DEFAULTS, "budget": 25}))
        self.assertEqual(k, {"tag": "-smoke"})
        [(a, k)] = self._main("smoke", "--budget", "10", "--tag=-x")
        self.assertEqual(a[3]["budget"], 10)
        self.assertEqual(k, {"tag": "-x"})

    def test_run_passes_flags(self):
        [(a, k)] = self._main("run", "--iface", "irc", "--agents", "4", "--rep", "2",
                              "--budget", "30", "--tag=-t")
        self.assertEqual(a, ("irc", 4, 2, {**HARNESS_DEFAULTS, "budget": 30}))
        self.assertEqual(k, {"tag": "-t"})


class ModelCliTest(unittest.TestCase):
    """CLI for non-default models: provider derivation, ledgered runs, exit codes and batch blocks."""

    def setUp(self):
        # ledger mechanics are tested at a fixed historical provider spec, independent of the
        # configured (authorized) credit/cap in config.PROVIDERS
        import config as _config
        _pin = mock.patch.dict(_config.PROVIDERS["berget"], {"credit_eur": 44.0, "spend_cap_eur": 38.0})
        _pin.start()
        self.addCleanup(_pin.stop)

    def _main(self, *argv, run_one=None):
        calls, envs = [], []
        td_obj = tempfile.TemporaryDirectory()
        self.addCleanup(td_obj.cleanup)
        td = Path(td_obj.name)

        def fake_run_one(*a, **k):
            calls.append((a, k))
            if run_one:
                run_one(*a, **k)

        out, err, code = io.StringIO(), io.StringIO(), None
        with mock.patch.multiple(orchestrate, RUNS=td / "runs", load_env=lambda *a: envs.append(a),
                                 run_one=fake_run_one, LOCK_DIR=td), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                orchestrate.main(list(argv))
            except SystemExit as e:
                code = e.code
        self.td = td
        return calls, envs, code, out.getvalue() + err.getvalue()

    def test_smoke_berget(self):
        [(a, k)], envs, code, _ = self._main("smoke", "--model", "zai-org/GLM-5.3-Flash", "--host-port-base", "19200")
        self.assertIsNone(code)
        self.assertEqual(envs, [("BERGET_API_KEY",)])
        self.assertEqual(a, ("forum", 1, 0, {**HARNESS_DEFAULTS, "budget": 25, "model": "zai-org/GLM-5.3-Flash",
                                             "provider": "berget"}))
        self.assertEqual(k["tag"], "-smoke")
        ctx = k["ctx"]
        self.assertEqual((ctx.slug, ctx.host_port_base, ctx.ledger.cap), ("glm53f", 19200, 38.0))
        self.assertEqual(ctx.ledger.path, self.td / "runs/_spend/berget.json")

    def test_provider_mismatch_and_missing_model(self):
        for argv in (("smoke", "--provider", "cerebras", "--model", "glm53f"),
                     ("smoke", "--provider", "berget"), ("smoke", "--model", "nope"),
                     ("smoke", "--model", "glm53f", "--cap-eur", "50")):
            calls, _, code, _ = self._main(*argv)
            self.assertEqual(calls, [])
            self.assertIsInstance(code, str)

    def test_default_model_only_on_default_lane_and_dashed_tags(self):
        for argv in (("pilot", "--host-port-base", "19100"), ("smoke", "--model", "qwen38", "--host-port-base", "19200"),
                     ("smoke", "--model", "glm53f", "--host-port-base", "19200", "--tag", "foo")):
            calls, _, code, _ = self._main(*argv)
            self.assertEqual(calls, [])
            self.assertIsInstance(code, str)

    def test_fatal_and_budget_exit_codes_escape_loops(self):
        def fatal(*a, **k):
            raise FatalProviderError("x: provider HTTP 402")

        def budget(*a, **k):
            raise BudgetCapReached("at x: committed €1.00 + projected €1.00 > cap €1.50")

        for argv in (("pilot",), ("pilot", "--model", "mistral24", "--host-port-base", "19100"),
                     ("batch", "--model", "mistral24", "--host-port-base", "19100")):
            calls, _, code, _ = self._main(*argv, run_one=fatal)
            self.assertEqual((len(calls), code), (1, 3))
            calls, _, code, text = self._main(*argv, run_one=budget)
            self.assertEqual((len(calls), code), (1, 4))
            self.assertIn("[budget] stop at x:", text)

    def test_ordinary_errors_continue_with_run_id_line(self):
        def boom(*a, **k):
            raise RuntimeError("msb failed")

        calls, _, code, text = self._main("pilot", "--reps", "1", "--model", "gemma31", "--host-port-base", "19300",
                                          run_one=boom)
        self.assertEqual((len(calls), code), (8, None))
        self.assertIn("[error] forum-multi4-r0--gemma31: msb failed", text)
        calls, _, _, text = self._main("pilot", "--reps", "1", run_one=boom)
        self.assertIn("[error] forum n=4 rep=0: msb failed", text)

    def test_batch_runs_blocks_until_rejected(self):
        """Blocks are admitted against prior projections (planned runs stay committed with run_one
        mocked) until the next block would pass the €38 cap; this lane runs only its own model."""
        calls, _, code, text = self._main("batch", "--model", "glm53f", "--host-port-base", "19200")
        self.assertIsNone(code)
        self.assertEqual([(a[2], a[0], a[1]) for a, _ in calls], orchestrate.pilot_schedule(6, 4))
        self.assertTrue(all(k["ctx"].slug == "glm53f" and k["tag"] == "" for _, k in calls))
        self.assertIn("[budget] stop at r6: committed €", text)
        st = json.loads((self.td / "runs/_spend/berget.json").read_text())
        self.assertEqual(st["blocks"]["mistral24,glm53f,gemma31"],
                         {**{str(r): "admitted" for r in range(6)}, "6": "rejected"})
        self.assertEqual(len(st["runs"]), 6 * 8 * 3)
        self.assertLessEqual(sum(e["projected_eur"] for e in st["runs"].values()), 38.0)

    def test_batch_needs_priced_model_in_batch(self):
        for argv in (("batch",), ("batch", "--model", "glm53f", "--batch-models", "mistral24,gemma31")):
            calls, _, code, _ = self._main(*argv)
            self.assertEqual(calls, [])
            self.assertIsInstance(code, str)


if __name__ == "__main__":
    unittest.main()


class VoidRetryTest(unittest.TestCase):
    """run_cells retries a void run once, then logs [void] and goes on with the next cell."""

    def test_void_retried_once_then_skipped(self):
        calls = []

        def fake_run_one(iface, n, rep, harness, tag="", sb=None, ctx=None):
            calls.append((iface, rep))
            if iface == "wiki":
                raise orchestrate.RunVoid(f"{iface}: every agent crashed (HTTP 400)")

        ctx = types.SimpleNamespace(is_default=False)
        err = io.StringIO()
        with mock.patch.object(orchestrate, "run_one", fake_run_one), contextlib.redirect_stderr(err):
            orchestrate.run_cells([(0, "wiki", 1), (0, "forum", 1)], {}, "", ctx)
        self.assertEqual(calls, [("wiki", 0), ("wiki", 0), ("forum", 0)])
        self.assertEqual(err.getvalue().count("[void] wiki: every agent crashed"), 1)

    def test_fatal_still_stops_the_lane(self):
        def fake_run_one(*a, **k):
            raise orchestrate.FatalProviderError("x: provider HTTP 402")

        with mock.patch.object(orchestrate, "run_one", fake_run_one), self.assertRaises(orchestrate.FatalProviderError):
            orchestrate.run_cells([(0, "wiki", 1)], {}, "", types.SimpleNamespace(is_default=False))


class CodeCommentIfaceTest(unittest.TestCase):
    """js/html (lab/board/codecomments.py) are first-class ifaces: registered, addressable over HTTP on
    board.internal, accepted by the CLI and the run-id grammar, and booted by the unchanged command
    sequence. irc stays valid."""

    NEW = ("js", "html")

    def test_registered_and_addressable(self):
        from board.channel import CHANNELS
        from config import ALL_INTERFACES, IFACE_RE, board_port_for, board_url_for
        for iface in self.NEW:
            self.assertIn(iface, ALL_INTERFACES)
            self.assertTrue(IFACE_RE.fullmatch(iface))
            ch = CHANNELS[iface]
            self.assertEqual(ch.scheme, "http")
            port = board_port_for(iface)
            self.assertIsInstance(port, int)
            self.assertNotEqual(port, CHANNELS["irc"].default_port)  # the relay's IRC LF fix stays off
            self.assertTrue(board_url_for(iface).startswith(f"http://board.internal:{port}/"))
        self.assertIn("irc", ALL_INTERFACES)

    def test_cli_accepts(self):
        for iface in self.NEW + ("irc",):
            self.assertEqual(orchestrate.parse_args(["run", "--iface", iface]).iface, iface)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            orchestrate.parse_args(["run", "--iface", "JS"])

    def test_run_id_grammar(self):
        from config import id_goal, vm_run_id
        for iface in self.NEW:
            rid = orchestrate.make_run_id(iface, 4, 2, "-x", "glm53f", "unsolvable")
            self.assertEqual(rid, f"{iface}-multi4-r2-unsolvable--glm53f-x")
            self.assertEqual(id_goal(rid), "unsolvable")
            self.assertEqual(vm_run_id(rid), f"{iface}-multi4-r2--glm53f-x")
            self.assertEqual(orchestrate.make_run_id(iface, 1, 0), f"{iface}-solo-r0")
        self.assertEqual(orchestrate.make_run_id("irc", 4, 0), "irc-multi4-r0")
        for bad in ("JS", "js2", "code-comments", "", "h_tml"):
            with self.assertRaises(ValueError):
                orchestrate.make_run_id(bad, 1, 0)

    def test_dash_run_id_grammar(self):
        dash = LAB.parent / "dash" / "server"
        sys.path.insert(0, str(dash))
        try:
            from obs import parse as P
        finally:
            sys.path.remove(str(dash))
        from config import id_goal
        for iface in self.NEW + ("irc", "guestbook", "kv"):
            for goal in ("solvable", "unsolvable"):
                rid = orchestrate.make_run_id(iface, 4, 1, "-smoke", "", goal)
                p = P.parse_run_id(rid)
                self.assertTrue(p["matched"], rid)
                self.assertEqual((p["iface"], p["n_agents"], p["rep"], p["tag"], p["goal"]),
                                 (iface, 4, 1, "smoke", goal))
                self.assertEqual(p["goal"], id_goal(rid))

    def test_boot_sequence_and_cfg(self):
        from config import board_port_for, board_url_for
        for iface in self.NEW:
            for n in (1, 4):
                with tempfile.TemporaryDirectory() as td:
                    td = Path(td)
                    lab, runs = td / "lab", td / "runs"
                    make_fixture(lab)
                    runs.mkdir()
                    recorded = []
                    sb = Sandbox(msb="msb", runner=ok_runner(recorded))
                    fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
                    with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                             RUNS=runs, time=fake_time):
                        run_quietly(iface, n, 0, dict(HARNESS_DEFAULTS), sb=sb)
                    self.assertEqual(split_polls(recorded), split_polls(expected_sequence(lab, runs, iface, n)))
                    rid = f"{iface}-{'solo' if n == 1 else 'multi4'}-r0"
                    meta = json.loads((runs / rid / "run.json").read_text())
                    self.assertEqual(meta["iface"], iface)
                    cfg = json.loads((runs / rid / f"cfg-a{n - 1}.json").read_text())
                    self.assertEqual((cfg["board_url"], cfg["board_port"]),
                                     (board_url_for(iface), board_port_for(iface)))
