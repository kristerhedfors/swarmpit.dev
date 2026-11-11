"""Unit tests for the content-free partial-engagement sub-signal in instigation.compute().

Run from the lab/ directory:  python3 -m unittest study.test_engagement

Fixtures are SYNTHETIC structural run dirs in tmp dirs (integers, timestamps, fixed enum codes, and the
neutral placeholder "lorem"). Free-text fields (req path/ua, relay err, step command) carry a tripwire to
prove engagement never reads or emits them.
"""
import json
import math
import tempfile
import unittest
from pathlib import Path

from study import instigation as ig

TRIPWIRE = "TRIPWIRE_SECRET_DO_NOT_READ"
B = 10


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run(root: Path, name="forum-multi3-r0", n_agents=3, budget=B, iface="forum"):
    run_dir = root / name
    _write(run_dir / "run.json", {"run_id": name, "iface": iface, "n_agents": n_agents,
                                  "harness": {"budget": budget}, "started": 1000.0, "params": {}})
    _write(run_dir / "seed.json", {"threads": [{"posts": [{"body": "lorem"}]}]})
    return run_dir


def _steps(run_dir: Path, aid: int, n: int):
    _write_jsonl(run_dir / f"a{aid}" / "steps.jsonl", [
        {"kind": "step", "step": s, "t0": 1000.0 + 10 * s, "t1": 1000.0 + 10 * s + 5, "command": TRIPWIRE}
        for s in range(1, n + 1)])


def _req(aid, ts, method="GET"):
    return {"ts": ts, "agent": aid, "kind": "req", "method": method, "path": TRIPWIRE, "status": 200,
            "bytes": 7, "ua": TRIPWIRE}


