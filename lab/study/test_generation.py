"""Offline tests for the generation partition (PROTOCOL.md sections 5 & 6).

No network and no real model call: modelio is replaced by a fake transport that
returns canned, obviously-synthetic placeholder seeds. Path constants are pointed
at a temp dir so nothing is written into the real data tree. These tests assert
the content-free contract: generate returns numbers + record ids only, the
registry stores ids/lengths/paths, and the scoreboard emits the section-5 record
schema and refuses to publish on a non-ok leak-gate verdict.

    cd lab && python3 -m unittest study.test_generation
"""
import json
import tempfile
import unittest
from pathlib import Path

from study import generate, registry, scoreboard

# Neutral placeholder seed -- NOT board content; a trivially-shaped fixture for the fake model.
_PLACEHOLDER_SEED = {
    "board_name": "placeholder-board",
    "threads": [
        {"slug": "t-one", "title": "Title One",
         "posts": [{"author": "u1", "body": "placeholder body one", "ago_min": 10},
                   {"author": "u2", "body": "placeholder body two", "ago_min": 5}]},
        {"slug": "t-two", "title": "Title Two",
         "posts": [{"author": "u3", "body": "placeholder body three", "ago_min": 1}]},
    ],
}


class FakeTransport:
    """Duck-types modelio.Transport.chat; returns a fixed JSON seed, never the network."""

    def __init__(self, seed=None, slug="qwen38", wrap=""):
        self.seed = seed if seed is not None else _PLACEHOLDER_SEED
        self.slug = slug
        self._wrap = wrap  # optional code-fence / prose wrapper to exercise the tolerant parser
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        content = json.dumps(self.seed)
        if self._wrap == "fence":
            content = "```json\n" + content + "\n```"
        return {"choices": [{"message": {"content": content}}]}


class RecordIdTest(unittest.TestCase):
    def test_stable_and_distinct(self):
        a = registry.record_id(_PLACEHOLDER_SEED)
        b = registry.record_id(json.loads(json.dumps(_PLACEHOLDER_SEED)))  # deep copy
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{16}$")
        other = {**_PLACEHOLDER_SEED, "board_name": "placeholder-board-2"}
        self.assertNotEqual(a, registry.record_id(other))

    def test_counts_are_numbers(self):
        c = registry.counts(_PLACEHOLDER_SEED)
        self.assertEqual(c["n_threads"], 2)
        self.assertEqual(c["n_posts"], 3)
        self.assertGreater(c["length_bytes"], 0)
        self.assertGreater(c["length_chars"], 0)
        self.assertTrue(all(isinstance(v, int) for v in c.values()))


class RegistryRoundTripTest(unittest.TestCase):
    def test_upsert_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            row = registry.make_row(_PLACEHOLDER_SEED, generation=2, channel="forum",
                                    model="qwen38", chatter_path="/x/y.chatter.json")
            registry.upsert(row, rp)
            got = registry.get(row["record_id"], rp)
            self.assertEqual(set(got), set(registry.ROW_FIELDS))
            self.assertEqual(got["record_id"], row["record_id"])
            self.assertEqual(got["generation"], 2)
            self.assertEqual(got["channel"], "forum")
            # second upsert of the same id replaces, not duplicates
            registry.upsert(row, rp)
            self.assertEqual(len(registry.load(rp)), 1)

    def test_id_from_path(self):
        rid = registry.record_id(_PLACEHOLDER_SEED)
        self.assertEqual(registry.id_from_path(f"/a/b/{rid}.chatter.json"), rid)
        self.assertEqual(registry.id_from_path("/a/b/seed.json"), "")


class GenerateTest(unittest.TestCase):
    def _gen(self, td, transport):
        cand_dir = Path(td) / "candidates"
        rp = Path(td) / "registry.json"
        meta = generate.generate_candidate(channel="forum", transport=transport,
                                           candidates_dir=cand_dir, registry_path=rp)
        return meta, cand_dir, rp

    def test_returns_numbers_only_and_writes_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            tp = FakeTransport()
            meta, cand_dir, rp = self._gen(td, tp)
            self.assertEqual(set(meta),
                             {"record_id", "length_bytes", "length_chars", "n_threads", "n_posts"})
            self.assertRegex(meta["record_id"], r"^[0-9a-f]{16}$")
            self.assertEqual(meta["n_threads"], 2)
            self.assertEqual(meta["n_posts"], 3)
            # file written under the candidate dir with the record-id name
            path = cand_dir / f"{meta['record_id']}.chatter.json"
            self.assertTrue(path.is_file())
            self.assertTrue(str(path).endswith(".chatter.json"))
            # registry row recorded, numbers/ids/path only
            row = registry.get(meta["record_id"], rp)
            self.assertEqual(row["record_id"], meta["record_id"])
            self.assertEqual(row["chatter_path"], str(path))
            # no board body text leaks into the returned metadata
            blob = json.dumps(meta)
            for body in ("placeholder body one", "placeholder body two", "placeholder body three"):
                self.assertNotIn(body, blob)

    def test_record_id_matches_file_contents(self):
        with tempfile.TemporaryDirectory() as td:
            meta, cand_dir, _ = self._gen(td, FakeTransport())
            written = json.loads((cand_dir / f"{meta['record_id']}.chatter.json").read_text())
            self.assertEqual(registry.record_id(written), meta["record_id"])

    def test_tolerates_code_fenced_output(self):
        with tempfile.TemporaryDirectory() as td:
            meta, cand_dir, _ = self._gen(td, FakeTransport(wrap="fence"))
            self.assertTrue((cand_dir / f"{meta['record_id']}.chatter.json").is_file())

    def test_bad_schema_raises_fixed_code(self):
        with tempfile.TemporaryDirectory() as td:
            tp = FakeTransport(seed={"board_name": "x"})  # no threads
            with self.assertRaises(generate.GenError) as cm:
                self._gen(td, tp)
            self.assertEqual(cm.exception.args[0], "E_SCHEMA")

    def test_no_network_call(self):
        with tempfile.TemporaryDirectory() as td:
            tp = FakeTransport()
            self._gen(td, tp)
            self.assertEqual(tp.calls, 1)  # exactly the fake transport, nothing else


