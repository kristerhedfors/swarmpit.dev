#!/usr/bin/env python3
"""Compute stickiness metrics from runs/<run_id>/ and write runs/analysis.json.

Board-step definition and metric formulas live in lab/analysis/metrics.py; aggregation in
lab/analysis/aggregate.py. By default only runs of the default model (config.DEFAULT_SLUG) and the
solvable goal are analysed; --model picks another model (written to analysis.<slug>.json), --compare
puts every model present side by side (analysis_models.json). --goal unsolvable analyses the runs where
the flag can never be found instead (".unsolvable" before ".json" in the default output names);
--goals compares the two goals per model on task-phase metrics (analysis_goals.json).

Usage: python3 lab/analyze.py [--include-tagged] [--runs DIR] [--out PATH] [--goal solvable|unsolvable]
                              [--model SLUG|ID | --compare | --list-models]
       python3 lab/analyze.py [--include-tagged] [--runs DIR] [--out PATH] --goals
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from analysis.aggregate import (DEFAULT_GOAL, GOALS, build_analysis, build_comparison, build_goal_comparison,
                                format_comparison, format_goals, format_table, run_goal, scan_runs)
from config import MODEL_ORDER, MODELS, RUNS, resolve_slug


def list_models(runs_dir: Path, include_tagged: bool, goal: str = DEFAULT_GOAL) -> list[str]:
    """Registry rows with the number of analysable runs of each model (of `goal`) under runs_dir."""
    counts = Counter(slug for _, meta, slug in scan_runs(runs_dir, include_tagged) if run_goal(meta) == goal)
    lines = ["\t".join(["slug", "provider", "model", "runs"])]
    lines += ["\t".join([s, MODELS[s]["provider"], MODELS[s]["model"], str(counts[s])]) for s in MODEL_ORDER]
    return lines


def out_name(stem: str, goal: str) -> str:
    """Default output file name: '<stem>.json', or '<stem>.<goal>.json' for a non-default goal."""
    return f"{stem}.json" if goal == DEFAULT_GOAL else f"{stem}.{goal}.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--include-tagged", action="store_true",
                    help="also analyse smoke/calibration runs whose run id has a suffix tag")
    ap.add_argument("--runs", type=Path, default=RUNS, help="runs directory (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: <runs>/analysis.json, analysis.<slug>.json, analysis_models.json, "
                         "analysis_goals.json; .<goal>.json for --goal unsolvable)")
    ap.add_argument("--goal", choices=GOALS, default=DEFAULT_GOAL,
                    help="analyse the runs of this goal (default: %(default)s)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--model", default=None, help="analyse this model's runs (slug or full model id)")
    mode.add_argument("--compare", action="store_true", help="per-model comparison of every model present")
    mode.add_argument("--list-models", action="store_true", help="list known models and their run counts")
    mode.add_argument("--goals", action="store_true",
                      help="per-model comparison of the solvable and unsolvable goals (ignores --goal)")
    args = ap.parse_args()
    if args.list_models:
        for line in list_models(args.runs, args.include_tagged, args.goal):
            print(line)
        return
    if args.goals:
        doc = build_goal_comparison(args.runs, args.include_tagged)
        (args.out or args.runs / "analysis_goals.json").write_text(json.dumps(doc, indent=2))
        for line in format_goals(doc):
            print(line)
        return
    if args.compare:
        doc = build_comparison(args.runs, args.include_tagged, args.goal)
        (args.out or args.runs / out_name("analysis_models", args.goal)).write_text(json.dumps(doc, indent=2))
        for line in format_comparison(doc):
            print(line)
        return
    if args.model is None:
        out = build_analysis(args.runs, args.include_tagged, goal=args.goal)
        path = args.out or args.runs / out_name("analysis", args.goal)
    else:
        try:
            slug = resolve_slug(args.model)
        except ValueError as e:
            ap.error(str(e))
        out = build_analysis(args.runs, args.include_tagged, slug, goal=args.goal)
        path = args.out or args.runs / out_name(f"analysis.{slug}", args.goal)
    path.write_text(json.dumps(out, indent=2))
    for line in format_table(out["cells"]):
        print(line)


if __name__ == "__main__":
    main()