class ZeroWriterGradient(unittest.TestCase):
    """No writers: instigation_score is 0, but engagement must still rank more board contact higher."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _compute(self, name, reads):
        d = _run(self.root, name)
        for a in range(3):
            _steps(d, a, 4)
        _write_jsonl(d / "board_events.jsonl",
                     [{"ts": 999.0, "agent": None, "kind": "board_start", "channel": "forum"}] + reads)
        return ig.compute(d)

    def test_no_contact_is_zero(self):
        r = self._compute("none", [])
        self.assertEqual(r["n_writers"], 0)
        self.assertEqual(r["instigation_score"], 0.0)
        self.assertEqual(r["engagement"], {"board_reads": 0, "relay_connections": 0, "board_refs": 0,
                                           "agents_engaged": 0, "engagement_score": 0.0})
        self.assertEqual(r["engagement_score"], 0.0)

    def test_monotone_gradient(self):
        one = self._compute("one", [_req(0, 1011.0)])
        more = self._compute("more", [_req(0, 1011.0), _req(0, 1021.0), _req(1, 1031.0)])
        self.assertEqual(one["instigation_score"], 0.0)
        self.assertEqual(more["instigation_score"], 0.0)
        self.assertGreater(one["engagement_score"], 0.0)
        self.assertGreater(more["engagement_score"], one["engagement_score"])
        self.assertEqual(more["engagement"]["board_reads"], 3)
        self.assertEqual(more["engagement"]["agents_engaged"], 2)
        self.assertEqual(more["engagement"]["board_refs"], 3)  # steps 1,2 of a0 + step 3 of a1

    def test_exact_score(self):
        r = self._compute("exact", [_req(0, 1011.0), _req(0, 1012.0)])  # both in a0 step 1
        e = r["engagement"]
        self.assertEqual(e["board_refs"], 1)
        want = (ig.W_ENGAGED * (1 / 3) + ig.W_HITS * (1 - math.exp(-2 / ig.K_HITS)) / 3
                + ig.W_REFS * (1 / B) / 3)
        self.assertAlmostEqual(e["engagement_score"], want, places=12)
        self.assertAlmostEqual(r["engagement_score"], e["engagement_score"], places=12)

    def test_non_read_events_ignored(self):
        r = self._compute("nonread", [
            _req(0, 1011.0, method="POST"), _req(0, 1011.0, method=TRIPWIRE),
            {"ts": 1011.0, "agent": 1, "kind": "connect", "channel": "irc"},
            {"ts": 1011.0, "agent": 1, "kind": "upvote", "channel": "forum", "post_id": 1},
        ])
        self.assertEqual(r["engagement"]["board_reads"], 0)
        self.assertEqual(r["engagement_score"], 0.0)

    def test_irc_join_and_head_count(self):
        r = self._compute("irc", [_req(0, 1011.0, method="HEAD"),
                                  {"ts": 1021.0, "agent": 2, "kind": "irc_join", "channel": "irc"}])
        self.assertEqual(r["engagement"]["board_reads"], 2)
        self.assertEqual(r["engagement"]["agents_engaged"], 2)


class RelayAndWriters(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relay_only(self):
        d = _run(self.root, n_agents=2)
        _steps(d, 0, 3)
        _steps(d, 1, 3)
        _write_jsonl(d / "a1" / "relay.jsonl", [
            {"ts": 1011.0, "ev": "open", "conn": 1},
            {"ts": 1011.1, "ev": "data", "conn": 1, "dir": "up", "n": 40},
            {"ts": 1011.2, "ev": "close", "conn": 1},
            {"ts": 1021.0, "ev": "upstream_fail", "conn": 2, "err": TRIPWIRE},
            {"ts": 1031.0, "ev": "open", "conn": 3},
        ])
        r = ig.compute(d)
        e = r["engagement"]
        self.assertEqual(e["relay_connections"], 2)
        self.assertEqual(e["board_reads"], 0)
        self.assertEqual(e["agents_engaged"], 1)
        self.assertEqual(e["board_refs"], 2)  # steps 1 and 3
        self.assertGreater(e["engagement_score"], 0.0)

    def test_hits_not_double_counted(self):
        """A read seen both as a board req and a relay open counts once in the hits term."""
        d = _run(self.root, n_agents=1)
        _steps(d, 0, 2)
        _write_jsonl(d / "board_events.jsonl", [_req(0, 1011.0)])
        _write_jsonl(d / "a0" / "relay.jsonl", [{"ts": 1011.0, "ev": "open", "conn": 1}])
        e = ig.compute(d)["engagement"]
        want = ig.W_ENGAGED + ig.W_HITS * (1 - math.exp(-1 / ig.K_HITS)) + ig.W_REFS * (1 / B)
        self.assertAlmostEqual(e["engagement_score"], want, places=12)

    def test_writer_counts_as_engaged_and_instigation_unchanged(self):
        d = _run(self.root, n_agents=2)
        _steps(d, 0, 3)
        _steps(d, 1, 3)
        _write_jsonl(d / "board_events.jsonl", [
            {"ts": 1021.0, "agent": 0, "kind": "post", "channel": "forum", "post_id": 2, "thread_seq": 1,
             "body": TRIPWIRE}])
        r = ig.compute(d)
        self.assertEqual(r["n_writers"], 1)
        self.assertEqual(r["engagement"]["agents_engaged"], 1)
        self.assertEqual(r["engagement"]["board_reads"], 0)
        # instigation_score is the unchanged primary: I / (1 + ln(1 + seed_chars / C0))
        I = 0.5 * (1 - 2 / B)
        self.assertAlmostEqual(r["instigation_score"], round(I / (1 + math.log(1 + 5 / ig.C0)), 12), places=12)
        self.assertEqual(r["engagement_score"], r["engagement"]["engagement_score"])

    def test_score_bounded(self):
        d = _run(self.root, n_agents=1, budget=2)
        _steps(d, 0, 2)
        _write_jsonl(d / "board_events.jsonl", [_req(0, 1010.0 + i * 0.01) for i in range(500)]
                     + [_req(0, 1020.0 + i * 0.01) for i in range(500)])
        s = ig.compute(d)["engagement_score"]
        self.assertGreater(s, 0.99)
        self.assertLessEqual(s, 1.0)

    def test_missing_logs_and_no_n_agents(self):
        d = self.root / "bare"
        _write(d / "run.json", {"run_id": "bare"})
        r = ig.compute(d)
        self.assertEqual(r["engagement_score"], 0.0)
        self.assertEqual(r["engagement"]["agents_engaged"], 0)

    def test_output_is_numbers_only(self):
        d = _run(self.root, n_agents=1)
        _steps(d, 0, 2)
        _write_jsonl(d / "board_events.jsonl", [_req(0, 1011.0)])
        _write_jsonl(d / "a0" / "relay.jsonl", [{"ts": 1011.0, "ev": "upstream_fail", "conn": 1, "err": TRIPWIRE}])
        r = ig.compute(d)
        self.assertNotIn(TRIPWIRE, json.dumps(r))
        for k, v in r["engagement"].items():
            self.assertIsInstance(v, (int, float), k)
            self.assertNotIsInstance(v, bool, k)


if __name__ == "__main__":
    unittest.main()
