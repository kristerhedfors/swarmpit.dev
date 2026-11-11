#!/usr/bin/env python3
"""Render runs/analysis.json into a self-contained HTML report (runs/report.html).

With --compare, render runs/analysis_models.json (analyze.py --compare) into runs/report_models.html
instead, with prose from runs/report_models_notes.json. With --goals, render runs/analysis_goals.json
(analyze.py --goals) into runs/report_goals.html, prose from runs/report_goals_notes.json.
--goal unsolvable switches the default file names of the first two to their ".unsolvable" variants
(analysis.unsolvable.json -> report.unsolvable.html, notes report_notes.unsolvable.json); the page
itself follows the input document's goal.

Usage: python3 lab/report.py [OUT] [--goal G] [--analysis PATH] [--notes PATH]
       python3 lab/report.py [OUT] [--goal G] --compare [PATH] [--notes PATH]
       python3 lab/report.py [OUT] --goals [PATH] [--notes PATH]
"""
import argparse
import json
from pathlib import Path

from analysis.aggregate import DEFAULT_GOAL, GOALS
from analysis.jsonl import read_json
from analysis.report import render_comparison, render_goals, render_report
from config import RUNS


def goal_name(stem: str, ext: str, goal: str) -> Path:
    """RUNS/<stem>.<ext>, or RUNS/<stem>.<goal>.<ext> for a non-default goal."""
    return RUNS / (f"{stem}.{ext}" if goal == DEFAULT_GOAL else f"{stem}.{goal}.{ext}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", type=Path, default=None,
                    help=f"output path (default: {RUNS / 'report.html'}, report_models.html with --compare, "
                         f"report_goals.html with --goals)")
    ap.add_argument("--goal", choices=GOALS, default=DEFAULT_GOAL,
                    help="pick the default file names of this goal (default: %(default)s)")
    ap.add_argument("--analysis", type=Path, default=None,
                    help=f"input (default: {RUNS / 'analysis.json'}, analysis.<goal>.json with --goal)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--compare", nargs="?", type=Path, const=True, default=None, metavar="PATH",
                      help=f"render the per-model comparison (default input: {RUNS / 'analysis_models.json'})")
    mode.add_argument("--goals", nargs="?", type=Path, const=RUNS / "analysis_goals.json", default=None,
                      metavar="PATH",
                      help=f"render the goal comparison (default input: {RUNS / 'analysis_goals.json'})")
    ap.add_argument("--notes", type=Path, default=None,
                    help=f"hand-written prose; optional (default: {RUNS / 'report_notes.json'}, "
                         f"report_models_notes.json with --compare, report_goals_notes.json with --goals)")
    args = ap.parse_args()
    if args.goals is not None:
        out = args.out or RUNS / "report_goals.html"
        page = render_goals(json.loads(args.goals.read_text()),
                            read_json(args.notes or RUNS / "report_goals_notes.json", {}))
    elif args.compare is not None:
        src = goal_name("analysis_models", "json", args.goal) if args.compare is True else args.compare
        out = args.out or goal_name("report_models", "html", args.goal)
        page = render_comparison(json.loads(src.read_text()),
                                 read_json(args.notes or goal_name("report_models_notes", "json", args.goal), {}))
    else:
        src = args.analysis or goal_name("analysis", "json", args.goal)
        out = args.out or goal_name("report", "html", args.goal)
        page = render_report(json.loads(src.read_text()),
                             read_json(args.notes or goal_name("report_notes", "json", args.goal), {}))
    out.write_text(page)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
