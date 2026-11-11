"""Aggregate per-agent metrics over runs into the analysis.json document.

Ordering matters for reproducibility: run dirs are read in sorted order, cells keep first-seen
order when pooled, and one Random(BOOTSTRAP_SEED) is consumed interface by interface, metric by
metric, so analysis.json is byte-stable for a given runs/ tree.

Every document covers one model (config.MODELS slug, default DEFAULT_SLUG): a run's model comes
from its run.json harness, and a non-default run id carries the same slug as "--<slug>".
build_comparison puts one such analysis per model side by side (analysis_models.json).

Every document also covers one goal (config.GOALS, default DEFAULT_GOAL): run.json params.goal, absent
on the historical runs, which are all "solvable". An unsolvable run (the flag can never be found, so
there is no free phase) carries the config.GOAL_SEGMENT "-unsolvable" right after the rep of its id
("forum-multi4-r3-unsolvable--glm53f", config.id_goal); a run whose id and run.json disagree is
skipped. A non-default goal's document says so ("goal" key first),
nulls the free-phase metrics (n/a) and gives agents the task-phase metrics. build_goal_comparison
puts solvable and unsolvable side by side per model (analysis_goals.json).
"""
import random
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

from config import (DEFAULT_GOAL, DEFAULT_SLUG, GOAL_SEGMENT, GOALS, MODEL_ORDER, MODELS, id_goal, price, run_goal,
                    run_slug_of)

from .jsonl import read_json, read_jsonl
from .metrics import REQUEST_KINDS, analyze_agent

# Pilot run ids, optionally with a model slug; anything else (smoke, calibration, checks) is
# tagged and skipped by default.
# The optional goal segment right after the rep ("-unsolvable"; config.GOAL_SEGMENT).
_GOAL = "(?:" + "|".join(re.escape(seg) for seg in GOAL_SEGMENT.values() if seg) + ")?"
UNTAGGED_RUN_ID = re.compile(r"\w+-(solo|multi\d+)-r\d+" + _GOAL)
MODEL_RUN_ID = re.compile(r"\w+-(?:solo|multi\d+)-r\d+" + _GOAL + r"--[a-z0-9]{2,12}")
# The reserved "--<slug>" segment right after the rep (and goal segment), followed by the end or a "-<tag>".
ID_SLUG = re.compile(r"\w+-(?:solo|multi\d+)-r\d+" + _GOAL + r"--([a-z0-9]{2,12})(?=-|$)")
GOAL_LABEL = {"solvable": "Solvable", "unsolvable": "Unsolvable"}
# Cell metrics that only exist after a solve; None (n/a) in a document without one.
FREE_CELL_KEYS = ("board_steps_free", "share_free", "posts_free", "idle_free")
BOOTSTRAP_SEED = 0
BOOTSTRAP_RESAMPLES = 4000
TABLE_COLUMNS = ["iface", "pop", "n_agents", "discovery_rate", "first_contact", "board_steps", "board_steps_free",
                 "share_after_contact", "share_free", "posts", "posts_free", "episodes", "solve_rate", "solve_step"]
# Pooled per-interface metrics, in output (and RNG consumption) order.
POOLED_METRICS: list[tuple[str, Callable[[dict], float]]] = [
    ("contacted", lambda a: a["discovered"]),
    ("posted", lambda a: a["posts"] > 0),
    ("posts", lambda a: a["posts"]),
    ("board_steps", lambda a: a["board_steps"]),
    ("board_steps_free", lambda a: a["board_steps_free"]),
]

Cells = dict[tuple[str, str], list[dict]]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.mean(xs), 3) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return round(st.stdev(xs), 3) if len(xs) > 1 else None


def population(n_agents: int) -> str:
    return "solo" if n_agents == 1 else "multi"


def cross_mentions(agents: list[dict]) -> int:
    """Posts that name another live agent of the same run (a crude signal of agents talking to each other)."""
    live_names = {p["author"] for a in agents for p in a["post_texts"]}
    return sum(1 for a in agents for p in a["post_texts"]
               if any(n and n != p["author"] and n in p["body"] for n in live_names))


