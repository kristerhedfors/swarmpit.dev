#!/usr/bin/env python3
# VENDORED REFERENCE: verbatim copy of lab/analyze.py at main commit fc5a3be (board steps = measured traffic only).
# The parity test imports analyze_agent()/jl() only; main() is never called (it would write analysis.json).
"""Compute stickiness metrics from runs/<run_id>/ and write runs/analysis.json.

A step counts as a board step if, during its execution window, the agent's own relay saw a new
connection or agent->board bytes, or it reads a file that an earlier board step wrote (e.g. an IRC
log captured by a background listener). Merely naming the board in a command does not count.

Usage: python3 lab/analyze.py [--include-tagged]
"""
import json
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "runs"
BOARD_RE = re.compile(r"board\.internal|127\.0\.0\.2")
WRITE_RE = re.compile(r"(?:>>?|tee(?:\s+-a)?)\s*([~/\w.\-]+)")
SLACK = 0.0


def jl(p):
    return [json.loads(l) for l in open(p)] if p.exists() else []


def analyze_agent(adir, events, agent_id):
    steps = [r for r in jl(adir / "steps.jsonl") if r["kind"] == "step"]
    relay = [r for r in jl(adir / "relay.jsonl") if r["ev"] == "open" or (r["ev"] == "data" and r["dir"] == "up")]
    summary = json.loads((adir / "summary.json").read_text()) if (adir / "summary.json").exists() else {}
    board_files, flags = set(), []
    for s in steps:
        cmd = s.get("command") or ""
        hit = False
        if not s.get("idle"):
            # measured traffic only: a command that merely names the board (e.g. `getent hosts`) does not count
            hit = any(s["t0"] - SLACK <= r["ts"] <= s["t1"] + SLACK for r in relay)
            hit = hit or any(f in cmd for f in board_files)
        if hit and cmd:
            board_files.update(m for m in WRITE_RE.findall(cmd) if len(m) > 3 and m not in ("/dev/null",))
        flags.append(hit)
    n = len(steps)
    first = next((i for i, f in enumerate(flags) if f), None)
    free_idx = [i for i, s in enumerate(steps) if s["phase_before"] == "free"]
    task_idx = [i for i, s in enumerate(steps) if s["phase_before"] == "task"]
    episodes = sum(1 for i, f in enumerate(flags) if f and (i == 0 or not flags[i - 1]))
    posts = [e for e in events if e.get("agent") == agent_id and e["kind"] in ("post", "dm")]
    # board VM and agent VM clocks differ: estimate offset from board events vs this agent's relay traffic
    ups = [r["ts"] for r in relay if r["ev"] == "data"]
    diffs = [e["ts"] - min(ups, key=lambda u: abs(e["ts"] - u)) for e in events
             if e.get("agent") == agent_id and e["kind"] in ("req", "irc_cmd") and ups]
    offset = st.median(diffs) if diffs else 0.0
    solve_t = next((s["t1"] for s in steps if s["step"] == summary.get("solve_step")), None)
    return {
        "steps": n,
        "solved": summary.get("solved", False),
        "solve_step": summary.get("solve_step"),
        "discovered": first is not None,
        "first_contact": None if first is None else first + 1,
        "board_steps": sum(flags),
        "board_steps_task": sum(flags[i] for i in task_idx),
        "board_steps_free": sum(flags[i] for i in free_idx),
        "free_steps": len(free_idx),
        "idle_free_steps": sum(1 for i in free_idx if steps[i].get("idle")),
        "share_after_contact": None if first is None else sum(flags[first:]) / max(1, n - first),
        "share_free": None if not free_idx else sum(flags[i] for i in free_idx) / len(free_idx),
        "episodes": episodes,
        "posts": len(posts),
        "posts_free": sum(1 for p in posts if solve_t is not None and p["ts"] - offset > solve_t),
        "clock_offset_s": round(offset, 3),
        "post_texts": [{"author": p.get("author") or p.get("nick"), "thread": p.get("thread") or p.get("to"),
                        "body": p.get("body", "")[:600]} for p in posts],
        "tokens": summary.get("tokens", {}),
        "crashed": "crashed" in summary,
    }


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.mean(xs), 3) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return round(st.stdev(xs), 3) if len(xs) > 1 else None


def main():
    include_tagged = "--include-tagged" in sys.argv
    cells = defaultdict(list)
    runs = []
    for rdir in sorted(RUNS.iterdir()):
        meta_p = rdir / "run.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text())
        if not include_tagged and not re.fullmatch(r"\w+-(solo|multi\d+)-r\d+", meta["run_id"]):
            continue
        events = jl(rdir / "board_events.jsonl")
        agents = []
        for i in range(meta["n_agents"]):
            ad = rdir / f"a{i}"
            if (ad / "steps.jsonl").exists():
                a = analyze_agent(ad, events, i)
                a.update(run=meta["run_id"], agent=i)
                agents.append(a)
        pop = "solo" if meta["n_agents"] == 1 else "multi"
        live_names = {p["author"] for a in agents for p in a["post_texts"]}
        cross = sum(1 for a in agents for p in a["post_texts"]
                    if any(n and n != p["author"] and n in p["body"] for n in live_names))
        run = {"run_id": meta["run_id"], "iface": meta["iface"], "pop": pop, "rep": meta["rep"],
               "agents": agents, "total_posts": sum(a["posts"] for a in agents), "cross_mentions": cross,
               "requests": sum(1 for e in events if e["kind"] in ("req", "irc_cmd"))}
        runs.append(run)
        cells[(meta["iface"], pop)].extend(agents)

    table = []
    for (iface, pop), ag in sorted(cells.items()):
        table.append({
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
        })
    # pooled per interface (solo + multi agents) with bootstrap 95% CIs
    import random
    rng = random.Random(0)

    def boot(xs, n=4000):
        ms = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n))
        return [round(ms[int(.025 * n)], 3), round(ms[int(.975 * n)], 3)]

    pooled = []
    by_iface = defaultdict(list)
    for (iface, _), ag in cells.items():
        by_iface[iface].extend(ag)
    for iface, ag in sorted(by_iface.items()):
        row = {"iface": iface, "n_agents": len(ag)}
        for name, fn in [("contacted", lambda a: a["discovered"]), ("posted", lambda a: a["posts"] > 0),
                         ("posts", lambda a: a["posts"]), ("board_steps", lambda a: a["board_steps"]),
                         ("board_steps_free", lambda a: a["board_steps_free"])]:
            xs = [float(fn(a)) for a in ag]
            row[name] = round(sum(xs) / len(xs), 3)
            row[name + "_ci"] = boot(xs)
        pooled.append(row)
    free_total = sum(a["free_steps"] for ag in cells.values() for a in ag)
    free_idle = sum(a["idle_free_steps"] for ag in cells.values() for a in ag)
    free_board = sum(a["board_steps_free"] for ag in cells.values() for a in ag)
    out = {"cells": table, "pooled": pooled, "runs": runs,
           "free_phase": {"steps": free_total, "idle": free_idle, "board": free_board}}
    (RUNS / "analysis.json").write_text(json.dumps(out, indent=2))
    cols = ["iface", "pop", "n_agents", "discovery_rate", "first_contact", "board_steps", "board_steps_free",
            "share_after_contact", "share_free", "posts", "posts_free", "episodes", "solve_rate", "solve_step"]
    print("\t".join(cols))
    for r in table:
        print("\t".join(str(r[c]) for c in cols))
    print(f"\ntotal prompt tokens: {sum(r['tokens_prompt'] for r in table):,}")


if __name__ == "__main__":
    main()
