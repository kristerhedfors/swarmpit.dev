"""Spend ledger: admission, reservation, settlement, projections and cost, plus the orchestrator's
port-lane lock and batch block slicing.

Every ledger lives in a temp dir; run dirs are tiny numeric-only fixtures written by the tests.
"""
import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import orchestrate  # noqa: E402
from config import COST_PRIOR_TOKENS, price  # noqa: E402
from spend import BudgetCapReached, Ledger, cost, run_usage  # noqa: E402


def prior(slug, n):
    return cost(slug, {k: n * v for k, v in COST_PRIOR_TOKENS.items()})


def write_agent(run_dir: Path, i, steps=None, summary=None):
    """a<i>/steps.jsonl from usage dicts (as `model` records) and/or a<i>/summary.json."""
    d = run_dir / f"a{i}"
    d.mkdir(parents=True, exist_ok=True)
    if steps is not None:
        recs = [{"kind": "start"}] + [{"kind": "model", "call": k, "usage": u} for k, u in enumerate(steps)]
        (d / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs) + "not json\n")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary))


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _contend(path, runs_dir, cap, prefix, q):
    """Child process: reserve runs until refused; report how many it got."""
    led = Ledger(path, cap=cap, runs_dir=runs_dir)
    got = 0
    for i in range(20):
        try:
            led.reserve(f"forum-solo-r{i}--{prefix}", "mistral24", 1, i)
            got += 1
        except BudgetCapReached:
            pass
    q.put(got)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        # ledger mechanics are tested at a fixed historical provider spec, independent of the
        # configured (authorized) credit/cap in config.PROVIDERS
        import config as _config
        _pin = mock.patch.dict(_config.PROVIDERS["berget"], {"credit_eur": 44.0, "spend_cap_eur": 38.0})
        _pin.start()
        self.addCleanup(_pin.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)
        self.runs = self.td / "runs"
        self.runs.mkdir()
        self.path = self.runs / "_spend/berget.json"

    def ledger(self, cap=38.0):
        return Ledger(self.path, cap=cap, runs_dir=self.runs)

    def raw(self):
        return json.loads(self.path.read_text())

    def settle_observed(self, led, run_id, slug, n, prompt, completion, reasoning=0):
        """Reserve and settle a run whose agents logged the given total usage (split over agent 0)."""
        led.reserve(run_id, slug, n, 0)
        d = self.runs / run_id
        write_agent(d, 0, steps=[{"prompt_tokens": prompt, "completion_tokens": completion,
                                  "reasoning_tokens": reasoning}])
        for i in range(1, n):
            write_agent(d, i, steps=[])
        return led.settle(run_id, run_usage(d))


class CostTest(unittest.TestCase):
    def test_cost_and_reasoning_billing(self):
        u = {"prompt": 1_000_000, "completion": 100_000, "reasoning": 50_000}
        self.assertAlmostEqual(cost("glm53f", u, bill_reasoning=True), 0.25 + 0.15 * 0.5)
        self.assertAlmostEqual(cost("glm53f", u, bill_reasoning=False), 0.25 + 0.10 * 0.5)
        # more reasoning than completion cannot have been included in completion: billed regardless
        u["reasoning"] = 200_000
        self.assertAlmostEqual(cost("glm53f", u, bill_reasoning=False), 0.25 + 0.30 * 0.5)
        self.assertEqual(cost("qwen38", u), 0.0)
        self.assertEqual(cost("mistral24", {"prompt": None, "completion": None}), 0.0)

    def test_prices_come_from_config(self):
        self.assertEqual(price("mistral24"), (0.30, 0.30))
        self.assertEqual(price("qwen38"), (None, None))