def analyze_run(rdir: Path, meta: dict, extended: bool = False) -> tuple[dict, list[dict]]:
    """Run-level summary and the run's per-agent metrics (agents without steps.jsonl are skipped).

    extended adds the agents' task-phase metrics and the run's live-live interaction total."""
    events = read_jsonl(rdir / "board_events.jsonl")
    agents = []
    for i in range(meta["n_agents"]):
        ad = rdir / f"a{i}"
        if (ad / "steps.jsonl").exists():
            a = analyze_agent(ad, events, i, extended)
            a.update(run=meta["run_id"], agent=i)
            agents.append(a)
    run = {"run_id": meta["run_id"], "iface": meta["iface"], "pop": population(meta["n_agents"]), "rep": meta["rep"],
           "agents": agents, "total_posts": sum(a["posts"] for a in agents), "cross_mentions": cross_mentions(agents),
           "requests": sum(1 for e in events if e["kind"] in REQUEST_KINDS)}
    if extended:
        run["live_interactions"] = sum(a["replies"] + a["dms_live"] for a in agents)
    return run, agents


def is_untagged(run_id: str) -> bool:
    return bool(UNTAGGED_RUN_ID.fullmatch(run_id) or MODEL_RUN_ID.fullmatch(run_id))


def id_slug(run_id: str) -> str:
    """Model slug named by the run id; ids without the "--<slug>" segment are DEFAULT_SLUG runs."""
    m = ID_SLUG.match(run_id)
    return m.group(1) if m else DEFAULT_SLUG


def run_slug(meta: dict) -> str | None:
    """Model slug of a run, from the harness it was started with (no provider/model: the default;
    None for a model outside config.MODELS)."""
    return run_slug_of(meta.get("harness") or {})


RunDirs = list[tuple[Path, dict, str]]


def scan_runs(runs_dir: Path, include_tagged: bool = False) -> RunDirs:
    """(dir, run.json, slug) of every run in dir order. A run whose id and harness name different
    models, or whose id and run.json name different goals, is skipped with a warning on stderr: its id
    would file it under the wrong model or condition."""
    found = []
    for rdir in sorted(runs_dir.iterdir()):
        meta = read_json(rdir / "run.json")
        if meta is None:
            continue
        if not include_tagged and not is_untagged(meta["run_id"]):
            continue
        slug = run_slug(meta)
        if slug is None:
            print(f"[analyze] skip {meta['run_id']}: harness model is not in config.MODELS", file=sys.stderr)
            continue
        if id_slug(meta["run_id"]) != slug:
            print(f"[analyze] skip {meta['run_id']}: run id names model {id_slug(meta['run_id'])}, "
                  f"harness is {slug}", file=sys.stderr)
            continue
        goal = run_goal(meta)
        if goal not in GOALS:
            print(f"[analyze] skip {meta['run_id']}: unknown goal {goal!r}", file=sys.stderr)
            continue
        if id_goal(meta["run_id"]) != goal:
            print(f"[analyze] skip {meta['run_id']}: run id names goal {id_goal(meta['run_id'])}, "
                  f"run.json is {goal}", file=sys.stderr)
            continue
        found.append((rdir, meta, slug))
    return found


def discover_slugs(scanned: RunDirs) -> list[str]:
    """Models present among scanned runs, in MODEL_ORDER."""
    present = {slug for *_, slug in scanned}
    return [s for s in MODEL_ORDER if s in present]


