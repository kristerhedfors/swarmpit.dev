"""Offline tests for the QD landscape publisher (contract item 5).

A stub archive stands in for study.landscape.Archive so these tests never depend on
real run data; every fixture is a neutral, content-free placeholder (numbers, enum
codes, 16-hex ids). Gates are injected, except the two hermetic real-gate tests,
which run tools/leakgate.py against a temp tree whose only "corpus" is a neutral
lorem placeholder -- never a real seed or chatter file.

    cd lab && python3 -m unittest study.test_landscape_scoreboard
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from study import registry, scoreboard

AXES = {"trigger_code": ["SAW_PEER_POSTS", "PEER_WROTE_FIRST", "NONE", "UNRESOLVED"],
        "channel": ["forum", "irc", "paste"],
        "length_band": ["le120", "121-200", "201-320", "gt320"]}

ENG = {"board_reads": 3, "relay_connections": 1, "board_refs": 2, "agents_engaged": 2,
       "engagement_score": 0.5}


def _rec(rid, trig, chan, band, score, chars, **kw):
    r = {"record_id": rid, "cell_key": f"{trig}|{chan}|{band}", "trigger_code": trig,
         "channel": chan, "length_band": band, "seed_chars": chars, "instigation_score": score,
         "n_writers": 2, "engagement": dict(ENG), "engagement_score": ENG["engagement_score"],
         "bootstrap": False, "origin": "mutation", "parent_id": None, "generation": 1,
         "run_id": "qd-" + rid + "-r0", "model": "qwen38"}
    r.update(kw)
    return r


class StubArchive:
    """v1-shaped archive: one elite per cell, model carried on the record."""

    def __init__(self, recs, axes=AXES):
        self.elites = {r["cell_key"]: r for r in recs}
        self.axes = axes


class StubModelArchive:
    """Per-agent-model archive (contract M): elites_by_model() -> {slug: {cell_key: rec}}."""

    def __init__(self, recs, axes=AXES, goal=None, models=None):
        self._by = {}
        for r in recs:
            self._by.setdefault(r["model"], {})[r["cell_key"]] = r
        self.axes = axes
        if goal is not None:
            self.goal = goal
        if models is not None:
            self.models = models

    def elites_by_model(self):
        return {s: dict(c) for s, c in self._by.items()}


def _archive():
    return StubArchive([
        _rec("0123456789abcdef", "SAW_PEER_POSTS", "forum", "le120", 0.4, 100),
        _rec("fedcba9876543210", "PEER_WROTE_FIRST", "irc", "121-200", 0.9, 150,
             parent_id="0123456789abcdef", origin="pilot", bootstrap=True),
        _rec("00000000000000aa", "NONE", "paste", "gt320", 0.9, 400),
    ])


def _model_archive(**kw):
    return StubModelArchive([
        _rec("0123456789abcdef", "SAW_PEER_POSTS", "forum", "le120", 0.4, 100, goal="unsolvable"),
        _rec("0123456789abcdef", "SAW_PEER_POSTS", "forum", "le120", 0.7, 100, model="mistral24",
             goal="unsolvable"),
        _rec("fedcba9876543210", "PEER_WROTE_FIRST", "irc", "121-200", 0.9, 150, goal="unsolvable"),
        _rec("00000000000000bb", "NONE", "paste", "gt320", 0.2, 400, model="mistral24", goal="unsolvable"),
    ], **kw)


def ok_gate(_p):
    return {"ok": True, "error_code": None}


class TestBuildLandscape(unittest.TestCase):
    def test_shape_v1_archive(self):
        ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
        self.assertEqual(ls["schema"], "instigation.landscape.v2")
        self.assertEqual(ls["goal"], "solvable")  # records predating the goal factor
        self.assertEqual(ls["models"], ["qwen38"])
        self.assertEqual(ls["coverage"], 3)
        self.assertEqual(ls["coverage_cells"], 3)
        self.assertEqual(ls["n_cells_possible"], 4 * 3 * 4)
        self.assertEqual(set(ls["cells"]), {"SAW_PEER_POSTS|forum|le120", "PEER_WROTE_FIRST|irc|121-200",
                                            "NONE|paste|gt320"})
        self.assertTrue(all(c["model"] == "qwen38" for c in ls["cells"].values()))
        self.assertEqual(ls["tallies"]["channel"], {"forum": 1, "irc": 1, "paste": 1})
        self.assertEqual(ls["tallies"]["model"], {"qwen38": 3})
        # score desc, then shorter seed first on equal score
        self.assertEqual([r["record_id"] for r in ls["leaderboard"]],
                         ["fedcba9876543210", "00000000000000aa", "0123456789abcdef"])
        self.assertNotIn("run_id", ls["leaderboard"][0])
        self.assertTrue(ls["leaderboard"][0]["bootstrap"])
        self.assertAlmostEqual(ls["mean_elite_score"], (0.4 + 0.9 + 0.9) / 3)

    def test_per_model(self):
        ls = scoreboard.build_landscape(_model_archive(), goal="unsolvable", generated_ts=1.0)
        self.assertEqual(ls["goal"], "unsolvable")
        self.assertEqual(ls["models"], ["qwen38", "mistral24"])  # config.MODEL_ORDER
        self.assertEqual(ls["coverage"], 4)  # (model, cell) pairs
        self.assertEqual(ls["coverage_cells"], 3)
        self.assertEqual(ls["cells"]["SAW_PEER_POSTS|forum|le120"]["model"], "mistral24")  # 0.7 > 0.4
        self.assertEqual(ls["cells"]["SAW_PEER_POSTS|forum|le120"]["instigation_score"], 0.7)
        q, m = ls["by_model"]["qwen38"], ls["by_model"]["mistral24"]
        self.assertEqual(set(q), {"cells", "coverage", "tallies", "origin", "size_b"})
        self.assertEqual((q["coverage"], q["origin"], q["size_b"]), (2, "cn", 27))
        self.assertEqual((m["coverage"], m["origin"], m["size_b"]), (2, "eu", 24))
        self.assertEqual(q["cells"]["SAW_PEER_POSTS|forum|le120"]["instigation_score"], 0.4)
        self.assertEqual(m["tallies"]["channel"], {"forum": 1, "paste": 1})
        self.assertEqual(ls["tallies"]["channel"], {"forum": 2, "irc": 1, "paste": 1})
        self.assertEqual(ls["tallies"]["model"], {"qwen38": 2, "mistral24": 2})
        self.assertEqual([(r["record_id"], r["model"]) for r in ls["leaderboard"]],
                         [("fedcba9876543210", "qwen38"), ("0123456789abcdef", "mistral24"),
                          ("0123456789abcdef", "qwen38"), ("00000000000000bb", "mistral24")])

    def test_combined_tie_goes_to_earlier_model(self):
        a = StubModelArchive([_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50, model="mistral24"),
                              _rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50)])
        ls = scoreboard.build_landscape(a, generated_ts=1.0)
        self.assertEqual(ls["cells"]["NONE|irc|le120"]["model"], "qwen38")

    def test_requested_models_listed_even_if_empty(self):
        ls = scoreboard.build_landscape(_model_archive(), goal="unsolvable", models=["gemma31", "qwen38"],
                                        generated_ts=1.0)
        self.assertEqual(ls["models"], ["qwen38", "mistral24", "gemma31"])
        self.assertEqual(ls["by_model"]["gemma31"]["coverage"], 0)
        self.assertEqual(ls["by_model"]["gemma31"]["cells"], {})
        self.assertNotIn("gemma31", ls["tallies"]["model"])
        ls2 = scoreboard.build_landscape(_model_archive(models=["glm53f"]), goal="unsolvable", generated_ts=1.0)
        self.assertIn("glm53f", ls2["models"])

    def test_gptoss120_origin_and_size(self):
        entry = {"provider": "cerebras", "model": "placeholder-model", "label": "GPT-OSS 120B",
                 "price_in": None, "price_out": None, "json_object": False}
        with mock.patch.dict(scoreboard.config.MODELS, {"gptoss120": entry}):
            a = StubModelArchive([_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50, model="gptoss120")])
            ls = scoreboard.build_landscape(a, generated_ts=1.0)
            g = ls["by_model"]["gptoss120"]
            self.assertEqual((g["origin"], g["size_b"]), ("us", 120))
            rec = scoreboard._record({"record_id": "0123456789abcdef", "model": "gptoss120"}, {})
            self.assertEqual((rec["model_origin"], rec["model_size_b"]), ("us", 120))
        self.assertEqual(scoreboard.model_origin("gptoss120"), "us")
        self.assertEqual(scoreboard._model_size_b("gptoss120"), 120)

    def test_goal_filtering_and_resolution(self):
        recs = [_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50, goal="unsolvable"),
                _rec("fedcba9876543210", "NONE", "forum", "le120", 0.5, 50),  # legacy -> solvable
                _rec("00000000000000aa", "NONE", "paste", "le120", 0.5, 50, goal="solvable")]
        ls = scoreboard.build_landscape(StubModelArchive(recs), goal="unsolvable", generated_ts=1.0)
        self.assertEqual(set(ls["cells"]), {"NONE|irc|le120"})
        ls = scoreboard.build_landscape(StubModelArchive(recs), goal="solvable", generated_ts=1.0)
        self.assertEqual(ls["coverage"], 2)
        # archive-level goal applies to records without their own goal
        ls = scoreboard.build_landscape(StubModelArchive(recs[:2], goal="unsolvable"), generated_ts=1.0)
        self.assertEqual((ls["goal"], ls["coverage"]), ("unsolvable", 2))
        # uniform record goal is picked up without an explicit one
        self.assertEqual(scoreboard.build_landscape(_model_archive(), generated_ts=1.0)["goal"], "unsolvable")

    def test_rejects_mixed_goals_without_choice(self):
        recs = [_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50, goal="unsolvable"),
                _rec("fedcba9876543210", "NONE", "forum", "le120", 0.5, 50, goal="solvable")]
        self._bad_archive(StubModelArchive(recs))

    def test_rejects_bad_goal(self):
        with self.assertRaises(scoreboard.ScoreboardError):
            scoreboard.build_landscape(_model_archive(), goal="lorem", generated_ts=1.0)
        self._bad_archive(StubModelArchive([_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50,
                                                 goal="lorem")]))

    def test_rejects_unknown_model(self):
        self._bad_archive(StubModelArchive([_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50,
                                                 model="lorem")]))
        self._bad_archive(StubArchive([_rec("0123456789abcdef", "NONE", "irc", "le120", 0.5, 50,
                                            model="lorem")]))
        with self.assertRaises(scoreboard.ScoreboardError):
            scoreboard.build_landscape(_model_archive(), models=["lorem"], generated_ts=1.0)

    def test_rejects_record_model_mismatch(self):
        a = _model_archive()
        by = a.elites_by_model()
        by["qwen38"]["PEER_WROTE_FIRST|irc|121-200"] = dict(by["qwen38"]["PEER_WROTE_FIRST|irc|121-200"],
                                                           model="mistral24")
        a.elites_by_model = lambda: by
        self._bad_archive(a)

    def test_extra_fields_projected_away(self):
        a = _model_archive()
        orig = a.elites_by_model
        a.elites_by_model = lambda: {s: {k: dict(r, extra="lorem") for k, r in c.items()}
                                     for s, c in orig().items()}
        ls = scoreboard.build_landscape(a, generated_ts=1.0)
        self.assertTrue(all("extra" not in c for c in ls["cells"].values()))
        self.assertTrue(all("extra" not in r for r in ls["leaderboard"]))

    def test_lazy_axes_fallback(self):
        a = _archive()
        a.axes = None
        ls = scoreboard.build_landscape(a, generated_ts=1.0)
        self.assertIn("UNRESOLVED", ls["axes"]["trigger_code"])
        self.assertIn("paste", ls["axes"]["channel"])

    def test_lazy_axes_include_code_comment_channels_and_legacy_irc(self):
        a = _archive()
        a.axes = None
        ls = scoreboard.build_landscape(a, generated_ts=1.0)
        self.assertTrue({"js", "html", "irc"} <= set(ls["axes"]["channel"]))
        self.assertTrue({"js", "html", "irc"} <= set(scoreboard._FALLBACK_CHANNELS))

    def test_code_comment_channel_cells_publish_and_validate(self):
        from study import landscape as real_ls
        axes = {"trigger_code": list(real_ls.TRIGGER_AXIS), "channel": list(real_ls.CHANNELS),
                "length_band": list(real_ls.BAND_LABELS)}
        recs = [_rec("00000000000000c1", "NONE", "js", "le120", 0.3, 80),
                _rec("00000000000000c2", "NONE", "html", "gt320", 0.6, 400),
                _rec("00000000000000c3", "NONE", "irc", "121-200", 0.2, 150)]
        ls = scoreboard.build_landscape(StubArchive(recs, axes=axes), generated_ts=1.0)
        self.assertEqual(set(ls["cells"]), {"NONE|js|le120", "NONE|html|gt320", "NONE|irc|121-200"})
        self.assertEqual(ls["tallies"]["channel"], {"js": 1, "html": 1, "irc": 1})
        scoreboard.validate_landscape(json.loads(json.dumps(ls)))

    def _bad_archive(self, a):
        with self.assertRaises(scoreboard.ScoreboardError) as cm:
            scoreboard.build_landscape(a, generated_ts=1.0)
        self.assertEqual(cm.exception.args[0], "E_LANDSCAPE_SCHEMA")

    def _bad(self, mutate):
        a = _model_archive()
        by = a.elites_by_model()
        mutate(by)
        a.elites_by_model = lambda: by
        self._bad_archive(a)

    def test_rejects_bad_cell_key(self):
        self._bad(lambda by: by["qwen38"].update({"lorem ipsum": next(iter(by["qwen38"].values()))}))

    def test_rejects_non_enum_part(self):
        self._bad(lambda by: by["qwen38"].update(
            {"BOGUS|forum|le120": _rec("0123456789abcdee", "BOGUS", "forum", "le120", 0.1, 10)}))

    def test_rejects_cell_key_record_disagreement(self):
        self._bad(lambda by: by["qwen38"].update(
            {"NONE|forum|le120": _rec("0123456789abcdee", "SAW_PEER_POSTS", "forum", "le120", 0.1, 10)}))

    def test_rejects_bad_record_id(self):
        def m(by):
            next(iter(by["qwen38"].values()))["record_id"] = "lorem"
        self._bad(m)

    def test_rejects_text_leaf(self):
        def m(by):
            next(iter(by["mistral24"].values()))["engagement"]["board_reads"] = "lorem"
        self._bad(m)

    def test_rejects_nan(self):
        def m(by):
            next(iter(by["mistral24"].values()))["instigation_score"] = float("nan")
        self._bad(m)

    def test_archive_exception_is_fixed_code(self):
        a = _model_archive()

        def boom():
            raise ValueError("lorem")
        a.elites_by_model = boom
        self._bad_archive(a)


class TestValidateLandscape(unittest.TestCase):
    def _bad(self, mutate):
        ls = scoreboard.build_landscape(_model_archive(), generated_ts=1.0)
        mutate(ls)
        with self.assertRaises(scoreboard.ScoreboardError) as cm:
            scoreboard.validate_landscape(ls)
        self.assertEqual(cm.exception.args[0], "E_LANDSCAPE_SCHEMA")

    def test_valid_roundtrip(self):
        ls = scoreboard.build_landscape(_model_archive(), generated_ts=1.0)
        scoreboard.validate_landscape(json.loads(json.dumps(ls)))

    def test_rejects_tampering(self):
        cases = [
            lambda ls: ls.update(goal="lorem"),
            lambda ls: ls.update(models=["qwen38", "lorem"]),
            lambda ls: ls.update(coverage=ls["coverage"] + 1),
            lambda ls: ls.update(coverage_cells=ls["coverage_cells"] - 1),
            lambda ls: ls.update(extra=1),
            lambda ls: ls["by_model"].pop("mistral24"),
            lambda ls: ls["by_model"]["qwen38"].update(origin="lorem"),
            lambda ls: ls["by_model"]["qwen38"].update(size_b=float("inf")),
            lambda ls: ls["by_model"]["qwen38"].update(coverage=9),
            lambda ls: ls["cells"]["SAW_PEER_POSTS|forum|le120"].update(model="qwen38"),
            lambda ls: ls["cells"]["SAW_PEER_POSTS|forum|le120"].update(model="lorem"),
            lambda ls: ls["by_model"]["qwen38"]["cells"]["PEER_WROTE_FIRST|irc|121-200"].update(model="mistral24"),
            lambda ls: ls["tallies"]["model"].update(lorem=1),
            lambda ls: ls["leaderboard"].pop(),
            lambda ls: ls["leaderboard"][0].update(model="mistral24"),
            lambda ls: ls["leaderboard"][0].update(record_id="00000000000000cc"),
            lambda ls: ls["leaderboard"].__setitem__(1, dict(ls["leaderboard"][0])),
        ]
        for i, mutate in enumerate(cases):
            with self.subTest(case=i):
                self._bad(mutate)


class TestWriteLandscape(unittest.TestCase):
    def test_ok_gate_publishes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            seen = []
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            scoreboard.write_landscape(ls, out_path=out, gate=lambda p: seen.append(p.suffix) or ok_gate(p),
                                       verify=None)
            self.assertEqual(seen, [".json"])  # candidate keeps a scannable suffix
            self.assertEqual(json.loads(out.read_text())["coverage"], 3)
            self.assertEqual([p.name for p in Path(td).iterdir()], ["landscape.json"])

    def test_fail_gate_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            with self.assertRaises(scoreboard.ScoreboardError) as cm:
                scoreboard.write_landscape(ls, out_path=out, gate=lambda p: {"ok": False, "error_code": "E_LEAK"})
            self.assertEqual(cm.exception.args[0], "E_LEAK")
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_empty_corpus_verdict_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            out.write_text("{}")
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            with self.assertRaises(scoreboard.ScoreboardError):
                scoreboard.write_landscape(ls, out_path=out, gate=ok_gate,
                                           verify=lambda: {"ok": False, "error_code": "E_CORPUS"})
            self.assertEqual(out.read_text(), "{}")  # rolled back

    def test_invalid_landscape_never_written(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            ls["cells"]["SAW_PEER_POSTS|forum|le120"]["record_id"] = "lorem"
            called = []
            with self.assertRaises(scoreboard.ScoreboardError) as cm:
                scoreboard.write_landscape(ls, out_path=out, gate=lambda p: called.append(1) or ok_gate(p),
                                           verify=None)
            self.assertEqual(cm.exception.args[0], "E_LANDSCAPE_SCHEMA")
            self.assertEqual(called, [])
            self.assertFalse(out.exists())

    def test_default_runs_wholetree_verify_and_rolls_back(self):
        """A direct call (no verify=) must run the whole-tree gate after publishing, with rollback."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            out.write_text("{}")
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            calls = []
            fail = lambda: calls.append(1) or {"ok": False, "error_code": "E_LEAK"}  # noqa: E731
            with mock.patch.object(scoreboard, "leakgate_wholetree", fail):
                with self.assertRaises(scoreboard.ScoreboardError) as cm:
                    scoreboard.write_landscape(ls, out_path=out, gate=ok_gate)
            self.assertEqual(cm.exception.args[0], "E_LEAK")
            self.assertEqual(calls, [1])
            self.assertEqual(out.read_text(), "{}")  # rolled back
            with mock.patch.object(scoreboard, "leakgate_wholetree", lambda: {"ok": True, "n": 1}):
                self.assertEqual(scoreboard.write_landscape(ls, out_path=out, gate=ok_gate), {"ok": True, "n": 1})
            self.assertEqual(json.loads(out.read_text())["coverage"], 3)

    def test_real_gate_empty_corpus_fails_closed(self):
        """Default gate with --root pointed at a tree holding no chatter -> E_CORPUS -> no publish."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "landscape.json"
            ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
            with mock.patch.object(registry, "REPO_DIR", Path(td)):
                with self.assertRaises(scoreboard.ScoreboardError) as cm:
                    scoreboard.write_landscape(ls, out_path=out)
            self.assertEqual(cm.exception.args[0], "E_LEAK")
            self.assertFalse(out.exists())

    def test_real_gate_scans_candidate(self):
        """Regression: the scoped gate must actually scan the candidate (a `.tmp` suffix
        was skipped by the gate and passed vacuously)."""
        filler = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "data" / "synthetic-agent-chatter" / "placeholder.chatter.json"
            src.parent.mkdir(parents=True)
            src.write_text(json.dumps({"body": filler}))
            out = root / "pub" / "scoreboard.json"
            with mock.patch.object(registry, "REPO_DIR", root):
                with self.assertRaises(scoreboard.ScoreboardError) as cm:
                    scoreboard.write({"x": filler}, out_path=out)
                self.assertEqual(cm.exception.args[0], "E_LEAK")
                self.assertFalse(out.exists())
                ls = scoreboard.build_landscape(_archive(), generated_ts=1.0)
                lout = root / "pub" / "landscape.json"
                verdict = scoreboard.write_landscape(ls, out_path=lout, verify=None)
                self.assertTrue(verdict["ok"])
                self.assertEqual(verdict["scanned_files"], 1)
                self.assertTrue(lout.exists())


class TestBuildAndWrite(unittest.TestCase):
    def _rows(self):
        return {"0123456789abcdef": {"record_id": "0123456789abcdef", "model": "qwen38",
                                     "length_bytes": 100, "channel": "forum"}}

    def test_without_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rp.write_text(json.dumps(self._rows()))
            out = Path(td) / "scoreboard.json"
            res = scoreboard.build_and_write({}, registry_path=rp, out_path=out, gate=ok_gate,
                                             verify=lambda: {"ok": True})
            self.assertEqual(set(res), {"ok", "n_records", "pareto"})
            self.assertFalse((Path(td) / "landscape.json").exists())

    def test_with_archive_publishes_both(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rp.write_text(json.dumps(self._rows()))
            out, lout = Path(td) / "scoreboard.json", Path(td) / "landscape.json"
            res = scoreboard.build_and_write({}, registry_path=rp, out_path=out, gate=ok_gate,
                                             verify=lambda: {"ok": True}, archive=_archive(),
                                             landscape_out_path=lout)
            self.assertEqual(res["landscape_coverage"], 3)
            self.assertTrue(out.exists() and lout.exists())

    def test_with_model_archive_passes_goal_and_models(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rp.write_text(json.dumps(self._rows()))
            out, lout = Path(td) / "scoreboard.json", Path(td) / "landscape.json"
            res = scoreboard.build_and_write({}, registry_path=rp, out_path=out, gate=ok_gate,
                                             verify=lambda: {"ok": True}, archive=_model_archive(),
                                             landscape_out_path=lout, goal="unsolvable",
                                             models=["qwen38", "gemma31"])
            self.assertEqual((res["landscape_coverage"], res["landscape_coverage_cells"]), (4, 3))
            pub = json.loads(lout.read_text())
            self.assertEqual((pub["goal"], pub["models"]), ("unsolvable", ["qwen38", "mistral24", "gemma31"]))

    def test_schema_failure_touches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rp.write_text(json.dumps(self._rows()))
            out, lout = Path(td) / "scoreboard.json", Path(td) / "landscape.json"
            with self.assertRaises(scoreboard.ScoreboardError) as cm:
                scoreboard.build_and_write({}, registry_path=rp, out_path=out, gate=ok_gate,
                                           verify=lambda: {"ok": True}, archive=_model_archive(),
                                           landscape_out_path=lout, goal="lorem")
            self.assertEqual(cm.exception.args[0], "E_LANDSCAPE_SCHEMA")
            self.assertFalse(out.exists() or lout.exists())

    def test_with_archive_verify_fail_rolls_back_both(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "registry.json"
            rp.write_text(json.dumps(self._rows()))
            out, lout = Path(td) / "scoreboard.json", Path(td) / "landscape.json"
            out.write_text("prev")
            with self.assertRaises(scoreboard.ScoreboardError):
                scoreboard.build_and_write({}, registry_path=rp, out_path=out, gate=ok_gate,
                                           verify=lambda: {"ok": False, "error_code": "E_CORPUS"},
                                           archive=_archive(), landscape_out_path=lout)
            self.assertEqual(out.read_text(), "prev")
            self.assertFalse(lout.exists())


class TestMainArchivePerGoal(unittest.TestCase):
    """`--archive` (bare) + `--goal` loads that goal's own archive (qd.goal_archive_path), never the
    solvable one filtered to nothing. Placeholder records in a tmp dir; publishing is stubbed."""

    def setUp(self):
        from study import landscape as ls
        from study import test_landscape as tl
        self.ls, self.tl = ls, tl
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name) / "landscape_archive.json"
        self.got = []

    def tearDown(self):
        self._td.cleanup()

    def _write(self, path, goal, n, rec_goal=None):
        a = self.ls.Archive(path, goal=goal)
        a.consider(self.tl.rec(n, goal=rec_goal or goal))
        a.save()

    def _main(self, *argv):
        def fake_build(scores, *, archive=None, goal=None, models=None, **_):
            self.got.append((archive, goal))
            return {"ok": True}
        with mock.patch.object(self.ls, "ARCHIVE_PATH", self.base), \
                mock.patch.object(scoreboard, "build_and_write", fake_build), \
                mock.patch("builtins.print"):
            return scoreboard.main(list(argv))

    def test_bare_archive_with_goal_loads_that_goals_file(self):
        from study import qd
        self._write(self.base, "solvable", 1)
        self._write(qd.goal_archive_path("unsolvable", self.base), "unsolvable", 2)
        self.assertEqual(self._main("--archive", "--goal", "unsolvable"), 0)
        archive, goal = self.got[-1]
        self.assertEqual(goal, "unsolvable")
        self.assertEqual(Path(archive.path), qd.goal_archive_path("unsolvable", self.base))
        self.assertEqual(archive.coverage(), 1)
        self.assertEqual(self._main("--archive", "--goal", "solvable"), 0)
        self.assertEqual(Path(self.got[-1][0].path), self.base)

    def test_only_other_goal_elites_refused_not_published_empty(self):
        self._write(self.base, None, 1, rec_goal="unsolvable")   # unpinned archive holding one unsolvable record
        with mock.patch("builtins.print") as pr:
            with mock.patch.object(self.ls, "ARCHIVE_PATH", self.base):
                rc = scoreboard.main(["--archive", str(self.base), "--goal", "solvable"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(pr.call_args[0][0]), {"ok": False, "error": "E_LANDSCAPE_SCHEMA"})

    def test_models_default_excludes_other_goal_models(self):
        a = self.ls.Archive(self.base)
        a.consider(self.tl.rec(1, goal="solvable"))
        a.consider(self.tl.rec(2, goal="unsolvable", model=self.tl.M2))
        out = scoreboard.build_landscape(a, goal="unsolvable", generated_ts=1.0)
        self.assertEqual(out["models"], [self.tl.M2])


if __name__ == "__main__":
    unittest.main()
