"""Cue selection, voice assignment and pacing (NARRATION.md 3.2-3.4). Takes a SafeRun; never touches files.

The script is a two-commentator broadcast: `guide` calls the play-by-play (setup, who did what, counts, phase
changes) and `expert` adds color (why it matters, reactions, the verdict). They alternate: an intro duet, beat
exchanges at key moments (often with a guide handoff question), and an outro wrap-up.

All cue text comes from vocab.render(); arguments are ints, floats in [0, 1], or enum keys. Template variants
rotate deterministically, seeded by the run id, so the same run always gets the same script.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from . import sfx
from . import timemap as TM
from . import vocab
from .bundle import EF, EVENT_FAMILY, HIDDEN_KINDS, SafeRun

GAP_MS = TM.GAP_MS
MIN_ANCHOR_SPACING_MS = 3000
MAX_CUES_PER_AUDIO_MIN = 12
MAX_VOICE_RUN = 3        # at most this many consecutive cues in one voice (droppable cues go first)
MAX_HOLD_MS = 9000       # a mid-run freeze longer than this is a pacing violation (one call + color answer fits)
HANDOFF_HOLD_MS = 2500   # ... plus this when the freeze holds a handoff question (call, question, answer)
MAX_RATIO = 1.4
SLIDE_MS = 12000         # an expert beat line may wait this long (run ms) for room to be spoken without a hold
MAX_CUES = 80
SUMMARY_WINDOW_MS = 20000
CLUSTER_MS = 8000           # exchanges this close together share one color answer
SOLVE_NEAR_FREE_MS = 5000   # a free-time start this close to the first solve gets no separate color line
STUCK_MIN_RUN_MS = 30000    # the halfway check needs a run at least this long
SOLVE_MERGE_MS = 1500       # the exporter folds solves closer than this into one chapter (export.py)
QUIET_MAX = 2               # at most this many quiet-stretch lines per run, never two windows in a row
SHARE_LOW = 0.15            # an agent's post-contact board share below this gets a "casual" color line
INTRO_SPAN_MS = 30000       # the running intro covers roughly this much run time: no phase line or activity
                            # summary inside it (the setup line already says the task comes first)
CAVEAT_ORDER = ("irc_inbound_broken", "synthetic", "heuristic_edges", "final_missing", "old_harness", "tagged")
STEP_KINDS = frozenset({"cmd", "board", "submit", "idle"})
BOARD_FAMILIES = frozenset({"read", "write", "social", "irc", "boardStep"})
FAMILY_ORDER = ("read", "write", "social", "irc", "boardStep", "task")


@dataclass(frozen=True)
class Cue:
    id: int
    anchor: int          # run ms
    row: int             # table row (play order on equal anchors)
    sub: int
    voice: str
    tpl: str
    text: str
    prio: int            # 0 never dropped .. 3 dropped first
    pair: int | None     # id of the cue this one follows up on (dropped with it)
    closing: bool = False
    needs: int | None = None   # a handoff question: id of the answer it needs (dropped without it)

    @property
    def key(self):
        return (self.anchor, self.row, self.sub, self.id)


def visible_event_times(run: SafeRun) -> list[int]:
    ev = run.events
    return [ev.t[i] for i in range(len(ev)) if ev.k[i] not in HIDDEN_KINDS and not ev.f[i] & EF.BACKGROUND]


def is_unsolvable(run: SafeRun) -> bool:
    """The unsolvable goal: the task can never be completed. Read from SafeRun enums only."""
    return getattr(run, "goal", None) == "unsolvable" or "unsolvable" in run.caveats


def _seed(run_id: str, base: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{run_id}\x00{base}".encode()).digest()[:4], "big")


class _Builder:
    def __init__(self, run: SafeRun):
        self.run = run
        self.cues: list[Cue] = []
        self.uses: dict[str, int] = {}
        self.beats = 0
        self.exchanges: list[tuple[int, int | None, int]] = []   # (answer id, ask id, guide id)

    def add(self, anchor: int, row: int, sub: int, tpl: str, prio: int, pair: int | None = None,
            closing: bool = False, **args) -> int:
        cid = len(self.cues)
        anchor = min(max(int(anchor), 0), self.run.tEnd)
        voice = vocab.TEMPLATES[tpl].voice
        self.cues.append(Cue(cid, anchor, row, sub, voice, tpl, vocab.render(tpl, **args), prio, pair, closing))
        return cid

    def pick(self, base: str, anchor: int, row: int, sub: int, prio: int, pair: int | None = None,
             closing: bool = False, **args) -> int:
        """Add the next variant of a template family (rotation seeded by the run id). Args a variant does not
        use are dropped; the rest are still type-checked by render()."""
        vs = vocab.variants(base)
        k = self.uses.get(base, 0)
        self.uses[base] = k + 1
        tpl = vs[(_seed(self.run.runId, base) + k) % len(vs)]
        names = {n for n, _ in vocab.TEMPLATES[tpl].slots}
        return self.add(anchor, row, sub, tpl, prio, pair, closing, **{n: v for n, v in args.items() if n in names})

    def exchange(self, g: int, base: str, prio: int, **args) -> int:
        """Color answer to guide cue `g` at its anchor. The first exchange of a run is always opened by a guide
        handoff question, later ones every other time (seeded); a question is dropped if its answer is dropped."""
        c = self.cues[g]
        self.beats += 1
        ask = None
        if self.beats == 1 or (_seed(self.run.runId, "ask") + self.beats) % 2 == 0:
            ask = self.pick("G.ask", c.anchor, c.row, c.sub + 1, 1 if self.beats == 1 else 2, pair=g)
        e = self.pick(base, c.anchor, c.row, c.sub + 2, prio, pair=g, **args)
        if ask is not None:
            self.cues[ask] = replace(self.cues[ask], needs=e)
        self.exchanges.append((e, ask, g))
        return e

    def cluster_color(self) -> set[int]:
        """Beats that land together (consecutive exchanges within CLUSTER_MS) get ONE color answer, the most
        important one, spoken after the cluster's last play-by-play call with the priority of the cluster's most
        important call (prio 0, never dropped, when any call is); the other answers are removed, and one of their
        handoff questions moves to the kept answer if it had none. Returns the removed cue ids."""
        ex = sorted(self.exchanges, key=lambda x: self.cues[x[2]].key)
        groups: list[list[tuple[int, int | None, int]]] = []
        for x in ex:
            if (groups and len(groups[-1]) < MAX_VOICE_RUN
                    and self.cues[x[2]].anchor - self.cues[groups[-1][-1][2]].anchor <= CLUSTER_MS):
                groups[-1].append(x)
            else:
                groups.append([x])
        removed: set[int] = set()
        for grp in groups:
            if len(grp) < 2:
                continue
            keep = max(grp, key=lambda x: (WEIGHT.get(_family(self.cues[x[0]].tpl), 0), -self.cues[x[2]].anchor))
            last = max((self.cues[x[2]] for x in grp), key=lambda c: c.key)
            spare = None      # a handoff from a merged-away answer moves to the kept one
            for e, ask, g in grp:
                if (e, ask, g) != keep:
                    removed.add(e)
                    if ask is not None:
                        if spare is None and keep[1] is None:
                            spare = ask
                        else:
                            removed.add(ask)
            e, ask, _ = keep
            # the answer follows the cluster's most important call (and goes only if that call goes)
            lead = min((self.cues[x[2]] for x in grp), key=lambda c: (c.prio, -c.anchor))
            if ask is None and spare is not None:
                ask = spare
                self.cues[ask] = replace(self.cues[ask], needs=e)
            self.cues[e] = replace(self.cues[e], anchor=last.anchor, row=last.row, sub=last.sub + 2,
                                   prio=lead.prio, pair=lead.id)
            if ask is not None:
                self.cues[ask] = replace(self.cues[ask], anchor=last.anchor, row=last.row, sub=last.sub + 1,
                                         pair=lead.id)
        return removed


WEIGHT = {"E.leak": 5, "E.firstSolve": 4, "E.firstSolveN": 4, "E.onset": 4, "E.firstContact": 3, "E.free": 2, "E.firstPost": 2,
          "E.pileIn": 1, "E.noSolve": 1}


def _family(tpl: str) -> str:
    for base in WEIGHT:
        if tpl in vocab.variants(base):
            return base
    return tpl


def _share(run: SafeRun, t0: int, t1: int, flag: int = 0) -> tuple[int, int]:
    """(steps, board steps) among step events in (t0, t1] carrying `flag` (0 = any)."""
    ev = run.events
    steps = board = 0
    for i in range(len(ev)):
        if t0 <= ev.t[i] <= t1 and ev.k[i] in STEP_KINDS and not ev.f[i] & EF.BACKGROUND \
                and (not flag or ev.f[i] & flag):
            steps += 1
            board += 1 if ev.f[i] & EF.BOARD else 0
    return steps, board


def candidates(run: SafeRun) -> list[Cue]:
    b = _Builder(run)
    ev = run.events
    noun = run.iface
    agents = {a.id: a for a in run.agents}
    n_agents = len(run.agents) or run.nAgents
    solo = run.pop == "solo" or n_agents == 1
    stuck = is_unsolvable(run)
    model = run.modelSpoken if run.modelSpoken in vocab.ENUMS["MODEL"] else None
    who = {(False, False): "multi", (True, False): "solo", (False, True): "stuck", (True, True): "stuckSolo"}[
        (solo, stuck)]
    chapters = sorted((c for c in run.chapters if 0 <= c.t <= run.tEnd), key=lambda c: c.t)
    first_solve_t = min((c.t for c in chapters if c.k == "solve"), default=None)
    free = next((sg for sg in run.segments if sg.title == "free" and 0 < sg.t0 < run.tEnd), None)

    # 1-3 intro duet: guide opens, color replies, guide sets up the task, color frames the question
    g0 = b.pick(f"G.open.{'solo' if solo else 'multi'}{'M' if model else ''}", 0, 1, 0, 0,
                n=min(n_agents, 999), model=model, iface=run.iface)
    b.pick("E.open", 0, 1, 1, 0, pair=g0)
    g1 = b.add(0, 2, 0, f"G.setup.{who}", 0, pair=g0)
    b.pick(f"E.ask.{who}", 0, 2, 1, 0, pair=g1, noun=noun)
    caveat = next((code for code in CAVEAT_ORDER if code in run.caveats), None)
    # with a caveat the how-to line stays (and goes) with it, so the intro never has two color lines in a row
    gh = b.pick("G.howto", 0, 3, 0, 1 if caveat else 2, pair=g0)
    if caveat:
        b.add(0, 3, 1, f"E.caveat.{caveat}", 1, pair=gh)

    # 4 phase changes (Boot is covered by the intro)
    for i, sg in enumerate(run.segments):
        if i == 0 or not 0 < sg.t0 < run.tEnd:
            continue
        if sg.title == "onTask":
            if sg.t0 < INTRO_SPAN_MS:
                continue
            g = b.pick("G.seg.onTask", sg.t0, 4, 0, 1)
            if stuck:
                b.add(sg.t0, 4, 2, "E.onTask.stuck", 1, pair=g)
            else:
                b.pick("E.onTask", sg.t0, 4, 2, 3, pair=g, noun=noun)
        elif sg.title == "free":
            near = first_solve_t is not None and abs(sg.t0 - first_solve_t) <= SOLVE_NEAR_FREE_MS
            # in a multi-agent run the free segment starts with the first finisher: unless everyone is through
            # by then, the task phase is not over and the lines must not say it is
            first = not solo and any(a.solveT is None or a.solveT > sg.t0 + 1000 for a in run.agents)
            g = b.pick("G.seg.freeFirst" if first else "G.seg.free", sg.t0, 4, 0, 1 if near else 0)
            if not near:
                b.exchange(g, "E.freeFirst" if first else "E.free", 1)
        elif sg.title == "wind":
            b.add(sg.t0, 4, 0, "G.seg.wind", 1)

    # 5-10 chapters
    first_contact = first_solve = first_leak = first_post = first_pile = True
    solved: set[int] = set()
    leaks = {(c.t, c.agent) for c in chapters if c.k == "leak"}
    first_post_t = min((ev.t[i] for i in range(len(ev)) if ev.k[i] == "post" and not ev.f[i] & EF.BACKGROUND),
                       default=None)
    for ch in chapters:
        ag = ch.agent
        if ch.k == "firstContact" and ag is not None:
            g = b.pick("G.firstContact", ch.t, 5, 0, 0, agent=ag, noun=noun)
            if first_contact:
                b.exchange(g, "E.firstContact", 1, noun=noun)
            first_contact = False
        elif ch.k == "contact":
            if ch.merged > 1:
                g = b.pick("G.contactN", ch.t, 6, 0, 2, n=min(ch.merged, 999), noun=noun)
                if first_pile and not first_contact:
                    b.exchange(g, "E.pileIn", 2)
                    first_pile = False
            elif ag is not None:
                b.pick("G.contact", ch.t, 6, 0, 2, agent=ag, noun=noun)
        elif ch.k == "solve" and ag is not None:
            step = agents[ag].solveStep if ag in agents else None
            pr = 0 if first_solve else 1
            who_ = _merged_solvers(run, ch, solved)
            if len(who_) > 1:
                g = b.pick("G.solveN", ch.t, 7, 0, pr, n=min(len(who_), 999))
            elif step is not None and step >= 0:
                g = b.pick("G.solve", ch.t, 7, 0, pr, agent=ag, steps=step)
            else:
                g = b.pick("G.solve.bare", ch.t, 7, 0, pr, agent=ag)
            solved |= who_
            if first_solve:
                b.exchange(g, "E.firstSolveN" if len(who_) > 1 else "E.firstSolve", 1)
            elif not solo and len(solved) == n_agents:
                b.add(ch.t, 7, 2, "E.solveAll", 2, pair=g, n=min(n_agents, 999))
            else:
                b.pick("E.solveMore", ch.t, 7, 2, 2, pair=g)
            first_solve = False
        elif ch.k == "leak" and ag is not None:
            if first_leak:
                g = b.pick("G.leak", ch.t, 8, 0, 0, agent=ag, noun=noun)
                b.exchange(g, "E.leak", 1)
            else:
                b.add(ch.t, 8, 0, "G.leakMore", 1, agent=ag)
            first_leak = False
        elif ch.k == "onset":
            g = b.pick("G.onset", ch.t, 9, 0, 0)
            b.exchange(g, "E.onset", 1)
        elif ch.k == "post" and ch.p == 1 and ag is not None:
            if (ch.t, ag) in leaks:
                continue      # the leak line already covers this post
            if first_post and first_post_t is not None and abs(ch.t - first_post_t) <= 1:
                g = b.add(ch.t, 10, 0, "G.firstPost", 1, agent=ag, noun=noun)
                b.exchange(g, "E.firstPost", 2, noun=noun)
            else:
                b.pick("G.post", ch.t, 10, 0, 2, agent=ag, noun=noun)
            first_post = False
        elif ag is not None and (ch.k == "upvote" and ch.p <= 2 or ch.k in ("dm", "persistence", "crash")):
            base = {"upvote": "G.upvote", "dm": "G.dm", "persistence": "G.persist", "crash": "G.crash"}[ch.k]
            g = b.pick(base, ch.t, 10, 0, 1 if ch.k == "crash" else 2, agent=ag)
            if ch.k == "crash":
                b.add(ch.t, 10, 2, "E.crash", 2, pair=g)

    # 11a free-time drift: the guide gives the share, the color reacts
    if free is not None:
        anchor = free.t0 + max(8000, int(0.4 * (free.t1 - free.t0)))
        if anchor < run.tEnd:
            steps, board = _share(run, free.t0, anchor, EF.FREE)
            if steps >= 3:
                pct = board / steps
                g = b.pick("G.drift", anchor, 11, 0, 1, p=pct)
                b.exchange(g, "E.driftHigh" if pct >= 0.3 else "E.driftLow", 1)

    # 11b halfway check when nothing is solved yet (always, for the unsolvable goal)
    task = next((sg for sg in run.segments if sg.title == "onTask"), None)
    t0 = task.t0 if task is not None else 0
    anchor = t0 + (run.tEnd - t0) // 2
    if (run.tEnd >= STUCK_MIN_RUN_MS and anchor < run.tEnd - MIN_ANCHOR_SPACING_MS
            and (first_solve_t is None or first_solve_t > anchor) and (free is None or free.t0 > anchor)):
        if stuck:
            g = b.pick("G.stuck", anchor, 11, 0, 1)
            steps, board = _share(run, t0, anchor)
            if steps >= 3:
                pct = board / steps
                g = b.add(anchor, 11, 1, "G.stuckShare", 1, pair=g, p=pct)
                b.exchange(g, "E.stuckHigh" if pct >= 0.3 else "E.stuckLow", 1)
            else:
                b.exchange(g, "E.stuckLow", 1)
        else:
            g = b.add(anchor, 11, 0, "G.noSolve.solo" if solo else "G.noSolve", 2)
            b.exchange(g, "E.noSolve", 2, noun=noun)

    # 12 agent ends (play-by-play stat, occasional color)
    for i in range(len(ev)):
        if ev.k[i] != "end" or ev.a[i] < 0 or ev.f[i] & EF.BACKGROUND or ev.a[i] not in agents:
            continue
        a = agents[ev.a[i]]
        if a.discovered and a.shareAfterContact is not None:
            g = b.add(ev.t[i], 12, 0, "G.agentShare", 2, agent=a.id, p=a.shareAfterContact)
            if a.shareAfterContact >= 0.5:
                b.pick("E.regular", ev.t[i], 12, 2, 3, pair=g)
            elif a.shareAfterContact < SHARE_LOW:
                b.pick("E.casual", ev.t[i], 12, 2, 3, pair=g)
        elif not a.discovered and a.ended is not None:
            g = b.add(ev.t[i], 12, 0, "G.agentEnd", 2, agent=a.id, why=a.ended)
            b.pick("E.agentEnd", ev.t[i], 12, 2, 3, pair=g)

    # 13 activity summaries over cue-free run windows (quiet stretches alternate voices)
    anchors = sorted({0, run.tEnd} | {c.anchor for c in b.cues if c.prio <= 2})
    vis = [i for i in range(len(ev)) if ev.k[i] not in HIDDEN_KINDS and not ev.f[i] & EF.BACKGROUND]
    quiet_n, last_quiet = 0, None
    for A, B in zip(anchors, anchors[1:]):
        w0 = max(A, INTRO_SPAN_MS - SUMMARY_WINDOW_MS)
        while w0 + SUMMARY_WINDOW_MS + MIN_ANCHOR_SPACING_MS <= B:
            w1 = w0 + SUMMARY_WINDOW_MS
            fam: dict[str, int] = {}
            for i in vis:
                if w0 < ev.t[i] <= w1:
                    f = EVENT_FAMILY[ev.k[i]]
                    fam[f] = fam.get(f, 0) + 1
            total = sum(fam.values())
            board = sum(v for f, v in fam.items() if f in BOARD_FAMILIES)
            if total >= 6:
                top = max(FAMILY_ORDER, key=lambda f: (fam.get(f, 0), -FAMILY_ORDER.index(f)))
                b.pick("G.busy", w1, 13, 0, 3, fam=top)
            elif board == 0 and quiet_n < QUIET_MAX and last_quiet != w0:
                b.pick("G.quiet" if quiet_n % 2 == 0 else "E.quiet", w1, 13, 0, 3, noun=noun)
                quiet_n += 1
                last_quiet = w1
            w0 = w1

    # 14 outro wrap-up (run held at tEnd): guide tally, color verdict, guide sign-off, color last word
    disc = [a for a in run.agents if a.discovered]
    n_solved = sum(1 for a in run.agents if a.solveT is not None)
    n_leak = sum(1 for a in run.agents if a.secretLeak)
    if solo:
        a0 = run.agents[0] if run.agents else None
        yn = (lambda v: "yes" if v else "no")
        b.add(run.tEnd, 14, 0, f"G.tally.{who}", 0, closing=True, f=yn(bool(a0 and a0.discovered)),
              l=yn(bool(a0 and a0.secretLeak)), **({} if stuck else {"s": yn(bool(a0 and a0.solveT is not None))}))
    else:
        b.add(run.tEnd, 14, 0, f"G.tally.{who}", 0, closing=True, n=min(n_agents, 999), d=min(len(disc), 999),
              noun=noun, l=min(n_leak, 999), **({} if stuck else {"s": min(n_solved, 999)}))
    shares = [a.shareAfterContact for a in disc if a.shareAfterContact is not None]
    sfx_ = "Solo" if solo else ""
    if not disc or not shares:
        b.add(run.tEnd, 14, 1, f"E.close.noContact{sfx_}", 0, closing=True)
    else:
        s = sum(shares) / len(shares)
        verdict = "sticky" if s >= 0.4 else "notSticky" if s < 0.15 else "mixed"
        b.add(run.tEnd, 14, 1, f"E.close.{verdict}{sfx_}", 0, closing=True, p=s)
    b.pick("G.signoff", run.tEnd, 14, 2, 0, closing=True)
    b.pick("E.signoff", run.tEnd, 14, 3, 0, closing=True)

    # the closing anchors at tEnd; every other cue anchored at tEnd moves just before it in play order
    gone = b.cluster_color()
    out, seen = [], set()
    for c in sorted((c for c in b.cues if c.id not in gone), key=lambda c: c.key):
        if c.prio > 0 and not c.closing and c.text in seen:
            continue          # never the same sentence twice in one broadcast (plan drops its follow-ups)
        seen.add(c.text)
        out.append(c)
    return out


def _merged_solvers(run: SafeRun, ch, solved: set[int]) -> set[int]:
    """The agents a solve chapter stands for. The exporter folds solves less than SOLVE_MERGE_MS apart into one
    chapter (`merged` members, earliest t first), so walk the not-yet-counted solve times from ch.t."""
    out = {ch.agent}
    if ch.merged <= 1:
        return out
    last = ch.t
    for t, aid in sorted((a.solveT, a.id) for a in run.agents
                         if a.solveT is not None and a.solveT >= ch.t and a.id not in solved and a.id != ch.agent):
        if len(out) >= ch.merged or t - last >= SOLVE_MERGE_MS:
            break
        out.add(aid)
        last = t
    return out


def estimate_ms(cue: Cue, cfg: dict) -> int:
    cps = float(cfg["voices"][cue.voice]["charsPerSec"])
    return int(len(cue.text) / cps * 1000 + 150)


# --------------------------------------------------------------------------------------------------
# Selection loop
# --------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    cues: list[Cue]
    durs: list[int]
    points: list[tuple[int, int]]
    placed: list[TM.Placed]
    dropped: int
    warnings: list[str]
    leads: list[int]
    intro_end: int = 0

    @property
    def duration_ms(self) -> int:
        return self.points[-1][0]


def paced_ms(pts, t_end: int, intro_end: int = 0) -> int:
    """Audio length minus the exempt holds: at runMs 0, inside the intro duet (audio before `intro_end`), and at
    tEnd (closing); compared with MAX_RATIO."""
    return pts[-1][0] - sum(a1 - a0 for a0, a1, r in TM.holds(pts) if r in (0, t_end) or a1 <= intro_end)


INTRO_ROWS = 3   # rows 1-3 are the intro duet


def _intro_end(kept: list[Cue], placed) -> int:
    """Audio ms where the intro duet ends (0 if none)."""
    return max((p.endMs + GAP_MS for c, p in zip(kept, placed) if c.row <= INTRO_ROWS and not c.closing), default=0)


def _spacing_prepass(cues: list[Cue]) -> list[Cue]:
    """Standalone low-priority lines closer than MIN_ANCHOR_SPACING_MS to the previous one are dropped.
    Follow-ups (pair set) belong to their beat and are exempt."""
    out, last = [], None
    for c in cues:
        if c.prio >= 2 and c.pair is None:
            if last is not None and c.anchor - last < MIN_ANCHOR_SPACING_MS:
                continue
            last = c.anchor
        out.append(c)
    return out


def _drop_orphans(cues: list[Cue]) -> list[Cue]:
    """Drop follow-ups whose cue is gone and handoff questions whose answer is gone (to a fixpoint)."""
    while True:
        ids = {c.id for c in cues}
        out = [c for c in cues if (c.pair is None or c.pair in ids or c.closing)
               and (c.needs is None or c.needs in ids)]
        if len(out) == len(cues):
            return out
        cues = out


def voice_runs(cues: list[Cue]) -> list[tuple[int, int]]:
    """Maximal [i, j) index ranges of consecutive same-voice cues (play order)."""
    out, i = [], 0
    while i < len(cues):
        j = i
        while j < len(cues) and cues[j].voice == cues[i].voice:
            j += 1
        out.append((i, j))
        i = j
    return out


def _run_intro(cues: list[Cue], dur_of) -> list[Cue]:
    """The intro duet talks over the start of the run instead of freezing it: every opening line after the first
    moves to the moment the previous one ends (run at 1x), as long as it ends before the next beat. A line that
    does not fit stays at the previous line's anchor (a hold), so play order never changes."""
    order = sorted(cues, key=lambda c: c.key)
    intro = [c for c in order if c.anchor == 0 and not c.closing]
    if len(intro) < 2:
        return order
    nxt = min((c.anchor for c in order if c.anchor > 0 or c.closing), default=None)
    moved = {}
    prev = 0
    busy = max(1, int(dur_of(intro[0]))) + GAP_MS
    for c in intro[1:]:
        d = max(1, int(dur_of(c)))
        at = busy if nxt is None or busy + d + GAP_MS <= nxt else prev
        moved[c.id] = replace(c, anchor=at)
        prev = at
        busy = max(busy, at) + d + GAP_MS
    return sorted((moved.get(c.id, c) for c in order), key=lambda c: c.key)


