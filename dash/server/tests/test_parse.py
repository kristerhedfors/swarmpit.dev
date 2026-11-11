import importlib.util

import pytest

from obs.config import MODEL_PRICES, REPO
from obs.parse import norm_steps_record, parse_pilot_line, parse_run_id, run_id_for, split_lines, vm_agent_id


def test_run_id_grammar():
    assert parse_run_id("forum-multi4-r0") == {"iface": "forum", "pop": "multi", "n_agents": 4, "rep": 0, "tag": "",
                                               "model_slug": "", "goal": "solvable", "matched": True}
    p = parse_run_id("forum-solo-r0-cal1")
    assert (p["pop"], p["n_agents"], p["tag"]) == ("solo", 1, "cal1")
    assert parse_run_id("irc-multi4-r0-invalid_irc_lf")["tag"] == "invalid_irc_lf"
    assert parse_run_id("_weird")["matched"] is False
    assert parse_run_id("_weird")["model_slug"] == ""


def test_run_id_model_slug():
    p = parse_run_id("forum-multi4-r3--glm53f")
    assert (p["iface"], p["n_agents"], p["rep"], p["model_slug"], p["tag"]) == ("forum", 4, 3, "glm53f", "")
    p = parse_run_id("forum-solo-r0--glm53f-smoke")
    assert (p["n_agents"], p["model_slug"], p["tag"]) == (1, "glm53f", "smoke")
    # archived abort: '_aborted/<id>-402' is ingested as '<id>-402-aborted'
    p = parse_run_id("forum-multi4-r9--gemma31-402-aborted")
    assert (p["rep"], p["model_slug"], p["tag"]) == (9, "gemma31", "402-aborted")
    # legacy ids keep an empty slug, including archived ones
    p = parse_run_id("forum-multi4-r9-402-aborted")
    assert (p["model_slug"], p["tag"]) == ("", "402-aborted")
    assert parse_run_id("irc-multi4-r1")["model_slug"] == ""
    assert run_id_for("forum", 4, 3, slug="glm53f") == "forum-multi4-r3--glm53f"
    assert run_id_for("forum", 1, 0, "smoke", "glm53f") == "forum-solo-r0--glm53f-smoke"
    assert run_id_for("wiki", 1, 2, "cal1") == "wiki-solo-r2-cal1"
    for rid in ("forum-multi4-r3--glm53f", "forum-solo-r0--glm53f-smoke", "cache-multi4-r1"):
        p = parse_run_id(rid)
        assert run_id_for(p["iface"], p["n_agents"], p["rep"], p["tag"], p["model_slug"]) == rid


def test_run_id_goal_segment():
    """'-unsolvable' right after the rep = goal unsolvable; it sits before the model slug and the tag."""
    p = parse_run_id("forum-multi4-r3-unsolvable")
    assert (p["iface"], p["n_agents"], p["rep"], p["goal"], p["model_slug"], p["tag"]) == \
        ("forum", 4, 3, "unsolvable", "", "")
    p = parse_run_id("wiki-solo-r0-unsolvable--glm53f-smoke")
    assert (p["rep"], p["goal"], p["model_slug"], p["tag"]) == (0, "unsolvable", "glm53f", "smoke")
    p = parse_run_id("irc-multi4-r12-unsolvable-402-aborted")
    assert (p["rep"], p["goal"], p["tag"]) == (12, "unsolvable", "402-aborted")
    # ids without the segment (every run before the goal factor) stay solvable; look-alike tags stay tags
    for rid in ("forum-multi4-r3", "forum-multi4-r3--glm53f", "forum-solo-r0-unknown", "cache-multi4-r1-unsolvablex"):
        assert parse_run_id(rid)["goal"] == "solvable", rid
    assert parse_run_id("cache-multi4-r1-unsolvablex")["tag"] == "unsolvablex"
    assert parse_run_id("forum-multi4-r3u")["matched"] is False
    assert parse_run_id("_weird")["goal"] is None
    assert run_id_for("forum", 4, 3, goal="unsolvable") == "forum-multi4-r3-unsolvable"
    assert run_id_for("forum", 1, 0, "smoke", "glm53f", "unsolvable") == "forum-solo-r0-unsolvable--glm53f-smoke"
    assert run_id_for("forum", 4, 3) == run_id_for("forum", 4, 3, goal="solvable") == "forum-multi4-r3"
    with pytest.raises(ValueError):
        run_id_for("forum", 4, 3, goal="maybe")
    for rid in ("forum-multi4-r3-unsolvable", "wiki-solo-r0-unsolvable--glm53f-smoke", "irc-multi4-r1-unsolvable-cal1",
                "forum-multi4-r3"):
        p = parse_run_id(rid)
        assert run_id_for(p["iface"], p["n_agents"], p["rep"], p["tag"], p["model_slug"], p["goal"]) == rid
    # pilot lines name the run id; agent VMs carry the id without the goal segment
    assert parse_pilot_line("[run] forum-multi4-r3-unsolvable--glm53f: finished in 31s")["run_id"] == \
        "forum-multi4-r3-unsolvable--glm53f"
    m = parse_pilot_line("[run] forum-multi4-r3-unsolvable: sb-forum-multi4-r3-a2 done")
    assert (m["run_id"], m["agent_id"]) == ("forum-multi4-r3-unsolvable", 2)