def collect_runs(runs_dir: Path, include_tagged: bool = False, slug: str = DEFAULT_SLUG,
                 scanned: RunDirs | None = None, goal: str = DEFAULT_GOAL,
                 extended: bool = False) -> tuple[list[dict], Cells]:
    """Run summaries of one model and goal in run-dir order, and agents grouped by (iface, pop) in
    first-seen order.

    The model and goal filters are independent of the tag filter. `scanned` reuses a scan_runs result;
    `extended` adds the task-phase metrics (analyze_run).
    """
    cells: Cells = defaultdict(list)
    runs = []
    for rdir, meta, s in scan_runs(runs_dir, include_tagged) if scanned is None else scanned:
        if s != slug or run_goal(meta) != goal:
            continue
        run, agents = analyze_run(rdir, meta, extended)
        runs.append(run)
        cells[(meta["iface"], run["pop"])].extend(agents)
    return runs, cells


def cell_row(iface: str, pop: str, ag: list[dict]) -> dict:
    """Per-agent means for one (interface, population) cell."""
    return {
        "iface": iface, "pop": pop, "n_agents": len(ag),
        "discovery_rate": mean([a["discovered"] for a in ag]),
        "first_contact": mean([a["first_contact"] for a in ag]),
        "board_steps": mean([a["board_steps"] for a in ag]), "board_steps_sd": sd([a["board_steps"] for a in ag]),
        "board_steps_free": mean([a["board_steps_free"] for a in ag]),
        "share_after_contact": mean([a["share_after_contact"] for a in ag]),
        "share_free": mean([a["share_free"] for a in ag]),
        "posts": mean([a["posts"] for a in ag]), "posts_free": mean([a["posts_free"] for a in ag]),
        "episodes": mean([a["episodes"] for a in ag]),
        "solve_rate": mean([a["solved"] for a in ag]), "solve_step": mean([a["solve_step"] for a in ag]),
        "idle_free": mean([a["idle_free_steps"] for a in ag]),
        "tokens_prompt": sum(a["tokens"].get("prompt", 0) for a in ag),
    }


def cells_table(cells: Cells) -> list[dict]:
    return [cell_row(iface, pop, ag) for (iface, pop), ag in sorted(cells.items())]


def bootstrap_ci(xs: list[float], rng: random.Random, n: int = BOOTSTRAP_RESAMPLES) -> list[float | None]:
    """Percentile 95% CI of the mean from n resamples drawn with `rng`; [None, None] (no draws) if xs is empty."""
    if not xs:
        return [None, None]
    ms = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return [round(ms[int(.025 * n)], 3), round(ms[int(.975 * n)], 3)]


def pooled_table(cells: Cells, metrics: list[tuple[str, Callable[[dict], float]]] = POOLED_METRICS,
                 na: tuple[str, ...] = ()) -> list[dict]:
    """Per interface, solo and multi agents pooled: mean and bootstrap CI of each `metrics` entry.

    A metric named in `na` is None with a [None, None] CI and draws nothing from the RNG; agents whose
    metric is None are left out of its mean and CI."""
    rng = random.Random(BOOTSTRAP_SEED)
    by_iface = defaultdict(list)
    for (iface, _), ag in cells.items():
        by_iface[iface].extend(ag)
    pooled = []
    for iface, ag in sorted(by_iface.items()):
        row = {"iface": iface, "n_agents": len(ag)}
        for name, fn in metrics:
            xs = [] if name in na else [float(v) for v in map(fn, ag) if v is not None]
            row[name] = round(sum(xs) / len(xs), 3) if xs else None
            row[name + "_ci"] = bootstrap_ci(xs, rng)
        pooled.append(row)
    return pooled


def free_phase_totals(cells: Cells) -> dict:
    """Free-phase steps over all agents, split into idle turns and board steps (the rest is other commands)."""
    agents = [a for ag in cells.values() for a in ag]
    return {"steps": sum(a["free_steps"] for a in agents),
            "idle": sum(a["idle_free_steps"] for a in agents),
            "board": sum(a["board_steps_free"] for a in agents)}


def null_free(rows: list[dict]) -> list[dict]:
    """Cells with the free-phase metrics set to None (n/a: the runs have no free phase)."""
    return [r | {k: None for k in FREE_CELL_KEYS if k in r} for r in rows]


