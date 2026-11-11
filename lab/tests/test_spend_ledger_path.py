"""Spend-ledger location: a git worktree charges the MAIN checkout's runs/_spend ledger, never a fresh one.

Builds a throwaway main checkout + worktree with git in a temp dir and points config/orchestrate at the
worktree. Ledgers hold numbers only; no model call, VM or real ledger is touched.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import config  # noqa: E402
import orchestrate  # noqa: E402
from spend import BudgetCapReached, Ledger  # noqa: E402


def make_worktree(td: Path):
    """(main checkout, worktree) of a one-commit repo under td."""
    main = td / "main"
    main.mkdir()
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["init", "-q"], check=True)
    (main / "f").write_text("x")
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    wt = td / "wt"
    subprocess.run(git + ["worktree", "add", "-q", str(wt)], check=True, capture_output=True)
    return main.resolve(), wt.resolve()


def cli_ns(**kw):
    base = dict(model="glm53f", provider=None, host_port_base=19100, tag="", goal=config.DEFAULT_GOAL,
                cap_eur=None, seed_path=None)
    return types.SimpleNamespace(**{**base, **kw})


class SpendDirTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name).resolve()
        self.main, self.wt = make_worktree(self.td)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(config.SPEND_DIR_ENV, None)

    @contextlib.contextmanager
    def in_checkout(self, repo: Path):
        """config and orchestrate as if imported from `repo` (its own runs tree)."""
        runs = repo / "runs"
        with mock.patch.multiple(config, REPO=repo, RUNS=runs, SPEND_DIR=runs / "_spend"), \
                mock.patch.multiple(orchestrate, REPO=repo, RUNS=runs):
            yield runs

    def test_main_checkout_of_worktree_and_main(self):
        self.assertEqual(config.main_checkout(self.wt), self.main)
        self.assertEqual(config.main_checkout(self.main), self.main)
        self.assertIsNone(config.main_checkout(self.td / "nowhere"))

    def test_worktree_resolves_main_ledger(self):
        with self.in_checkout(self.wt) as runs:
            self.assertEqual(config.spend_dir(), self.main / "runs" / "_spend")
            self.assertEqual(config.spend_dir(runs), self.main / "runs" / "_spend")
            self.assertEqual(Ledger().path, self.main / "runs" / "_spend" / "berget.json")
        with self.in_checkout(self.main):
            self.assertEqual(config.spend_dir(), self.main / "runs" / "_spend")

    def test_env_override(self):
        os.environ[config.SPEND_DIR_ENV] = str(self.td / "ledgers")
        with self.in_checkout(self.wt):
            self.assertEqual(config.spend_dir(), self.td / "ledgers")
            self.assertEqual(Ledger(provider="berget").path, self.td / "ledgers" / "berget.json")
            # a scratch runs tree keeps its own ledger, whatever the environment says
            self.assertEqual(config.spend_dir(self.td / "scratch"), self.td / "scratch" / "_spend")

    def test_scratch_runs_tree_keeps_its_own_ledger(self):
        with self.in_checkout(self.wt):
            self.assertEqual(config.spend_dir(self.td / "scratch"), self.td / "scratch" / "_spend")
            self.assertEqual(Ledger(runs_dir=self.td / "scratch").path, self.td / "scratch/_spend/berget.json")

    def test_no_git_falls_back_to_local(self):
        nowhere = self.td / "nowhere"
        with self.in_checkout(nowhere) as runs:
            self.assertEqual(config.spend_dir(), runs / "_spend")

    def test_worktree_sees_main_spend_and_never_creates_a_fresh_ledger(self):
        main_ledger = self.main / "runs" / "_spend" / "berget.json"
        Ledger(main_ledger, cap=2.0, runs_dir=self.main / "runs").halt("placeholder")
        with self.in_checkout(self.wt):
            ctx = orchestrate.ctx_from_args(cli_ns(cap_eur=2.0))
            self.assertEqual(ctx.ledger.path, main_ledger)
            self.assertTrue(ctx.ledger.halted)
            with self.assertRaises(BudgetCapReached):
                ctx.ledger.reserve("forum-solo-r0--glm53f", "glm53f", 1, 0)
        self.assertFalse((self.wt / "runs" / "_spend").exists())

    def test_orchestrate_ledger_charges_main_committed_total(self):
        main_ledger = self.main / "runs" / "_spend" / "berget.json"
        led = Ledger(main_ledger, cap=1.0, runs_dir=self.main / "runs")
        led.reserve("forum-solo-r0--glm53f", "glm53f", 1, 0)
        led.settle("forum-solo-r0--glm53f", {"prompt": 3_000_000, "completion": 0, "reasoning": 0,
                                             "measured": 1, "usage_source": "steps"})
        self.assertAlmostEqual(led.committed(), 0.75)
        with self.in_checkout(self.wt):
            ctx = orchestrate.ctx_from_args(cli_ns(cap_eur=1.0))
            self.assertAlmostEqual(ctx.ledger.committed(), 0.75)
            with self.assertRaises(BudgetCapReached):  # projection on top of the main spend exceeds the cap
                ctx.ledger.reserve("forum-solo-r1--glm53f", "glm53f", 1, 1)

    def test_env_file_still_resolves_main(self):
        (self.main / ".env").write_text("QD_TEST_PLACEHOLDER_KEY=lorem\n")
        with self.in_checkout(self.wt):
            self.assertEqual(orchestrate.env_file(), self.main / ".env")


class RecoveryRunsDirTest(unittest.TestCase):
    """A dead reservation is settled from the runs tree of the process that reserved it."""

    def test_recover_reads_reserving_runs_dir(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td).resolve()
            path, wt_runs, main_runs = td / "_spend/berget.json", td / "wt/runs", td / "main/runs"
            rid = "forum-solo-r0--glm53f"
            Ledger(path, cap=5.0, runs_dir=wt_runs).reserve(rid, "glm53f", 1, 0)
            st = json.loads(path.read_text())
            self.assertEqual(st["runs"][rid]["runs_dir"], str(wt_runs))
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            st["runs"][rid]["pid"] = dead.pid
            path.write_text(json.dumps(st))
            (wt_runs / rid / "a0").mkdir(parents=True)
            (wt_runs / rid / "a0" / "summary.json").write_text(json.dumps({"tokens": {"prompt": 1_000_000, "completion": 0}}))
            e = Ledger(path, cap=5.0, runs_dir=main_runs).state()["runs"][rid]
            self.assertEqual((e["status"], e["usage_source"], e["prompt"], e.get("recovered")),
                             ("settled", "summary", 1_000_000, True))
            self.assertAlmostEqual(e["eur"], 0.25)


class ProviderInvariantTest(unittest.TestCase):
    def test_unpriced_models_are_on_uncapped_providers(self):
        for slug, m in config.MODELS.items():
            capped = config.PROVIDERS[m["provider"]].get("spend_cap_eur") is not None
            self.assertEqual(None in config.price(slug), not capped, slug)


if __name__ == "__main__":
    unittest.main()