class RunUsageTest(unittest.TestCase):
    def test_prefers_steps_then_summary_then_missing(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "forum-multi4-r0--glm53f"
            write_agent(d, 0, steps=[{"prompt_tokens": 100, "completion_tokens": 10,
                                      "completion_tokens_details": {"reasoning_tokens": 4}},
                                     {"prompt_tokens": 200, "completion_tokens": None, "reasoning_tokens": 3,
                                      "prompt_tokens_details": None}],
                        summary={"tokens": {"prompt": 9999, "completion": 9999}})
            write_agent(d, 1, summary={"crashed": "x", "tokens": {"prompt": 50, "completion": None}})
            write_agent(d, 2, steps=[{"prompt_tokens": None, "completion_tokens": 7, "reasoning_tokens": None}])
            write_agent(d, 3)  # nothing collected
            u = run_usage(d)
        self.assertEqual(u, {"prompt": 350, "completion": 17, "reasoning": 7, "measured": 3, "usage_source": "mixed"})

    def test_empty_run_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(run_usage(Path(td)), {"prompt": 0, "completion": 0, "reasoning": 0, "measured": 0,
                                                   "usage_source": "none"})


class ReserveTest(LedgerTestCase):
    def test_reserve_refuses_over_cap(self):
        p = prior("mistral24", 4)
        led = self.ledger(cap=2.5 * p)
        led.reserve("forum-multi4-r0--mistral24", "mistral24", 4, 0)
        led.reserve("wiki-multi4-r0--mistral24", "mistral24", 4, 0)
        with self.assertRaises(BudgetCapReached):
            led.reserve("irc-multi4-r0--mistral24", "mistral24", 4, 0)
        self.assertAlmostEqual(led.committed(), 2 * p)
        self.assertNotIn("irc-multi4-r0--mistral24", self.raw()["runs"])

    def test_settle_charges_measured_usage_and_projects_lost_agents(self):
        led = self.ledger()
        rid = "forum-multi2-r0--glm53f"
        proj = led.reserve(rid, "glm53f", 2, 0)
        d = self.runs / rid
        write_agent(d, 0, steps=[{"prompt_tokens": 1_000_000, "completion_tokens": 0}])
        eur = led.settle(rid, run_usage(d))
        self.assertAlmostEqual(eur, 0.25 + proj / 2)
        e = self.raw()["runs"][rid]
        self.assertEqual((e["status"], e["usage_source"], e["prompt"]), ("settled", "steps+projection", 1_000_000))

    def test_setup_failure_before_harness_charges_nothing_extra(self):
        led = self.ledger()
        rid = "forum-solo-r0--glm53f"
        led.reserve(rid, "glm53f", 1, 0)
        self.assertEqual(led.settle(rid, run_usage(self.runs / rid), started=False), 0.0)
        self.assertEqual(self.raw()["runs"][rid]["usage_source"], "not-started")

    def test_rerun_rekeys_old_entry(self):
        led = self.ledger()
        rid = "forum-solo-r0--glm53f"
        led.reserve(rid, "glm53f", 1, 0)
        led.settle(rid, run_usage(self.runs / rid), started=False)
        led.reserve(rid, "glm53f", 1, 0)
        runs = self.raw()["runs"]
        self.assertEqual(runs[rid]["status"], "reserved")
        self.assertEqual([k.split("@")[0] for k in runs], [rid, rid])

    def test_abort_rekeys_and_halt_blocks_everything(self):
        led = self.ledger()
        rid = "forum-solo-r0--glm53f"
        led.reserve(rid, "glm53f", 1, 0)
        led.settle(rid, run_usage(self.runs / rid), aborted=True)
        [(key, e)] = self.raw()["runs"].items()
        self.assertTrue(key.startswith(rid + "@"))
        self.assertEqual(e["status"], "aborted")
        led.halt("x: provider HTTP 402")
        other = self.ledger()  # another lane
        with self.assertRaises(BudgetCapReached):
            other.reserve("wiki-solo-r0--gemma31", "gemma31", 1, 0)
        with self.assertRaises(BudgetCapReached):
            other.admit_block("k", 0, [])
        other.clear_halt()
        other.reserve(rid, "glm53f", 1, 0)  # the rerun gets a fresh entry
        self.assertEqual(self.raw()["runs"][rid]["status"], "reserved")

    def test_stale_reservations_are_recovered(self):
        led = self.ledger()
        logged, lost = "forum-solo-r0--glm53f", "wiki-multi4-r0--glm53f"
        led.reserve(logged, "glm53f", 1, 0)
        proj = led.reserve(lost, "glm53f", 4, 0)
        st = self.raw()
        pid = dead_pid()
        for e in st["runs"].values():
            e["pid"] = pid
        self.path.write_text(json.dumps(st))
        write_agent(self.runs / logged, 0, steps=[{"prompt_tokens": 400_000, "completion_tokens": 0}])
        (self.runs / lost).mkdir()  # killed before any log was collected
        runs = self.ledger().state()["runs"]
        self.assertEqual((runs[logged]["status"], runs[logged]["usage_source"]), ("settled", "steps"))
        self.assertAlmostEqual(runs[logged]["eur"], 0.1)
        self.assertEqual((runs[lost]["status"], runs[lost]["usage_source"]), ("settled", "projection"))
        self.assertAlmostEqual(runs[lost]["eur"], proj)
        self.assertTrue(runs[lost]["recovered"])

    def test_stale_reservation_found_under_aborted(self):
        led = self.ledger()
        rid = "forum-solo-r0--glm53f"
        led.reserve(rid, "glm53f", 1, 0)
        st = self.raw()
        st["runs"][rid]["pid"] = dead_pid()
        self.path.write_text(json.dumps(st))
        write_agent(self.runs / "_aborted" / f"{rid}-402", 0, summary={"tokens": {"prompt": 400_000, "completion": 0}})
        self.assertEqual(self.ledger().state()["runs"][rid]["usage_source"], "summary")

    def test_ledger_holds_no_secrets_and_rejects_cap_over_credit(self):
        with self.assertRaises(ValueError):
            self.ledger(cap=45.0)
        led = self.ledger()
        led.reserve("forum-solo-r0--glm53f", "glm53f", 1, 0)
        st = self.raw()
        self.assertEqual((st["provider"], st["credit_eur"], st["cap_eur"]), ("berget", 44.0, 38.0))
        self.assertNotIn("KEY", self.path.read_text())


class ContentionTest(LedgerTestCase):
    def test_two_processes_never_exceed_cap(self):
        p = prior("mistral24", 1)
        cap = 9.5 * p
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_contend, args=(self.path, self.runs, cap, tag, q)) for tag in ("aa", "bb")]
        for pr in procs:
            pr.start()
        got = [q.get(timeout=60) for _ in procs]
        for pr in procs:
            pr.join(timeout=60)
        self.assertEqual(sum(got), 9)
        runs = self.raw()["runs"]
        self.assertEqual(len(runs), 9)
        self.assertLessEqual(sum(e["projected_eur"] for e in runs.values()), cap)


