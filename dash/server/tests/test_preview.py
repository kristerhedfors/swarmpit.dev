"""Browser-lab runs (lab/browser/PROTOCOL.md §5.3) are tagged 'preview' by ingest and derive, so the default roll-ups
(v_cell_metrics, API cells) never pool their unverified solves with pilot runs. That covers runs with
params.preview and also the runs of the first deployed version (params env/topology, no `preview`), runs whose
params were stripped (the 'browser-' run id), and runs whose derive fails. Runs are tiny numeric-only dirs built in
tmp_path with a placeholder seed (no free text)."""
import json
import os

import pytest
from starlette.testclient import TestClient

from obs import derive as D
from obs.api import create_app
from obs.config import Config
from obs.db import Database
from obs.ingest import Ingester
from obs.sse import Broadcaster

from conftest import OLD
from test_goal import _write_run

PREVIEW = "browser-forum-solo-20260101000000-abcd"
PILOT = "forum-solo-r0"
# params of a6a5c3c / c538ac4 labctl: no `preview` key
LEGACY_PARAMS = {"env": "cheerpx", "topology": "board-co-resident", "provider": "placeholder:model",
                 "cx_version": "1.3.9", "image": "placeholder"}
CURRENT_PARAMS = {"env": "cheerpx", "isolation": "weak-uid", "preview": True, "solve_verified": False}


def _setup(tmp_path, browser_runs):
    """A solved pilot run plus unsolved browser-lab runs {run_id: params or None (no params key)}."""
    runs = tmp_path / "runs"
    _write_run(runs / PILOT, solve=True)
    for rid, params in browser_runs.items():
        _write_run(runs / rid, solve=False)
        meta = json.loads((runs / rid / "run.json").read_text())
        if params is not None:
            meta["params"] = params
        (runs / rid / "run.json").write_text(json.dumps(meta))
    for p in runs.rglob("*"):
        os.utime(p, (OLD, OLD))
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"board_name": "placeholder", "threads": []}))
    cfg = Config(runs_dir=runs, db_path=tmp_path / "data" / "obs.duckdb", data_dir=tmp_path / "data",
                 seed_path=seed, live_tap=False, msb=None, web_dist=tmp_path / "no-dist")
    db = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    db.setup()
    ing = Ingester(db, cfg)
    ing.quiet = True
    return cfg, db, ing


def _tags(db):
    return dict(db.con.execute("SELECT run_id, tag FROM runs").fetchall())


def _mark_done(db):
    for t, col in (("runs", "status"), ("mat_agent_metrics", "run_status"), ("mat_run_metrics", "status")):
        db.con.execute(f"UPDATE {t} SET {col} = 'done'")


def test_preview_runs_are_tagged_and_left_out_of_default_rates(tmp_path):
    cfg, db, ing = _setup(tmp_path, {PREVIEW: CURRENT_PARAMS})
    try:
        ing.scan()
        ing.derive_dirty()
        _mark_done(db)
        assert _tags(db) == {PILOT: "", PREVIEW: "preview"}
        assert db.con.execute("SELECT tag FROM mat_agent_metrics WHERE run_id = ?", [PREVIEW]).fetchone()[0] == "preview"
        assert db.con.execute("SELECT sum(n_runs), avg(solve_rate) FROM v_cell_metrics").fetchone() == (1, 1.0)

        c = TestClient(create_app(cfg, db, Broadcaster()))
        d = c.get("/api/metrics/cells?metrics=solved&ci=none").json()
        assert [(x["n_runs"], x["values"]["solved"]["value"]) for x in d["cells"]] == [(1, 1.0)]
        # opting in still works, and the tag keeps the preview runs apart when grouped
        d = c.get("/api/metrics/cells?metrics=solved&ci=none&include_tagged=1&group=tag").json()
        assert {x["key"]["tag"]: x["n_runs"] for x in d["cells"]} == {"": 1, "preview": 1}
        assert c.get("/api/runs?tag=preview").json()["total"] == 1
    finally:
        db.close()


def test_pre_preview_and_stripped_browser_runs_are_tagged(tmp_path):
    """Runs of the first deployed version carry no params.preview; any browser-lab marker tags them."""
    runs = {"browser-forum-solo-20250101000000-0001": LEGACY_PARAMS,          # as a6a5c3c wrote them
            "renamed-legacy-run": LEGACY_PARAMS,                               # id renamed: params still mark it
            "renamed-env-only": {"env": "cheerpx"},
            "renamed-topology-only": {"topology": "board-co-resident"},
            "browser-forum-solo-20250101000000-0002": None,                    # params stripped: the id marks it
            "browser-forum-solo-20250101000000-0003": {"preview": False}}      # an edited flag does not untag it
    cfg, db, ing = _setup(tmp_path, runs)
    try:
        ing.scan()
        assert _tags(db) == {PILOT: "", **{rid: "preview" for rid in runs}}  # tagged at ingest, before any derive
        ing.derive_dirty()
        _mark_done(db)
        assert _tags(db) == {PILOT: "", **{rid: "preview" for rid in runs}}
        assert db.con.execute("SELECT sum(n_runs), avg(solve_rate) FROM v_cell_metrics").fetchone() == (1, 1.0)
    finally:
        db.close()


def test_run_tag_rule():
    assert D.run_tag(PILOT, {}, "") == ""
    assert D.run_tag(PILOT, {"env": "microsandbox"}, "") == ""
    assert D.run_tag(PILOT, None, "archive1") == "archive1"
    assert D.run_tag("browser-x", {}, "archive1") == "archive1"  # an explicit tag already keeps it out
    for params in (CURRENT_PARAMS, LEGACY_PARAMS, {"env": "cheerpx"}, {"topology": "board-co-resident"}):
        assert D.run_tag("x", params, "") == "preview"
    assert D.run_tag("browser-forum-solo-1", None, None) == "preview"


@pytest.mark.parametrize("params", [CURRENT_PARAMS, LEGACY_PARAMS])
def test_failed_derive_leaves_browser_run_tagged(tmp_path, monkeypatch, params):
    cfg, db, ing = _setup(tmp_path, {PREVIEW: params})
    try:
        ing.scan()
        ing.derive_dirty()
        assert _tags(db)[PREVIEW] == "preview"
        # re-ingest of run.json (what a changed run.json triggers) keeps the tag: it never passes through ''
        db.con.execute("UPDATE runs SET dirty = TRUE WHERE run_id = ?", [PREVIEW])
        ing.apply_run_json(PREVIEW, (cfg.runs_dir / PREVIEW / "run.json").read_bytes())
        assert _tags(db)[PREVIEW] == "preview"

        def boom(*a, **k):
            raise RuntimeError("forced")
        monkeypatch.setattr(D, "derive_run", boom)
        ing.derive_dirty()
        row = db.con.execute("SELECT tag, dirty FROM runs WHERE run_id = ?", [PREVIEW]).fetchone()
        assert row == ("preview", False)

        # a DB written before the tag rule (tag '') whose re-derive fails is tagged on the failure path too
        db.con.execute("UPDATE runs SET tag = '', dirty = TRUE WHERE run_id = ?", [PREVIEW])
        ing.derive_dirty()
        row = db.con.execute("SELECT tag, dirty FROM runs WHERE run_id = ?", [PREVIEW]).fetchone()
        assert row == ("preview", False)
        assert _tags(db)[PILOT] == ""
    finally:
        db.close()