def _slide_experts(cues: list[Cue], dur_of, t_end: int | None = None) -> list[Cue]:
    """Delay non-closing expert lines (up to SLIDE_MS) to the first moment where they can be spoken while the run
    keeps playing at 1x, instead of freezing the run during a dense cluster. Lines that find no room stay put.

    `busy` is the run time at which the previous line's audio would end if the run kept moving. A line that stays
    at its anchor A is spoken from run time A (the run waits there while earlier audio finishes), so it frees the
    audio at A + d, not later: counting the wait as well would slide the next line too far and leave dead air (a
    handoff question followed by silence). A slide never moves a line past the next cue in play order, so the
    order (an intro reply after its line, an answer after its question) never changes."""
    out: list[Cue] = []
    busy = 0
    order = sorted(cues, key=lambda c: c.key)
    for i, c in enumerate(order):
        d = max(1, int(dur_of(c)))
        if c.voice == "expert" and not c.closing and c.anchor > 0 and busy > c.anchor:
            nxt = order[i + 1].anchor if i + 1 < len(order) else None
            if (busy - c.anchor <= SLIDE_MS and (nxt is None or busy + d + GAP_MS <= nxt)
                    and (t_end is None or busy + d + GAP_MS <= t_end)):
                c = replace(c, anchor=busy)
        out.append(c)
        busy = c.anchor + d + GAP_MS
    return sorted(out, key=lambda c: c.key)


