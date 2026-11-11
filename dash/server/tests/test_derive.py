"""Multi-model derive: models rows (family/provider/price), condition labels, cost_eur, and Cerebras condition ids
pinned on the shared fixture. The non-Cerebras runs are tiny numeric-only dirs built in tmp_path."""
import json
import os

import pytest

from obs.config import REPO, Config
from obs.db import Database
from obs.derive import condition_for
from obs.ingest import Ingester

from conftest import OLD

GLM = "zai-org/GLM-5.3-Flash"
GEMMA = "google/gemma-4-31B-it"


def _write_run(d, model, provider, n_prompt=1000, n_completion=100, rep=0):
    """One-agent run: cfg + run.json + a0 (start, one model call, one step, end) + summary. No free text."""
    harness = {"model": model, "budget": 40, "temperature": 0.7, "max_tokens": 2048, "provider": provider}
    (d / "a0").mkdir(parents=True)
    (d / "cfg-a0.json").write_text(json.dumps({"run": d.name, "agent": 0, "harness": harness,
                                               "relay_host_port": 19101}))
    recs = [{"kind": "start", "ts": 1000.0},
            {"kind": "model", "call": 1, "ts": 1001.0, "phase": "task", "content": None,
             "tool_calls": [{"id": "call1x0", "function": {"name": "bash", "arguments": json.dumps({"command": "true"})}}],
             "usage": {"prompt_tokens": n_prompt, "completion_tokens": n_completion, "reasoning_tokens": 7,
                       "prompt_tokens_details": None}},
            {"kind": "step", "step": 1, "t0": 1001.5, "t1": 1002.0, "phase_before": "task", "phase": "task",
             "command": "true", "exit": 0, "output": ""},
            {"kind": "end", "ts": 1003.0, "ended": "budget"}]
    (d / "a0" / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (d / "a0" / "summary.json").write_text(json.dumps({"solved": False, "steps_used": 1, "model_calls": 1,
                                                       "ended": "budget",
                                                       "tokens": {"prompt": n_prompt, "completion": n_completion}}))
    # same key set as lab/orchestrate.run_one (no "tag": the id carries it)
    (d / "run.json").write_text(json.dumps({"run_id": d.name, "iface": "forum", "n_agents": 1, "rep": rep,
                                            "harness": harness, "started": 999.0, "ctf": {},
                                            "finished": 1004.0}))


@pytest.fixture
def multi(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs / "forum-solo-r0--glm53f", GLM, "berget")
    _write_run(runs / "forum-solo-r1--glm53f-smoke", GLM, "berget", rep=1)
    _write_run(runs / "_aborted" / "forum-solo-r2--gemma31-402", GEMMA, "berget", rep=2)
    (runs / "pilot-berget-glm53f.log").write_text(
        "[run] forum-solo-r0--glm53f: starting board + 1 agent(s)\n"
        "[run] forum-solo-r0--glm53f: finished in 5s\n"
        "[error] forum-solo-r3--glm53f: boom\n"
        "[abort] forum-solo-r2--gemma31: provider HTTP 402\n"
        "[budget] stop at r4: committed 37.5 / cap 38.0\n")
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
    yield db, ing
    db.close()


def test_models_rows_and_cost(multi):
    db, _ = multi
    rows = {r[0]: r[1:] for r in db.con.execute(
        "SELECT model, provider, family, price_in_per_mtok, price_out_per_mtok FROM models").fetchall()}
    assert rows[GLM] == ("berget", "glm", 0.25, 0.50)
    assert rows[GEMMA] == ("berget", "gemma", 0.25, 0.50)
    # replace keeps prices current when an older row (e.g. without prices) exists
    db.con.execute("UPDATE models SET price_in_per_mtok = NULL, family = 'x' WHERE model = ?", [GLM])
    db.con.execute("UPDATE runs SET dirty = TRUE")
    multi[1].derive_dirty()
    assert db.con.execute("SELECT family, price_in_per_mtok FROM models WHERE model = ?", [GLM]).fetchone() == ("glm", 0.25)
    cost = db.con.execute("SELECT cost_eur, tok_reasoning FROM mat_agent_metrics WHERE run_id = 'forum-solo-r0--glm53f'").fetchone()
    assert cost[0] == pytest.approx((1000 * 0.25 + 100 * 0.50) / 1e6) and cost[1] == 7


def test_run_ids_tags_and_labels(multi):
    db, _ = multi
    runs = {r[0]: r[1:] for r in db.con.execute("SELECT run_id, tag, model, rep, condition_id FROM runs").fetchall()}
    assert set(runs) == {"forum-solo-r0--glm53f", "forum-solo-r1--glm53f-smoke", "forum-solo-r2--gemma31-402-aborted"}
    assert runs["forum-solo-r0--glm53f"][:2] == ("", GLM)
    assert runs["forum-solo-r1--glm53f-smoke"][0] == "smoke"
    assert runs["forum-solo-r2--gemma31-402-aborted"][0] == "402-aborted"
    # same model, iface, pop and harness: tags do not split the condition
    assert runs["forum-solo-r0--glm53f"][3] == runs["forum-solo-r1--glm53f-smoke"][3]
    label = db.con.execute("SELECT label FROM conditions WHERE condition_id = ?", [runs["forum-solo-r0--glm53f"][3]]).fetchone()[0]
    assert label == f"{GLM} | forum | solo | b40" and "provider" not in label
    params = json.loads(db.con.execute("SELECT params FROM conditions WHERE condition_id = ?",
                                       [runs["forum-solo-r0--glm53f"][3]]).fetchone()[0])
    assert not {"host_port_base", "relay_host_port", "spend"} & set(params)


def test_pilot_markers_new_kinds(multi):
    db, _ = multi
    kinds = dict(db.con.execute("SELECT kind, log_run_id FROM run_markers WHERE source = 'pilot-berget-glm53f.log' "
                                "AND kind IN ('error', 'abort', 'budget_stop')").fetchall())
    assert kinds == {"error": "forum-solo-r3--glm53f", "abort": "forum-solo-r2--gemma31", "budget_stop": "_pilot"}


def test_run_specific_params_not_factors():
    base = {"model": GLM, "iface": "forum", "n_agents": 1, "seed_hash": "s", "harness": {"model": GLM, "budget": 40},
            "params": {"seed_hash": "s"}}
    a = condition_for(base)
    b = condition_for({**base, "params": {"seed_hash": "s", "host_port_base": 19100, "relay_host_port": 19101,
                                          "spend": {"eur": 0.1}}})
    assert a["condition_id"] == b["condition_id"]
    c = condition_for({**base, "harness": {"model": GLM, "budget": 40, "provider": "berget"}})
    assert c["label"] == a["label"] and c["condition_id"] != a["condition_id"]


def test_cerebras_condition_ids_unchanged(db, ingested):
    """Pinned before the multi-model change; Cerebras ids must stay byte-identical."""
    got = dict(db.con.execute("SELECT run_id, condition_id FROM runs").fetchall())
    assert got == {"cache-multi4-r1": "c_b67162735ab3", "forum-multi4-r0": "c_38925b7dd900",
                   "forum-solo-r0-smoke": "c_4f68fc57e6f4", "irc-multi4-r1": "c_192000d8b6b9"}
    assert db.con.execute("SELECT model, provider, family, price_in_per_mtok FROM models").fetchall() == [
        ("qwen-3.8-27b", "cerebras", "qwen", None)]
    assert db.con.execute("SELECT count(*) FROM mat_agent_metrics WHERE cost_eur IS NOT NULL").fetchone()[0] == 0
