"""Unit tests for the MAP-Elites landscape archive (lab/study/landscape.py).

Run from the lab/ directory:  python3 -m unittest study.test_landscape

All fixtures are SYNTHETIC and content-free: hex ids, numbers and enum codes built in memory or in tmp
dirs. Nothing here reads seeds, runs or any chatter file.
"""
import json
import tempfile
import unittest
from pathlib import Path

import config
from study import landscape as ls
from study import triggers as tg

# A second agent model: the scale-up's free Cerebras model when config has it, else any other slug.
M1 = "qwen38"
M2 = "gptoss120" if "gptoss120" in config.MODELS else next(m for m in config.MODEL_ORDER if m != M1)
M3 = next(m for m in config.MODEL_ORDER if m not in (M1, M2))


def rid(n: int) -> str:
    return f"{n:016x}"


def rec(n, *, trig="SAW_SEED_ONLY", chan="forum", chars=100, score=0.5, eng=0.0, **kw):
    r = {"record_id": rid(n), "trigger_code": trig, "channel": chan, "seed_chars": chars,
         "instigation_score": score, "n_writers": 1,
         "engagement": {"board_reads": 1, "relay_connections": 0, "board_refs": 0, "agents_engaged": 1,
                        "engagement_score": eng},
         "engagement_score": eng, "bootstrap": False, "origin": "mutation", "parent_id": None,
         "generation": 0, "run_id": "forum-multi3-r0-unsolvable", "model": M1, "goal": "unsolvable"}
    r.update(kw)
    return r


def metric(*agents):
    """agents: (agent_id, wrote, ttfw)."""
    return {"agents": [{"agent_id": a, "wrote": w, "time_to_first_write_s": t} for a, w, t in agents]}


def art(pwf=None, records=()):
    return {"run_id": "r", "peer_wrote_first": {str(k): v for k, v in (pwf or {}).items()},
            "records": list(records)}


def codes(aid, *pairs):
    return {"run_id": "r", "agent_id": aid, "codes": [{"code": c, "confidence": p} for c, p in pairs]}


