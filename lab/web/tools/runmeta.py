#!/usr/bin/env python3
"""Safe metadata for a browser-lab run zip: counts, codes and timings only, never text content.

    lab/web/tools/runmeta.py <run.zip>    -> one JSON object on stdout

`ok` is false with a fixed `error` code when the zip cannot be read (E_ZIP), is not one run directory with a
run.json (E_LAYOUT), or `obs ingest` fails (E_INGEST). The zip is extracted into a temporary directory that
is always removed. obs runs from dash/server with $OBS_PYTHON, else dash/server/.venv's python if present,
else this python.
"""
import collections
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SERVER = Path(__file__).resolve().parents[3] / "dash" / "server"
OBS_COUNTS = ("ingest_errors", "runs", "steps", "model_calls", "tool_calls", "submissions", "board_events",
              "relay_events", "posts", "interactions")


class Fail(Exception):
    """args[0] is the fixed error code."""


def obs_python():
    venv = SERVER / ".venv" / "bin" / "python"
    return os.environ.get("OBS_PYTHON") or (str(venv) if venv.exists() else sys.executable)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return [r for r in out if isinstance(r, dict)]


def summarize(d):
    """Metadata of one extracted run directory."""
    run = load_json(d / "run.json", {})
    summ = load_json(d / "a0" / "summary.json", {})
    steps = load_jsonl(d / "a0" / "steps.jsonl")
    st = [r for r in steps if r.get("kind") == "step"]
    exits = collections.Counter(str(r.get("exit")) for r in st)
    retries = [r for r in steps if r.get("kind") == "api_retry"]
    board = load_jsonl(d / "board_events.jsonl")
    fatal = load_json(d / "a0" / "FATAL", "unparseable") if (d / "a0" / "FATAL").exists() else None
    params = run.get("params") or {}
    times = sorted(round(r["t1"] - r["t0"], 1) for r in st if isinstance(r.get("t0"), (int, float))
                   and isinstance(r.get("t1"), (int, float)))
    return {
        "run_id": d.name, "iface": run.get("iface"), "params": params,
        "budget": (run.get("harness") or {}).get("budget"),
        "has_ctf": bool((run.get("ctf") or {}).get("flag")), "agent_stopped": params.get("agent_stopped"),
        "fatal": run.get("fatal"), "timed_out": run.get("timed_out"),
        "wall_s": round(summ.get("wall_s") or 0),
        "summary": {k: summ.get(k) for k in ("ended", "solved", "solve_step", "steps_used", "model_calls", "tokens",
                                             "crashed", "fatal")},
        "step_kinds": dict(collections.Counter(r.get("kind") for r in steps)), "exit_codes": dict(exits),
        "timeouts": exits.get("-9", 0), "idle_steps": sum(1 for r in st if r.get("idle")),
        "submissions": sum(len(r.get("submissions") or []) for r in st),
        "phase_free_steps": sum(1 for r in st if r.get("phase") == "free"),
        "median_step_s": times[len(times) // 2] if times else None,
        "api_retries": len(retries),
        "retry_prefixes": dict(collections.Counter(str(r.get("detail") or "")[:8] for r in retries)),  # "HTTP 429"
        "board_events": dict(collections.Counter(r.get("ev") or r.get("event") or r.get("kind") for r in board)),
        "relay_events": len(load_jsonl(d / "a0" / "relay.jsonl")),
        "fatal_status": fatal.get("status") if isinstance(fatal, dict) else fatal,
        "files": sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()),
    }


def obs_counts(runs_dir, db):
    """obs ingest + stats over runs_dir; the stats counts, or Fail("E_INGEST")."""
    py = obs_python()
    ingest = subprocess.run([py, "-m", "obs.cli", "ingest", "--runs", runs_dir, "--db", db], cwd=SERVER,
                            capture_output=True)
    stats = subprocess.run([py, "-m", "obs.cli", "stats", "--runs", runs_dir, "--db", db], cwd=SERVER,
                           capture_output=True, text=True)
    if ingest.returncode or stats.returncode:
        raise Fail("E_INGEST")
    counts = {}
    for line in stats.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            counts[parts[0]] = int(parts[1])
    return {k: counts.get(k) for k in OBS_COUNTS}


def main(zip_path):
    with tempfile.TemporaryDirectory(prefix="labmeta-") as tmp:
        runs = Path(tmp) / "runs"
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(runs)  # zipfile drops absolute and ".." components
        except (OSError, zipfile.BadZipFile):
            raise Fail("E_ZIP")
        dirs = list(runs.iterdir())
        if len(dirs) != 1 or not dirs[0].is_dir() or not (dirs[0] / "run.json").is_file():
            raise Fail("E_LAYOUT")
        out = summarize(dirs[0])
        out["obs"] = obs_counts(str(runs), str(Path(tmp) / "obs.duckdb"))
        return out


if __name__ == "__main__":
    try:
        result = {"ok": True, "error": None, **main(sys.argv[1])}
    except Fail as e:
        result = {"ok": False, "error": e.args[0]}
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)