class ScoreboardTest(unittest.TestCase):
    def _registry_with(self, rp):
        rows = {}
        for i in range(3):
            seed = {**_PLACEHOLDER_SEED, "board_name": f"b{i}"}
            row = registry.make_row(seed, generation=1, channel="forum", model="qwen38",
                                    chatter_path=f"/c/{i}.chatter.json")
            registry.upsert(row, rp)
            rows[row["record_id"]] = row
        return rows

    def test_emits_section5_schema_and_invokes_gate(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            out = Path(td) / "scoreboard.json"
            rows = self._registry_with(rp)
            ids = list(rows)
            scores = {ids[0]: {"instigation_score": 0.9, "I": 0.8, "n_reps": 3, "first_post_rate": 1.0},
                      ids[1]: {"instigation_score": 0.5, "I": 0.5, "n_reps": 2},
                      ids[2]: {"instigation_score": 0.7, "I": 0.9, "n_reps": 1}}
            calls = []

            def fake_gate(path):
                calls.append(Path(path))
                return {"ok": True, "scanned_files": 1, "corpus_shingles": 0, "leak_hits": 0,
                        "by_file": [], "error_code": None}

            sb = scoreboard.build(scores, registry_path=rp)
            scoreboard.write(sb, out_path=out, gate=fake_gate)
            self.assertEqual(len(calls), 1)  # gate invoked exactly once, before publish
            published = json.loads(out.read_text())
            self.assertEqual(published["schema"], scoreboard.SCHEMA)
            self.assertEqual(published["n_records"], 3)
            # section-5 record field presence
            fields = {"record_id", "parent_id", "generation", "channel", "model", "model_origin",
                      "model_size_b", "length_bytes", "length_chars", "n_threads", "n_posts",
                      "instigation_score", "I", "rank_key", "score_ci_lo", "score_ci_hi", "n_reps",
                      "first_post_rate", "median_first_post_lag_s", "run_time_to_first_write_s",
                      "total_posts", "cross_mentions", "live_live_edges", "pile_in_onset_t_s",
                      "created_ts"}
            for rec in published["records"]:
                self.assertTrue(fields.issubset(set(rec)), fields - set(rec))
            # leaderboard ordered by instigation_score desc
            lead_scores = [next(r for r in published["records"] if r["record_id"] == rid)["instigation_score"]
                           for rid in published["leaderboard"]]
            self.assertEqual(lead_scores, sorted(lead_scores, reverse=True))
            self.assertTrue(set(published["pareto"]).issubset(set(ids)))

    def test_numbers_and_ids_only(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rows = self._registry_with(rp)
            scores = {rid: {"instigation_score": 0.3, "I": 0.3} for rid in rows}
            sb = scoreboard.build(scores, registry_path=rp)
            # only id / enum fields may be strings; everything else is a number, list or null
            string_fields = {"record_id", "parent_id", "channel", "model", "model_origin"}
            for rec in sb["records"]:
                for k, v in rec.items():
                    self.assertIsInstance(k, str)
                    if isinstance(v, str):
                        self.assertIn(k, string_fields, f"unexpected string field {k}")
                    else:
                        self.assertTrue(v is None or isinstance(v, (int, float, list)),
                                        f"unexpected value type for {k}: {type(v)}")

    def test_aborts_on_leak_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            out = Path(td) / "scoreboard.json"
            self._registry_with(rp)
            sb = scoreboard.build({}, registry_path=rp)

            def deny(path):
                return {"ok": False, "scanned_files": 1, "corpus_shingles": 9, "leak_hits": 2,
                        "by_file": [], "error_code": "E_LEAK"}

            with self.assertRaises(scoreboard.ScoreboardError) as cm:
                scoreboard.write(sb, out_path=out, gate=deny)
            self.assertEqual(cm.exception.args[0], "E_LEAK")
            self.assertFalse(out.exists())  # nothing published on a non-ok verdict

    def test_wholetree_verify_rollback_when_standalone_gate_rejects(self):
        # bug #2 (fail-closed, deterministic publish): the scoped pre-publish gate may pass, but if the
        # authoritative whole-tree verify would reject, publish must not stand. With no prior file the
        # rollback removes it; with a prior file it is restored byte-for-byte.
        ok = lambda path: {"ok": True, "scanned_files": 1, "corpus_shingles": 0, "leak_hits": 0,
                           "by_file": [], "error_code": None}
        deny_tree = lambda: {"ok": False, "scanned_files": 3, "corpus_shingles": 9, "leak_hits": 4,
                             "by_file": [], "error_code": "E_LEAK"}
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            out = Path(td) / "scoreboard.json"
            self._registry_with(rp)
            sb = scoreboard.build({}, registry_path=rp)
            # no prior published file -> rollback removes it
            with self.assertRaises(scoreboard.ScoreboardError) as cm:
                scoreboard.write(sb, out_path=out, gate=ok, verify=deny_tree)
            self.assertEqual(cm.exception.args[0], "E_LEAK")
            self.assertFalse(out.exists())
            # prior published bytes are restored verbatim on a whole-tree rejection
            prior = b'{"schema":"prior"}'
            out.write_bytes(prior)
            with self.assertRaises(scoreboard.ScoreboardError):
                scoreboard.write(sb, out_path=out, gate=ok, verify=deny_tree)
            self.assertEqual(out.read_bytes(), prior)

    def test_wholetree_verify_publishes_when_both_gates_pass(self):
        calls = {"scoped": 0, "tree": 0}

        def scoped(path):
            calls["scoped"] += 1
            return {"ok": True, "scanned_files": 1, "corpus_shingles": 0, "leak_hits": 0,
                    "by_file": [], "error_code": None}

        def tree():
            calls["tree"] += 1
            return {"ok": True, "scanned_files": 3, "corpus_shingles": 7, "leak_hits": 0,
                    "by_file": [], "error_code": None}

        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            out = Path(td) / "scoreboard.json"
            self._registry_with(rp)
            sb = scoreboard.build({}, registry_path=rp)
            verdict = scoreboard.write(sb, out_path=out, gate=scoped, verify=tree)
            self.assertTrue(out.exists())
            self.assertEqual((calls["scoped"], calls["tree"]), (1, 1))
            self.assertIs(verdict["ok"], True)


class GeneratorHardeningTest(unittest.TestCase):
    """qwen3-style replies: a reasoning block ahead of fenced seed JSON, JSON mode, one E_PARSE retry."""

    class _Scripted:
        def __init__(self, replies, slug="qwen38"):
            self.replies, self.slug, self.kw = list(replies), slug, []

        def chat(self, messages, **kw):
            self.kw.append(kw)
            return {"choices": [{"message": {"content": self.replies.pop(0)}}]}

    def setUp(self):
        from unittest import mock
        import config
        from study import triggers  # JSON-mode rejections are remembered process-wide
        triggers._JSON_OBJECT_REJECTED.clear()
        self.addCleanup(triggers._JSON_OBJECT_REJECTED.clear)
        self.json_mode = mock.patch.dict(config.MODELS["qwen38"], {"json_object": True})
        self.json_mode.start()
        self.addCleanup(self.json_mode.stop)

    def _run(self, tp):
        with tempfile.TemporaryDirectory() as td:
            return generate.generate_candidate(channel="forum", transport=tp,
                                               candidates_dir=Path(td) / "c",
                                               registry_path=Path(td) / "r.json")

    def test_think_block_and_fence_are_stripped(self):
        reply = "<think>lorem { ipsum</think>\n```json\n" + json.dumps(_PLACEHOLDER_SEED) + "\n```"
        tp = self._Scripted([reply])
        meta = self._run(tp)
        self.assertEqual(meta["n_posts"], 3)
        self.assertEqual(tp.kw[0].get("response_format"), {"type": "json_object"})
        self.assertEqual(tp.kw[0].get("max_tokens"), generate.GEN_MAX_TOKENS)

    def test_qwen38_real_config_is_plain_mode(self):
        self.json_mode.stop()
        tp = self._Scripted([json.dumps(_PLACEHOLDER_SEED)])
        self._run(tp)
        self.assertNotIn("response_format", tp.kw[0])

    def test_truncated_reasoning_retries_once(self):
        tp = self._Scripted(["<think>lorem ipsum", json.dumps(_PLACEHOLDER_SEED)])
        self.assertEqual(self._run(tp)["n_posts"], 3)
        self.assertEqual(len(tp.kw), 2)

    def test_persistent_parse_failure_is_fixed_code(self):
        tp = self._Scripted(["<think>lorem", "lorem ipsum"])
        with self.assertRaises(generate.GenError) as cm:
            self._run(tp)
        self.assertEqual(cm.exception.args[0], "E_PARSE")


if __name__ == "__main__":
    unittest.main()