class AxesTest(unittest.TestCase):
    def test_band_edges(self):
        cases = {0: "le120", 120: "le120", 121: "121-200", 200: "121-200", 201: "201-320",
                 320: "201-320", 321: "gt320", 10_000: "gt320"}
        for n, band in cases.items():
            self.assertEqual(ls.length_band(n), band, n)

    def test_band_rejects_bad(self):
        for bad in (-1, 1.5, "100", None, True, float("nan")):
            with self.assertRaises(ValueError):
                ls.length_band(bad)

    def test_channels_match_board_registry(self):
        from board.channel import INTERFACES
        # every registered interface (code-comment channels js/html included) maps onto the axis,
        # and the axis carries nothing unregistered
        self.assertEqual({ls.channel_axis(i) for i in INTERFACES}, set(ls.CHANNELS))
        self.assertEqual(ls.channel_axis("pastebin"), "paste")
        with self.assertRaises(ValueError):
            ls.channel_axis("email")

    def test_code_comment_channels_and_legacy_irc(self):
        for ch in ("js", "html"):
            self.assertIn(ch, ls.CHANNELS)
            self.assertEqual(ls.channel_axis(ch), ch)
            self.assertEqual(ls.cell_key("NONE", ch, "le120"), f"NONE|{ch}|le120")
            self.assertEqual(ls.split_cell_key(f"NONE|{ch}|gt320"), ("NONE", ch, "gt320"))
        self.assertIn("irc", ls.CHANNELS)   # out of the default grid, still loadable

    def test_code_comment_records_round_trip_with_legacy_irc(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.json"
            a = ls.Archive(p, goal="unsolvable")
            for n, ch in enumerate(("js", "html", "irc"), 1):
                self.assertTrue(a.consider(rec(n, chan=ch)))
            a.save()
            b = ls.Archive.load(p, goal="unsolvable")
            self.assertEqual(b.coverage(), 3)
            self.assertEqual({r["channel"] for r in b.elites.values()}, {"js", "html", "irc"})

    def test_trigger_axis(self):
        self.assertEqual(ls.TRIGGER_AXIS, tuple(tg.ENUM) + ("UNRESOLVED", "NO_WRITERS"))
        self.assertNotIn("NO_WRITERS", tg.ENUM_SET)  # distinct from the classifier's NONE

    def test_cell_key_validation(self):
        self.assertEqual(ls.cell_key("NONE", "irc", "le120"), "NONE|irc|le120")
        with self.assertRaises(ValueError):
            ls.cell_key("lorem", "irc", "le120")
        with self.assertRaises(ValueError):
            ls.cell_key("NONE", "lorem", "le120")
        with self.assertRaises(ValueError):
            ls.cell_key("NONE", "irc", "lorem")
        with self.assertRaises(ValueError):
            ls.cell_key("NONE|x", "irc", "le120")

    def test_error_messages_do_not_echo_values(self):
        try:
            ls.cell_key("lorem-ipsum", "irc", "le120")
        except ValueError as e:
            self.assertNotIn("lorem", str(e))
            self.assertEqual(str(e), "E_ENUM_TRIGGER")


class PrimaryTriggerTest(unittest.TestCase):
    def test_no_writers(self):
        self.assertEqual(ls.primary_trigger(None, metric((0, False, None))), "NO_WRITERS")
        self.assertEqual(ls.primary_trigger(art(), {"agents": []}), "NO_WRITERS")

    def test_classified_none_distinct_from_no_writers(self):
        a = art(records=[codes(0, ("NONE", 0.9))])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0))), "NONE")

    def test_all_errors_unresolved(self):
        a = art(pwf={0: False, 1: False},
                records=[{"ok": False, "error_code": "E_PARSE", "line": 1, "col": 1, "agent_id": 0},
                         {"ok": False, "error_code": "E_SCHEMA", "line": 1, "col": 1, "agent_id": 1}])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0), (1, True, 2.0))), "UNRESOLVED")
        self.assertEqual(ls.primary_trigger(None, metric((0, True, 1.0))), "UNRESOLVED")

    def test_highest_confidence_per_writer(self):
        a = art(records=[codes(0, ("TASK_HINT", 0.3), ("CALL_TO_ACTION", 0.9))])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0))), "CALL_TO_ACTION")

    def test_invalid_codes_skipped(self):
        a = art(records=[codes(0, ("lorem", 1.0), ("TASK_HINT", 0.2))])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0))), "TASK_HINT")

    def test_error_record_skipped_valid_used(self):
        a = art(records=[{"ok": False, "error_code": "E_PARSE", "agent_id": 0, "line": 1, "col": 1},
                         codes(0, ("TASK_HINT", 0.5))])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0))), "TASK_HINT")

    def test_peer_wrote_first_writers_do_not_vote(self):
        """Peer-instigated writers are excluded: the run keeps the seed-instigated writer's code."""
        a = art(pwf={0: False, 1: True, 2: True},
                records=[codes(0, ("SAW_SEED_ONLY", 0.9)), codes(1, ("TASK_HINT", 0.9)),
                         codes(2, ("TASK_HINT", 0.9))])
        m = metric((0, True, 1.0), (1, True, 2.0), (2, True, 3.0))
        self.assertEqual(ls.primary_trigger(a, m), "SAW_SEED_ONLY")

    def test_peer_wrote_first_fallback_when_seed_writer_unresolved(self):
        a = art(pwf={0: False, 1: True, 2: True},
                records=[{"ok": False, "error_code": "E_PARSE", "agent_id": 0, "line": 1, "col": 1},
                         codes(1, ("TASK_HINT", 0.9))])
        m = metric((0, True, 1.0), (1, True, 2.0), (2, True, 3.0))
        self.assertEqual(ls.primary_trigger(a, m), "PEER_WROTE_FIRST")

    def test_classified_pwf_ignored_when_authoritatively_false(self):
        a = art(pwf={0: False}, records=[codes(0, ("PEER_WROTE_FIRST", 0.9), ("TASK_HINT", 0.4))])
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0))), "TASK_HINT")

    def test_pwf_from_metric_when_artifact_missing(self):
        m = metric((0, True, 1.0), (1, True, 2.0), (2, True, 3.0), (3, True, 4.0))
        self.assertEqual(ls.primary_trigger(None, m), "PEER_WROTE_FIRST")
        # simultaneous first writes: nobody strictly preceded anyone -> no PWF, unresolved
        self.assertEqual(ls.primary_trigger(None, metric((0, True, 1.0), (1, True, 1.0))), "UNRESOLVED")
        # artifact without the pwf map (records only) still uses the metric's deterministic value
        a = {"records": [codes(1, ("TASK_HINT", 0.9))]}
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0), (1, True, 2.0))), "PEER_WROTE_FIRST")

    def test_majority(self):
        a = art(pwf={0: False, 1: False, 2: False}, records=[codes(0, ("SAW_SEED_ONLY", 0.9)), codes(1, ("TASK_HINT", 0.9)),
                         codes(2, ("TASK_HINT", 0.8))])
        m = metric((0, True, 1.0), (1, True, 2.0), (2, True, 3.0))
        self.assertEqual(ls.primary_trigger(a, m), "TASK_HINT")

    def test_tie_goes_to_earliest_writer(self):
        a = art(pwf={0: False, 1: False}, records=[codes(0, ("SAW_SEED_ONLY", 0.9)), codes(1, ("TASK_HINT", 0.9))])
        # agent 1 wrote first
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 5.0), (1, True, 2.0))), "TASK_HINT")
        self.assertEqual(ls.primary_trigger(a, metric((0, True, 1.0), (1, True, 2.0))), "SAW_SEED_ONLY")

    def test_non_writers_ignored(self):
        a = art(records=[codes(0, ("TASK_HINT", 0.9)), codes(1, ("NONE", 0.9)), codes(2, ("NONE", 0.9))])
        m = metric((0, True, 1.0), (1, False, None), (2, False, None))
        self.assertEqual(ls.primary_trigger(a, m), "TASK_HINT")

    def test_result_in_axis(self):
        a = art(records=[codes(0, ("UNPROMPTED", 0.4))])
        self.assertIn(ls.primary_trigger(a, metric((0, True, 1.0))), ls.TRIGGER_AXIS)


