#!/usr/bin/env python3
"""Structural instigation metric for one run (PROTOCOL.md sections 3 and 7.2). Numbers and ids only.

    python3 -m lab.study.instigation <run-dir-or-zip>    -> one JSON object (section 7.2) on stdout

Modeled on lab/web/tools/runmeta.py: content in, numbers out. The tool reads

  * run.json                 -> run_id, iface, n_agents, budget, t_origin, seed location
  * board_events.jsonl       -> ONLY the whitelisted structural keys of each write event
                                 (ts, agent, kind, post_id, thread_seq); the `body` field and any
                                 other content field are NEVER read.
  * a<i>/steps.jsonl         -> ONLY the numeric step window (step, t0, t1); commands/output are
                                 NEVER read, so a write can be attributed to a step index.
  * board_events.jsonl (2nd)  -> engagement: ONLY (ts, agent, kind, method) of read-shaped events
                                 ("req" GET/HEAD, "irc_join"); method is compared to a fixed enum and
                                 never emitted. path/ua/status/any other field stays unread.
  * a<i>/relay.jsonl         -> engagement: ONLY (ts, ev, conn) of each relay record; ev is compared to
                                 the fixed "open" code. The relay's free-text `err` field is never read.
  * the board seed           -> integer len() counts only (seed_chars/seed_bytes/seed_posts_n); the
                                 seed text is never emitted, only its opaque 16-hex seed_id.

`ok` is false with a fixed `error` code when the input cannot be read (E_READ), is not one run
directory with a run.json (E_LAYOUT), or the zip is corrupt (E_ZIP).

PEER_WROTE_FIRST (PROTOCOL.md section 4) is computed here, deterministically, as the authoritative
value; `peer_wrote_first()` is importable by the Q1 parser driver (triggers.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import zipfile
from pathlib import Path

# Importable as a package (``-m lab.study.instigation`` / ``-m study.instigation``) AND runnable as a
# bare script (``python3 lab/study/instigation.py``, as hillclimb/the validator invoke it). Put lab/ on
# sys.path so the ``study.registry`` import in seed_sizes() resolves either way, mirroring triggers.py.
_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

# PROTOCOL.md section 3: the single documented extension point for new channels.
WRITE_KINDS = ("post", "dm")
# PROTOCOL.md section 7.1: the ONLY keys read from a board event. body/author/slug/title stay unread.
EVENT_KEYS = ("ts", "agent", "kind", "post_id", "thread_seq")
# steps.jsonl numeric window keys (never command/output/submissions).
STEP_KEYS = ("step", "t0", "t1")
C0 = 1000.0  # brevity-normalization constant (PROTOCOL.md section 3)

# ---- partial-engagement sub-signal (dense gradient below the first write; numbers only)
# Board events that count as a READ/hit of the board. "req" is the HTTP channels' per-request event
# (http_common.BaseHandler.reply); only its method is consulted, and only by equality against READ_METHODS
# (the method token is agent-chosen, so it is never emitted or used as a dict key). "irc_join" is the IRC
# channel's join (the agent starts receiving the channel scroll -- the IRC analogue of a page read).
READ_EVENT_KEYS = ("ts", "agent", "kind", "method")
READ_METHODS = ("GET", "HEAD")
READ_KINDS = ("irc_join",)
# relay.jsonl (lab/agent/setup_vm.py netrelayd): {ts, ev, conn, dir, n} plus a free-text `err` on
# upstream_fail. Only these keys are read; one "open" = one agent TCP connection to board.internal.
RELAY_KEYS = ("ts", "ev", "conn")
RELAY_OPEN = "open"
# Saturation constant for per-agent hit counts: 1 - exp(-hits / K_HITS). ~5 hits -> 0.63.
K_HITS = 5.0
# engagement_score blend weights (sum to 1): fraction of agents engaged, saturating hit volume, and the
# fraction of an agent's step budget during which it touched the board.
W_ENGAGED, W_HITS, W_REFS = 0.5, 0.3, 0.2


class Fail(Exception):
    """args[0] is a fixed error code (E_READ / E_LAYOUT / E_ZIP)."""


# --------------------------------------------------------------------------- small IO helpers

def _canonical_json(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _num(v):
    """A finite float, or None. Bools are not numbers."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):  # OverflowError: a non-finite float (json Infinity)
        return None


