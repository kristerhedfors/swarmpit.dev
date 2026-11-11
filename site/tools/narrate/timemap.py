"""Audio-master time map (NARRATION.md 3.5). Pure functions over SafeRun-derived numbers; no file access.

A map is a list of integer points [(audioMs, runMs), ...]: audioMs strictly increasing, runMs non-decreasing,
every segment slope (drun/daudio) in [0, MAX_SLOPE]. The player derives run t = a2r(audio.currentTime).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

QUIET_MS = 4000          # a gap between visible events longer than this is fast-forwarded
QUIET_EDGE_MS = 400      # played at 1x at both ends of a quiet gap
QUIET_TARGET_MS = 1500   # audio budget for the fast middle of a quiet gap (when the slope cap allows)
MAX_SLOPE = 8            # contract cap on drun/daudio
LEAD_MS = 300            # run stops this far short of the next beat while a cue is spoken
GAP_MS = 350             # audio silence between consecutive cues
TAIL_MS = 2200           # tail after the last cue: room for the outro SFX (<= 2000 ms); run held at tEnd
MAX_POINTS = 600


@dataclass(frozen=True)
class CueSlot:
    anchor: int     # run ms
    dur: int        # audio ms (>= 1)
    lead: int = 0   # audio ms of run-held lead-in before the voice (room for a key SFX to play in the clear)


@dataclass(frozen=True)
class Placed:
    audioMs: int
    endMs: int
    runMs: int


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def build(t_end: int, cues: list[CueSlot], visible: list[int]) -> tuple[list[tuple[int, int]], list[Placed]]:
    """Walk the run, speaking each cue when the run reaches its anchor. `cues` must be in play order
    (sorted by anchor; ties keep their given order). Returns (normalized points, per-cue placement)."""
    t_end = int(t_end)
    vis = sorted(int(t) for t in visible if 0 < t < t_end)
    pts: list[tuple[int, int]] = [(0, 0)]
    a = r = 0
    placed: list[Placed] = []

    def push(a2: int, r2: int) -> None:
        nonlocal a, r
        if a2 <= a:
            return
        pts.append((a2, r2))
        a, r = a2, r2

    def approach(R: int) -> None:
        if R <= r:
            return
        lo = bisect.bisect_right(vis, r)
        hi = bisect.bisect_left(vis, R)
        stops = [r] + vis[lo:hi] + [R]
        for x, y in zip(stops, stops[1:]):
            L = y - x
            if L <= 0:
                continue
            if L > QUIET_MS:
                e = QUIET_EDGE_MS
                mid = L - 2 * e
                aud = max(_ceil_div(mid, MAX_SLOPE), min(mid, QUIET_TARGET_MS))
                push(a + e, x + e)
                push(a + aud, x + e + mid)
                push(a + e, y)
            else:
                push(a + L, y)

    for i, c in enumerate(cues):
        R = min(max(int(c.anchor), r), t_end)
        approach(R)
        if c.lead > 0:
            push(a + int(c.lead), r)
        start, run_at = a, r
        d = max(1, int(c.dur))
        r_next = min(max(int(cues[i + 1].anchor), r), t_end) if i + 1 < len(cues) else t_end
        dr = min(d, max(0, r_next - r - LEAD_MS))
        push(a + d, r + dr)
        placed.append(Placed(start, start + d, run_at))
        if i + 1 < len(cues):
            push(a + GAP_MS, r + min(GAP_MS, max(0, r_next - r)))
    approach(t_end)
    push(a + TAIL_MS, t_end)
    return normalize(pts), placed


def normalize(pts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Round to int, drop zero-length segments, merge collinear points."""
    out: list[tuple[int, int]] = []
    for a, r in pts:
        a, r = int(round(a)), int(round(r))
        if out and a <= out[-1][0]:
            continue
        out.append((a, r))
        while len(out) >= 3:
            (a0, r0), (a1, r1), (a2, r2) = out[-3:]
            if (r1 - r0) * (a2 - a1) == (r2 - r1) * (a1 - a0):
                del out[-2]
            else:
                break
    return out


def a2r(pts, a: float) -> float:
    if a <= pts[0][0]:
        return float(pts[0][1])
    if a >= pts[-1][0]:
        return float(pts[-1][1])
    j = bisect.bisect_right([p[0] for p in pts], a) - 1
    (a0, r0), (a1, r1) = pts[j], pts[j + 1]
    return r0 + (r1 - r0) * (a - a0) / (a1 - a0)


def r2a(pts, r: float) -> float:
    """Smallest audio position a with a2r(a) >= r (start of a hold when r is a held beat)."""
    if r <= pts[0][1]:
        return float(pts[0][0])
    if r > pts[-1][1]:
        return float(pts[-1][0])
    j = bisect.bisect_left([p[1] for p in pts], r)
    (a0, r0), (a1, r1) = pts[j - 1], pts[j]
    return a0 + (r - r0) * (a1 - a0) / (r1 - r0)


def holds(pts) -> list[tuple[int, int, int]]:
    """Slope-0 segments as (a0, a1, runMs). Consecutive holds are already merged by normalize()."""
    return [(a0, a1, r0) for (a0, r0), (a1, r1) in zip(pts, pts[1:]) if r1 == r0]


def finalize(pts, duration_ms: int) -> list[tuple[int, int]]:
    """Set the last point's audioMs to the measured mp3 duration (absorbs encoder padding)."""
    out = list(pts)
    a_last, r_last = out[-1]
    duration_ms = int(duration_ms)
    if len(out) >= 2 and duration_ms <= out[-2][0]:
        raise ValueError("E_MAP_DURATION")
    if len(out) >= 2:
        (a0, r0) = out[-2]
        if (r_last - r0) > MAX_SLOPE * (duration_ms - a0):
            raise ValueError("E_MAP_DURATION")
    out[-1] = (duration_ms, r_last)
    return out


def check(pts, duration_ms: int | None = None, run_end_ms: int | None = None) -> list[str]:
    """Contract invariants (NARRATION.md 2.1 #4). Returns fixed error codes."""
    errs: list[str] = []
    if not isinstance(pts, list) or not 2 <= len(pts) <= MAX_POINTS:
        return ["E_MAP_LEN"]
    for p in pts:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in p)):
            return ["E_MAP_POINT"]
    if tuple(pts[0]) != (0, 0):
        errs.append("E_MAP_START")
    if duration_ms is not None and run_end_ms is not None and tuple(pts[-1]) != (duration_ms, run_end_ms):
        errs.append("E_MAP_END")
    for (a0, r0), (a1, r1) in zip(pts, pts[1:]):
        if a1 <= a0 or r1 < r0:
            errs.append("E_MAP_MONO")
            break
        if r1 - r0 > MAX_SLOPE * (a1 - a0):
            errs.append("E_MAP_SLOPE")
            break
    return errs