class AdmitBlockTest(LedgerTestCase):
    MODELS = ("mistral24", "glm53f", "gemma31")

    def plan(self, rep):
        return [(orchestrate.make_run_id(iface, n, r, slug=s), s, n)
                for s in self.MODELS for r, iface, n in orchestrate.batch_block(rep, 4)]

    def block_cost(self):
        return sum(prior(s, n) for _, s, n in self.plan(0))

    def test_all_or_nothing_and_first_decision_is_final(self):
        bc = self.block_cost()
        lane_a, lane_b = self.ledger(cap=1.5 * bc), self.ledger(cap=1.5 * bc)
        self.assertEqual(lane_a.admit_block("k", 0, self.plan(0)), "admitted")
        self.assertEqual(len(self.raw()["runs"]), 24)
        self.assertTrue(all(e["status"] == "planned" for e in self.raw()["runs"].values()))
        self.assertEqual(lane_b.admit_block("k", 0, self.plan(0)), "admitted")  # obeys, adds nothing
        self.assertEqual(len(self.raw()["runs"]), 24)
        self.assertEqual(lane_b.admit_block("k", 1, self.plan(1)), "rejected")
        self.assertEqual(len(self.raw()["runs"]), 24)  # nothing of the rejected block is planned
        # the decision stands even if a later lane would fit (e.g. a larger cap)
        self.assertEqual(self.ledger(cap=38.0).admit_block("k", 1, self.plan(1)), "rejected")

    def test_planned_runs_reserve_within_their_projection(self):
        led = self.ledger(cap=self.block_cost() + 1e-6)
        led.admit_block("k", 0, self.plan(0))
        for rid, s, n in self.plan(0):
            led.reserve(rid, s, n, 0)
        self.assertAlmostEqual(led.committed(), self.block_cost())
        with self.assertRaises(BudgetCapReached):  # an unplanned run does not fit
            led.reserve("forum-solo-r9--glm53f", "glm53f", 1, 9)

    def test_overrun_truncates_block(self):
        led = self.ledger(cap=self.block_cost() + 0.01)
        led.admit_block("k", 0, self.plan(0))
        rid, s, n = self.plan(0)[0]
        led.reserve(rid, s, n, 0)
        write_agent(self.runs / rid, 0, steps=[{"prompt_tokens": 3_000_000, "completion_tokens": 0}])
        led.settle(rid, run_usage(self.runs / rid))  # cost far above its projection
        nxt, s2, n2 = self.plan(0)[1]
        with self.assertRaises(BudgetCapReached):
            led.reserve(nxt, s2, n2, 0)
        st = self.raw()
        self.assertEqual(st["blocks"]["k"]["0"], "truncated")
        self.assertNotIn(nxt, st["runs"])
        self.assertEqual(led.admit_block("k", 0, self.plan(0)), "truncated")


