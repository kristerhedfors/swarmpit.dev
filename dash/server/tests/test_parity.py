"""analyze.py parity: every metric analyze.py computes per agent must match v_agent_metrics (and the
materialized cache) exactly. The reference is a vendored copy of lab/analyze.py at main fc5a3be
(tests/ref_analyze_fc5a3be.py; board steps = measured traffic only). Only analyze_agent() is called, against
a temporary copy of the fixtures (its main() would write runs/analysis.json, so it is never run)."""
from pathlib import Path
import importlib.util
import json

import pytest


SHARED = ["steps", "solved", "solve_step", "discovered", "first_contact", "board_steps", "board_steps_task",
          "board_steps_free", "free_steps", "idle_free_steps", "share_after_contact", "share_free", "episodes",
          "posts", "posts_free", "crashed"]


@pytest.fixture(scope="module")
def analyze():
    spec = importlib.util.spec_from_file_location("lab_analyze", Path(__file__).parent / "ref_analyze_fc5a3be.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(db, sql):
    res = db.con.execute(sql)
    cols = [c[0] for c in res.description]
    return {(r[0], r[1]): dict(zip(cols, r)) for r in res.fetchall()}


def test_parity_with_analyze(cfg, db, ingested, analyze):
    view = _rows(db, "SELECT * FROM v_agent_metrics")
    mat = _rows(db, "SELECT * FROM mat_agent_metrics")
    assert view == mat
    n = 0
    for rd in sorted(cfg.runs_dir.iterdir()):
        if not (rd / "run.json").exists():
            continue
        meta = json.loads((rd / "run.json").read_text())
        events = analyze.jl(rd / "board_events.jsonl")
        for i in range(meta["n_agents"]):
            ad = rd / f"a{i}"
            if not (ad / "steps.jsonl").exists():
                continue
            a = analyze.analyze_agent(ad, events, i)
            d = view[(meta["run_id"], i)]
            n += 1
            for k in SHARED:
                x, y = a[k], d[k]
                if isinstance(x, float) or isinstance(y, float):
                    assert (x is None) == (y is None), (meta["run_id"], i, k, x, y)
                    if x is not None:
                        assert abs(x - y) < 1e-9, (meta["run_id"], i, k, x, y)
                else:
                    assert x == y, (meta["run_id"], i, k, x, y)
            assert round(d["clock_offset_s"], 3) == a["clock_offset_s"]
            assert d["tok_prompt"] == a["tokens"].get("prompt")
    assert n >= 10


def test_run_metrics_cache_equals_view(db, ingested):
    v = _rows(db, "SELECT * FROM v_run_metrics")
    m = _rows(db, "SELECT * FROM mat_run_metrics")
    assert {k[0] for k in v} == {k[0] for k in m}
    v = {r["run_id"]: r for r in v.values()}
    m = {r["run_id"]: r for r in m.values()}
    for rid in v:
        assert v[rid] == m[rid], rid


def _analyze_runs(analyze, runs_dir):
    """Run-level dicts + cells exactly as lab/analyze.py main() builds them (main() itself writes analysis.json
    into the runs dir, so its body is mirrored here instead of called)."""
    import re
    from collections import defaultdict
    runs, cells = [], defaultdict(list)
    for rd in sorted(runs_dir.iterdir()):
        if not (rd / "run.json").exists():
            continue
        meta = json.loads((rd / "run.json").read_text())
        if not re.fullmatch(r"\w+-(solo|multi\d+)-r\d+", meta["run_id"]):
            continue
        events = analyze.jl(rd / "board_events.jsonl")
        agents = []
        for i in range(meta["n_agents"]):
            ad = rd / f"a{i}"
            if (ad / "steps.jsonl").exists():
                agents.append(analyze.analyze_agent(ad, events, i))
        live_names = {p["author"] for a in agents for p in a["post_texts"]}
        cross = sum(1 for a in agents for p in a["post_texts"]
                    if any(n and n != p["author"] and n in p["body"] for n in live_names))
        runs.append({"run_id": meta["run_id"], "total_posts": sum(a["posts"] for a in agents), "cross_mentions": cross,
                     "requests": sum(1 for e in events if e["kind"] in ("req", "irc_cmd"))})
        cells[(meta["iface"], "solo" if meta["n_agents"] == 1 else "multi")].extend(agents)
    table = {k: {"discovery_rate": analyze.mean([a["discovered"] for a in ag]),
                 "board_steps": analyze.mean([a["board_steps"] for a in ag]),
                 "posts": analyze.mean([a["posts"] for a in ag]),
                 "share_after_contact": analyze.mean([a["share_after_contact"] for a in ag]),
                 "n_agents": len(ag)} for k, ag in cells.items()}
    return runs, table


def test_run_level_parity_with_analyze(cfg, db, ingested, analyze):
    """total_posts / requests / cross_mentions per run equal analyze.py's run dict (cross_mentions uses its
    plain-substring rule; the graph-based count is mention_edges_live)."""
    runs, _ = _analyze_runs(analyze, cfg.runs_dir)
    assert runs
    mat = {r[0]: r[1:] for r in db.con.execute(
        "SELECT run_id, total_posts, requests, cross_mentions FROM mat_run_metrics").fetchall()}
    for r in runs:
        assert mat[r["run_id"]] == (r["total_posts"], r["requests"], r["cross_mentions"]), r


def test_default_ui_cells_equal_analyze_cells(cfg, db, ingested, analyze):
    """The overview's default cell query (tagged runs excluded, irc_inbound_broken runs excluded) must reproduce
    analysis.json's cells: a valid run must never be flagged broken."""
    from starlette.testclient import TestClient
    from obs.api import create_app
    from obs.sse import Broadcaster
    _, table = _analyze_runs(analyze, cfg.runs_dir)
    c = TestClient(create_app(cfg, db, Broadcaster()))
    d = c.get("/api/metrics/cells?group=iface,pop&metrics=discovered,board_steps,posts,share_after_contact"
              "&exclude_broken=1&B=50").json()
    got = {(x["key"]["iface"], x["key"]["pop"]): x for x in d["cells"]}
    assert set(got) == set(table)
    for k, ref in table.items():
        v = got[k]["values"]
        assert got[k]["n_agents"] == ref["n_agents"], k
        for m, rk in (("discovered", "discovery_rate"), ("board_steps", "board_steps"), ("posts", "posts"),
                      ("share_after_contact", "share_after_contact")):
            x, y = v[m]["mean"], ref[rk]
            assert (x is None) == (y is None) and (x is None or abs(x - y) < 1e-3), (k, m, x, y)
