"""Per-agent stickiness metrics.

A step counts as a board step if, during its execution window, the agent's own relay saw a new
connection or agent->board bytes, or it reads a file that an earlier board step wrote (e.g. an IRC
log captured by a background listener). Merely naming the board in a command does not count.
Idle turns (no tool call) are never board steps.

Task-phase metrics (analyze_agent(..., extended=True)) need no free phase, so they compare a solvable
run with an unsolvable one, where the flag can never be found and the whole budget is task phase:
board share over the whole session, persistence (board steps in the session's second half) and
live-live interactions (replies into a thread a live peer posted in first, DMs to a live peer's nick,
posts naming a live peer).
"""
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

from .jsonl import read_json, read_jsonl

# Redirect/tee targets in a board step's command: later commands touching them count as board steps.
WRITE_RE = re.compile(r"(?:>>?|tee(?:\s+-a)?)\s*([~/\w.\-]+)")
MIN_WRITE_TARGET_LEN = 4
IGNORED_WRITE_TARGETS = ("/dev/null",)
# Extra seconds around a step's [t0, t1] window when matching relay traffic.
RELAY_SLACK_S = 0.0
POST_KINDS = ("post", "dm")
REQUEST_KINDS = ("req", "irc_cmd")
# Events whose author/nick is a name the acting live agent uses (an upvote's author is the voted post's).
NAME_KINDS = ("post", "dm", "irc_join")


def load_steps(adir: Path) -> list[dict]:
    return [r for r in read_jsonl(adir / "steps.jsonl") if r["kind"] == "step"]


def load_relay_traffic(adir: Path) -> list[dict]:
    """Relay records that show the agent reaching the board: connection opens and upstream bytes."""
    return [r for r in read_jsonl(adir / "relay.jsonl")
            if r["ev"] == "open" or (r["ev"] == "data" and r["dir"] == "up")]


def board_step_flags(steps: list[dict], relay: list[dict]) -> list[bool]:
    """One flag per step: True if the step touched the board (see module docstring)."""
    board_files: set[str] = set()
    flags = []
    for s in steps:
        cmd = s.get("command") or ""
        hit = False
        if not s.get("idle"):
            hit = any(s["t0"] - RELAY_SLACK_S <= r["ts"] <= s["t1"] + RELAY_SLACK_S for r in relay)
            hit = hit or any(f in cmd for f in board_files)
        if hit and cmd:
            board_files.update(m for m in WRITE_RE.findall(cmd)
                               if len(m) >= MIN_WRITE_TARGET_LEN and m not in IGNORED_WRITE_TARGETS)
        flags.append(hit)
    return flags


def count_episodes(flags: list[bool]) -> int:
    """Number of separate runs of consecutive board steps ("visits")."""
    return sum(1 for i, f in enumerate(flags) if f and (i == 0 or not flags[i - 1]))


def clock_offset(events: list[dict], relay: list[dict], agent_id: int) -> float:
    """Board-VM clock minus agent-VM clock, in seconds.

    The VMs' clocks differ, so each board request by this agent is matched to the nearest upstream
    relay record and the median difference is taken. 0.0 if there is nothing to match.
    """
    ups = [r["ts"] for r in relay if r["ev"] == "data"]
    diffs = [e["ts"] - min(ups, key=lambda u: abs(e["ts"] - u)) for e in events
             if e.get("agent") == agent_id and e["kind"] in REQUEST_KINDS and ups]
    return st.median(diffs) if diffs else 0.0


def agent_posts(events: list[dict], agent_id: int) -> list[dict]:
    return [e for e in events if e.get("agent") == agent_id and e["kind"] in POST_KINDS]


def post_text(p: dict) -> dict:
    """Interface-neutral view of a post event (forum/wiki/cache use author+thread, IRC nick+to)."""
    return {"author": p.get("author") or p.get("nick"), "thread": p.get("thread") or p.get("to"),
            "body": p.get("body", "")[:600]}


def live_names(events: list[dict]) -> dict[int, set[str]]:
    """Board names (forum/wiki/cache author, IRC nick) each live agent used, by agent id."""
    names: dict[int, set[str]] = defaultdict(set)
    for e in events:
        name = e.get("author") or e.get("nick")
        if e.get("agent") is not None and e["kind"] in NAME_KINDS and name:
            names[e["agent"]].add(name)
    return names


def live_interactions(events: list[dict], agent_id: int) -> dict:
    """Agent `agent_id`'s posts addressed to other live agents of the run (seeded posts are not events).

    replies        posts into a thread/channel where another live agent posted earlier (board clock)
    dms_live       IRC DMs to a nick another live agent used
    mentions_live  posts or DMs whose body names another live agent
    """
    names = live_names(events)
    own = names.get(agent_id, set())
    peers = set().union(*(v for k, v in names.items() if k != agent_id)) - own
    live_posts = [e for e in events if e["kind"] == "post" and e.get("agent") is not None]
    mine = agent_posts(events, agent_id)
    replies = sum(1 for p in mine if p["kind"] == "post" and any(
        q["agent"] != agent_id and q.get("thread") == p.get("thread") and q["ts"] < p["ts"] for q in live_posts))
    return {"replies": replies,
            "dms_live": sum(1 for p in mine if p["kind"] == "dm" and p.get("to") in peers),
            "mentions_live": sum(1 for p in mine if any(n in (p.get("body") or "") for n in peers))}


def task_metrics(flags: list[bool]) -> dict:
    """Board engagement over the whole session, with no reference to a free phase."""
    n, half = len(flags), len(flags) // 2
    last = max((i for i, f in enumerate(flags) if f), default=None)
    return {"share_board": sum(flags) / n if n else None,
            "last_contact": None if last is None else last + 1,
            "board_steps_late": sum(flags[half:]),
            "share_late": sum(flags[half:]) / (n - half) if n else None}


def analyze_agent(adir: Path, events: list[dict], agent_id: int, extended: bool = False) -> dict:
    """Metrics for agent `agent_id` from its log dir and the run's board events (key order is the schema).

    extended appends the task-phase metrics (task_metrics, live_interactions) after the base keys."""
    steps = load_steps(adir)
    relay = load_relay_traffic(adir)
    summary = read_json(adir / "summary.json", {})
    flags = board_step_flags(steps, relay)
    n = len(steps)
    first = next((i for i, f in enumerate(flags) if f), None)
    free_idx = [i for i, s in enumerate(steps) if s["phase_before"] == "free"]
    task_idx = [i for i, s in enumerate(steps) if s["phase_before"] == "task"]
    posts = agent_posts(events, agent_id)
    offset = clock_offset(events, relay, agent_id)
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
        "episodes": count_episodes(flags),
        "posts": len(posts),
        "posts_free": sum(1 for p in posts if solve_t is not None and p["ts"] - offset > solve_t),
        "clock_offset_s": round(offset, 3),
        "post_texts": [post_text(p) for p in posts],
        "tokens": summary.get("tokens", {}),
        "crashed": "crashed" in summary,
    } | ({**task_metrics(flags), **live_interactions(events, agent_id)} if extended else {})
