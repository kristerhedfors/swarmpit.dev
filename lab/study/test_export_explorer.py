"""Offline tests for the landscape explorer exporter (study/export_explorer.py).

Run from the lab/ directory:  python3 -m unittest study.test_export_explorer

Every fixture is SYNTHETIC and content-free: structural run dirs in a temp tree (integers, timestamps,
fixed enum codes, 16-hex ids) whose only seed text is the neutral placeholder "lorem". Gates are injected,
except the hermetic real-gate tests, which run tools/leakgate.py against a temp tree whose only corpus is a
lorem placeholder, and site/tools/check_public.py over a temp copy of a fixture document.
"""
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import config
from study import export_explorer as X
from study import landscape as L
from study import qd

R_ROOT = "0123456789abcdef"   # scratch ancestor (no episode of its own)
R_CHILD = "fedcba9876543210"  # mutation of R_ROOT, run in several episodes
R_PILOT = "00000000000000aa"  # pilot seed
B = 40
CEREBRAS = {"model": "qwen-3.8-27b", "budget": B}
GPTOSS = {"provider": "cerebras", "model": "gpt-oss-120b", "budget": B}
MISTRAL = {"provider": "berget", "model": "mistralai/Mistral-Small-3.2-24B-Instruct-2506", "budget": B}


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _seed(n_posts=2):
    return {"threads": [{"posts": [{"body": "lorem"} for _ in range(n_posts)]}]}


def make_run(runs: Path, name, rid, *, harness=CEREBRAS, iface="forum", goal=None, rep=7000,
             writes=(), n_agents=4, artifact=None, fatal=False, seed_posts=2):
    """A structural run dir. ``writes`` = [(agent, ts)]; agents step every 10 s from t=1000."""
    d = runs / name
    params = {"candidate_id": rid}
    if goal:
        params["goal"] = goal
    meta = {"run_id": name, "iface": iface, "n_agents": n_agents, "rep": rep, "harness": dict(harness),
            "started": 1000.0, "finished": 2000.0, "params": params}
    if fatal:
        meta["fatal"] = {"agent": 0, "status": 500}
    _write(d / "run.json", meta)
    _write(d / "seed.json", _seed(seed_posts))
    for a in range(n_agents):
        _write_jsonl(d / f"a{a}" / "steps.jsonl",
                     [{"kind": "step", "step": s, "t0": 1000.0 + 10 * s, "t1": 1005.0 + 10 * s}
                      for s in range(1, 11)])
    rows = [{"ts": ts, "agent": a, "kind": "post", "post_id": i + 1, "thread_seq": 1}
            for i, (a, ts) in enumerate(writes)]
    rows += [{"ts": 1012.0, "agent": a, "kind": "req", "method": "GET"} for a in range(n_agents)]
    _write_jsonl(d / "board_events.jsonl", rows)
    if artifact is not None:
        _write(d / "instigation" / "triggers.json", artifact)
    return d


def panel_artifact(name, codes, agreement, errors=()):
    recs = [{"run_id": name, "agent_id": a, "codes": [{"code": c, "confidence": 0.8}], "parser_version": 3,
             "model_slug": "qwen38"} for a, c in codes.items()]
    recs += [{"ok": False, "error_code": "E_PANEL", "line": 1, "col": 1, "run_id": name, "agent_id": a}
             for a in errors]
    return {"run_id": name, "parser_version": 3, "model_slug": "qwen38+gptoss120", "peer_wrote_first": {},
            "records": recs, "panel": {"slugs": ["qwen38", "gptoss120"], "votes": {},
                                       "agreement": {str(a): v for a, v in agreement.items()}}}


