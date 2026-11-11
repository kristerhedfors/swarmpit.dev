"""Pure parsers: run_id grammar, JSONL line splitter, record normalizers (CONTRACT section 3.2).

Every normalizer takes a decoded JSON record plus its identity (run_id, agent_id, line_off, seq, src) and
returns {table: [row dicts]}. They are shared by the file ingester and the live tap so that both produce
identical natural keys.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# '-unsolvable' right after the rep marks the unsolvable-goal condition, '--<slug>' names a non-default model
# (lab/config.MODELS slug) and the tag follows them (lab make_run_id / config.GOAL_SEGMENT). Ids without the goal
# segment are goal 'solvable'. VM names leave the goal segment out (config.vm_run_id: they become guest hostnames).
# The iface is any lowercase-letter name (config.IFACE_RE): pilot channels and opt-in extras (guestbook/paste/kv/js/html).
GOALS = ("solvable", "unsolvable")
DEFAULT_GOAL = "solvable"
GOAL_SEGMENT = {"solvable": "", "unsolvable": "-unsolvable"}
RUN_RE = re.compile(r"^(?P<iface>[a-z]+)-(?P<pop>solo|multi(?P<n>\d+))-r(?P<rep>\d+)"
                    r"(?P<goal>-unsolvable)?(?:--(?P<mslug>[a-z0-9]{2,12}))?(?:-(?P<tag>[A-Za-z0-9_-]+))?$")
VM_AGENT_RE = re.compile(r"-a(\d+)$")
VM_RE = re.compile(r"^sb-(?P<run>.+)-(?P<role>board|a(?P<idx>\d+))$")
PILOT_RE = [
    ("starting", re.compile(r"^\[run\] (?P<run>\S+): starting board \+ (?P<n>\d+) agent")),
    ("agents_running", re.compile(r"^\[run\] (?P<run>\S+): agents running")),
    ("agent_done", re.compile(r"^\[run\] (?P<run>\S+): (?P<vm>\S+) done\s*$")),
    ("finished", re.compile(r"^\[run\] (?P<run>\S+): finished in (?P<secs>[\d.]+)s")),
    ("skip", re.compile(r"^\[skip\] (?P<run>\S+) already done")),
    ("error", re.compile(r"^\[error\] (?P<iface>\w+) n=(?P<n>\d+) rep=(?P<rep>\d+): ?(?P<msg>.*)$")),
    ("error", re.compile(r"^\[error\] (?P<run>\S+): ?(?P<msg>.*)$")),  # runs with a non-default model / port lane
    ("abort", re.compile(r"^\[abort\] (?P<run>\S+): (?P<msg>.*)$")),
    ("budget_stop", re.compile(r"^\[budget\] stop (?P<msg>.*)$")),
]


def parse_run_id(run_id: str) -> dict:
    m = RUN_RE.match(run_id)
    if not m:
        return {"iface": None, "pop": None, "n_agents": None, "rep": None, "tag": "", "model_slug": "",
                "goal": None, "matched": False}
    n = 1 if m["pop"] == "solo" else int(m["n"])
    return {"iface": m["iface"], "pop": "solo" if n == 1 else "multi", "n_agents": n, "rep": int(m["rep"]),
            "tag": m["tag"] or "", "model_slug": m["mslug"] or "",
            "goal": "unsolvable" if m["goal"] else DEFAULT_GOAL, "matched": True}


def vm_run_id(run_id: str) -> str:
    """The run id without its goal segment: what the run's VM names carry (identity for solvable ids)."""
    m = RUN_RE.match(run_id)
    if not (m and m["goal"]):
        return run_id
    return run_id[:m.start("goal")] + run_id[m.end("goal"):]


def vm_run_candidates(vm_run: str) -> list[str]:
    """Run ids whose VMs are named after `vm_run` (the run part of 'sb-<run>-a<i>'): one per goal."""
    p = parse_run_id(vm_run)
    if not p["matched"] or p["goal"] != DEFAULT_GOAL:
        return [vm_run]
    return [vm_run] + [run_id_for(p["iface"], p["n_agents"], p["rep"], p["tag"], p["model_slug"], g)
                       for g in GOALS if g != DEFAULT_GOAL]