def test_vm_run_id_and_candidates():
    from obs.parse import vm_run_candidates, vm_run_id
    assert vm_run_id("forum-multi4-r3-unsolvable--glm53f-smoke") == "forum-multi4-r3--glm53f-smoke"
    assert vm_run_id("forum-multi4-r3-unsolvable") == "forum-multi4-r3"
    assert vm_run_id("forum-multi4-r3-smoke") == "forum-multi4-r3-smoke"
    assert vm_run_id("_weird") == "_weird"
    assert vm_run_candidates("forum-multi4-r3--glm53f-smoke") == ["forum-multi4-r3--glm53f-smoke",
                                                                 "forum-multi4-r3-unsolvable--glm53f-smoke"]
    assert vm_run_candidates("_weird") == ["_weird"]
    for rid in ("forum-multi4-r3-unsolvable--glm53f-smoke", "wiki-solo-r0", "irc-multi4-r1-unsolvable"):
        assert rid in vm_run_candidates(vm_run_id(rid))


def test_goal_grammar_matches_lab_config():
    """obs.parse mirrors lab/config's goal grammar (GOALS, GOAL_SEGMENT, id_goal, vm_run_id)."""
    spec = importlib.util.spec_from_file_location("lab_config_goal", REPO / "lab" / "config.py")
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    from obs import parse as P
    assert tuple(lab.GOALS) == P.GOALS and lab.DEFAULT_GOAL == P.DEFAULT_GOAL
    assert dict(lab.GOAL_SEGMENT) == P.GOAL_SEGMENT
    for rid in ("forum-multi4-r3-unsolvable--glm53f-smoke", "forum-multi4-r3", "wiki-solo-r0-unsolvable",
                "irc-multi4-r9-unsolvable-402-aborted", "cache-multi4-r1-unsolvablex", "forum-solo-r0--gemma31"):
        assert lab.id_goal(rid) == P.parse_run_id(rid)["goal"], rid
        assert lab.vm_run_id(rid) == P.vm_run_id(rid), rid


def test_run_goal():
    from obs.parse import run_goal
    assert run_goal(None) == run_goal({}) == run_goal({"goal": ""}) == "solvable"
    assert run_goal({"goal": "unsolvable"}) == "unsolvable"


def test_split_lines_keeps_partial_tail():
    lines, consumed = split_lines(b'{"a":1}\n{"b":2}\n{"c"', 100)
    assert [o for o, _ in lines] == [100, 108]
    assert consumed == 16


def test_pilot_grammar():
    assert parse_pilot_line("[run] forum-multi4-r0: starting board + 4 agent(s)")["kind"] == "starting"
    m = parse_pilot_line("[run] forum-multi4-r0: sb-forum-multi4-r0-a3 done")
    assert (m["kind"], m["agent_id"]) == ("agent_done", 3)
    assert parse_pilot_line("[run] wiki-solo-r0: finished in 31s")["detail"] == "31"
    assert parse_pilot_line("[skip] forum-multi4-r0 already done")["kind"] == "skip"
    e = parse_pilot_line("[error] irc n=4 rep=1: boom")
    assert (e["kind"], e["run_id"]) == ("error", "irc-multi4-r1")
    assert parse_pilot_line("garbage") is None
    # lanes with a model context log the run id itself; the legacy form above still wins for Cerebras lines
    e = parse_pilot_line("[error] forum-multi4-r3--glm53f: boom")
    assert (e["kind"], e["run_id"], e["detail"]) == ("error", "forum-multi4-r3--glm53f", "boom")
    a = parse_pilot_line("[abort] wiki-solo-r2--gemma31: provider HTTP 402")
    assert (a["kind"], a["run_id"], a["agent_id"], a["detail"]) == ("abort", "wiki-solo-r2--gemma31", None,
                                                                    "provider HTTP 402")
    b = parse_pilot_line("[budget] stop at r5: committed 37.1 / cap 38.0")
    assert (b["kind"], b["run_id"], b["detail"]) == ("budget_stop", "_pilot", "at r5: committed 37.1 / cap 38.0")
    assert vm_agent_id("sb-x-a12") == 12


def test_contract_schema_mirror():
    """CONTRACT.md section 2 mirrors obs/schema.sql verbatim."""
    from obs.config import DASH
    from obs.db import SCHEMA_PATH
    text = (DASH / "CONTRACT.md").read_text()
    block = text.split("```sql\n", 1)[1].split("\n```", 1)[0]
    assert block.strip() == SCHEMA_PATH.read_text().strip()


def _model_rec(usage):
    return {"kind": "model", "call": 1, "ts": 1.0, "phase": "task", "content": None, "tool_calls": [],
            "usage": usage}


def test_model_usage_token_fields():
    row = lambda u: norm_steps_record(_model_rec(u), "r", 0, 0, 0, "file", None)["model_calls"][0]  # noqa: E731
    # top-level reasoning_tokens (some OpenAI-compatible providers) and null details
    r = row({"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3, "prompt_tokens_details": None,
             "completion_tokens_details": None})
    assert (r["prompt_tokens"], r["completion_tokens"], r["reasoning_tokens"], r["cached_tokens"]) == (10, 5, 3, None)
    # nested details win; an explicit 0 stays 0 (not replaced by the top-level value)
    r = row({"completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 0}, "reasoning_tokens": 4,
             "prompt_tokens_details": {"cached_tokens": 2}})
    assert (r["reasoning_tokens"], r["cached_tokens"]) == (0, 2)
    assert row({"completion_tokens": 5})["reasoning_tokens"] is None
    assert row(None)["prompt_tokens"] is None


def _lab_models():
    path = REPO / "lab" / "config.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_lab_config_for_dash_test", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - lab config not importable here: nothing to compare against
        return None
    return getattr(mod, "MODELS", None)


def test_model_prices_match_lab_config():
    models = _lab_models()
    if not models:
        pytest.skip("lab/config.MODELS not available")
    get = lambda m, k: m.get(k) if isinstance(m, dict) else getattr(m, k)  # noqa: E731
    lab = {get(m, "model"): (get(m, "price_in"), get(m, "price_out")) for m in models.values()}
    assert lab == MODEL_PRICES