def arc_rec(rid, trig, chan, chars, model, goal, run_id, score=0.2, n_writers=1):
    return {"record_id": rid, "trigger_code": trig, "channel": chan, "seed_chars": chars,
            "instigation_score": score, "I": score, "n_writers": n_writers, "engagement": {},
            "engagement_score": 0.4, "bootstrap": False, "origin": "mutation", "parent_id": None,
            "generation": 1, "run_id": run_id, "model": model, "goal": goal}


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        cand = self.root / "cands"
        _write(cand / f"{R_ROOT}.chatter.json", _seed(3))  # 15 chars: sized by len() only
        reg = {"records": {
            R_ROOT: {"record_id": R_ROOT, "parent_id": None, "generation": 0, "channel": "forum",
                     "model": "qwen38", "chatter_path": str(cand / f"{R_ROOT}.chatter.json"),
                     "origin": "scratch"},
            R_CHILD: {"record_id": R_CHILD, "parent_id": R_ROOT, "generation": 2, "channel": "js",
                      "model": "", "origin": "mutation"},
            R_PILOT: {"record_id": R_PILOT, "parent_id": None, "generation": 0, "channel": "forum",
                      "model": "qwen38", "origin": "pilot", "bootstrap": True},
        }}
        self.registry = self.root / "registry.json"
        _write(self.registry, reg)

        # unsolvable, qwen38, js: two writers, panel artifact, one classifier error
        a = panel_artifact("js-multi4-r7001-unsolvable", {0: "TASK_HINT", 2: "TASK_HINT"},
                           {0: 1.0, 2: 0.5}, errors=())
        a["records"].append({"ok": False, "error_code": "E_PANEL", "line": 1, "col": 1, "agent_id": 3})
        make_run(self.runs, "js-multi4-r7001-unsolvable", R_CHILD, iface="js", goal="unsolvable", rep=7001,
                 writes=[(0, 1025.0), (2, 1047.0)], artifact=a)
        # same seed, same cell, another rep: no writers
        make_run(self.runs, "js-multi4-r7002-unsolvable", R_CHILD, iface="js", goal="unsolvable", rep=7002)
        # unsolvable, gptoss120 (retired), html, no writers, no artifact
        make_run(self.runs, "html-multi4-r7003-unsolvable--gptoss120", R_CHILD, harness=GPTOSS, iface="html",
                 goal="unsolvable", rep=7003)
        # unsolvable, mistral24, kv: one writer, artifact without panel block
        make_run(self.runs, "kv-multi4-r7004-unsolvable--mistral24", R_PILOT, harness=MISTRAL, iface="kv",
                 goal="unsolvable", rep=7004, writes=[(1, 1031.0)],
                 artifact={"run_id": "x", "records": [{"agent_id": 1, "codes": [
                     {"code": "SAW_SEED_ONLY", "confidence": 0.9}]}]})
        # solvable (no goal param), qwen38, forum, writer but no artifact -> UNRESOLVED
        make_run(self.runs, "forum-multi4-r7005", R_PILOT, rep=7005, writes=[(3, 1015.0)])
        # skipped / ignored
        make_run(self.runs, "forum-multi4-r7006", R_PILOT, rep=7006, fatal=True)            # void
        (self.runs / "forum-multi4-r7007").mkdir()                                          # no run.json
        _write(self.runs / "forum-multi4-r1" / "run.json", {"run_id": "forum-multi4-r1", "iface": "forum",
                                                            "n_agents": 4, "rep": 1, "harness": CEREBRAS})
        make_run(self.runs / "_aborted", "forum-multi4-r7008", R_PILOT, rep=7008)          # aborted
        make_run(self.runs, "forum-multi4-r7009", "not-a-hex-id", rep=7009)                # bad id

        self.archive = self.root / "landscape_archive.json"
        un = L.Archive(qd.goal_archive_path("unsolvable", self.archive), goal="unsolvable")
        un.consider(arc_rec(R_CHILD, "TASK_HINT", "js", 10, "qwen38", "unsolvable", "js-multi4-r7001-unsolvable"))
        un.consider(arc_rec(R_CHILD, "NO_WRITERS", "html", 10, "gptoss120", "unsolvable", "gone-run", 0.0, 0))
        un.save()
        so = L.Archive(self.archive, goal="solvable")
        so.consider(arc_rec(R_PILOT, "UNRESOLVED", "forum", 10, "qwen38", "solvable", "forum-multi4-r7005"))
        so.save()

    def tearDown(self):
        self._tmp.cleanup()

    def build(self):
        return X.build(self.runs, registry_path=self.registry, archive_base=self.archive, generated_ts=1.0)


