#!/usr/bin/env python3
"""Hill-climb loop for the instigation study (PROTOCOL.md sections 3 & 6).

Mutate/select over candidate board-seeds, driving everything by record_id and
numbers:

  1. pick top-scoring parents from the registry + current scores,
  2. ask the eval model (via generate.generate_mutation) for mutated children,
     written straight to new `*.chatter.json`,
  3. run short-budget scoring episodes through orchestrate (booting each run from
     the child's chatter file via --seed-path / ctx),
  4. ingest the runs into obs and read back the numeric instigation_score from
     the metric tool,
  5. update the registry and republish the scoreboard.

No board/chatter text ever flows through this process's stdout: generation and
scoring are done by model/stdlib subprocesses that read content and emit only
numbers + record ids. This module is the orchestrator of record ids and floats.

NOTE: running this performs real model calls and evals; the study validator
runs it, not a coding agent.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402
import orchestrate  # noqa: E402

from study import generate, registry, scoreboard  # noqa: E402

SERVER = registry.REPO_DIR / "dash" / "server"
# The study scores every episode on the unsolvable task (config.GOALS): no flag can be recovered, so the
# agents keep working for the whole budget and the seed's pull is measured over all of it.
STUDY_GOAL = "unsolvable"
assert STUDY_GOAL in config.GOALS
INSTIGATION_CLI = _LAB / "study" / "instigation.py"  # owned by the METRICS partition


def _obs_python():
    venv = SERVER / ".venv" / "bin" / "python"
    return os.environ.get("OBS_PYTHON") or (str(venv) if venv.exists() else sys.executable)


def obs_ingest(runs_dir: Path, db: Path, run_id: str = None) -> bool:
    """Incremental obs ingest + derive over runs_dir (optionally one run). Best-effort."""
    argv = [_obs_python(), "-m", "obs.cli", "ingest", "--runs", str(runs_dir), "--db", str(db)]
    if run_id:
        argv += ["--run", run_id]
    try:
        return subprocess.run(argv, cwd=SERVER, capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def score_run(run_dir: Path) -> dict:
    """Numeric instigation metric (PROTOCOL.md 7.2) for one run via the metric tool.

    Returns the parsed numbers-only object, or {} if the tool is unavailable or
    fails. The tool reads run structure and emits numbers; no content is read here.
    """
    if not INSTIGATION_CLI.exists():
        return {}
    try:
        proc = subprocess.run([sys.executable, str(INSTIGATION_CLI), str(run_dir)],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def select_parents(scores: dict, registry_path: Path = None, k: int = 4):
    """Top-k record ids by instigation_score (desc), tie-broken by shorter length."""
    rows = registry.load(registry_path)
    ranked = []
    for rid, row in rows.items():
        sc = (scores or {}).get(rid) or {}
        s = sc.get("instigation_score")
        if isinstance(s, (int, float)):
            ranked.append((rid, s, row.get("length_bytes") or (1 << 62)))
    ranked.sort(key=lambda r: (-r[1], r[2]))
    return [rid for rid, _, _ in ranked[:k]]


def _ctx(seed_path: Path, slug: str, host_port_base: int, cap_eur, allow_paid, goal: str = config.DEFAULT_GOAL):
    """Build an orchestrate RunContext for a scoring episode booted from a candidate.

    `goal` reaches the context exactly as orchestrate's --goal flag would (ctx_from_args validates it)."""
    ns = types.SimpleNamespace(model=None if slug == config.DEFAULT_SLUG else slug, provider=None,
                               host_port_base=host_port_base, tag="", goal=goal,
                               cap_eur=cap_eur, seed_path=str(seed_path))
    return orchestrate.ctx_from_args(ns)


def candidate_tag(seed_path) -> str:
    """Run-id tag that makes a scoring episode's run dir unique to ITS candidate (PROTOCOL.md 3 & 6).

    The run id is otherwise just ``<iface>-<pop>-r<rep>``, so two different candidates scored at the
    same (iface, n_agents, rep) -- e.g. generation g of two separate hill-climb invocations -- would
    derive the same run dir; run_one then prints ``[skip] already done`` and the new candidate is
    scored against the OLD run. Folding the opaque record id (content-free; hex, so never the goal
    marker) into the id forces a fresh episode per candidate. Empty for a non-candidate seed.
    """
    cand = orchestrate.candidate_id_from_seed_path(str(seed_path) if seed_path else None)
    return f"-{cand}" if cand else ""


def episode_run_id(seed_path, *, iface: str, n_agents: int, rep: int, slug: str = config.DEFAULT_SLUG,
                   goal: str = STUDY_GOAL) -> str:
    """Run id of a scoring episode: `<iface>-<pop>-r<rep>[-unsolvable][--<slug>]-<record_id>`, unique per
    (candidate, channel, model, goal, rep). Pure: no context, ledger or VM."""
    return orchestrate.make_run_id(iface, n_agents, rep, candidate_tag(seed_path),
                                   "" if slug == config.DEFAULT_SLUG else slug, goal)