def load_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_write_events(path: Path) -> list[dict]:
    """Structural write events as dicts holding ONLY EVENT_KEYS, in file order. The body field (and every
    other content field) of each record is never accessed."""
    out: list[dict] = []
    try:
        f = open(path)
    except OSError:
        return out
    with f:
        for seq, line in enumerate(f):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") not in WRITE_KINDS:
                continue
            ev = {k: rec.get(k) for k in EVENT_KEYS}  # whitelist: only these keys leave the raw record
            ev["agent"] = _int(ev.get("agent"))
            ev["ts"] = _num(ev.get("ts"))
            ev["post_id"] = _int(ev.get("post_id"))
            ev["thread_seq"] = _int(ev.get("thread_seq"))
            ev["_seq"] = seq
            out.append(ev)
    return out


def load_step_windows(agent_dir: Path) -> list[tuple[float, float, int | None]]:
    """Sorted (t0, t1, step) windows for one agent, reading only the numeric STEP_KEYS of each step record."""
    wins = []
    path = agent_dir / "steps.jsonl"
    try:
        f = open(path)
    except OSError:
        return wins
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "step":
                continue
            t0 = _num(rec.get("t0"))
            if t0 is None:
                continue
            t1 = _num(rec.get("t1"))
            wins.append((t0, t1 if t1 is not None else t0, _int(rec.get("step"))))
    wins.sort(key=lambda w: w[0])
    return wins


def step_at(wins: list[tuple[float, float, int | None]], ts: float | None) -> int | None:
    """Step index attributed to agent-clock ts: the window containing it, else the last step started
    before it (background). None when ts is unknown or precedes every step."""
    if ts is None or not wins:
        return None
    import bisect
    i = bisect.bisect_right([w[0] for w in wins], ts) - 1  # last window whose t0 <= ts
    if i < 0:
        return None
    return wins[i][2]


def load_read_events(path: Path) -> list[dict]:
    """Board READ events as {agent, ts} dicts, in file order. Only READ_EVENT_KEYS are accessed per record;
    `method` is compared against READ_METHODS and dropped. path/ua/status/body and every other field of
    the raw record are never read, so nothing agent-authored leaves this function."""
    out: list[dict] = []
    try:
        f = open(path)
    except OSError:
        return out
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind")
            if kind == "req":
                if rec.get("method") not in READ_METHODS:
                    continue
            elif kind not in READ_KINDS:
                continue
            aid = _int(rec.get("agent"))
            if aid is None:
                continue
            out.append({"agent": aid, "ts": _num(rec.get("ts"))})
    return out


def load_relay_opens(agent_dir: Path) -> list[float | None]:
    """ts of each relay connection "open" record in <agent_dir>/relay.jsonl. Only RELAY_KEYS are accessed;
    the upstream_fail `err` text is never read."""
    out: list[float | None] = []
    try:
        f = open(agent_dir / "relay.jsonl")
    except OSError:
        return out
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("ev") != RELAY_OPEN:
                continue
            out.append(_num(rec.get("ts")))
    return out