class BuildTests(Fixture):
    def test_episodes_and_skips(self):
        doc, skipped = self.build()
        self.assertEqual(doc["schema"], "instigation.explorer.v1")
        ids = [e["run_id"] for e in doc["episodes"]]
        self.assertEqual(len(ids), 5)
        self.assertNotIn("forum-multi4-r7006", ids)
        self.assertNotIn("forum-multi4-r7008", ids)
        self.assertEqual(skipped, {"E_VOID": 1, "E_NO_RUNJSON": 1, "E_RECORD_ID": 1})
        # sorted: goal, then model order, then channel order
        self.assertEqual([(e["goal"], e["model"], e["channel"]) for e in doc["episodes"]],
                         [("solvable", "qwen38", "forum"), ("unsolvable", "qwen38", "js"),
                          ("unsolvable", "qwen38", "js"), ("unsolvable", "mistral24", "kv"),
                          ("unsolvable", "gptoss120", "html")])

    def test_episode_fields(self):
        doc, _ = self.build()
        ep = {e["run_id"]: e for e in doc["episodes"]}
        e = ep["js-multi4-r7001-unsolvable"]
        self.assertEqual(e["record_id"], R_CHILD)
        self.assertEqual(e["generation"], 2)  # the seed's registry generation
        self.assertEqual((e["n_agents"], e["budget"], e["rep"], e["n_writers"]), (4, B, 7001, 2))
        self.assertAlmostEqual(e["write_fraction"], 0.5)
        self.assertEqual(e["trigger_code"], "TASK_HINT")
        self.assertEqual(e["agent_codes"], ["TASK_HINT", None, "TASK_HINT", None])
        self.assertEqual(e["panel"], {"slugs": ["qwen38", "gptoss120"], "agreement_mean": 0.75})
        self.assertEqual(e["classifier_errors"], 1)
        self.assertEqual([p[1:] for p in e["posts_over_time"]], [[1, 1], [2, 2]])
        self.assertEqual(e["pile_in_onset_t_s"], 47.0)
        self.assertEqual(e["run_ttfw_s"], 25.0)
        self.assertEqual(set(e["engagement"]), set(X.ENGAGEMENT_KEYS))
        self.assertEqual(e["engagement"]["board_reads"], 4)
        self.assertGreater(e["I"], 0)
        self.assertLessEqual(e["instigation_score"], e["I"])

        self.assertEqual(ep["js-multi4-r7002-unsolvable"]["trigger_code"], "NO_WRITERS")
        self.assertEqual(ep["js-multi4-r7002-unsolvable"]["agent_codes"], [None] * 4)
        g = ep["html-multi4-r7003-unsolvable--gptoss120"]
        self.assertEqual((g["model"], g["panel"], g["classifier_errors"]), ("gptoss120", None, 0))
        k = ep["kv-multi4-r7004-unsolvable--mistral24"]
        self.assertEqual((k["model"], k["trigger_code"], k["panel"]), ("mistral24", "SAW_SEED_ONLY", None))
        f = ep["forum-multi4-r7005"]
        self.assertEqual((f["goal"], f["trigger_code"], f["agent_codes"][3]), ("solvable", "UNRESOLVED", None))

    def test_elite_in(self):
        doc, _ = self.build()
        ep = {e["run_id"]: e for e in doc["episodes"]}
        # the elite names its run: only that rep is marked
        self.assertEqual(ep["js-multi4-r7001-unsolvable"]["elite_in"], ["TASK_HINT|js|le120"])
        self.assertEqual(ep["js-multi4-r7002-unsolvable"]["elite_in"], [])
        # the elite's run is not exported: every run of that (record, model, goal, channel) is marked
        self.assertEqual(ep["html-multi4-r7003-unsolvable--gptoss120"]["elite_in"], ["NO_WRITERS|html|le120"])
        self.assertEqual(ep["forum-multi4-r7005"]["elite_in"], ["UNRESOLVED|forum|le120"])
        self.assertEqual(ep["kv-multi4-r7004-unsolvable--mistral24"]["elite_in"], [])

    def test_seeds_and_dims(self):
        doc, _ = self.build()
        seeds = doc["seeds"]
        self.assertEqual(set(seeds), {R_ROOT, R_CHILD, R_PILOT})  # ancestor included, sized by len()
        self.assertEqual(seeds[R_ROOT]["seed_chars"], 15)
        self.assertEqual(seeds[R_CHILD], {"seed_chars": 10, "length_band": "le120", "origin": "mutation",
                                          "bootstrap": False, "parent_id": R_ROOT, "generation": 2,
                                          "gen_model": None})
        self.assertEqual((seeds[R_PILOT]["origin"], seeds[R_PILOT]["bootstrap"]), ("pilot", True))
        dims = doc["dims"]
        self.assertEqual([m["slug"] for m in dims["model"]], list(config.MODEL_ORDER))
        retired = {m["slug"]: m["retired"] for m in dims["model"]}
        self.assertTrue(retired["gptoss120"])
        self.assertFalse(retired["qwen38"])
        self.assertEqual([c["id"] for c in dims["channel"]], ["forum", "kv", "js", "html"])
        self.assertEqual(dims["goal"], ["solvable", "unsolvable"])
        self.assertEqual(dims["trigger_code"], list(L.TRIGGER_AXIS))
        self.assertEqual(doc["landscapes"]["unsolvable"]["goal"], "unsolvable")
        self.assertEqual(doc["landscapes"]["unsolvable"]["coverage"], 2)
        self.assertEqual(doc["landscapes"]["solvable"]["coverage"], 1)

    def test_serialized_is_numbers_enums_ids_only(self):
        doc, _ = self.build()
        allowed = (set(X.TOP_KEYS) | set(X.DIM_KEYS) | set(X.MODEL_DIM_KEYS) | set(X.CHANNEL_DIM_KEYS)
                   | set(X.SEED_KEYS) | set(X.EPISODE_KEYS) | set(X.ENGAGEMENT_KEYS) | set(X.PANEL_KEYS))
        strings = []

        def walk(o, in_landscape=False):
            if isinstance(o, dict):
                for k, v in o.items():
                    if not in_landscape:
                        self.assertTrue(k in allowed or X._HEX16.match(k) or k in config.GOALS, k)
                    walk(v, in_landscape or k == "landscapes")
            elif isinstance(o, list):
                for v in o:
                    walk(v, in_landscape)
            elif isinstance(o, str):
                strings.append(o)
            elif isinstance(o, float):
                self.assertTrue(math.isfinite(o))

        walk(doc)
        self.assertNotIn("lorem", json.dumps(doc))
        self.assertTrue(all("lorem" not in s for s in strings))