class FitnessAndInsertTest(unittest.TestCase):
    def test_insert_and_replace(self):
        a = ls.Archive(path="/nonexistent")
        self.assertTrue(a.consider(rec(1, score=0.4)))
        self.assertFalse(a.consider(rec(2, score=0.3)))
        self.assertTrue(a.consider(rec(3, score=0.6)))
        self.assertEqual(a.coverage(), 1)
        (elite,) = a.elites.values()
        self.assertEqual(elite["record_id"], rid(3))
        self.assertEqual(elite["cell_key"], "SAW_SEED_ONLY|forum|le120")

    def test_distinct_cells(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, chars=120))
        a.consider(rec(2, chars=121))
        a.consider(rec(3, chan="irc"))
        a.consider(rec(4, trig="TASK_HINT"))
        self.assertEqual(a.coverage(), 4)

    def test_near_tie_goes_shorter(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.5, chars=110))
        self.assertTrue(a.consider(rec(2, score=0.5 - 1e-12, chars=50)))
        self.assertEqual(a.elites["SAW_SEED_ONLY|forum|le120"]["record_id"], rid(2))
        # longer near-tie does not replace
        self.assertFalse(a.consider(rec(3, score=0.5 + 1e-12, chars=60)))
        # exact full tie keeps incumbent
        self.assertFalse(a.consider(rec(4, score=0.5, chars=50)))

    def test_engagement_secondary(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.5, chars=50, eng=0.1))
        self.assertTrue(a.consider(rec(2, score=0.5, chars=100, eng=0.9)))

    def test_score_beats_shorter(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.5, chars=10))
        self.assertTrue(a.consider(rec(2, score=0.51, chars=119)))

    def test_objective_is_raw_I_not_brevity_normalized(self):
        """Same cell (201-320): the longer in-band seed with higher raw I wins, even though its
        brevity-normalized instigation_score is lower."""
        a = ls.Archive(path="/nonexistent")
        self.assertTrue(a.consider(rec(1, chars=205, I=0.50, score=0.421)))
        self.assertTrue(a.consider(rec(2, chars=320, I=0.53, score=0.415)))
        self.assertEqual(a.elites["SAW_SEED_ONLY|forum|201-320"]["record_id"], rid(2))
        self.assertEqual(a.elites["SAW_SEED_ONLY|forum|201-320"]["I"], 0.53)

    def test_mixed_basis_falls_back_to_instigation_score(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, chars=205, score=0.5))           # legacy record, no I
        self.assertFalse(a.consider(rec(2, chars=210, I=0.9, score=0.4)))
        self.assertTrue(a.consider(rec(3, chars=210, I=0.1, score=0.6)))

    def test_gentle_overflow_penalty_only_gt_softcap(self):
        f_in = ls.fitness(rec(1, chars=600, score=1.0))[0]
        f_over = ls.fitness(rec(2, chars=1280, score=1.0))[0]
        self.assertEqual(f_in, 1.0)
        self.assertAlmostEqual(f_over, 1.0 / 1.1)
        # mild: a modestly higher score still wins despite overflow
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, chars=400, score=1.0))
        self.assertTrue(a.consider(rec(2, chars=1280, score=1.2)))

    def test_mean_and_tallies_and_map(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.2))
        a.consider(rec(2, score=0.4, chan="irc", chars=250))
        self.assertAlmostEqual(a.mean_elite_score(), 0.3)
        t = a.tallies()
        self.assertEqual(t["channel"], {"forum": 1, "irc": 1})
        self.assertEqual(t["length_band"], {"201-320": 1, "le120": 1})
        self.assertEqual(t["trigger_code"], {"SAW_SEED_ONLY": 2})
        m = a.to_map()
        self.assertEqual(set(m["SAW_SEED_ONLY|irc|201-320"]),
                         {"record_id", "instigation_score", "length", "trigger_code", "channel",
                          "n_writers", "engagement", "model", "goal"})
        self.assertEqual(m["SAW_SEED_ONLY|irc|201-320"]["length"], 250)
        self.assertEqual(ls.Archive(path="/x").mean_elite_score(), 0.0)


