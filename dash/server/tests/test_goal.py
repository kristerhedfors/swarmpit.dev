"""Goal factor (solvable | unsolvable): run-id marker, params, condition ids/labels, free-phase metrics n/a, and the
API facet / filters / cell defaults. Runs are tiny numeric-only dirs built in tmp_path (no free text)."""
import json
import os

import pytest
from starlette.testclient import TestClient

from obs.api import create_app
from obs.config import REPO, Config
from obs.db import Database
from obs.derive import condition_for
from obs.ingest import Ingester
from obs.sse import Broadcaster

from conftest import OLD

QWEN = "qwen-3.8-27b"
FREE = ("board_steps_free", "free_steps", "idle_free_steps", "share_free", "posts_free")


def _write_run(d, rep=0, solve=False, goal=None, run_json=True):
    """One-agent forum run: two steps (the first one solves when `solve`), an end record and a summary."""
    harness = {"model": QWEN, "budget": 40, "temperature": 0.7, "max_tokens": 2048}
    (d / "a0").mkdir(parents=True)
    (d / "cfg-a0.json").write_text(json.dumps({"run_id": d.name, "agent_id": 0, "harness": harness,
                                               "relay_host_port": 19000}))
    recs = [{"kind": "start", "ts": 1000.0},
            {"kind": "step", "step": 1, "t0": 1001.0, "t1": 1002.0, "phase_before": "task",
             "phase": "free" if solve else "task", "command": "true", "exit": 0, "output": ""},
            {"kind": "step", "step": 2, "t0": 1003.0, "t1": 1004.0, "phase_before": "free" if solve else "task",
             "phase": "free" if solve else "task", "command": "true", "exit": 0, "output": ""},
            {"kind": "end", "ts": 1005.0, "ended": "budget"}]
    (d / "a0" / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (d / "a0" / "summary.json").write_text(json.dumps({"solved": solve, "solve_step": 1 if solve else None,
                                                       "steps_used": 2, "model_calls": 2, "ended": "budget"}))
    if run_json:
        meta = {"run_id": d.name, "iface": "forum", "n_agents": 1, "rep": rep, "harness": harness,
                "started": 999.0, "ctf": {}, "finished": 1006.0}
        if goal:
            meta["params"] = {"goal": goal}
        (d / "run.json").write_text(json.dumps(meta))


@pytest.fixture
def goal_db(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs / "forum-solo-r0", solve=True)
    _write_run(runs / "forum-solo-r1", rep=1, solve=True)
    _write_run(runs / "forum-solo-r0-unsolvable", goal="unsolvable")
    _write_run(runs / "forum-solo-r1-unsolvable", rep=1, run_json=False)  # still running: goal known from the id only
    _write_run(runs / "forum-solo-r2", rep=2, goal="unsolvable")  # inconsistent: run.json wins, note recorded
    for p in runs.rglob("*"):
        os.utime(p, (OLD, OLD))
    cfg = Config(runs_dir=runs, db_path=tmp_path / "data" / "obs.duckdb", data_dir=tmp_path / "data",
                 seed_path=REPO / "lab" / "seeds" / "seed.json", live_tap=False, msb=None,
                 web_dist=tmp_path / "no-dist")
    db = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    db.setup()
    ing = Ingester(db, cfg)
    ing.quiet = True
    ing.scan()
    ing.derive_dirty()
    db.con.execute("UPDATE runs SET status = 'done'")  # the run_json=False run would otherwise stay pending
    db.con.execute("UPDATE mat_agent_metrics SET run_status = 'done'")
    db.con.execute("UPDATE mat_run_metrics SET status = 'done'")
    yield cfg, db, ing
    db.close()


def _runs(db):
    return {r[0]: {"params": json.loads(r[1] or "{}"), "condition_id": r[2], "notes": json.loads(r[3] or "[]")}
            for r in db.con.execute("SELECT run_id, params, condition_id, notes FROM runs").fetchall()}


def test_goal_params_and_conditions(goal_db):
    _, db, _ = goal_db
    runs = _runs(db)
    assert "goal" not in runs["forum-solo-r0"]["params"] and "goal" not in runs["forum-solo-r1"]["params"]
    assert runs["forum-solo-r0-unsolvable"]["params"]["goal"] == "unsolvable"
    assert runs["forum-solo-r1-unsolvable"]["params"]["goal"] == "unsolvable"
    assert runs["forum-solo-r2"]["params"]["goal"] == "unsolvable"
    assert any(str(n).startswith("goal mismatch") for n in runs["forum-solo-r2"]["notes"])
    assert not any(str(n).startswith("goal mismatch") for n in runs["forum-solo-r0-unsolvable"]["notes"])
    # same condition within a goal, distinct conditions across goals
    assert runs["forum-solo-r0"]["condition_id"] == runs["forum-solo-r1"]["condition_id"]
    assert runs["forum-solo-r0-unsolvable"]["condition_id"] == runs["forum-solo-r1-unsolvable"]["condition_id"]
    assert runs["forum-solo-r0-unsolvable"]["condition_id"] != runs["forum-solo-r0"]["condition_id"]
    labels = dict(db.con.execute("SELECT condition_id, label FROM conditions").fetchall())
    assert labels[runs["forum-solo-r0"]["condition_id"]] == f"{QWEN} | forum | solo | b40"
    assert labels[runs["forum-solo-r0-unsolvable"]["condition_id"]] == f"{QWEN} | forum | solo | b40 | goal=unsolvable"


def test_explicit_solvable_goal_keeps_condition_id():
    base = {"model": QWEN, "iface": "forum", "n_agents": 4, "seed_hash": "s", "harness": {"model": QWEN, "budget": 40},
            "params": {"seed_hash": "s"}}
    a = condition_for(base)
    b = condition_for({**base, "params": {"seed_hash": "s", "goal": "solvable"}})
    u = condition_for({**base, "params": {"seed_hash": "s", "goal": "unsolvable"}})
    assert a["condition_id"] == b["condition_id"] and a["label"] == b["label"]
    assert u["condition_id"] != a["condition_id"] and u["factors"]["params"]["goal"] == "unsolvable"


def test_free_phase_metrics_na_when_unsolvable(goal_db):
    _, db, _ = goal_db
    cols = ", ".join(FREE)
    rows = {r[0]: r[1:] for r in db.con.execute(f"SELECT run_id, solved, board_steps_task, {cols} "
                                                "FROM mat_agent_metrics").fetchall()}
    for rid in ("forum-solo-r0-unsolvable", "forum-solo-r1-unsolvable", "forum-solo-r2"):
        assert rows[rid][0] is False and rows[rid][2:] == (None,) * len(FREE), rid
    solved = rows["forum-solo-r0"]
    assert solved[0] is True and solved[2:5] == (0, 1, 0) and solved[6] == 0  # free metrics stay numbers
    # the view and its cache agree; the default roll-up keeps solvable runs only
    v = {r[0]: r[1:] for r in db.con.execute(f"SELECT run_id, {cols} FROM v_agent_metrics").fetchall()}
    assert v == {k: r[2:] for k, r in rows.items()}
    assert db.con.execute("SELECT sum(n_runs) FROM v_cell_metrics").fetchone()[0] == 2


def test_api_goal_facet_filters_and_cells(goal_db):
    cfg, db, _ = goal_db
    c = TestClient(create_app(cfg, db, Broadcaster()))
    runs = {r["run_id"]: r for r in c.get("/api/runs").json()["runs"]}
    assert runs["forum-solo-r0"]["goal"] == "solvable" and runs["forum-solo-r0-unsolvable"]["goal"] == "unsolvable"
    assert c.get("/api/runs/forum-solo-r1-unsolvable").json()["run"]["goal"] == "unsolvable"
    dims = {d["key"]: d for d in c.get("/api/facets").json()["dims"]}
    goal = dims["param.goal"]
    assert goal["kind"] == "cat"
    assert {v["value"]: v["n_runs"] for v in goal["values"]} == {"solvable": 2, "unsolvable": 3}
    assert c.get("/api/runs?param.goal=unsolvable").json()["total"] == 3
    assert c.get("/api/runs?param.goal=solvable").json()["total"] == 2
    assert c.get("/api/runs?goal=solvable,unsolvable").json()["total"] == 5
    assert {r["run_id"] for r in c.get("/api/runs?goal=solvable").json()["runs"]} == {"forum-solo-r0", "forum-solo-r1"}
    # chip counts equal what a click returns
    for v in goal["values"]:
        assert c.get(f"/api/runs?param.goal={v['value']}").json()["total"] == v["n_runs"]
    # cells: no goal filter / group -> solvable only (historical numbers); grouping or filtering opts in
    m = "solved,share_free,posts_free,board_steps_task"
    d = c.get(f"/api/metrics/cells?metrics={m}&ci=none").json()
    assert [x["n_runs"] for x in d["cells"]] == [2] and d["cells"][0]["values"]["solved"]["value"] == 1.0
    d = c.get(f"/api/metrics/cells?metrics={m}&ci=none&group=model,param.goal").json()
    by = {x["key"]["param.goal"]: x for x in d["cells"]}
    assert set(by) == {"solvable", "unsolvable"} and by["unsolvable"]["n_runs"] == 3
    assert by["unsolvable"]["values"]["solved"]["value"] == 0.0
    assert by["unsolvable"]["values"]["share_free"]["value"] is None
    assert by["unsolvable"]["values"]["share_free"]["na"].startswith("no free phase")
    assert "na" not in by["unsolvable"]["values"]["board_steps_task"]
    assert "na" not in by["solvable"]["values"]["posts_free"]
    d = c.get(f"/api/metrics/cells?metrics={m}&ci=none&group=goal&goal=unsolvable").json()
    assert [x["key"]["goal"] for x in d["cells"]] == ["unsolvable"]
    # mixed cell (explicit filter for both goals, not grouped): free metrics come from solvable agents only
    d = c.get(f"/api/metrics/cells?metrics=free_steps&ci=none&goal=solvable,unsolvable").json()
    assert d["cells"][0]["n_runs"] == 5 and d["cells"][0]["values"]["free_steps"]["n"] == 2
    assert "na" not in d["cells"][0]["values"]["free_steps"]
    s = c.get("/api/metrics/series?metric=cum_board_steps&group=param.goal").json()
    assert s is not None


def test_run_json_without_goal_overrides_unsolvable_id(tmp_path):
    """Id says unsolvable, run.json has no params.goal: run.json wins (solvable) and a goal mismatch note is kept,
    matching lab analysis, which skips such a run from the unsolvable condition."""
    runs = tmp_path / "runs"
    _write_run(runs / "forum-solo-r3-unsolvable", rep=3)
    cfg = Config(runs_dir=runs, db_path=tmp_path / "data" / "obs.duckdb", data_dir=tmp_path / "data",
                 seed_path=REPO / "lab" / "seeds" / "seed.json", live_tap=False, msb=None,
                 web_dist=tmp_path / "no-dist")
    db = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    db.setup()
    ing = Ingester(db, cfg)
    ing.quiet = True
    ing.scan()
    r = _runs(db)["forum-solo-r3-unsolvable"]
    db.close()
    assert "goal" not in r["params"]
    assert any(str(n).startswith("goal mismatch") for n in r["notes"])


def test_seal_key_counts_as_secret_leak():
    """Agrees with the site export, which allow-lists and highlights ctf.seal_key_hex as a leak span."""
    from obs.classify import post_flags
    ctf = {"flag": "FLAG{00000000000000000001}", "key_hex": "0011223344556677",
           "seal_key_hex": "8899aabbccddeeff", "sealed_flag": "FLAG{00000000000000000002}"}
    assert "secret_leak" in post_flags("k 8899aabbccddeeff", ctf)
    assert "secret_leak" in post_flags("FLAG{00000000000000000002}", ctf)
    assert "secret_leak" not in post_flags("8899aabbcc", ctf)
    assert "secret_leak" not in post_flags("8899aabbccddeeff", {"key_hex": "0011223344556677"})
