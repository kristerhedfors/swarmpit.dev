#!/usr/bin/env python3
"""Tests for tools/export.py and the published bundles (python3 tools/test_export.py).

- pure helpers (truncation, head/tail, leak spans, density) - stdlib only
- multi-model and goal (solvable vs unsolvable) findings, ordering, featured pick, index.models, CTF seal/allow
  helpers and goal validation on tiny numeric sqlite fixtures - stdlib only
- every bundle in public/data validates against the src/types.ts shapes (validate_core/detail/index)
- referential integrity + hash naming + the forum-multi4-r1 acceptance figures (SPEC 15)
- no residual leaks (check_public.py --data-only) and the redaction totals are recorded
- a re-export into a temp dir is byte-identical for the core bundles (determinism; needs the dash venv)
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SITE = TOOLS.parent
DATA = SITE / "public" / "data"
sys.path.insert(0, str(TOOLS))
import export as X  # noqa: E402


def load(p):
    with open(p, "rb") as fh:
        return json.loads(fh.read())


class Helpers(unittest.TestCase):
    def test_trunc(self):
        self.assertEqual(X.trunc("abc", 5), "abc")
        out = X.trunc("cat /home/agent/x and then /home/agent/ctf/project/file", 30)
        self.assertLessEqual(len(out), 30)
        self.assertTrue(out.endswith("…"))
        self.assertNotRegex(out, r"/home/ag…$")

    def test_head_tail(self):
        s = "\n".join(f"line {i:04d} /home/agent/ctf" for i in range(400))
        out = X.head_tail(s)
        self.assertIn("…[", out)
        self.assertLess(len(out), 1200)
        m = re.search(r"…\[(\d+) chars\]…", out)
        left, right = out.split(m.group(0))
        self.assertEqual(len(left) - 1 + int(m.group(1)) + len(right) - 1, len(s), "omitted count is exact")
        self.assertTrue(left.endswith("\n") and right.startswith("\n"))
        self.assertEqual(X.head_tail("short"), "short")

    def test_leak_spans(self):
        self.assertEqual(X.leak_spans("x FLAG{a} y FLAG{a}", ["FLAG{a}"]), [[2, 9], [12, 19]])
        self.assertEqual(X.leak_spans("abcdef", ["bcd", "cde"]), [[1, 5]])
        self.assertEqual(X.leak_spans("nothing", ["zzz", ""]), [])

    def test_tables_match_types_ts(self):
        ts = (SITE / "src" / "types.ts").read_text()
        for name, py in (("NODE_KINDS", X.NODE_KINDS), ("STEP_CATS", X.STEP_CATS), ("CHAPTER_KINDS", X.CHAPTER_KINDS)):
            m = re.search(rf"export const {name} = \[(.*?)\] as const", ts, re.S)
            self.assertIsNotNone(m, name)
            self.assertEqual(re.findall(r"'([^']+)'", m.group(1)), py, name)
        for name, py in (("EDGE_KINDS", X.EDGE_KINDS), ("EVENT_KINDS", X.EVENT_KINDS)):
            m = re.search(rf"export const {name} = \[(.*?)\] as const", ts, re.S)
            body = re.sub(r"//[^\n]*", "", m.group(1))
            self.assertEqual(re.findall(r"'([^']+)'", body), py, name)
        m = re.search(r"export const EF = \{(.*?)\} as const", ts, re.S)
        ef = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", re.sub(r"//[^\n]*", "", m.group(1)))}
        self.assertEqual(ef, X.EF)


GLM = "zai-org/GLM-5.3-Flash"
MISTRAL = "mistralai/Mistral-Small-3.2-24B-Instruct-2506"
# (run_id, model, iface, pop, agent_id, discovered, posts, board_steps, board_steps_free, solved, share_after_contact)
AGENTS = [
    ("forum-solo-r0", X.PRIMARY, "forum", "solo", 0, 1, 1, 6, 2, 1, 0.5),
    ("forum-solo-r1", X.PRIMARY, "forum", "solo", 0, 0, 0, 0, 0, 1, None),
    ("forum-multi2-r0", X.PRIMARY, "forum", "multi", 0, 1, 2, 9, 4, 1, 0.25),
    ("forum-multi2-r0", X.PRIMARY, "forum", "multi", 1, 1, 0, 3, 1, 0, 0.125),
    ("wiki-solo-r0", X.PRIMARY, "wiki", "solo", 0, 1, 0, 2, 0, 1, 0.75),
    ("forum-solo-r0--glm53f", GLM, "forum", "solo", 0, 0, 0, 1, 0, 0, None),
    ("forum-solo-r1--glm53f", GLM, "forum", "solo", 0, 1, 3, 12, 7, 1, 0.5),
    ("forum-multi2-r0--glm53f", GLM, "forum", "multi", 0, 1, 1, 5, 5, 1, 0.375),
    ("forum-multi2-r0--glm53f", GLM, "forum", "multi", 1, 1, 0, 4, 0, 1, 0.5),
]


def fixture_db(agents=AGENTS, extra=()) -> sqlite3.Connection:
    """In-memory stand-in for the scratch obs DB: only the tables/views build_findings reads, numbers only.
    `extra` rows are unsolvable-goal agents: AGENTS columns + (steps, live_edges_out); their runs get
    params.goal = 'unsolvable' and, as in dash, board_steps_free NULL and no part in v_cell_metrics."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mat_agent_metrics (run_id, model, iface, pop, agent_id, discovered, posts, board_steps, "
                "board_steps_free, solved, share_after_contact, steps DEFAULT 10, live_edges_out DEFAULT 0, "
                "tag DEFAULT '', run_status DEFAULT 'done')")
    con.executemany("INSERT INTO mat_agent_metrics (run_id, model, iface, pop, agent_id, discovered, posts, board_steps, "
                    "board_steps_free, solved, share_after_contact) VALUES (?,?,?,?,?,?,?,?,?,?,?)", agents)
    con.executemany("INSERT INTO mat_agent_metrics (run_id, model, iface, pop, agent_id, discovered, posts, board_steps, "
                    "board_steps_free, solved, share_after_contact, steps, live_edges_out) VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                    [x[:8] + x[9:] for x in extra])
    con.execute("CREATE TABLE runs (run_id, model, tag DEFAULT '', status DEFAULT 'done', harness, params)")
    for rid in sorted({a[0] for a in agents}):
        mid = next(a[1] for a in agents if a[0] == rid)
        con.execute("INSERT INTO runs (run_id, model, harness, params) VALUES (?, ?, ?, ?)",
                    [rid, mid, json.dumps({"budget": 40 if mid == X.PRIMARY else 30}), json.dumps({"image": "x"})])
    for rid in sorted({a[0] for a in extra}):
        mid = next(a[1] for a in extra if a[0] == rid)
        con.execute("INSERT INTO runs (run_id, model, harness, params) VALUES (?, ?, ?, ?)",
                    [rid, mid, json.dumps({"budget": 60}), json.dumps({"image": "x", "goal": "unsolvable"})])
    con.execute("CREATE VIEW v_cell_metrics AS SELECT m.model, m.iface, m.pop, count(*) AS n_agents, "
                "avg(m.discovered) AS discovery_rate, avg(m.share_after_contact) AS share_after_contact, avg(m.posts) AS posts, "
                "avg(m.solved) AS solve_rate, avg(m.board_steps) AS board_steps FROM mat_agent_metrics m LEFT JOIN runs r USING (run_id) "
                "WHERE m.tag = '' AND m.run_status = 'done' "
                "AND coalesce(json_extract(r.params, '$.goal'), 'solvable') = 'solvable' GROUP BY m.model, m.iface, m.pop")
    return con


def analysis_of(model: str, agents=None, free=True) -> dict:
    """analysis.json-shaped cells/pooled computed from AGENTS (or `agents`) for one model (values rounded as lab
    does). free=False is the unsolvable shape: board_steps_free None with a [None, None] CI."""
    ag = [a for a in (AGENTS if agents is None else agents) if a[1] == model]
    mean = lambda xs: sum(xs) / len(xs)
    cells = []
    for iface, pop in sorted({(a[2], a[3]) for a in ag}):
        g = [a for a in ag if (a[2], a[3]) == (iface, pop)]
        sac = [a[10] for a in g if a[10] is not None]
        cells.append({"iface": iface, "pop": pop, "n_agents": len(g), "discovery_rate": round(mean([a[5] for a in g]), 3),
                      "share_after_contact": round(mean(sac), 3) if sac else None,
                      "posts": round(mean([a[6] for a in g]), 3), "solve_rate": round(mean([a[9] for a in g]), 3),
                      "board_steps": round(mean([a[7] for a in g]), 3)})
    pooled = []
    for iface in X.IFACE_ORDER:
        g = [a for a in ag if a[2] == iface]
        if not g:
            continue
        row = {"iface": iface, "n_agents": len(g)}
        for k, fn in (("contacted", lambda a: float(a[5])), ("posted", lambda a: 1.0 if a[6] else 0.0),
                      ("posts", lambda a: float(a[6])), ("board_steps", lambda a: float(a[7])),
                      ("board_steps_free", lambda a: float(a[8]))):
            if k == "board_steps_free" and not free:
                row[k], row[k + "_ci"] = None, [None, None]
                continue
            row[k] = round(mean([fn(a) for a in g]), 3)
            row[k + "_ci"] = [0.0, 9.0]
        pooled.append(row)
    return {"cells": cells, "pooled": pooled}


def card(rid, model, iface="forum", pop="solo", rep=0, score=1, hidden=False, caveats=()):
    return {"id": rid, "model": model, "iface": iface, "pop": pop, "rep": rep, "tag": "", "score": score,
            "durationMs": 1000, "hidden": hidden, "caveats": list(caveats), "nAgents": 1 if pop == "solo" else 2}


class MultiModel(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.runs = Path(self.td.name)
        self.con = fixture_db()
        self.cards = [card(a[0], a[1], a[2], a[3]) for a in AGENTS if a[4] == 0]

    def tearDown(self):
        self.con.close()
        self.td.cleanup()

    def write(self, name, obj):
        (self.runs / name).write_text(json.dumps(obj))

    def findings(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return X.build_findings(self.con, self.runs, self.cards)

    def test_registry_matches_lab_config(self):
        cfg = X._lab_config()
        reg = X.model_registry()
        self.assertEqual([m["slug"] for m in reg], list(cfg.MODEL_ORDER))
        self.assertEqual(reg[0]["id"], X.PRIMARY)
        self.assertEqual(cfg.MODELS[cfg.DEFAULT_SLUG]["model"], X.PRIMARY)
        for m in reg:
            self.assertEqual(m["id"], cfg.MODELS[m["slug"]]["model"])
            self.assertEqual(m["provider"], cfg.MODELS[m["slug"]]["provider"])

    def test_cerebras_analysis_plus_berget_obs(self):
        self.write("analysis.json", analysis_of(X.PRIMARY))
        f = self.findings()
        self.assertEqual(f["source"], "analysis.json")
        self.assertEqual(f["mismatches"], [])
        self.assertEqual(f["models"], [X.PRIMARY, GLM])
        keys = [(c["model"], c["iface"], c["pop"]) for c in f["cells"]]
        self.assertEqual(keys, [(X.PRIMARY, "forum", "multi"), (X.PRIMARY, "forum", "solo"), (X.PRIMARY, "wiki", "solo"),
                                (GLM, "forum", "multi"), (GLM, "forum", "solo")])
        self.assertEqual([(p["model"], p["iface"], p["nAgents"]) for p in f["pooled"]],
                         [(X.PRIMARY, "forum", 4), (X.PRIMARY, "wiki", 1), (GLM, "forum", 4)])
        q = next(c for c in f["cells"] if c["model"] == X.PRIMARY and c["iface"] == "forum" and c["pop"] == "solo")
        g = next(c for c in f["cells"] if c["model"] == GLM and c["iface"] == "forum" and c["pop"] == "solo")
        self.assertEqual((q["discoveryRate"], q["boardSteps"]), (0.5, 3))
        self.assertEqual((g["discoveryRate"], g["boardSteps"]), (0.5, 6.5))
        pq = next(p for p in f["pooled"] if p["model"] == X.PRIMARY and p["iface"] == "forum")
        pg = next(p for p in f["pooled"] if p["model"] == GLM)
        self.assertEqual(pq["metrics"]["contacted"], {"v": 0.75, "lo": 0.0, "hi": 9.0})   # analysis.json CI kept
        self.assertEqual(pg["metrics"]["board_steps_free"]["v"], 3)
        self.assertNotEqual(pg["metrics"]["board_steps_free"]["hi"], 9.0)               # obs bootstrap CI
        self.assertEqual(set(f["notes"]), {X.PRIMARY, GLM})
        self.assertIn("budget 40", f["notes"][X.PRIMARY])
        self.assertIn("budget 30", f["notes"][GLM])
        self.assertEqual(f["note"], f["notes"][X.PRIMARY])

    def test_primary_only_unchanged_shape(self):
        self.con.execute("DELETE FROM mat_agent_metrics WHERE model <> ?", [X.PRIMARY])
        self.cards = [c for c in self.cards if c["model"] == X.PRIMARY]
        self.write("analysis.json", analysis_of(X.PRIMARY))
        f = self.findings()
        self.assertEqual((f["source"], f["mismatches"], f["models"]), ("analysis.json", [], [X.PRIMARY]))
        self.assertEqual(f["note"], f"Pilot snapshot: n=1/4 agent sessions per interface, {X.PRIMARY}, budget 40")
        self.assertEqual(len(f["caveats"]), 3)

    def test_primary_mismatch_still_flips(self):
        an = analysis_of(X.PRIMARY)
        an["cells"][0]["posts"] += 1
        self.write("analysis.json", an)
        f = self.findings()
        self.assertEqual(f["source"], "obs")
        self.assertEqual(len(f["mismatches"]), 1)
        self.assertTrue(f["mismatches"][0].startswith("cell forum·multi posts:"))

    def test_analysis_models_cross_check(self):
        self.write("analysis.json", analysis_of(X.PRIMARY))
        self.write("analysis_models.json", {"models": ["qwen38", "glm53f"], "by_model": {
            "qwen38": {"model": X.PRIMARY, "cells": [], "pooled": []},   # ignored: analysis.json is the primary side
            "glm53f": {"model": GLM, **analysis_of(GLM)}}})
        f = self.findings()
        self.assertEqual((f["source"], f["mismatches"]), ("analysis.json", []))
        pg = next(p for p in f["pooled"] if p["model"] == GLM)
        self.assertEqual(pg["metrics"]["board_steps_free"], {"v": 3, "lo": 0.0, "hi": 9.0})
        bad = analysis_of(GLM)
        bad["pooled"][0]["posted"] = 0.9
        self.write("analysis_models.json", {"by_model": {"glm53f": bad}})   # no "model" key: resolved by slug
        f = self.findings()
        self.assertEqual(f["source"], "obs")
        self.assertEqual(f["mismatches"], ["pooled glm53f·forum posted: analysis.json=0.9 obs=0.5"])

    def test_irc_exclusion_is_per_model(self):
        self.cards.append(card("irc-solo-r0--glm53f", GLM, "irc", "solo", caveats=["irc_inbound_broken"]))
        self.con.execute("INSERT INTO mat_agent_metrics (run_id, model, iface, pop, agent_id, discovered, posts, "
                         "board_steps, board_steps_free, solved) VALUES ('irc-solo-r0--glm53f', ?, 'irc', 'solo', 0, 0, 0, 0, 0, 1)", [GLM])
        self.con.execute("INSERT INTO mat_agent_metrics (run_id, model, iface, pop, agent_id, discovered, posts, "
                         "board_steps, board_steps_free, solved) VALUES ('irc-solo-r0', ?, 'irc', 'solo', 0, 1, 0, 1, 0, 1)", [X.PRIMARY])
        f = self.findings()
        ex = {(c["model"], c["iface"]): c["excludedByDefault"] for c in f["cells"] if c["iface"] == "irc"}
        self.assertEqual(ex, {(X.PRIMARY, "irc"): False, (GLM, "irc"): True})

    def test_ordering_featured_and_index_models(self):
        cards = [card("forum-solo-r0--gemma31", "google/gemma-4-31B-it", score=99),
                 card("forum-solo-r0--glm53f", GLM, score=50), card("forum-solo-r0--mistral24", MISTRAL),
                 card("wiki-solo-r0", X.PRIMARY, "wiki", score=3), card("forum-multi2-r1", X.PRIMARY, "forum", "multi", 1),
                 card("forum-solo-r0", X.PRIMARY, score=2), card("irc-solo-r0", X.PRIMARY, "irc", score=80,
                                                                caveats=["irc_inbound_broken"]),
                 card("_synth-500", "synthetic", hidden=True, caveats=["synthetic"])]
        listed = sorted([c for c in cards if not c["hidden"]], key=X.run_sort_key)
        self.assertEqual([c["id"] for c in listed], ["forum-solo-r0", "forum-multi2-r1", "wiki-solo-r0", "irc-solo-r0",
                                                     "forum-solo-r0--mistral24", "forum-solo-r0--glm53f",
                                                     "forum-solo-r0--gemma31"])
        self.assertEqual(X.pick_featured(listed), "wiki-solo-r0")   # primary only, broken irc excluded
        self.assertEqual(X.pick_featured([c for c in listed if c["model"] == GLM]), "forum-solo-r0--glm53f")
        self.assertEqual(X.model_rank("someone/new-model"), (4, "someone/new-model"))
        ms = X.index_models(cards)
        self.assertEqual([(m["slug"], m["nRuns"]) for m in ms], [("qwen38", 4), ("mistral24", 1), ("glm53f", 1), ("gemma31", 1)])
        self.assertEqual(ms[2], {"id": GLM, "slug": "glm53f", "label": "GLM-5.3 Flash", "provider": "berget", "nRuns": 1})

    def test_validate_index_models(self):
        def ix(models, cells):
            runs = [{"id": c["id"], "core": f"runs/{c['id']}.abcd1234.core.json", "detail": "d", "hash": "abcd1234",
                     "iface": c["iface"], "pop": c["pop"], "tag": "", "model": c["model"], "blurb": "", "density": [0] * 64,
                     "hidden": c["hidden"], "caveats": c["caveats"]} for c in cards]
            return {"format": X.INDEX_FORMAT, "featured": "forum-solo-r0", "models": models, "runs": runs,
                    "findings": {"source": "obs", "cells": cells, "pooled": []}}
        cards = [card("forum-solo-r0", X.PRIMARY), card("forum-solo-r0--glm53f", GLM),
                 card("_synth-500", "synthetic", hidden=True, caveats=["synthetic"])]
        good = X.index_models(cards)
        cells = [{"model": X.PRIMARY, "iface": "forum", "pop": "solo"}, {"model": GLM, "iface": "forum", "pop": "solo"}]
        self.assertEqual(X.validate_index(ix(good, cells)), [])
        self.assertTrue(any("match listed" in e for e in X.validate_index(ix(good[:1], cells))))
        self.assertTrue(any("unique" in e for e in X.validate_index(ix(good, cells + cells[:1]))))
        bad = [dict(good[0], nRuns=2), good[1]]
        self.assertTrue(any("nRuns count" in e for e in X.validate_index(ix(bad, cells))))

    def test_model_ids_survive_redaction(self):
        R = X.Redactor(deny=X.default_denylist([]))
        tree = {"models": [{k: m[k] for k in ("id", "slug", "label", "provider")} for m in X.model_registry()]}
        self.assertEqual(X.redact_tree(tree, R), tree)
        self.assertEqual(sum(R.counts.values()), 0)


# unsolvable-goal agents: AGENTS columns (board_steps_free ignored: NULL, as in dash) + (steps, live_edges_out)
UNSOLV = [
    ("forum-multi2-r0-unsolvable", X.PRIMARY, "forum", "multi", 0, 1, 3, 12, None, 0, 0.6, 20, 2),
    ("forum-multi2-r0-unsolvable", X.PRIMARY, "forum", "multi", 1, 1, 1, 8, None, 0, 0.4, 20, 1),
    ("wiki-solo-r0-unsolvable", X.PRIMARY, "wiki", "solo", 0, 0, 0, 0, None, 0, None, 20, 0),
    ("forum-solo-r0-unsolvable--glm53f", GLM, "forum", "solo", 0, 1, 2, 10, None, 0, 0.5, 40, 0),
]


def unsolvable_analysis_of(model: str) -> dict:
    """analysis.unsolvable.json-shaped doc (lab --goal unsolvable): free-phase metrics None, n/a CIs."""
    an = analysis_of(model, [a for a in UNSOLV if a[1] == model], free=False)
    return {"goal": "unsolvable", **an}


class Goals(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.runs = Path(self.td.name)
        self.cards = [card(a[0], a[1], a[2], a[3]) for a in AGENTS if a[4] == 0]
        self.ucards = [dict(card(a[0], a[1], a[2], a[3]), goal="unsolvable", caveats=["unsolvable"])
                       for a in UNSOLV if a[4] == 0]
        (self.runs / "analysis.json").write_text(json.dumps(analysis_of(X.PRIMARY)))

    def tearDown(self):
        self.td.cleanup()

    def findings(self, extra=UNSOLV, cards=None):
        import contextlib
        import io
        con = fixture_db(AGENTS, extra)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                f = X.build_findings(con, self.runs, self.cards + self.ucards if cards is None else cards)
        finally:
            con.close()
        self.log = buf.getvalue()
        return f

    def test_helpers(self):
        self.assertEqual(X.run_goal(None), "solvable")
        self.assertEqual(X.run_goal('{"goal": "unsolvable"}'), "unsolvable")
        self.assertEqual(X.run_goal({"goal": "unsolvable"}), "unsolvable")
        self.assertEqual(X.run_goal({"goal": "bogus"}), "solvable")
        self.assertEqual(X.run_goal("[1]"), "solvable")
        self.assertEqual(X.goal_kw("solvable"), {})
        self.assertEqual(X.goal_kw("unsolvable"), {"goal": "unsolvable"})
        self.assertEqual(X.goal_of({}), "solvable")
        self.assertIn("unsolvable", X.CAVEAT_TEXT)
        ts = (SITE / "src" / "types.ts").read_text()
        m = re.search(r"export type CaveatCode = (.*?);", ts, re.S)
        self.assertEqual(set(re.findall(r"'([^']+)'", m.group(1))), set(X.CAVEAT_TEXT))

    def test_solvable_rows_unchanged_by_unsolvable_runs(self):
        f0 = self.findings(extra=(), cards=self.cards)
        f1 = self.findings(cards=self.cards)
        self.assertNotIn("goals", f0)
        self.assertNotIn("goalNotes", f0)
        for k in ("source", "mismatches", "notes", "note", "models"):
            self.assertEqual(f1[k], f0[k], k)
        self.assertEqual(X.jdump([c for c in f1["cells"] if "goal" not in c]), X.jdump(f0["cells"]))
        self.assertEqual(X.jdump([p for p in f1["pooled"] if "goal" not in p]), X.jdump(f0["pooled"]))
        self.assertEqual(f1["caveats"][:len(f0["caveats"])], f0["caveats"])

    def test_unsolvable_rows(self):
        f = self.findings()
        self.assertEqual(f["source"], "analysis.json", "no unsolvable analysis side: unchecked, no flip")
        self.assertEqual(f["mismatches"], [])
        self.assertIn("(unsolvable goal): no analysis-side cells", self.log)
        self.assertEqual(f["goals"], ["solvable", "unsolvable"])
        uc = [(c["model"], c["iface"], c["pop"]) for c in f["cells"] if c.get("goal") == "unsolvable"]
        self.assertEqual(uc, [(X.PRIMARY, "forum", "multi"), (X.PRIMARY, "wiki", "solo"), (GLM, "forum", "solo")])
        order = [(X.model_rank(c["model"]), X.GOALS.index(X.goal_of(c))) for c in f["cells"]]
        self.assertEqual(order, sorted(order), "cells: model, then goal (solvable first)")
        self.assertTrue(all(c["solveRate"] == 0 for c in f["cells"] if c.get("goal")))
        q = next(c for c in f["cells"] if c.get("goal") and c["iface"] == "forum" and c["model"] == X.PRIMARY)
        self.assertEqual((q["nAgents"], q["discoveryRate"], q["posts"], q["boardSteps"], q["shareAfterContact"]),
                         (2, 1, 2, 10, 0.5))
        up = [p for p in f["pooled"] if p.get("goal") == "unsolvable"]
        self.assertEqual([(p["model"], p["iface"], p["nAgents"]) for p in up],
                         [(X.PRIMARY, "forum", 2), (X.PRIMARY, "wiki", 1), (GLM, "forum", 1)])
        pq = up[0]["metrics"]
        self.assertEqual(set(pq), {"contacted", "posted", "posts", "board_steps", "share_board", "live_edges"})
        self.assertNotIn("board_steps_free", pq, "free time is n/a under the unsolvable goal")
        self.assertEqual((pq["contacted"]["v"], pq["posts"]["v"], pq["board_steps"]["v"], pq["share_board"]["v"],
                          pq["live_edges"]["v"]), (1, 2, 10, 0.5, 1.5))
        self.assertEqual(set(f["goalNotes"]), {"unsolvable"})
        self.assertEqual(set(f["goalNotes"]["unsolvable"]), {X.PRIMARY, GLM})
        self.assertTrue(f["goalNotes"]["unsolvable"][X.PRIMARY].startswith("Unsolvable-goal snapshot: n=1/2"))
        self.assertIn("budget 60", f["goalNotes"]["unsolvable"][GLM])
        self.assertIn("budget 40", f["notes"][X.PRIMARY], "solvable notes ignore unsolvable runs")
        self.assertTrue(any("unsolvable-goal runs" in c for c in f["caveats"]))
        ix = {"format": X.INDEX_FORMAT, "featured": "forum-solo-r0", "runs": [], "findings": f}
        self.assertEqual([e for e in X.validate_index(ix) if "featured" not in e], [])

    def test_unsolvable_analysis_side(self):
        self.write = lambda n, o: (self.runs / n).write_text(json.dumps(o))
        self.write("analysis.unsolvable.json", unsolvable_analysis_of(X.PRIMARY))
        self.write("analysis_models.unsolvable.json", {"goal": "unsolvable", "models": ["glm53f"],
                                                       "by_model": {"glm53f": {"model": GLM, **unsolvable_analysis_of(GLM)}}})
        f = self.findings()
        self.assertEqual((f["source"], f["mismatches"]), ("analysis.json", []))
        self.assertNotIn("(unsolvable goal): no analysis-side cells", self.log)
        pq = next(p for p in f["pooled"] if p.get("goal") and p["model"] == X.PRIMARY and p["iface"] == "forum")
        self.assertEqual(pq["metrics"]["contacted"], {"v": 1, "lo": 0.0, "hi": 9.0}, "analysis CI kept")
        bad = unsolvable_analysis_of(X.PRIMARY)
        bad["cells"][0]["posts"] += 1
        self.write("analysis.unsolvable.json", bad)
        f = self.findings()
        self.assertEqual(f["source"], "obs")
        self.assertEqual([m.split(":")[0] for m in f["mismatches"]], ["cell unsolvable·forum·multi posts"])
        badg = unsolvable_analysis_of(GLM)
        badg["pooled"][0]["posted"] = 0.25
        self.write("analysis.unsolvable.json", unsolvable_analysis_of(X.PRIMARY))
        self.write("analysis_models.unsolvable.json", {"by_model": {"glm53f": badg}})
        f = self.findings()
        self.assertEqual(f["mismatches"], ["pooled unsolvable·glm53f·forum posted: analysis.json=0.25 obs=1.0"])

    def test_validate_index_goal_rules(self):
        f = self.findings()
        ix = lambda fd, runs=(): {"format": X.INDEX_FORMAT, "featured": "x", "runs": list(runs), "findings": fd}
        errs = lambda fd, runs=(): [e for e in X.validate_index(ix(fd, runs)) if "featured" not in e]
        self.assertEqual(errs(f), [])
        g = dict(f)
        g.pop("goals")
        self.assertTrue(any("need findings.goals" in e for e in errs(g)))
        dup = dict(f, cells=f["cells"] + [c for c in f["cells"] if c.get("goal")][:1])
        self.assertTrue(any("(model, goal, iface, pop) unique" in e for e in errs(dup)))
        expl = dict(f, cells=[dict(c, goal="solvable") if "goal" not in c else c for c in f["cells"]])
        self.assertTrue(any("goal values" in e for e in errs(expl)), "solvable is never written")
        run = {"id": "forum-solo-r0-unsolvable", "core": "runs/forum-solo-r0-unsolvable.abcd1234.core.json", "detail": "d", "hash": "abcd1234",
               "iface": "forum", "pop": "solo", "tag": "", "model": X.PRIMARY, "blurb": "", "density": [0] * 64,
               "hidden": False, "caveats": ["unsolvable"], "goal": "solvable"}
        self.assertTrue(any("goal (only 'unsolvable'" in e for e in errs(f, [run])))

    def test_order_and_featured(self):
        cards = [dict(card("forum-solo-r0-unsolvable", X.PRIMARY, score=99), goal="unsolvable"), card("forum-solo-r0", X.PRIMARY),
                 card("wiki-solo-r0", X.PRIMARY, "wiki", score=5), dict(card("forum-solo-r0-unsolvable--glm53f", GLM), goal="unsolvable"),
                 card("forum-solo-r0--glm53f", GLM)]
        ids = [c["id"] for c in sorted(cards, key=X.run_sort_key)]
        self.assertEqual(ids, ["forum-solo-r0", "wiki-solo-r0", "forum-solo-r0-unsolvable", "forum-solo-r0--glm53f",
                               "forum-solo-r0-unsolvable--glm53f"])
        rows = [{"model": X.PRIMARY, "iface": "forum", "pop": "solo", "rep": 0, "tag": "", "params": '{"goal":"unsolvable"}'},
                {"model": X.PRIMARY, "iface": "irc", "pop": "multi", "rep": 3, "tag": "", "params": None}]
        self.assertEqual([r["iface"] for r in sorted(rows, key=X.run_sort_key)], ["irc", "forum"], "runs rows: goal from params")
        self.assertEqual(X.pick_featured(cards), "wiki-solo-r0", "unsolvable runs never auto-feature")
        only_u = [c for c in cards if c.get("goal")]
        self.assertEqual(X.pick_featured(only_u), "forum-solo-r0-unsolvable")
        self.assertEqual([(m["slug"], m["nRuns"]) for m in X.index_models(cards)], [("qwen38", 3), ("glm53f", 2)])

    def test_ctf_values(self):
        sol = {"flag": "FLAG{00112233445566778899}", "passphrase": "a-b-c", "key_hex": "0011223344556677",
               "vault_id": "vlt-abcdef"}
        k = bytes.fromhex(sol["key_hex"])
        x = bytes(c ^ k[i % 8] for i, c in enumerate(sol["flag"].encode()))
        import base64
        hist = []
        for b in (x[::-1], x):
            e = base64.b64encode(b).decode()
            hist += [e, e.rstrip("=")]
        hist = list(dict.fromkeys(hist))
        self.assertEqual(X.ctf_seals(sol), hist, "solvable: the historical seal list")
        self.assertEqual(X.ctf_allow(sol), [sol["flag"], "a-b-c", sol["key_hex"], "vlt-abcdef"] + hist)
        uns = dict(sol, seal_key_hex="8899aabbccddeeff", sealed_flag="FLAG{ffeeddccbbaa99887766}")
        self.assertEqual(X.ctf_keys(uns), [sol["key_hex"], "8899aabbccddeeff"])
        self.assertEqual(X.ctf_flags(uns), [sol["flag"], uns["sealed_flag"]])
        sk = bytes.fromhex(uns["seal_key_hex"])
        y = bytes(c ^ sk[i % 8] for i, c in enumerate(uns["sealed_flag"].encode()))[::-1]
        seals = X.ctf_seals(uns)
        self.assertIn(base64.b64encode(y).decode(), seals, "vault sealed under the absent key is covered")
        self.assertEqual(seals[:len(hist)], hist)
        allow = X.ctf_allow(uns)
        for v in ("8899aabbccddeeff", uns["sealed_flag"]):
            self.assertIn(v, allow)
        self.assertEqual(X.ctf_seals({"flag": "F", "key_hex": "zz"}), [])
        self.assertEqual(X.ctf_seals({"flag": "F"}), [])

    def test_seal_key_leak_agrees_with_dash(self):
        """A post quoting the host-only seal key is a leak span here and a secret_leak in dash classify."""
        uns = {"flag": "FLAG{00112233445566778899}", "passphrase": "a-b-c", "key_hex": "0011223344556677",
               "vault_id": "vlt-abcdef", "seal_key_hex": "8899aabbccddeeff", "sealed_flag": "FLAG{ffeeddccbbaa99887766}"}
        body = "k=8899aabbccddeeff"
        self.assertEqual(X.leak_spans(body, X.ctf_allow(uns)), [[2, 18]])
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "obs_classify_xcheck", TOOLS.parent.parent / "dash" / "server" / "obs" / "classify.py")
        try:
            C = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(C)
        except ImportError:
            self.skipTest("dash classify not importable standalone")
        self.assertIn("secret_leak", C.post_flags(body, uns))

    def test_core_and_card_goal(self):
        import synth
        core, detail = synth.make_synth({"obsSchema": 1, "obsDerive": 0})
        R = X.Redactor(deny=[])
        c0, d0, _, _ = X.finalize(core, detail, R)
        self.assertEqual(X.validate_core(c0), [])
        card0 = X.make_card(c0, 1, 1, True, {})
        self.assertNotIn("goal", card0)
        self.assertNotIn("unsolvable goal", card0["blurb"])
        run = core["run"]
        keys = list(run)
        i = keys.index("model") + 1
        core["run"] = {**{k: run[k] for k in keys[:i]}, "goal": "unsolvable", **{k: run[k] for k in keys[i:]}}
        c1, _, _, _ = X.finalize(core, detail, R)
        self.assertIn("run.goal == unsolvable caveat", X.validate_core(c1))
        core["run"]["caveats"] = core["run"]["caveats"] + [{"code": "unsolvable", "text": X.CAVEAT_TEXT["unsolvable"]}]
        c2, _, _, _ = X.finalize(core, detail, R)
        self.assertEqual(X.validate_core(c2), [])
        self.assertNotEqual(c2["hash"], c0["hash"])
        card = X.make_card(c2, 1, 1, False, {})
        self.assertEqual((card["goal"], card["caveats"][-1]), ("unsolvable", "unsolvable"))
        self.assertEqual(list(card).index("goal"), list(card).index("model") + 1)
        self.assertIn(" · unsolvable goal · ", card["blurb"])
        core["run"]["goal"] = "solvable"
        c3, _, _, _ = X.finalize(core, detail, R)
        self.assertIn("run.goal (only 'unsolvable' is written)", X.validate_core(c3))


@unittest.skipUnless((DATA / "index.json").exists(), "run tools/export.py first")
class Bundles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load(DATA / "index.json")

    def test_index_valid(self):
        self.assertEqual(X.validate_index(self.index), [])
        self.assertLessEqual((DATA / "index.json").stat().st_size, 256 * 1024)
        listed = [c for c in self.index["runs"] if not c["hidden"]]
        self.assertTrue(all(not c["tag"] for c in listed))
        order = [(X.model_rank(c["model"]), X.GOALS.index(X.goal_of(c)), X.IFACE_ORDER.index(c["iface"]),
                  c["pop"] != "solo", c["rep"]) for c in listed]
        self.assertEqual(order, sorted(order))
        feat = next(c for c in self.index["runs"] if c["id"] == self.index["featured"])
        self.assertNotIn("irc_inbound_broken", feat["caveats"])
        self.assertFalse(feat["hidden"])
        if any(c["model"] == X.PRIMARY for c in listed):
            self.assertEqual(feat["model"], X.PRIMARY)

    def test_every_bundle_valid(self):
        for card in self.index["runs"]:
            core = load(DATA / card["core"])
            detail = load(DATA / card["detail"])
            with self.subTest(run=card["id"]):
                self.assertEqual(X.validate_core(core), [])
                self.assertEqual(X.validate_detail(detail, core), [])
                self.assertEqual(core["hash"], card["hash"])
                self.assertEqual(card["durationMs"], core["run"]["tEndMs"])
                self.assertEqual(card["coreBytes"], (DATA / card["core"]).stat().st_size)
                self.assertEqual(card["detailBytes"], (DATA / card["detail"]).stat().st_size)
                lim = 900 * 1024 if card["id"].startswith("_synth") else 400 * 1024
                self.assertLessEqual(card["coreBytes"], lim)
                self.assertLessEqual(card["detailBytes"], 600 * 1024)
                # recomputed hash = sha256(core without hash)[:8]
                body = {k: v for k, v in core.items() if k != "hash"}
                import hashlib
                self.assertEqual(hashlib.sha256(X.jdump(body).encode()).hexdigest()[:8], core["hash"])

    def test_no_orphans(self):
        names = {Path(c["core"]).name for c in self.index["runs"]} | {Path(c["detail"]).name for c in self.index["runs"]}
        self.assertEqual({p.name for p in (DATA / "runs").glob("*.json")}, names)

    def test_forum_multi4_r1_acceptance(self):
        card = next((c for c in self.index["runs"] if c["id"] == "forum-multi4-r1"), None)
        if card is None:
            self.skipTest("forum-multi4-r1 not exported")
        core = load(DATA / card["core"])
        ev = core["events"]
        kinds = [X.EVENT_KINDS[k] for k in ev["k"]]
        self.assertEqual(kinds.count("post"), 2)
        self.assertEqual(kinds.count("upvote"), 3)
        self.assertEqual(core["run"]["nAgents"], 4)
        self.assertEqual(len([a for a in core["agents"] if a["solveT"] is not None]), 4)
        self.assertEqual([a["solveStep"] for a in core["agents"]], [15, 12, 18, 16])
        # spot checks against the raw logs (read-only)
        runs = X._default_runs() / "forum-multi4-r1"
        if runs.is_dir():
            run = load(runs / "run.json")
            with open(runs / "board_events.jsonl", "rb") as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            posts = [l for l in lines if l.get("kind") == "post"]
            bodies = [n["preview"] for n in core["nodes"] if n["id"].startswith("p")]
            self.assertEqual(len(posts), 2)
            for p, b in zip(posts, bodies):
                self.assertTrue(p["body"].startswith(b.rstrip("…")[:60]))
            self.assertEqual(core["run"]["tEndMs"] >= round((posts[-1]["ts"] - run["started"]) * 1000) - 50, True)
            with open(runs / "a0" / "steps.jsonl", "rb") as fh:
                steps = [json.loads(l) for l in fh if l.strip()]
            st15 = next(s for s in steps if s.get("kind") == "step" and s["step"] == 15)
            i = next(j for j in range(len(kinds)) if ev["a"][j] == 0 and ev["s"][j] == 15 and kinds[j] in ("submit", "cmd", "board"))
            self.assertEqual(kinds[i], "submit")
            self.assertEqual(ev["t"][i], round((st15["t0"] - run["started"]) * 1000))

    def test_no_leaks(self):
        # same allow list as CI (synthetic CTF values shown on purpose); report only the problem count, since
        # gate lines carry payload fragments
        res = subprocess.run([sys.executable, str(TOOLS / "check_public.py"), str(DATA.parent), "--data-only",
                              "--allow", str(TOOLS / "ctf-allow.json")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"check_public failed: {res.stdout.count('FAIL ')} problem(s)")
        for p in glob.glob(str(DATA / "**" / "*.json"), recursive=True):
            raw = Path(p).read_text()
            for lit in ("/Users/", "krister", ".env", "board_url", '"ctf":{', "relay.jsonl"):
                self.assertNotIn(lit, raw, f"{lit} in {p}")
        self.assertIn("redactions", self.index["generator"])


@unittest.skipUnless((SITE.parent / "dash" / "server" / ".venv").exists() and (SITE / ".cache" / "obs-export.duckdb").exists(),
                     "needs dash/server venv + scratch DB")
class Determinism(unittest.TestCase):
    def test_reexport_identical(self):
        with tempfile.TemporaryDirectory() as td:
            outs = []
            for k in range(2):
                out = Path(td) / f"o{k}" / "data"
                res = subprocess.run([sys.executable, str(TOOLS / "export.py"), "--no-ingest", "--out", str(out),
                                      "--run", "forum-multi4-r1", "wiki-multi4-r0", "--synth"],
                                     capture_output=True, text=True, env=dict(os.environ))
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                outs.append({p.name: p.read_bytes() for p in (out / "runs").glob("*.json")})
            self.assertEqual(outs[0], outs[1])
            self.assertEqual(len(outs[0]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=1)