def build_analysis(runs_dir: Path, include_tagged: bool = False, slug: str = DEFAULT_SLUG,
                   scanned: RunDirs | None = None, goal: str = DEFAULT_GOAL) -> dict:
    """The analysis.json document (analysis[.<slug>][.<goal>].json for a non-default model or goal).

    A non-default goal's document starts with "goal", has the free-phase cell and pooled metrics None
    (free_phase still counts free-phase steps: anything but 0 means a run was solvable after all) and
    its agents and runs carry the task-phase metrics."""
    if goal == DEFAULT_GOAL:
        runs, cells = collect_runs(runs_dir, include_tagged, slug, scanned)
        return {"cells": cells_table(cells), "pooled": pooled_table(cells), "runs": runs,
                "free_phase": free_phase_totals(cells)}
    runs, cells = collect_runs(runs_dir, include_tagged, slug, scanned, goal, extended=True)
    return {"goal": goal, "cells": null_free(cells_table(cells)),
            "pooled": pooled_table(cells, na=("board_steps_free",)), "runs": runs,
            "free_phase": free_phase_totals(cells)}


def format_table(cells: list[dict]) -> list[str]:
    """Tab-separated cells table plus the prompt-token total, as printed by lab/analyze.py."""
    lines = ["\t".join(TABLE_COLUMNS)]
    lines += ["\t".join(str(r[c]) for c in TABLE_COLUMNS) for r in cells]
    lines.append(f"\ntotal prompt tokens: {sum(r['tokens_prompt'] for r in cells):,}")
    return lines


def agent_summaries(scanned: RunDirs, slug: str, goal: str = DEFAULT_GOAL) -> list[dict | None]:
    """summary.json of every agent session of the model's `goal` runs (None where an agent left none)."""
    return [read_json(rdir / f"a{i}" / "summary.json") for rdir, meta, s in scanned
            if s == slug and run_goal(meta) == goal for i in range(meta["n_agents"])]


def ended_counts(summaries: list[dict | None]) -> dict:
    """How agent sessions ended: budget, stalled (idle cut-off), crashed, missing (no summary), others."""
    c = Counter("missing" if s is None else s.get("ended") or ("crashed" if "crashed" in s else "unknown")
                for s in summaries)
    return {k: c[k] for k in ("budget", "stalled", "crashed", "missing")} | dict(sorted(
        (k, v) for k, v in c.items() if k not in ("budget", "stalled", "crashed", "missing")))


def token_totals(summaries: list[dict | None]) -> dict:
    """Prompt and completion tokens summed over agent summaries (crash summaries carry them too)."""
    toks = [(s or {}).get("tokens") or {} for s in summaries]
    return {k: sum(t.get(k) or 0 for t in toks) for k in ("prompt", "completion")}


def token_cost(slug: str, tokens: dict) -> float | None:
    """EUR at the model's list price, from summary token counts; None for an unpriced model.

    An estimate for comparison only: the spend ledger (lab/spend.py) is the billing record.
    """
    p_in, p_out = price(slug)
    if p_in is None or p_out is None:
        return None
    return round((tokens["prompt"] * p_in + tokens["completion"] * p_out) / 1e6, 4)


def runs_per_cell(runs: list[dict]) -> dict:
    """'<iface>-<pop>' -> number of runs, in key order (reps can be unequal across models)."""
    return dict(sorted(Counter(f"{r['iface']}-{r['pop']}" for r in runs).items()))