def run_id_for(iface: str, n: int, rep: int, tag: str = "", slug: str = "", goal: str = DEFAULT_GOAL) -> str:
    if goal not in GOAL_SEGMENT:
        raise ValueError(f"goal must be one of {GOALS}")
    return (f"{iface}-{'solo' if n == 1 else f'multi{n}'}-r{rep}{GOAL_SEGMENT[goal]}{('--' + slug) if slug else ''}"
            f"{('-' + tag) if tag else ''}")


def run_goal(params: dict | None) -> str:
    """A run's goal from runs.params (absent = the historical solvable CTF)."""
    g = (params or {}).get("goal")
    return g if isinstance(g, str) and g else DEFAULT_GOAL


def vm_agent_id(name: str) -> int | None:
    m = VM_AGENT_RE.search(name or "")
    return int(m.group(1)) if m else None


def canonical_json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha1_hex(s: str | bytes) -> str:
    return hashlib.sha1(s.encode() if isinstance(s, str) else s).hexdigest()


def split_lines(buf: bytes, base_off: int) -> tuple[list[tuple[int, bytes]], int]:
    """Split on b'\\n', keep only complete lines. Returns ([(line_off, line_bytes_without_nl)], consumed)."""
    out = []
    pos = 0
    n = len(buf)
    while pos < n:
        j = buf.find(b"\n", pos)
        if j < 0:
            break
        out.append((base_off + pos, buf[pos:j]))
        pos = j + 1
    return out, pos


def parse_pilot_line(line: str) -> dict | None:
    line = line.rstrip("\r\n")
    for kind, rx in PILOT_RE:
        m = rx.match(line)
        if not m:
            continue
        d = m.groupdict()
        if kind in ("error", "abort", "budget_stop"):
            if d.get("run"):
                run = d["run"]
            elif kind == "error":
                run = run_id_for(d["iface"], int(d["n"]), int(d["rep"]))
            else:  # budget stop: a lane-level event, not a run
                run = "_pilot"
            return {"kind": kind, "run_id": run, "agent_id": None, "detail": d.get("msg"), "data": d}
        agent = vm_agent_id(d.get("vm") or "") if kind == "agent_done" else None
        detail = d.get("secs") or d.get("n") or d.get("vm")
        return {"kind": kind, "run_id": d["run"], "agent_id": agent, "detail": detail, "data": d}
    return None


