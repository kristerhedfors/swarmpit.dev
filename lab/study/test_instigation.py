"""Unit tests for the instigation metric tool and the Q1 trigger-parser validator.

Run from the lab/ directory:  python3 -m unittest study.test_instigation

The fixture is a SYNTHETIC, content-free structural board: integers and timestamps only. One board event
carries a `body` tripwire to prove the metric tool never reads (or emits) post content.
"""
import json
import math
import tempfile
import unittest
from pathlib import Path

from study import instigation as ig
from study import triggers as tg

TRIPWIRE = "TRIPWIRE_SECRET_DO_NOT_READ"
B = 10  # budget


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_fixture(root: Path) -> Path:
    """A 3-agent run. a0 writes first (step 2), a1 writes second (step 2), a2 never writes."""
    run_dir = root / "forum-multi3-r0"
    _write(run_dir / "run.json", {"run_id": "forum-multi3-r0", "iface": "forum", "n_agents": 3,
                                  "harness": {"budget": B}, "started": 1000.0, "params": {}})
    # seed: 2 posts, bodies 4 + 6 = 10 chars (ascii -> 10 bytes)
    _write(run_dir / "seed.json", {"board_name": "b", "threads": [
        {"slug": "t", "title": "T", "posts": [
            {"author": "s1", "body": "aaaa", "ago_min": 60},
            {"author": "s2", "body": "bbbbbb", "ago_min": 30}]}]})
    # board events: only writes (post) count; the req is ignored. a0's first post carries a body tripwire.
    _write_jsonl(run_dir / "board_events.jsonl", [
        {"ts": 1001.0, "agent": 0, "kind": "req", "channel": "forum", "post_id": None, "thread_seq": None},
        {"ts": 1002.0, "agent": 0, "kind": "post", "channel": "forum", "post_id": 3, "thread_seq": 1,
         "body": TRIPWIRE, "author": TRIPWIRE, "body_len_chars": len(TRIPWIRE)},
        {"ts": 1005.0, "agent": 1, "kind": "post", "channel": "forum", "post_id": 4, "thread_seq": 1},
        {"ts": 1007.0, "agent": 0, "kind": "post", "channel": "forum", "post_id": 5, "thread_seq": 1},
    ])
    _write_jsonl(run_dir / "a0" / "steps.jsonl", [
        {"kind": "step", "step": 1, "t0": 1000.5, "t1": 1001.5, "command": TRIPWIRE},
        {"kind": "step", "step": 2, "t0": 1001.8, "t1": 1002.5, "command": TRIPWIRE},
        {"kind": "step", "step": 3, "t0": 1006.5, "t1": 1007.5, "command": TRIPWIRE},
    ])
    _write_jsonl(run_dir / "a1" / "steps.jsonl", [
        {"kind": "step", "step": 1, "t0": 1000.5, "t1": 1001.0},
        {"kind": "step", "step": 2, "t0": 1004.5, "t1": 1005.5},
    ])
    _write_jsonl(run_dir / "a2" / "steps.jsonl", [
        {"kind": "step", "step": 1, "t0": 1000.5, "t1": 1001.0},
    ])
    return run_dir


