"""Compact human-readable transcript of a run: each agent's steps and messages, then board activity."""
import json
from collections.abc import Iterator
from pathlib import Path

from .jsonl import read_json

BOARD_EVENT_KINDS = ("post", "upvote", "irc_join", "dm", "delete")


def agent_header(adir: Path) -> str:
    s = read_json(adir / "summary.json", {})
    return (f"=== {adir.name}: solved={s.get('solved')} at={s.get('solve_step')} steps={s.get('steps_used')} "
            f"ended={s.get('ended')} tok={s.get('tokens')}")


def step_lines(r: dict) -> Iterator[str]:
    """Lines for one steps.jsonl record (most kinds print nothing)."""
    if r["kind"] == "step":
        if r.get("idle"):
            yield f"#{r['step']:>2} [{r['phase']}] (idle turn)"
        else:
            yield f"#{r['step']:>2} [{r['phase']}] $ {r['command'][:160]!r}\n      -> {r['output'][:180]!r}"
    elif r["kind"] == "model" and (r.get("content") or "").strip():
        yield "   MSG: " + r["content"][:220].replace("\n", " ")
    elif r["kind"] in ("api_retry", "crash"):
        yield f"   !! {r}"


def agent_lines(adir: Path) -> Iterator[str]:
    yield agent_header(adir)
    with open(adir / "steps.jsonl") as f:
        for line in f:
            yield from step_lines(json.loads(line))


def board_lines(events_path: Path) -> Iterator[str]:
    if not events_path.exists():
        return
    with open(events_path) as f:
        for line in f:
            r = json.loads(line)
            if r["kind"] in BOARD_EVENT_KINDS:
                yield (f"BOARD a{r['agent']} {r['kind']}: {r.get('author') or r.get('nick')} -> "
                       f"{r.get('thread') or r.get('chan') or ''}: {str(r.get('body', ''))[:200]!r}")


def run_transcript(run: Path) -> Iterator[str]:
    """Agents in a*/ glob order, then board events from board_events.jsonl."""
    for adir in sorted(run.glob("a*/")):
        yield from agent_lines(adir)
    yield from board_lines(run / "board_events.jsonl")