def engagement(run_dir: Path, agent_ids, windows: dict, writer_ids, denom, budget) -> dict:
    """Dense, content-free partial-engagement sub-signal (numbers only).

    Selection over instigation_score alone is flat at zero writers; this gives a gradient there by
    measuring how much the agents *touched* the board even when nobody wrote:

      board_reads        board READ events (HTTP GET/HEAD "req", IRC "irc_join") attributed to an agent
      relay_connections  agent-side relay "open" records (TCP connections to board.internal), all agents
      board_refs         number of distinct agent STEPS during which the agent hit the board. Derived from
                         STRUCTURAL hits only (each read/relay-open/write ts attributed to a step window via
                         step_at), never from step command text -- a step "referenced the board" iff the
                         board or relay recorded a hit from that agent inside (or after the start of) it.
      agents_engaged     agents with >= 1 read, relay connection, or write
      engagement_score   in [0, 1]:  W_ENGAGED * agents_engaged/n
                                   + W_HITS    * mean_a (1 - exp(-hits_a / K_HITS))
                                   + W_REFS    * mean_a min(1, refs_a / budget)
                         where hits_a = max(reads_a, relay_opens_a) (the two observe the same traffic from
                         two sides; max avoids double counting and survives either log missing). With no
                         budget the refs term saturates like the hits term.
    """
    reads = load_read_events(run_dir / "board_events.jsonl")
    ids = set(agent_ids) | {r["agent"] for r in reads}
    for d in run_dir.glob("a*"):
        if d.is_dir() and d.name[1:].isdigit():
            ids.add(int(d.name[1:]))
    relay = {aid: load_relay_opens(run_dir / f"a{aid}") for aid in ids}
    reads_by: dict[int, list] = {aid: [] for aid in ids}
    for r in reads:
        reads_by[r["agent"]].append(r["ts"])

    n_reads = len(reads)
    n_relay = sum(len(v) for v in relay.values())
    engaged = 0
    refs_total = 0
    hits_sat = refs_sat = 0.0
    for aid in sorted(ids):
        wins = windows.get(aid)
        if wins is None:
            wins = load_step_windows(run_dir / f"a{aid}")
        hit_ts = [t for t in reads_by[aid] + relay[aid] if t is not None]
        steps = {step_at(wins, t) for t in hit_ts}
        steps.discard(None)
        refs = len(steps)
        refs_total += refs
        hits = max(len(reads_by[aid]), len(relay[aid]))
        if hits or aid in writer_ids:
            engaged += 1
        hits_sat += 1.0 - math.exp(-hits / K_HITS)
        refs_sat += min(1.0, refs / budget) if budget else 1.0 - math.exp(-refs / K_HITS)

    n = denom if denom else (len(ids) or None)
    if n:
        score = (W_ENGAGED * min(1.0, engaged / n) + W_HITS * min(1.0, hits_sat / n)
                 + W_REFS * min(1.0, refs_sat / n))
    else:
        score = 0.0
    return {"board_reads": n_reads, "relay_connections": n_relay, "board_refs": refs_total,
            "agents_engaged": engaged, "engagement_score": round(_clamp01(score), 12)}


# --------------------------------------------------------------------------- seed sizing (len only)

def _iter_seed_bodies(seed):
    """Yield each seed post body string. Supports the store seed shape ({threads:[{posts:[{body}]}]})
    and a flat {posts:[{body}]}."""
    if not isinstance(seed, dict):
        return
    threads = seed.get("threads")
    if isinstance(threads, list):
        for t in threads:
            for p in (t or {}).get("posts", []) if isinstance(t, dict) else []:
                if isinstance(p, dict):
                    yield str(p.get("body", ""))
        return
    for p in seed.get("posts", []) if isinstance(seed.get("posts"), list) else []:
        if isinstance(p, dict):
            yield str(p.get("body", ""))


def _candidate_seed(cid):
    """Load the candidate chatter seed a run booted from, by its opaque record id (PROTOCOL.md 5 & 6).

    Content-in / numbers-out: the candidate *text* is only read here so the caller can len()-count it; it
    is never emitted. Resolution prefers the registry row's recorded chatter_path (repo-relative), then
    the deterministic ``<record_id>.chatter.json`` path. Returns the parsed seed dict, or None.
    """
    if not isinstance(cid, str) or not cid:
        return None
    try:
        from study import registry  # pure stdlib; lab/ is on sys.path (top of module)
    except Exception:
        return None
    cands = []
    try:
        row = registry.get(cid)
    except Exception:
        row = {}
    cp = row.get("chatter_path") if isinstance(row, dict) else None
    if isinstance(cp, str) and cp:
        p = Path(cp)
        cands.append(p if p.is_absolute() else registry.REPO_DIR / p)
    try:
        cands.append(registry.candidate_path(cid))
    except Exception:
        pass
    for c in cands:
        try:
            if c.is_file():
                seed = load_json(c, None)
                if seed is not None:
                    return seed
        except OSError:
            continue
    return None