class RejectionTest(unittest.TestCase):
    def test_rejects(self):
        a = ls.Archive(path="/nonexistent")
        bad = [
            rec(1, trig="lorem"), rec(1, chan="lorem"), rec(1, chars=-1), rec(1, chars="100"),
            rec(1, record_id="ABCDEF0123456789"), rec(1, record_id="0123"), rec(1, record_id=None),
            rec(1, length_band="gt320"), rec(1, cell_key="NONE|forum|le120"),
            rec(1, origin="lorem"), rec(1, parent_id="lorem"), rec(1, run_id="lorem ipsum"),
            rec(1, model="lorem ipsum"), rec(1, model="lorem"), rec(1, model=None), rec(1, model=""),
            rec(1, goal="lorem"), rec(1, goal=None), rec(1, goal=""), rec(1, score="0.5"), rec(1, score=float("inf")),
            rec(1, bootstrap="yes"), rec(1, engagement={"board_reads": "lorem"}), "lorem",
        ]
        for b in bad:
            with self.assertRaises(ValueError):
                a.consider(b)
        self.assertEqual(a.coverage(), 0)

    def test_extra_fields_dropped(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, body="lorem", extra={"x": "lorem"}))
        (elite,) = a.elites.values()
        self.assertEqual(set(elite), set(ls.RECORD_FIELDS))
        self.assertNotIn("lorem", json.dumps(elite))


class SelectionTest(unittest.TestCase):
    def _archive(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.9))
        a.consider(rec(2, score=0.1, chan="irc"))
        a.consider(rec(3, score=0.2, chan="wiki", chars=250))
        a.consider(rec(4, score=0.3, trig="TASK_HINT", chan="kv", chars=500))
        return a

    def test_distinct_cells_first(self):
        a = self._archive()
        picks = a.select_parents(4, generation=0)
        self.assertEqual(len(set(picks)), 4)

    def test_spreads_across_generations(self):
        a = self._archive()
        seen = []
        for g in range(4):
            seen += a.select_parents(1, generation=g)
        self.assertEqual(sorted(seen), sorted(rid(i) for i in range(1, 5)))

    def test_not_just_top_score(self):
        a = self._archive()
        seen = set()
        for g in range(3):
            seen.update(a.select_parents(1, generation=g))
        self.assertTrue(seen - {rid(1)})

    def test_deterministic(self):
        self.assertEqual(self._archive().select_parents(3, 7), self._archive().select_parents(3, 7))

    def test_k_larger_than_cells_cycles(self):
        a = self._archive()
        picks = a.select_parents(6, generation=1)
        self.assertEqual(len(picks), 6)
        self.assertEqual(len(set(picks)), 4)

    def test_prefers_cells_with_empty_neighbours(self):
        a = ls.Archive(path="/nonexistent")
        # forum/irc cells crowd each other; kv 201-320 has the most empty neighbours (and sorts last,
        # so rotation alone would not pick it first)
        a.consider(rec(1, chars=100))
        a.consider(rec(2, chars=150))
        a.consider(rec(3, chars=100, chan="irc"))
        a.consider(rec(4, chars=250, chan="kv"))
        self.assertEqual(a.select_parents(1, generation=0), [rid(4)])

    def test_empty(self):
        self.assertEqual(ls.Archive(path="/x").select_parents(3, 0), [])
        self.assertEqual(self._archive().select_parents(0, 0), [])


class PersistenceTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "landscape_archive.json"
            a = ls.Archive(path=p)
            a.consider(rec(1, score=0.3, origin="pilot", bootstrap=True))
            a.consider(rec(2, chan="paste", chars=321, parent_id=rid(1), generation=2))
            a.select_parents(1, 0)
            a.save()
            b = ls.Archive.load(p)
            self.assertEqual(b.elites, a.elites)
            self.assertEqual(b._selections, a._selections)
            self.assertEqual(b.to_map(), a.to_map())
            self.assertEqual(b.n_dropped_on_load, 0)
            self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])  # no tmp left

    def test_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ls.Archive.load(Path(d) / "nope.json").coverage(), 0)
            p = Path(d) / "bad.json"
            p.write_text("{not json")
            self.assertEqual(ls.Archive.load(p).coverage(), 0)

    def test_load_drops_invalid_records(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            good = ls.sanitize_record(rec(1))
            doc = {"elites": {good["cell_key"]: good,
                              "lorem|forum|le120": dict(good, trigger_code="lorem"),
                              "NONE|irc|le120": good},  # key/cell mismatch
                   "selections": {"lorem": 3, good["cell_key"]: 2}}
            p.write_text(json.dumps(doc))
            b = ls.Archive.load(p)
            self.assertEqual(b.coverage(), 1)
            self.assertEqual(b.n_dropped_on_load, 2)
            self.assertEqual(b._selections, {"qwen38": {good["cell_key"]: 2}})


class GoalAndModelEnumTest(unittest.TestCase):
    def test_model_and_goal_axes_are_config_enums(self):
        self.assertEqual(ls.MODEL_AXIS, tuple(config.MODEL_ORDER))
        self.assertEqual(set(ls.MODEL_AXIS), set(config.MODELS))
        self.assertEqual(ls.GOAL_AXIS, tuple(config.GOALS))
        self.assertIn(ls.LEGACY_MODEL, config.MODELS)
        self.assertIn(ls.LEGACY_GOAL, config.GOALS)
        self.assertEqual(ls.Archive.N_PAIRS_POSSIBLE, ls.Archive.N_CELLS_POSSIBLE * len(config.MODELS))

    def test_error_codes_fixed(self):
        for kw, code in (({"model": "lorem"}, "E_ENUM_MODEL"), ({"goal": "lorem"}, "E_ENUM_GOAL")):
            with self.assertRaises(ValueError) as cm:
                ls.sanitize_record(rec(1, **kw))
            self.assertEqual(str(cm.exception), code)

    def test_legacy_defaults_only_for_missing(self):
        r = rec(1)
        del r["model"], r["goal"]
        with self.assertRaises(ValueError):
            ls.sanitize_record(r)
        clean = ls.sanitize_record(r, legacy=True)
        self.assertEqual((clean["model"], clean["goal"]), ("qwen38", "solvable"))
        with self.assertRaises(ValueError):  # present-but-invalid is never repaired
            ls.sanitize_record(rec(1, model="lorem"), legacy=True)

    def test_goal_recorded(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, goal="solvable"))
        (elite,) = a.elites.values()
        self.assertEqual(elite["goal"], "solvable")

    def test_goal_pinned_archive(self):
        a = ls.Archive(path="/nonexistent", goal="unsolvable")
        self.assertTrue(a.consider(rec(1)))
        with self.assertRaises(ValueError) as cm:
            a.consider(rec(2, goal="solvable", score=9.0))
        self.assertEqual(str(cm.exception), "E_GOAL_MISMATCH")
        with self.assertRaises(ValueError):
            ls.Archive(path="/x", goal="lorem")