def build_comparison(runs_dir: Path, include_tagged: bool = False, goal: str = DEFAULT_GOAL) -> dict:
    """The analysis_models.json document: one analysis per model with runs of `goal`, in MODEL_ORDER.
    A non-default goal's document starts with "goal" (analysis_models.<goal>.json)."""
    scanned = [r for r in scan_runs(runs_dir, include_tagged) if run_goal(r[1]) == goal]
    slugs = discover_slugs(scanned)
    by_model = {}
    for slug in slugs:
        a = build_analysis(runs_dir, include_tagged, slug, scanned, goal)
        summaries = agent_summaries(scanned, slug, goal)
        tokens = token_totals(summaries)
        ended = ended_counts(summaries)
        m = MODELS[slug]
        by_model[slug] = {
            "label": m["label"], "model": m["model"], "provider": m["provider"],
            "n_runs": len(a["runs"]), "reps": runs_per_cell(a["runs"]),
            "cells": a["cells"], "pooled": a["pooled"], "free_phase": a["free_phase"],
            "tokens": tokens, "cost_eur": token_cost(slug, tokens), "ended": ended,
            "crashed_agents": sum(1 for s in summaries if s is not None and "crashed" in s),
        }
    return ({} if goal == DEFAULT_GOAL else {"goal": goal}) | {"models": slugs, "by_model": by_model}


COMPARE_METRICS = ("contacted", "posted", "posts", "board_steps_free")


def format_comparison(doc: dict) -> list[str]:
    """Tab-separated per-model summary and pooled per-interface metrics, as printed by analyze.py --compare."""
    lines = ["\t".join(["model", "runs", "agent_sessions", "stalled", "crashed", "prompt_tok", "completion_tok",
                        "cost_eur"])]
    for slug, m in doc["by_model"].items():
        lines.append("\t".join(str(v) for v in (
            slug, m["n_runs"], sum(m["ended"].values()), m["ended"]["stalled"], m["crashed_agents"],
            m["tokens"]["prompt"], m["tokens"]["completion"], m["cost_eur"])))
    lines.append("")
    lines.append("\t".join(["model", "iface", "n_agents", *COMPARE_METRICS]))
    for slug, m in doc["by_model"].items():
        lines += ["\t".join(str(v) for v in (slug, p["iface"], p["n_agents"], *(p[k] for k in COMPARE_METRICS)))
                  for p in m["pooled"]]
    return lines


# Goal comparison (analysis_goals.json): task-phase metrics, which need no free phase, per model and goal.
GOAL_COLUMNS = ["iface", "pop", "n_agents", "discovery_rate", "first_contact", "board_steps", "share_board",
                "share_after_contact", "share_late", "active_late", "posts", "posted", "episodes", "replies",
                "dms_live", "mentions_live", "solve_rate", "board_steps_free", "share_free", "posts_free"]
# Live-live interaction metrics: None for solo agents (no live peer to address).
PEER_KEYS = ("replies", "dms_live", "mentions_live")
GOAL_POOLED_METRICS: list[tuple[str, Callable[[dict], float | None]]] = [
    ("contacted", lambda a: a["discovered"]),
    ("posted", lambda a: a["posts"] > 0),
    ("posts", lambda a: a["posts"]),
    ("board_steps", lambda a: a["board_steps"]),
    ("share_board", lambda a: a["share_board"]),
    ("share_late", lambda a: a["share_late"]),
    ("interactions", lambda a: None if a["replies"] is None else a["replies"] + a["dms_live"]),
    ("board_steps_free", lambda a: a["board_steps_free"]),
]
GOAL_COMPARE_METRICS = ("contacted", "posted", "posts", "share_board", "share_late", "interactions")


def peer_view(cells: Cells) -> Cells:
    """Cells with the live-live interaction metrics None for solo agents."""
    return {k: ag if k[1] != "solo" else [a | dict.fromkeys(PEER_KEYS) for a in ag] for k, ag in cells.items()}