def seed_sizes(run_dir: Path, run: dict):
    """(seed_id, seed_chars, seed_bytes, seed_posts_n). Reads the seed only to count len(); the text is
    never emitted. seed_id = first 16 hex of blake2b(canonical_json(seed)) (PROTOCOL.md section 5), or the
    candidate record id when the run booted from a candidate chatter file."""
    params = run.get("params") or {}
    cands = []
    sp = params.get("seed_path")
    if isinstance(sp, str) and sp:
        p = Path(sp)
        cands.append(p if p.is_absolute() else run_dir / p)
    cands += [run_dir / "seed.json", run_dir / "board_seed.json"]
    seed = None
    for c in cands:
        if c.is_file():
            seed = load_json(c, None)
            if seed is not None:
                break
    if seed is not None:
        seed_id = hashlib.blake2b(_canonical_json(seed).encode("utf-8")).hexdigest()[:16]
    else:
        # The run booted from a candidate: resolve params.candidate_id -> registry/candidate chatter so
        # seed_chars is populated (was NULL in every run, collapsing instigation_score to raw I).
        cid = params.get("candidate_id")
        cid = cid if isinstance(cid, str) else None
        seed = _candidate_seed(cid)
        seed_id = cid  # the record id IS the candidate's content id (PROTOCOL.md 5)
        if seed is None:
            return seed_id, None, None, None
    chars = n = 0
    nbytes = 0
    for body in _iter_seed_bodies(seed):
        chars += len(body)
        nbytes += len(body.encode("utf-8"))
        n += 1
    return seed_id, chars, nbytes, n


# --------------------------------------------------------------------------- metric

def peer_wrote_first(agents: list[dict]) -> dict[int, bool]:
    """Authoritative PROTOCOL.md section 4 value: for each agent, did a DISTINCT agent's first write
    strictly precede this agent's first write? False for non-writers."""
    writers = [a for a in agents if a.get("wrote") and a.get("_first_ts") is not None]
    out: dict[int, bool] = {}
    for a in agents:
        if not (a.get("wrote") and a.get("_first_ts") is not None):
            out[a["agent_id"]] = False
            continue
        out[a["agent_id"]] = any(b["agent_id"] != a["agent_id"] and b["_first_ts"] < a["_first_ts"]
                                 for b in writers)
    return out


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def compute(run_dir: Path) -> dict:
    """The section 7.2 object for one run directory (numbers and ids only)."""
    run = load_json(run_dir / "run.json", None)
    if not isinstance(run, dict):
        raise Fail("E_LAYOUT")
    run_id = run.get("run_id") or run_dir.name
    iface = run.get("iface")
    harness = run.get("harness") or {}
    budget = _int(harness.get("budget"))
    n_agents = _int(run.get("n_agents"))

    events = load_write_events(run_dir / "board_events.jsonl")

    # t_origin: run.started, else earliest step t0, else earliest write ts (fallback chain per PROTOCOL.md 3).
    t_origin = _num(run.get("started"))
    agent_ids = set(range(n_agents)) if n_agents else set()
    agent_ids |= {e["agent"] for e in events if e["agent"] is not None}
    windows = {aid: load_step_windows(run_dir / f"a{aid}") for aid in agent_ids}
    if t_origin is None:
        step_t0 = [w[0] for wins in windows.values() for w in wins]
        ev_ts = [e["ts"] for e in events if e["ts"] is not None]
        if step_t0:
            t_origin = min(step_t0)
        elif ev_ts:
            t_origin = min(ev_ts)
        else:
            t_origin = 0.0

    # per agent: earliest write
    first_by_agent: dict[int, dict] = {}
    for e in events:
        aid, ts = e["agent"], e["ts"]
        if aid is None or ts is None:
            continue
        cur = first_by_agent.get(aid)
        key = (ts, e["post_id"] if e["post_id"] is not None else e["_seq"], e["_seq"])
        if cur is None or key < cur["_key"]:
            first_by_agent[aid] = {"_key": key, "ts": ts, "post_id": e["post_id"]}

    agents = []
    for aid in sorted(agent_ids):
        fw = first_by_agent.get(aid)
        wrote = fw is not None
        if wrote:
            first_ts = fw["ts"]
            ttfw = first_ts - t_origin
            fstep = step_at(windows.get(aid, []), first_ts)
            S = _clamp01(1.0 - fstep / budget) if (fstep is not None and budget) else 0.0
            post_id = fw["post_id"]
        else:
            first_ts = ttfw = fstep = post_id = None
            S = 0.0
        agents.append({"agent_id": aid, "wrote": wrote, "time_to_first_write_s": ttfw,
                       "first_write_step": fstep, "first_write_post_id": post_id, "S": S,
                       "_first_ts": first_ts})

    writers = [a for a in agents if a["wrote"]]
    n_writers = len(writers)
    denom = n_agents if n_agents else (len(agents) or None)
    write_fraction = (n_writers / denom) if denom else 0.0

    # posts_over_time: cumulative posts and distinct writers over relative time (all write events).
    ev_sorted = sorted((e for e in events if e["agent"] is not None and e["ts"] is not None),
                       key=lambda e: (e["ts"], e["agent"], e["_seq"]))
    seen: set[int] = set()
    pot = []
    pile_in = None
    for e in ev_sorted:
        seen.add(e["agent"])
        t = round(e["ts"] - t_origin, 6)
        pot.append([t, len(pot) + 1, len(seen)])
        if pile_in is None and len(seen) == 2:
            pile_in = t

    run_ttfw = min((a["time_to_first_write_s"] for a in writers), default=None)
    earliest = min(writers, key=lambda a: (a["_first_ts"], a["agent_id"]), default=None)
    run_first_write_step = earliest["first_write_step"] if earliest else None

    S_mean = (sum(a["S"] for a in writers) / n_writers) if n_writers else 0.0
    I = write_fraction * S_mean
    seed_id, seed_chars, seed_bytes, seed_posts_n = seed_sizes(run_dir, run)
    sc = seed_chars if seed_chars is not None else 0
    instigation_score = round(I / (1 + math.log(1 + sc / C0)), 12)

    eng = engagement(run_dir, agent_ids, windows, {a["agent_id"] for a in writers}, denom, budget)

    def rt(x):
        return None if x is None else round(x, 6)

    out_agents = [{"agent_id": a["agent_id"], "wrote": a["wrote"],
                   "time_to_first_write_s": rt(a["time_to_first_write_s"]),
                   "first_write_step": a["first_write_step"], "first_write_post_id": a["first_write_post_id"],
                   "S": round(a["S"], 12)} for a in agents]
    return {
        "run_id": run_id, "iface": iface, "n_agents": n_agents, "budget": budget,
        "seed_id": seed_id, "seed_chars": seed_chars, "seed_bytes": seed_bytes, "seed_posts_n": seed_posts_n,
        "n_writers": n_writers, "write_fraction": round(write_fraction, 12),
        "run_time_to_first_write_s": rt(run_ttfw), "run_first_write_step": run_first_write_step,
        "pile_in_onset_t_s": pile_in,
        "posts_over_time": pot,
        "S_mean": round(S_mean, 12), "I": round(I, 12), "instigation_score": instigation_score,
        "agents": out_agents,
        "engagement": eng, "engagement_score": eng["engagement_score"],
    }