def run_episode(seed_path: Path, *, iface: str, n_agents: int, rep: int, budget: int,
                slug: str = config.DEFAULT_SLUG, host_port_base: int = None, cap_eur=None,
                allow_paid: bool = False, goal: str = STUDY_GOAL) -> Path:
    """Run one short-budget scoring episode booted from the candidate chatter file.

    Returns the run directory. The run records params.candidate_id (the record id)
    in run.json and folds that id into the run id (candidate_tag) so each candidate
    is scored against its own fresh episode; VM names stay content-free. `goal`
    (default unsolvable, the study condition) gives the run id its `-unsolvable`
    segment and run.json params.goal; VM names leave it out (config.vm_run_id). An
    unsolvable episode also disables the harness stall exit, as orchestrate's CLI
    does, so idle agents spend their budget instead of ending early.
    """
    base = host_port_base if host_port_base is not None else config.HOST_PORT_BASE
    ctx = _ctx(seed_path, slug, base, cap_eur, allow_paid, goal)
    orchestrate.load_env(ctx.provider["secret_env"])
    harness = orchestrate.harness_for(ctx, {**config.HARNESS_DEFAULTS, "budget": budget})
    if ctx.goal != config.DEFAULT_GOAL:
        harness = {**harness, "stall_exit": False}
    tag = candidate_tag(seed_path)
    run_id = orchestrate.run_id_for(iface, n_agents, rep, tag, ctx)
    with orchestrate.port_lane(base, n_agents):
        orchestrate.run_one(iface, n_agents, rep, harness, tag=tag, ctx=ctx)
    return orchestrate.RUNS / run_id


def step(scores: dict, *, iface: str, n_agents: int, budget: int, generation: int,
         parents_k: int, children_per_parent: int, rep0: int = 0, slug: str = config.DEFAULT_SLUG,
         host_port_base: int = None, cap_eur=None, allow_paid: bool = False,
         db: Path = None, registry_path: Path = None, transport=None, goal: str = STUDY_GOAL) -> dict:
    """One hill-climb generation. Returns {record_id: instigation_score} for the children."""
    db = Path(db) if db is not None else (orchestrate.RUNS / "_study" / "instigation.duckdb")
    db.parent.mkdir(parents=True, exist_ok=True)
    parents = select_parents(scores, registry_path, parents_k)
    out = {}
    rep = rep0
    for pid in parents:
        parent_path = registry.candidate_path(pid)
        for _ in range(children_per_parent):
            meta = generate.generate_mutation(parent_path, slug=slug, channel=iface,
                                              generation=generation, transport=transport,
                                              allow_paid=allow_paid, registry_path=registry_path)
            child_id = meta["record_id"]
            child_path = registry.candidate_path(child_id)
            run_dir = run_episode(child_path, iface=iface, n_agents=n_agents, rep=rep, budget=budget,
                                  slug=slug, host_port_base=host_port_base, cap_eur=cap_eur,
                                  allow_paid=allow_paid, goal=goal)
            obs_ingest(orchestrate.RUNS, db, run_id=run_dir.name)
            metric = score_run(run_dir)
            s = metric.get("instigation_score")
            out[child_id] = s if isinstance(s, (int, float)) else None
            rep += 1
    return out


def _merge_scores(scores: dict, new: dict) -> dict:
    merged = dict(scores or {})
    for rid, s in new.items():
        if isinstance(s, (int, float)):
            merged.setdefault(rid, {})["instigation_score"] = s
    return merged


def main(argv=None):
    ap = argparse.ArgumentParser(description="hill-climb candidate board-seeds (numbers / ids only)")
    ap.add_argument("--iface", default="forum")
    ap.add_argument("--agents", type=int, default=config.MULTI_AGENTS)
    ap.add_argument("--budget", type=int, default=40)  # short budgets drew 0 writers; see PROTOCOL.md 3
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--parents", type=int, default=4)
    ap.add_argument("--children", type=int, default=2)
    ap.add_argument("--model", default=config.DEFAULT_SLUG)
    ap.add_argument("--allow-paid", action="store_true")
    ap.add_argument("--host-port-base", type=int, default=config.HOST_PORT_BASE)
    ap.add_argument("--cap-eur", type=float, default=None)
    ap.add_argument("--goal", choices=config.GOALS, default=STUDY_GOAL,
                    help="episode task goal (default unsolvable: agents keep trying for the whole budget)")
    ap.add_argument("--scores", help="path to a {record_id: {metrics}} JSON file to seed/update")
    a = ap.parse_args(argv)

    scores = {}
    if a.scores and Path(a.scores).exists():
        with open(a.scores) as f:
            scores = json.load(f)
    history = []
    for g in range(a.generations):
        new = step(scores, iface=a.iface, n_agents=a.agents, budget=a.budget, generation=g + 1,
                   parents_k=a.parents, children_per_parent=a.children, rep0=g * 1000,
                   slug=config.resolve_slug(a.model), host_port_base=a.host_port_base,
                   cap_eur=a.cap_eur, allow_paid=a.allow_paid, goal=a.goal)
        scores = _merge_scores(scores, new)
        history.append({"generation": g + 1, "children": len(new),
                        "scored": sum(1 for v in new.values() if v is not None)})
    if a.scores:
        with open(a.scores, "w") as f:
            json.dump(scores, f, indent=2)
    try:
        scoreboard.build_and_write(scores)
    except scoreboard.ScoreboardError as e:
        print(json.dumps({"ok": False, "error": e.args[0], "history": history}))
        return 1
    print(json.dumps({"ok": True, "generations": history, "n_scored": len(scores),
                      "updated_ts": time.time()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