class ProjectionTest(LedgerTestCase):
    def test_prior_then_cross_model_then_own_max(self):
        led = self.ledger()
        self.assertAlmostEqual(led.projected("glm53f", 4), prior("glm53f", 4))
        self.settle_observed(led, "forum-multi4-r0--mistral24", "mistral24", 4, 800_000, 40_000)
        self.assertAlmostEqual(led.projected("glm53f", 4),
                               1.2 * cost("glm53f", {"prompt": 800_000, "completion": 40_000}))
        self.assertAlmostEqual(led.projected("glm53f", 1), prior("glm53f", 1))  # other n: still prior
        e1 = self.settle_observed(led, "forum-multi4-r0--glm53f", "glm53f", 4, 400_000, 10_000)
        e2 = self.settle_observed(led, "wiki-multi4-r0--glm53f", "glm53f", 4, 600_000, 10_000, reasoning=5_000)
        self.assertAlmostEqual(led.projected("glm53f", 4), 1.2 * max(e1, e2))

    def test_degenerate_runs_ignored_and_single_own_run_raises(self):
        led = self.ledger()
        for rid in ("forum-multi4-r0--glm53f", "wiki-multi4-r0--glm53f"):  # crashed at once: ~no tokens
            self.settle_observed(led, rid, "glm53f", 4, 0, 0)
        self.assertAlmostEqual(led.projected("glm53f", 4), prior("glm53f", 4))
        big = self.settle_observed(led, "irc-multi4-r0--glm53f", "glm53f", 4, 3_000_000, 50_000)
        self.assertAlmostEqual(led.projected("glm53f", 4), 1.2 * big)  # one own run above the prior counts
        self.settle_observed(led, "cache-multi4-r0--mistral24", "mistral24", 4, 200_000, 1_000)
        # one own run below the cross-model estimate does not lower it
        self.assertAlmostEqual(led.projected("mistral24", 4),
                               1.2 * cost("mistral24", {"prompt": 3_000_000, "completion": 50_000}))

    def test_tagged_and_projected_runs_do_not_feed_projections(self):
        led = self.ledger()
        self.settle_observed(led, "forum-solo-r0--mistral24-smoke", "mistral24", 1, 5_000_000, 0)
        rid = "wiki-solo-r0--mistral24"
        led.reserve(rid, "mistral24", 1, 0)
        led.settle(rid, run_usage(self.runs / rid))  # nothing collected: projection
        self.assertAlmostEqual(led.projected("glm53f", 1), prior("glm53f", 1))

    def test_unsolvable_runs_are_keyed_apart_and_feed_projections(self):
        """`-unsolvable` ids are their own ledger entries; untagged ones are observed runs like solvable
        ones (same model, population and budget), tagged ones (smoke) are not."""
        led = self.ledger()
        self.settle_observed(led, "forum-solo-r0-unsolvable--mistral24-smoke", "mistral24", 1, 5_000_000, 0)
        self.assertAlmostEqual(led.projected("glm53f", 1), prior("glm53f", 1))
        a = self.settle_observed(led, "forum-solo-r0--glm53f", "glm53f", 1, 400_000, 20_000)
        b = self.settle_observed(led, "forum-solo-r0-unsolvable--glm53f", "glm53f", 1, 800_000, 20_000)
        self.assertEqual(sorted(self.raw()["runs"]), ["forum-solo-r0--glm53f", "forum-solo-r0-unsolvable--glm53f",
                                                     "forum-solo-r0-unsolvable--mistral24-smoke"])
        self.assertAlmostEqual(led.projected("glm53f", 1), 1.2 * max(a, b))


class PortLaneTest(unittest.TestCase):
    def test_second_holder_fails_and_other_lanes_are_free(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(orchestrate, "LOCK_DIR", Path(td)):
            with orchestrate.port_lane(19100, 4):
                with self.assertRaises(SystemExit):
                    with orchestrate.port_lane(19100, 4):
                        pass
                with orchestrate.port_lane(19200, 4):
                    pass
                with self.assertRaises(SystemExit):  # overlaps 19102..19103
                    with orchestrate.port_lane(19102, 4):
                        pass
                with orchestrate.port_lane(19104, 4):  # adjacent, disjoint
                    pass
            with orchestrate.port_lane(19100, 4):  # released on exit
                pass
            with self.assertRaises(SystemExit):
                with orchestrate.port_lane(65534, 4):
                    pass


class BatchBlockTest(unittest.TestCase):
    def test_blocks_are_pilot_schedule_reps(self):
        full = orchestrate.pilot_schedule(10, 4)
        for r in range(10):
            block = orchestrate.batch_block(r, 4)
            self.assertEqual(block, full[8 * r:8 * r + 8])
            self.assertEqual(orchestrate.pilot_schedule(r + 1, 4), full[:8 * (r + 1)])
            self.assertTrue(all(rep == r for rep, _, _ in block))
            self.assertEqual(sorted((i, n) for _, i, n in block),
                             sorted((i, n) for i in ("forum", "wiki", "cache", "irc") for n in (1, 4)))


if __name__ == "__main__":
    unittest.main()
