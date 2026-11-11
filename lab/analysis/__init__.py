"""Offline analysis of board-stickiness runs (host side only).

jsonl      log loading helpers
metrics    per-agent metrics from one agent's logs plus the run's board events (plus task-phase
           metrics: whole-session board share, persistence, live-live interactions)
aggregate  cells table, pooled bootstrap CIs, free-phase totals, run summaries -> analysis.json;
           per goal (solvable / unsolvable) and the goal comparison -> analysis_goals.json
charts     SVG chart builders for the report
report     HTML page assembly from analysis.json + report_notes.json (and the models/goals pages)
transcript compact per-run transcript (lab/show.py)
"""