def _f(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _s(v) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


# --------------------------------------------------------------------------- steps.jsonl

def norm_steps_record(rec: dict, run_id: str, agent_id: int, line_off: int, seq: int, src: str,
                      flag: str | None) -> dict[str, list[dict]]:
    kind = rec.get("kind")
    base = {"run_id": run_id, "agent_id": agent_id, "line_off": line_off, "seq": seq, "src": src}
    if kind == "step":
        subs = rec.get("submissions") or []
        if not isinstance(subs, list):
            subs = [subs]
        sub_rows, parsed = [], []
        for k, s in enumerate(subs):
            s = str(s)
            ep, _, val = s.partition("\t")
            if not _:
                ep, val = None, s
            correct = bool(flag) and flag in val
            parsed.append({"ts": _i(ep), "value": val, "correct": correct})
            sub_rows.append({"run_id": run_id, "agent_id": agent_id, "line_off": -(1000 * (_i(rec.get("step")) or 0) + k),
                             "ts": _f(ep), "value": val, "correct": correct, "step": _i(rec.get("step")), "src": src})
        out = rec.get("output")
        cmd = rec.get("command")
        row = {**base, "step": _i(rec.get("step")), "t0_raw": _f(rec.get("t0")), "t1_raw": _f(rec.get("t1")),
               "ts": _f(rec.get("t0")),
               "dur_s": (_f(rec.get("t1")) - _f(rec.get("t0"))) if _f(rec.get("t1")) is not None and _f(rec.get("t0")) is not None else None,
               "phase_before": rec.get("phase_before"), "phase": rec.get("phase"),
               "is_solve": rec.get("phase_before") == "task" and rec.get("phase") == "free",
               "idle": bool(rec.get("idle", False)), "command": cmd, "exit": _i(rec.get("exit")),
               "output": _s(out), "output_len": len(out) if isinstance(out, str) else 0,
               "submissions": parsed, "submit_correct": any(p["correct"] for p in parsed) if parsed else None}
        return {"steps": [row], "submissions": sub_rows}
    if kind == "model":
        u = rec.get("usage")
        u = u if isinstance(u, dict) else {}
        calls = rec.get("tool_calls") or []
        calls = calls if isinstance(calls, list) else []
        tc_rows = []
        for idx, c in enumerate(calls):
            c = c if isinstance(c, dict) else {}
            fn = c.get("function")
            fn = fn if isinstance(fn, dict) else {}
            args = fn.get("arguments")
            valid, command = False, None
            if isinstance(args, str):
                try:
                    a = json.loads(args)
                    if isinstance(a, dict):
                        valid = True
                        command = a.get("command")
                        command = command if isinstance(command, str) else (None if command is None else json.dumps(command))
                except ValueError:
                    pass
            elif isinstance(args, dict):
                valid, command = True, args.get("command")
                args = json.dumps(args)
            tc_rows.append({"run_id": run_id, "agent_id": agent_id, "call": _i(rec.get("call")), "idx": idx,
                            "tool_call_id": (c or {}).get("id"), "name": fn.get("name"), "arguments": _s(args),
                            "args_valid": valid, "command": command})
        ctd = u.get("completion_tokens_details")
        ctd = ctd if isinstance(ctd, dict) else {}
        ptd = u.get("prompt_tokens_details")
        ptd = ptd if isinstance(ptd, dict) else {}
        # some providers report reasoning_tokens top-level instead of in completion_tokens_details (0 stays 0)
        rt = ctd.get("reasoning_tokens")
        rt = u.get("reasoning_tokens") if rt is None else rt
        row = {**base, "call": _i(rec.get("call")), "ts_raw": _f(rec.get("ts")), "ts": _f(rec.get("ts")),
               "phase": rec.get("phase"), "content": _s(rec.get("content")), "reasoning": _s(rec.get("reasoning")),
               "n_tool_calls": len(calls), "prompt_tokens": _i(u.get("prompt_tokens")),
               "completion_tokens": _i(u.get("completion_tokens")), "reasoning_tokens": _i(rt),
               "cached_tokens": _i(ptd.get("cached_tokens")), "total_tokens": _i(u.get("total_tokens")), "usage": u}
        return {"model_calls": [row], "tool_calls": tc_rows}
    # start / end / crash / api_retry / unknown
    detail = None
    if kind == "api_retry":
        detail = _s(rec.get("detail"))
        try:  # compact the provider error to its code when JSON
            d = json.loads(detail or "")
            if isinstance(d, dict) and d.get("code"):
                detail = str(d["code"])
        except ValueError:
            pass
    elif kind == "crash":
        detail = _s(rec.get("error"))
    elif kind == "end":
        detail = rec.get("ended")
    row = {**base, "kind": kind or "unknown", "ts_raw": _f(rec.get("ts")), "ts": _f(rec.get("ts")),
           "attempt": _i(rec.get("attempt")), "wait": _f(rec.get("wait")), "detail": detail, "data": rec}
    return {"agent_records": [row]}


# --------------------------------------------------------------------------- board / relay / submissions

# Structural / routing fields promoted into the board_events table (CONTRACT 3.2, PROTOCOL.md 7.1).
# Post *content* (body, author) and raw IRC lines are never promoted here: they live only in the
# quarantined content sink and are joined back in by post_id host-side (see load_content_sink). The
# opaque thread_seq replaces the free-form slug for agent writes; `thread`/`chan` survive only for the
# req-target resolution of legacy fixtures that still carry them.
BOARD_PROMOTED = {"method": _s, "path": _s, "status": _i, "bytes": _i, "ua": _s, "thread": _s, "post_id": _i,
                  "thread_seq": _i, "body_len_chars": _i, "body_len_bytes": _i, "nick": _s, "cmd": _s, "chan": _s,
                  "channel": _s, "iface": _s}


def norm_board_record(rec: dict, run_id: str, line_off: int, seq: int, src: str) -> dict[str, list[dict]]:
    row = {"run_id": run_id, "line_off": line_off, "seq": seq, "ts_raw": _f(rec.get("ts")), "ts": _f(rec.get("ts")),
           "agent_id": _i(rec.get("agent")), "kind": rec.get("kind") or "unknown", "data": rec, "src": src}
    for k, conv in BOARD_PROMOTED.items():
        if k in rec:
            row[k] = conv(rec[k])
    # Legacy run artifacts (and the test fixtures) carry post content inline in board_events.jsonl.
    # New runs keep content ONLY in the quarantined sibling content sink, joined back by post_id host-side
    # (see load_content_sink / store.add_post), so these keys are simply absent there. Recover them when a
    # legacy record carries them so host-side derive/classify keep working unchanged over old runs; nothing
    # is read from a new-run event here (store.add_post never writes author/body/line/thread to the log).
    for k, conv in (("author", _s), ("body", _s), ("line", _s)):
        if k in rec:
            row[k] = conv(rec[k])
    # New structural events name the channel `channel`; keep the `iface` column populated for consumers
    # that still read it (byte-identical behavior for legacy records that already carry `iface`).
    if "channel" in rec and "iface" not in rec:
        row["iface"] = _s(rec["channel"])
    if "to" in rec:
        row["to_nick"] = _s(rec["to"])
    if "delivered" in rec:
        row["delivered"] = bool(rec["delivered"])
    return {"board_events": [row]}


def load_content_sink(path) -> dict[int, dict]:
    """Read a quarantined ``*.content.chatter.json`` sink into ``{post_id: {"author", "body", ...}}``.

    The obs ingest joins post content back in by ``post_id`` from this sink instead of the structural
    event log, so derive/classify keep working host-side without board_events.jsonl carrying content.
    This is a system-tool read (never surfaced to a coding agent); missing/empty sinks yield ``{}`` and
    malformed lines are skipped. ``path`` may be a str or ``os.PathLike``.
    """
    import os

    out: dict[int, dict] = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            pid = _i(rec.get("post_id"))
            if pid is not None:
                out[pid] = rec
    return out


def norm_relay_record(rec: dict, run_id: str, agent_id: int, line_off: int, seq: int, src: str) -> dict[str, list[dict]]:
    return {"relay_events": [{"run_id": run_id, "agent_id": agent_id, "line_off": line_off, "seq": seq,
                              "ts_raw": _f(rec.get("ts")), "ts": _f(rec.get("ts")), "ev": rec.get("ev"),
                              "conn": _i(rec.get("conn")), "dir": rec.get("dir"), "n": _i(rec.get("n")),
                              "err": _s(rec.get("err") or rec.get("error"))}]}


def norm_submission_line(line: str, run_id: str, agent_id: int, line_off: int, src: str,
                         flag: str | None) -> dict[str, list[dict]]:
    ep, sep, val = line.partition("\t")
    if not sep:
        ep, val = None, line
    return {"submissions": [{"run_id": run_id, "agent_id": agent_id, "line_off": line_off, "ts": _f(ep),
                             "value": val, "correct": bool(flag) and flag in val, "src": src}]}


def merge_rows(acc: dict[str, list[dict]], new: dict[str, list[dict]]):
    for k, v in new.items():
        acc.setdefault(k, []).extend(v)