def _leads(kept: list[Cue], leads: dict[int, int]) -> list[int]:
    """The first kept cue at a key-effect run time gets that lead-in; later cues at the same anchor do not."""
    seen: set[int] = set()
    out = []
    for c in kept:
        out.append(0 if c.anchor in seen else int(leads.get(c.anchor, 0)))
        seen.add(c.anchor)
    return out


def plan(run: SafeRun, cues: list[Cue], dur_of, visible: list[int] | None = None,
         leads: dict[int, int] | None = None) -> Plan:
    """Drop cues (lowest priority first) until pacing constraints hold; returns the final map.
    `dur_of(cue) -> int ms`. Only drops, so it can run again after TTS with measured durations.
    `leads`: run ms -> audio lead-in for cues anchored at key SFX times (sfx.key_leads)."""
    vis = visible_event_times(run) if visible is None else visible
    n0 = len(cues)
    kept = _slide_experts(_run_intro(_drop_orphans(_spacing_prepass(sorted(cues, key=lambda c: c.key))), dur_of),
                          dur_of, run.tEnd)
    durs_by_id = {c.id: max(1, int(dur_of(c))) for c in kept}
    warnings: set[str] = set()
    while True:
        durs = [durs_by_id[c.id] for c in kept]
        lds = _leads(kept, leads or {})
        pts, placed = TM.build(run.tEnd, [TM.CueSlot(c.anchor, d, ld) for c, d, ld in zip(kept, durs, lds)], vis)
        held = {}
        for i, p in enumerate(placed):
            held[i] = (p.endMs - p.audioMs) - int(TM.a2r(pts, p.endMs) - TM.a2r(pts, p.audioMs))
        cand: set[int] = set()
        violated = []
        if len(kept) > MAX_CUES:
            violated.append(("E_PACE_COUNT", set(range(len(kept)))))
        intro_end = _intro_end(kept, placed)
        for a0, a1, r in TM.holds(pts):
            if r in (0, run.tEnd) or a1 <= intro_end:
                continue
            idx = {i for i, p in enumerate(placed) if p.audioMs < a1 and p.endMs > a0}
            if a1 - a0 > MAX_HOLD_MS + (HANDOFF_HOLD_MS if any(kept[i].needs is not None for i in idx) else 0):
                violated.append(("W_PACE_HOLD", idx))
        # the rate limit is for the beats: the fixed intro duet (rows 1-3) is not counted
        starts = [p.audioMs for p in placed]
        beat = [not (c.row <= INTRO_ROWS and not c.closing) for c in kept]
        for i, s in enumerate(starts):
            if not beat[i]:
                continue
            win = {j for j in range(i, len(starts)) if beat[j] and starts[j] < s + 60000}
            if len(win) > MAX_CUES_PER_AUDIO_MIN:
                violated.append(("W_PACE_RATE", win))
        if paced_ms(pts, run.tEnd, intro_end) > MAX_RATIO * run.tEnd:
            violated.append(("W_PACE_RATIO", set(range(len(kept)))))
        for i, j in voice_runs(kept):
            if j - i > MAX_VOICE_RUN:
                violated.append(("W_PACE_VOICE", set(range(i, j))))
        asks = [i for i, c in enumerate(kept) if c.needs is not None]
        for code, idx in violated:
            # the run's last handoff question is the last thing the soft (W_) checks drop: the broadcast keeps at
            # least one real back-and-forth unless nothing else is left to drop (then the W_ code is a warning);
            # only the hard cue-count limit takes it
            keep_ask = asks[0] if len(asks) == 1 and code.startswith("W_") and code != "W_PACE_VOICE" else None
            droppable = {i for i in idx if kept[i].prio > 0 and not kept[i].closing and i != keep_ask}
            if droppable:
                cand |= droppable
                break
            warnings.add(code)
        if not cand:
            break
        victim = max(cand, key=lambda i: (kept[i].prio, kept[i].needs is None, held[i], kept[i].key))
        gone = kept[victim].id
        kept = _drop_orphans([c for c in kept if c.id != gone])
    return Plan(kept, durs, pts, placed, n0 - len(kept), sorted(warnings), lds, intro_end)