class PerModelTest(unittest.TestCase):
    def _archive(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1, score=0.4))                     # M1 forum le120
        a.consider(rec(2, score=0.6, model=M2))           # M2 same cell, better
        a.consider(rec(3, score=0.2, model=M2, chan="irc"))
        return a

    def test_models_do_not_displace_each_other(self):
        a = self._archive()
        self.assertFalse(a.consider(rec(4, score=0.3)))   # M1 incumbent 0.4 stays
        byk = a.elites_by_model()
        self.assertEqual(list(byk), [M1, M2])
        self.assertEqual(byk[M1]["SAW_SEED_ONLY|forum|le120"]["record_id"], rid(1))
        self.assertEqual(byk[M2]["SAW_SEED_ONLY|forum|le120"]["record_id"], rid(2))
        self.assertEqual(a.elite(M1, "SAW_SEED_ONLY|forum|le120")["record_id"], rid(1))
        self.assertIsNone(a.elite(M1, "SAW_SEED_ONLY|irc|le120"))

    def test_coverage(self):
        a = self._archive()
        self.assertEqual(a.coverage(), 3)
        self.assertEqual(a.coverage_cells(), 2)
        self.assertEqual(a.coverage(M1), 1)
        self.assertEqual(a.coverage(M2), 2)
        self.assertEqual(a.coverage(M3), 0)
        with self.assertRaises(ValueError):
            a.coverage("lorem")

    def test_best_per_cell_across_models(self):
        a = self._archive()
        e = a.elites
        self.assertEqual(set(e), {"SAW_SEED_ONLY|forum|le120", "SAW_SEED_ONLY|irc|le120"})
        self.assertEqual(e["SAW_SEED_ONLY|forum|le120"]["model"], M2)
        m = a.to_map()
        self.assertEqual(m["SAW_SEED_ONLY|forum|le120"]["model"], M2)
        self.assertEqual(set(a.to_map(M1)), {"SAW_SEED_ONLY|forum|le120"})
        self.assertEqual(a.to_map(M1)["SAW_SEED_ONLY|forum|le120"]["record_id"], rid(1))

    def test_exact_tie_across_models_goes_to_model_order(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(2, model=M2))
        a.consider(rec(1, model=M1))
        self.assertEqual(a.elites["SAW_SEED_ONLY|forum|le120"]["model"], M1)

    def test_tallies_combined_and_per_model(self):
        a = self._archive()
        t = a.tallies()
        self.assertEqual(t["channel"], {"forum": 2, "irc": 1})
        self.assertEqual(t["model"], {M1: 1, M2: 2})
        self.assertEqual(t["goal"], {"unsolvable": 3})
        t2 = a.tallies(M2)
        self.assertEqual(t2["channel"], {"forum": 1, "irc": 1})
        self.assertNotIn("model", t2)
        self.assertAlmostEqual(a.mean_elite_score(), 0.4)
        self.assertAlmostEqual(a.mean_elite_score(M2), 0.4)

    def test_by_model_and_axes(self):
        a = self._archive()
        bm = a.by_model()
        self.assertEqual(list(bm), [M1, M2])
        self.assertEqual(bm[M2]["coverage"], 2)
        self.assertEqual(set(bm[M1]), {"cells", "coverage", "mean_elite_score", "tallies"})
        ax = a.axes()
        self.assertEqual(ax["model"], list(config.MODEL_ORDER))
        self.assertEqual(ax["goal"], list(config.GOALS))
        self.assertEqual(ax["length_band"], list(ls.BAND_LABELS))

    def test_leaderboard_all_pairs(self):
        lb = self._archive().leaderboard()
        self.assertEqual([r["record_id"] for r in lb], [rid(2), rid(1), rid(3)])
        self.assertEqual([r["record_id"] for r in self._archive().leaderboard(M1)], [rid(1)])

    def test_select_favours_cells_missing_in_other_models(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1))                                # forum le120 in both models
        a.consider(rec(2, model=M2))
        a.consider(rec(3, chan="kv", chars=250))          # kv only under M1 -> transfer candidate
        self.assertEqual(a.select_parents(1, 0), [rid(3)])

    def test_select_scope_and_distinct_ids(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1))
        a.consider(rec(1, model=M2))                      # same seed elite under two models
        a.consider(rec(2, chan="irc", model=M2))
        picks = a.select_parents(2, 0)
        self.assertEqual(sorted(picks), [rid(1), rid(2)])
        self.assertEqual(a.select_parents(3, 1, models=[M1]), [rid(1)] * 3)
        self.assertEqual(a.select_parents(2, 0, models=[M3]), [])
        with self.assertRaises(ValueError):
            a.select_parents(1, 0, models=["lorem"])

    def test_selection_counts_per_pair(self):
        a = ls.Archive(path="/nonexistent")
        a.consider(rec(1))
        a.consider(rec(2, model=M2))
        a.select_parents(2, 0)
        self.assertEqual(a._selections, {M1: {"SAW_SEED_ONLY|forum|le120": 1},
                                         M2: {"SAW_SEED_ONLY|forum|le120": 1}})