class ValidateTests(Fixture):
    def _bad(self, mutate):
        doc, _ = self.build()
        mutate(doc)
        with self.assertRaises(X.ExportError) as cm:
            X.validate(doc)
        self.assertRegex(str(cm.exception), r"^E_[A-Z_]+$")  # fixed code, never the value

    def test_rejects_unknown_key(self):
        self._bad(lambda d: d["episodes"][0].__setitem__("note", 1))

    def test_rejects_free_text_enum(self):
        self._bad(lambda d: d["episodes"][0].__setitem__("trigger_code", "placeholder words"))

    def test_rejects_bad_ids(self):
        self._bad(lambda d: d["episodes"][0].__setitem__("record_id", "XYZ"))
        self._bad(lambda d: d["episodes"][0].__setitem__("run_id", "has spaces in it"))
        self._bad(lambda d: d["seeds"][R_CHILD].__setitem__("parent_id", "lorem"))

    def test_rejects_non_finite_and_wrong_types(self):
        self._bad(lambda d: d["episodes"][0].__setitem__("I", float("nan")))
        self._bad(lambda d: d["episodes"][0].__setitem__("n_writers", True))
        self._bad(lambda d: d["episodes"][0]["posts_over_time"].append(["lorem", 1, 1]))
        self._bad(lambda d: d["episodes"][0]["agent_codes"].append("NONE"))

    def test_rejects_label_drift_and_unknown_channel(self):
        self._bad(lambda d: d["dims"]["channel"][0].__setitem__("label", "lorem"))
        self._bad(lambda d: d["episodes"][0].__setitem__("channel", "wiki"))

    def test_rejects_dangling_record_and_bad_elite(self):
        self._bad(lambda d: d["seeds"].pop(R_PILOT))
        self._bad(lambda d: d["episodes"][0].__setitem__("elite_in", ["TASK_HINT|js|le120"]))

    def test_rejects_bad_landscape(self):
        self._bad(lambda d: d["landscapes"]["solvable"].__setitem__("goal", "unsolvable"))
        self._bad(lambda d: d["landscapes"].pop("solvable"))