class InstigationMetric(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = make_fixture(Path(self._tmp.name))
        self.result = ig.compute(self.run_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_level_values(self):
        r = self.result
        self.assertEqual(r["n_agents"], 3)
        self.assertEqual(r["budget"], B)
        self.assertEqual(r["n_writers"], 2)
        self.assertAlmostEqual(r["write_fraction"], 2 / 3, places=12)
        self.assertEqual(r["seed_chars"], 10)
        self.assertEqual(r["seed_bytes"], 10)
        self.assertEqual(r["seed_posts_n"], 2)
        # S0 = 1 - 2/10 = 0.8, S1 = 0.8  -> S_mean = 0.8
        self.assertAlmostEqual(r["S_mean"], 0.8, places=12)
        I = (2 / 3) * 0.8
        self.assertAlmostEqual(r["I"], I, places=12)
        expected_score = round(I / (1 + math.log(1 + 10 / 1000.0)), 12)
        self.assertEqual(r["instigation_score"], expected_score)
        # distinct writers reaches 2 at a1's first write, t = 5.0
        self.assertEqual(r["pile_in_onset_t_s"], 5.0)
        self.assertEqual(r["run_time_to_first_write_s"], 2.0)
        self.assertEqual(r["run_first_write_step"], 2)
        self.assertEqual(r["posts_over_time"], [[2.0, 1, 1], [5.0, 2, 2], [7.0, 3, 2]])

    def test_agent_level_values(self):
        by = {a["agent_id"]: a for a in self.result["agents"]}
        self.assertEqual(by[0]["wrote"], True)
        self.assertEqual(by[0]["first_write_step"], 2)
        self.assertEqual(by[0]["first_write_post_id"], 3)
        self.assertEqual(by[0]["time_to_first_write_s"], 2.0)
        self.assertAlmostEqual(by[0]["S"], 0.8, places=12)
        self.assertEqual(by[1]["wrote"], True)
        self.assertEqual(by[1]["first_write_step"], 2)
        self.assertEqual(by[1]["time_to_first_write_s"], 5.0)
        self.assertEqual(by[2]["wrote"], False)
        self.assertIsNone(by[2]["time_to_first_write_s"])
        self.assertIsNone(by[2]["first_write_step"])
        self.assertEqual(by[2]["S"], 0.0)

    def test_peer_wrote_first_authoritative(self):
        agents = [{**a, "_first_ts": (a["time_to_first_write_s"] if a["wrote"] else None)}
                  for a in self.result["agents"]]
        pwf = ig.peer_wrote_first(agents)
        self.assertEqual(pwf, {0: False, 1: True, 2: False})

    def test_body_never_read_or_emitted(self):
        # the whole section 7.2 object must not contain any post body/author text
        self.assertNotIn(TRIPWIRE, json.dumps(self.result))
        # the board-event loader returns only whitelisted structural keys (never `body`/`author`)
        allowed = set(ig.EVENT_KEYS) | {"_seq"}
        for ev in ig.load_write_events(self.run_dir / "board_events.jsonl"):
            self.assertTrue(set(ev) <= allowed, ev.keys())
            self.assertNotIn(TRIPWIRE, json.dumps(ev))

    def test_section_7_2_keys_exact(self):
        expected = {"run_id", "iface", "n_agents", "budget", "seed_id", "seed_chars", "seed_bytes",
                    "seed_posts_n", "n_writers", "write_fraction", "run_time_to_first_write_s",
                    "run_first_write_step", "pile_in_onset_t_s", "posts_over_time", "S_mean", "I",
                    "instigation_score", "agents", "engagement", "engagement_score"}
        self.assertEqual(set(self.result), expected)
        self.assertEqual(set(self.result["engagement"]),
                         {"board_reads", "relay_connections", "board_refs", "agents_engaged",
                          "engagement_score"})
        agent_keys = {"agent_id", "wrote", "time_to_first_write_s", "first_write_step",
                      "first_write_post_id", "S"}
        for a in self.result["agents"]:
            self.assertEqual(set(a), agent_keys)
        self.assertEqual(len(self.result["seed_id"]), 16)


class SeedFromCandidate(unittest.TestCase):
    """bug #1: a run booted from a candidate records only params.candidate_id (no seed.json in the run
    dir), so seed sizes must be resolved from the candidate chatter file; otherwise seed_chars is NULL
    and instigation_score collapses to raw I."""

    def test_seed_sizes_resolved_from_candidate_id(self):
        from study import registry
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        run_dir = make_fixture(root)
        (run_dir / "seed.json").unlink()  # force resolution through params.candidate_id
        cid = "a1b2c3d4e5f60718"
        run = json.loads((run_dir / "run.json").read_text())
        run["params"] = {"candidate_id": cid}
        (run_dir / "run.json").write_text(json.dumps(run))
        cands = root / "cands"
        cands.mkdir()
        candidate = {"board_name": "b", "threads": [
            {"slug": "t", "title": "T", "posts": [
                {"author": "x", "body": "aaaa", "ago_min": 1},
                {"author": "y", "body": "bbbbbb", "ago_min": 2}]}]}
        (cands / f"{cid}.chatter.json").write_text(registry.canonical_json(candidate))
        orig = registry.CANDIDATES_DIR
        registry.CANDIDATES_DIR = cands
        try:
            r = ig.compute(run_dir)
        finally:
            registry.CANDIDATES_DIR = orig
        self.assertEqual(r["seed_chars"], 10)
        self.assertEqual(r["seed_bytes"], 10)
        self.assertEqual(r["seed_posts_n"], 2)
        self.assertEqual(r["seed_id"], cid)
        # the brevity denominator is now applied: instigation_score is strictly below I, not equal to it
        self.assertGreater(r["I"], 0.0)
        self.assertLess(r["instigation_score"], r["I"])

    def test_missing_candidate_still_null_sizes(self):
        # an unresolvable candidate id leaves sizes NULL (fixed behavior), never raising or leaking.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = make_fixture(Path(tmp.name))
        (run_dir / "seed.json").unlink()
        run = json.loads((run_dir / "run.json").read_text())
        run["params"] = {"candidate_id": "ffffffffffffffff"}
        (run_dir / "run.json").write_text(json.dumps(run))
        from study import registry
        orig = registry.CANDIDATES_DIR
        registry.CANDIDATES_DIR = Path(tmp.name) / "empty"
        try:
            r = ig.compute(run_dir)
        finally:
            registry.CANDIDATES_DIR = orig
        self.assertIsNone(r["seed_chars"])
        self.assertEqual(r["seed_id"], "ffffffffffffffff")


class TriggerValidator(unittest.TestCase):
    def test_valid_response(self):
        # The model's self-reported model_slug ("injectedslug") must be IGNORED; the record carries the
        # SCRIPT's trusted slug passed in ("qwen38"), so adversarial text cannot ride model_slug into the
        # numbers-only artifact (PROTOCOL.md section 4).
        text = json.dumps({"run_id": "r", "agent_id": 0, "parser_version": 1, "model_slug": "injectedslug",
                           "codes": [{"code": "SAW_PEER_POSTS", "confidence": 0.9},
                                     {"code": "PEER_WROTE_FIRST", "confidence": 0.5}]})
        rec = tg.validate_response(text, "r", 0, "qwen38")
        self.assertEqual([c["code"] for c in rec["codes"]], ["SAW_PEER_POSTS", "PEER_WROTE_FIRST"])
        self.assertEqual(rec["parser_version"], tg.PARSER_VERSION)
        self.assertEqual(rec["model_slug"], "qwen38")

    def test_model_slug_rejects_unknown_script_slug(self):
        # Defense in depth: even the trusted param is constrained to config.MODELS keys; anything else
        # (a typo, an unknown slug) becomes "" rather than free text in the artifact.
        text = json.dumps({"codes": [{"code": "NONE", "confidence": 0.1}], "model_slug": "x"})
        rec = tg.validate_response(text, "r", 0, "not_a_real_model")
        self.assertEqual(rec["model_slug"], "")

    def test_non_enum_code_is_schema_error_with_location(self):
        text = ('{"codes": [{"code": "INJECTED_EVIL", "confidence": 1.0}], '
                '"model_slug": "x"}')
        with self.assertRaises(tg.SchemaError) as cm:
            tg.validate_response(text, "r", 0, "qwen38")
        e = cm.exception
        self.assertEqual(e.code, "E_SCHEMA")
        self.assertIsInstance(e.line, int)
        self.assertIsInstance(e.col, int)
        self.assertGreaterEqual(e.line, 1)
        self.assertGreaterEqual(e.col, 1)

    def test_malformed_json_is_parse_error_with_location(self):
        with self.assertRaises(tg.SchemaError) as cm:
            tg.validate_response("{not json", "r", 0, "qwen38")
        e = cm.exception
        self.assertEqual(e.code, "E_PARSE")
        self.assertGreaterEqual(e.line, 1)
        self.assertGreaterEqual(e.col, 1)

    def test_bad_confidence_is_schema_error(self):
        text = '{"codes": [{"code": "NONE", "confidence": 2.0}], "model_slug": "x"}'
        with self.assertRaises(tg.SchemaError) as cm:
            tg.validate_response(text, "r", 0, "qwen38")
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_run_writes_no_content_on_schema_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = make_fixture(Path(tmp.name))

        def stub(system_prompt, user_content, model_slug):
            # an adversarial model response carrying injected content and a non-enum code
            return json.dumps({"codes": [{"code": "INJECTED_" + TRIPWIRE, "confidence": 1.0}],
                               "model_slug": "stub", "note": TRIPWIRE})

        artifact = tg.run(run_dir, agents=[0], model_slug="stub", call_model=stub)
        # the on-disk artifact carries a fixed error code + numeric location, never the injected content
        disk = (run_dir / "instigation" / "triggers.json").read_text()
        self.assertNotIn(TRIPWIRE, disk)
        self.assertNotIn(TRIPWIRE, json.dumps(artifact))
        rec = artifact["records"][0]
        self.assertEqual(rec["error_code"], "E_SCHEMA")
        self.assertIn("line", rec)
        self.assertIn("col", rec)
        # the raw model response is quarantined on disk (not returned through any caller's context)
        self.assertTrue((run_dir / "instigation" / "raw" / "a0.txt").is_file())

    def test_run_valid_response_records_codes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = make_fixture(Path(tmp.name))

        def stub(system_prompt, user_content, model_slug):
            return json.dumps({"run_id": "forum-multi3-r0", "agent_id": 0, "parser_version": 1,
                               "model_slug": "stub",
                               "codes": [{"code": "EMPTY_BOARD", "confidence": 0.7}]})

        artifact = tg.run(run_dir, agents=[0, 1], model_slug="stub", call_model=stub)
        self.assertEqual(artifact["peer_wrote_first"], {"0": False, "1": True, "2": False})
        self.assertEqual(len(artifact["records"]), 2)
        for rec in artifact["records"]:
            self.assertNotIn("error_code", rec)
            self.assertEqual(rec["codes"][0]["code"], "EMPTY_BOARD")


if __name__ == "__main__":
    unittest.main()