class LegacyLoadTest(unittest.TestCase):
    def _v1_record(self, n, **kw):
        r = ls.sanitize_record(rec(n, **kw))
        del r["goal"]
        r["model"] = ""  # what the v1 sanitizer stored for a missing model
        return r

    def test_v1_archive_loads_as_qwen38_solvable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            r1 = self._v1_record(1)
            r2 = self._v1_record(2, chan="irc")
            r2.pop("model")
            r3 = dict(self._v1_record(3, chan="wiki"), model=M2)  # v1 record that named its model
            doc = {"schema": ls.ARCHIVE_SCHEMA_V1,
                   "elites": {r1["cell_key"]: r1, r2["cell_key"]: r2, r3["cell_key"]: r3,
                              "NONE|kv|le120": dict(r1, model="lorem")},
                   "selections": {r1["cell_key"]: 4, "lorem": 1}}
            p.write_text(json.dumps(doc))
            a = ls.Archive.load(p)
            self.assertEqual(a.n_dropped_on_load, 1)
            self.assertEqual(a.coverage(), 3)
            self.assertEqual(a.coverage(M1), 2)
            self.assertEqual(a.coverage(M2), 1)
            self.assertEqual({r["goal"] for r in a.elites.values()}, {"solvable"})
            self.assertEqual(a._selections, {M1: {r1["cell_key"]: 4}})
            a.save()
            raw = json.loads(p.read_text())
            self.assertEqual(raw["schema"], ls.ARCHIVE_SCHEMA)
            self.assertEqual(set(raw["elites"]), {M1, M2})
            b = ls.Archive.load(p)
            self.assertEqual(b.elites_by_model(), a.elites_by_model())
            self.assertEqual(b._selections, a._selections)

    def test_goal_pinned_load_skips_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            r1 = self._v1_record(1)
            p.write_text(json.dumps({"schema": ls.ARCHIVE_SCHEMA_V1, "elites": {r1["cell_key"]: r1}}))
            a = ls.Archive.load(p, goal="unsolvable")
            self.assertEqual(a.coverage(), 0)
            self.assertEqual(a.n_other_goal_on_load, 1)
            self.assertEqual(a.n_dropped_on_load, 0)
            self.assertEqual(ls.Archive.load(p, goal="solvable").coverage(), 1)

    def test_v2_rejects_model_mismatch_and_unknown_model(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            good = ls.sanitize_record(rec(1))
            doc = {"schema": ls.ARCHIVE_SCHEMA,
                   "elites": {M1: {good["cell_key"]: good},
                              M2: {good["cell_key"]: good},            # record says M1
                              "lorem": {good["cell_key"]: good}},
                   "selections": {M1: {good["cell_key"]: 1}, "lorem": {good["cell_key"]: 1},
                                  M2: {"lorem": 2, good["cell_key"]: True}}}
            p.write_text(json.dumps(doc))
            a = ls.Archive.load(p)
            self.assertEqual(a.coverage(), 1)
            self.assertEqual(a.n_dropped_on_load, 2)
            self.assertEqual(a._selections, {M1: {good["cell_key"]: 1}})
            # v2 records must carry model/goal: no legacy defaulting
            bad = dict(good)
            del bad["goal"]
            p.write_text(json.dumps({"schema": ls.ARCHIVE_SCHEMA, "elites": {M1: {good["cell_key"]: bad}}}))
            self.assertEqual(ls.Archive.load(p).coverage(), 0)


class MainTest(unittest.TestCase):
    def test_main_prints_numbers_only(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            a = ls.Archive(path=p)
            a.consider(rec(1))
            a.consider(rec(2, model=M2, chan="irc"))
            a.save()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(ls.main(["--archive", str(p)]), 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["coverage"], 2)
            self.assertEqual(out["coverage_cells"], 2)
            self.assertEqual(out["coverage_by_model"], {M1: 1, M2: 1})


if __name__ == "__main__":
    unittest.main()