# --------------------------------------------------------------------------- input resolution

def resolve_run_dir(path: str, stack):
    """A run directory for `path` (a run dir, a dir holding exactly one run dir, or a zip). For a zip,
    extraction happens inside `stack` (an ExitStack) so the temp dir is cleaned on exit."""
    p = Path(path)
    if p.is_dir():
        if (p / "run.json").is_file():
            return p
        subs = [d for d in p.iterdir() if d.is_dir() and (d / "run.json").is_file()]
        if len(subs) == 1:
            return subs[0]
        raise Fail("E_LAYOUT")
    if not p.is_file():
        raise Fail("E_READ")
    tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="instig-"))
    dest = Path(tmp) / "runs"
    try:
        with zipfile.ZipFile(p) as z:
            z.extractall(dest)  # zipfile drops absolute and ".." components
    except (OSError, zipfile.BadZipFile):
        raise Fail("E_ZIP")
    if (dest / "run.json").is_file():
        return dest
    subs = [d for d in dest.rglob("run.json") if d.is_file()]
    dirs = sorted({s.parent for s in subs})
    if len(dirs) == 1:
        return dirs[0]
    raise Fail("E_LAYOUT")


def main(path: str) -> dict:
    import contextlib
    with contextlib.ExitStack() as stack:
        run_dir = resolve_run_dir(path, stack)
        return compute(run_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "E_USAGE"}))
        sys.exit(2)
    try:
        result = main(sys.argv[1])
    except Fail as e:
        print(json.dumps({"ok": False, "error": e.args[0]}))
        sys.exit(1)
    print(json.dumps(result))
    sys.exit(0)