def build_script(run: SafeRun, cfg: dict) -> dict:
    """Candidates -> estimated plan -> script dict (written to the cache; its text is never printed)."""
    cands = candidates(run)
    p = plan(run, cands, lambda c: estimate_ms(c, cfg), leads=sfx.key_leads(run, cfg["sfx"]["library"]))
    # the script keeps each cue's candidate anchor (before the intro relay and expert slides), so the mix-time
    # plan re-times both with measured clip durations instead of re-sliding already moved lines
    orig = {c.id: c.anchor for c in cands}
    return {
        "v": 2, "runId": run.runId, "runHash": run.hash, "tEnd": run.tEnd,
        "cues": [{"id": c.id, "anchor": orig[c.id], "row": c.row, "sub": c.sub, "voice": c.voice, "tpl": c.tpl,
                  "text": c.text, "prio": c.prio, "pair": c.pair, "closing": c.closing, "needs": c.needs,
                  "estMs": d}
                 for c, d in zip(p.cues, p.durs)],
        "candidates": len(cands), "dropped": p.dropped, "estDurationMs": p.duration_ms,
        "mapPoints": len(p.points), "pacedMs": paced_ms(p.points, run.tEnd, p.intro_end), "warnings": p.warnings,
    }


def cues_from_script(script: dict) -> list[Cue]:
    """Rebuild Cue objects from a cached script, re-validating every text against its template."""
    out = []
    for i, c in enumerate(script["cues"]):
        vocab.validate_cue(c["tpl"], c["text"], index=i, voice=c["voice"])
        needs = c.get("needs")
        out.append(Cue(int(c["id"]), int(c["anchor"]), int(c["row"]), int(c["sub"]), c["voice"], c["tpl"],
                       c["text"], int(c["prio"]), c["pair"] if c["pair"] is None else int(c["pair"]),
                       bool(c["closing"]), None if needs is None else int(needs)))
    return sorted(out, key=lambda c: c.key)


def summary(script: dict) -> dict:
    """Safe numeric summary of a script (no text)."""
    cues = script["cues"]
    g = [c for c in cues if c["voice"] == "guide"]
    e = [c for c in cues if c["voice"] == "expert"]
    est = script["estDurationMs"]
    return {"cues": len(cues), "guide": len(g), "expert": len(e),
            "chars_guide": sum(len(c["text"]) for c in g), "chars_expert": sum(len(c["text"]) for c in e),
            "candidates": script["candidates"], "dropped": script["dropped"], "est_audio_ms": est,
            "map_points": script["mapPoints"], "ratio": round(est / script["tEnd"], 2) if script["tEnd"] else 0.0,
            "paced_ratio": round(script["pacedMs"] / script["tEnd"], 2) if script["tEnd"] else 0.0,
            "warnings": ",".join(script["warnings"]) or "-"}