class PublishTests(Fixture):
    def setUp(self):
        super().setUp()
        self.doc, _ = self.build()
        self.out = self.root / "pub" / "explorer.json"

    def test_success_writes_exact_bytes(self):
        n = X.publish(self.doc, out_path=self.out, gate=lambda p: {"ok": True}, verify=lambda p: {"ok": True})
        self.assertEqual(self.out.read_bytes(), X.serialize(self.doc))
        self.assertEqual(n, len(X.serialize(self.doc)))
        self.assertEqual(list(self.out.parent.iterdir()), [self.out])

    def test_gate_failure_publishes_nothing(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_bytes(b"{}")
        for gate in (lambda p: {"ok": False, "error_code": "E_LEAK"}, lambda p: None,
                     mock.Mock(side_effect=RuntimeError("x"))):
            with self.assertRaises(X.ExportError):
                X.publish(self.doc, out_path=self.out, gate=gate, verify=None)
            self.assertEqual(self.out.read_bytes(), b"{}")
            self.assertEqual(list(self.out.parent.iterdir()), [self.out])

    def test_verify_failure_rolls_back(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_bytes(b"{\"prev\":1}")
        with self.assertRaises(X.ExportError) as cm:
            X.publish(self.doc, out_path=self.out, gate=lambda p: {"ok": True}, verify=lambda p: {"ok": False})
        self.assertEqual(cm.exception.args[0], "E_VERIFY")
        self.assertEqual(self.out.read_bytes(), b"{\"prev\":1}")
        fresh = self.root / "pub" / "fresh.json"
        with self.assertRaises(X.ExportError):
            X.publish(self.doc, out_path=fresh, gate=lambda p: {"ok": True}, verify=lambda p: {"ok": False})
        self.assertFalse(fresh.exists())

    def test_invalid_doc_never_reaches_gate(self):
        gate = mock.Mock(return_value={"ok": True})
        self.doc["episodes"][0]["trigger_code"] = "lorem"
        with self.assertRaises(X.ExportError):
            X.publish(self.doc, out_path=self.out, gate=gate, verify=None)
        gate.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_cli_prints_numbers_only_summary(self):
        buf = io.StringIO()
        with mock.patch.object(X, "default_gate", return_value={"ok": True}), \
                mock.patch.object(X, "check_public", return_value={"ok": True}), redirect_stdout(buf):
            rc = X.main(["--runs", str(self.runs), "--registry", str(self.registry), "--archive",
                         str(self.archive), "--out", str(self.out)])
        self.assertEqual(rc, 0)
        res = json.loads(buf.getvalue())
        self.assertEqual(res["n_episodes"], 5)
        self.assertEqual(res["by_goal"], {"solvable": 1, "unsolvable": 4})
        self.assertEqual(res["by_model"], {"qwen38": 3, "mistral24": 1, "gptoss120": 1})
        self.assertEqual(res["n_seeds"], 3)
        self.assertTrue(res["ok"])
        self.assertTrue(self.out.exists())

    def test_cli_failure_is_fixed_code(self):
        buf = io.StringIO()
        with mock.patch.object(X, "default_gate", return_value={"ok": False, "error_code": "E_LEAK"}), \
                redirect_stdout(buf):
            rc = X.main(["--runs", str(self.runs), "--registry", str(self.registry), "--archive",
                         str(self.archive), "--out", str(self.out)])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(buf.getvalue()), {"ok": False, "error": "E_LEAK"})
        self.assertFalse(self.out.exists())


class RealGateTests(Fixture):
    """Hermetic: the real gate tools over temp trees whose only corpus is a lorem placeholder."""

    def test_leakgate_and_check_public_pass_fixture_doc(self):
        doc, _ = self.build()
        tree = self.root / "tree"
        _write(tree / "data" / "synthetic-agent-chatter" / "c.chatter.json",
               {"body": " ".join(["lorem ipsum dolor sit amet"] * 4)})
        out = tree / "site" / "public" / "data" / "instigation" / "explorer.json"
        X.publish(doc, out_path=out, gate=lambda p: X.leakgate(p, root=tree), verify=X.check_public)
        self.assertTrue(out.exists())

    def test_leakgate_flags_corpus_overlap(self):
        tree = self.root / "tree"
        phrase = " ".join(["lorem ipsum dolor sit amet"] * 4)
        _write(tree / "data" / "synthetic-agent-chatter" / "c.chatter.json", {"body": phrase})
        bad = tree / "site" / "public" / "data" / "instigation" / "bad.json"
        _write(bad, {"x": phrase})
        self.assertFalse(X.leakgate(bad, root=tree).get("ok"))

    def test_check_public_flags_literal(self):
        bad = self.root / "bad.json"
        _write(bad, {"x": "/Users/placeholder"})
        self.assertFalse(X.check_public(bad)["ok"])


if __name__ == "__main__":
    unittest.main()