def goal_cell_row(iface: str, pop: str, ag: list[dict]) -> dict:
    """Per-agent means of the task-phase metrics for one (interface, population) cell."""
    m = lambda k: mean([a[k] for a in ag])
    return {"iface": iface, "pop": pop, "n_agents": len(ag), "discovery_rate": m("discovered"),
            "first_contact": m("first_contact"), "board_steps": m("board_steps"), "share_board": m("share_board"),
            "share_after_contact": m("share_after_contact"), "share_late": m("share_late"),
            "active_late": mean([a["board_steps_late"] > 0 for a in ag]), "posts": m("posts"),
            "posted": mean([a["posts"] > 0 for a in ag]), "episodes": m("episodes"), "replies": m("replies"),
            "dms_live": m("dms_live"), "mentions_live": m("mentions_live"), "solve_rate": m("solved"),
            "board_steps_free": m("board_steps_free"), "share_free": m("share_free"), "posts_free": m("posts_free")}


def goal_block(runs_dir: Path, include_tagged: bool, slug: str, goal: str, scanned: RunDirs) -> dict:
    """One model's runs of one goal: run counts, solve rate, task-phase cells and pooled CIs, endings, cost.
    The free-phase metrics are None (n/a) for a goal without a free phase."""
    runs, cells = collect_runs(runs_dir, include_tagged, slug, scanned, goal, extended=True)
    cells = peer_view(cells)
    agents = [a for ag in cells.values() for a in ag]
    na = () if goal == DEFAULT_GOAL else ("board_steps_free",)
    rows = [goal_cell_row(iface, pop, ag) for (iface, pop), ag in sorted(cells.items())]
    summaries = agent_summaries(scanned, slug, goal)
    tokens = token_totals(summaries)
    return {"n_runs": len(runs), "reps": runs_per_cell(runs), "n_agents": len(agents),
            "solved_agents": sum(1 for a in agents if a["solved"]), "solve_rate": mean([a["solved"] for a in agents]),
            "cells": rows if goal == DEFAULT_GOAL else null_free(rows),
            "pooled": pooled_table(cells, GOAL_POOLED_METRICS, na), "free_phase": free_phase_totals(cells),
            "live_interactions": sum(r["live_interactions"] for r in runs if r["pop"] == "multi"),
            "tokens": tokens, "cost_eur": token_cost(slug, tokens), "ended": ended_counts(summaries)}


def build_goal_comparison(runs_dir: Path, include_tagged: bool = False) -> dict:
    """The analysis_goals.json document: per model present (MODEL_ORDER), one goal_block per goal it has
    runs of (GOALS order)."""
    scanned = scan_runs(runs_dir, include_tagged)
    slugs = discover_slugs(scanned)
    present = {(s, run_goal(meta)) for _, meta, s in scanned}
    by_model = {}
    for slug in slugs:
        goals = [g for g in GOALS if (slug, g) in present]
        m = MODELS[slug]
        by_model[slug] = {"label": m["label"], "model": m["model"], "provider": m["provider"], "goals": goals,
                          "by_goal": {g: goal_block(runs_dir, include_tagged, slug, g, scanned) for g in goals}}
    return {"goals": [g for g in GOALS if any(g in m["goals"] for m in by_model.values())], "models": slugs,
            "by_model": by_model}


def format_goals(doc: dict) -> list[str]:
    """Tab-separated per-model, per-goal summary and pooled task-phase metrics, as printed by analyze.py --goals."""
    lines = ["\t".join(["model", "goal", "runs", "agent_sessions", "solve_rate", "stalled", "crashed",
                        "live_interactions"])]
    for slug, m in doc["by_model"].items():
        for goal, g in m["by_goal"].items():
            lines.append("\t".join(str(v) for v in (slug, goal, g["n_runs"], g["n_agents"], g["solve_rate"],
                                                     g["ended"]["stalled"], g["ended"]["crashed"],
                                                     g["live_interactions"])))
    lines.append("")
    lines.append("\t".join(["model", "goal", "iface", "n_agents", *GOAL_COMPARE_METRICS]))
    for slug, m in doc["by_model"].items():
        for goal, g in m["by_goal"].items():
            lines += ["\t".join(str(v) for v in (slug, goal, p["iface"], p["n_agents"],
                                                 *(p[k] for k in GOAL_COMPARE_METRICS))) for p in g["pooled"]]
    return lines
