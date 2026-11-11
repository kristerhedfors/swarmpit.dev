"""The ONLY module that opens run data (data/index.json and core bundles). NARRATION.md 3.1.

It parses with json and projects each run into a frozen SafeRun holding only numbers and fixed-vocabulary
enum values. Free-text fields (node labels, chapter label/caption, strings[], agent names, post text,
run.boardName, caveat text, card blurb, event `l`/`x`) are never read. Nothing read here is ever printed;
errors carry fixed codes only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import PUBLIC

EVENT_KINDS = ("boardStart", "start", "end", "crash", "retry", "think", "cmd", "board", "submit", "idle",
               "read", "list", "doc", "miss", "post", "upvote", "delete", "dm", "join", "connect", "disconnect",
               "proto")
EVENT_FAMILY = {"boardStart": "lifecycle", "start": "lifecycle", "end": "lifecycle", "crash": "lifecycle",
                "retry": "lifecycle", "think": "think", "cmd": "task", "board": "boardStep", "submit": "task",
                "idle": "idle", "read": "read", "list": "read", "doc": "read", "miss": "read", "post": "write",
                "upvote": "social", "delete": "write", "dm": "social", "join": "irc", "connect": "irc",
                "disconnect": "irc", "proto": "irc"}
HIDDEN_KINDS = frozenset({"think", "idle", "proto", "connect", "disconnect"})
CHAPTER_KINDS = ("boot", "firstContact", "contact", "solve", "phaseFree", "post", "leak", "onset", "upvote", "dm",
                 "persistence", "crash", "end")
IFACE = ("forum", "wiki", "cache", "irc")
POP = ("solo", "multi")
CAVEAT_CODES = ("irc_inbound_broken", "tagged", "old_harness", "final_missing", "heuristic_edges", "synthetic",
                "unsolvable")
SEGMENT_TITLES = {"Boot": "boot", "On task": "onTask", "Free time": "free", "Wind-down": "wind"}
AGENT_STATUS = ("done", "crashed", "timed_out", "running")
ENDED = ("budget", "stalled", "voluntary_stop")


class EF:
    BOARD, SOLVE, LEAK, PERSIST, FREE, BACKGROUND, HEURISTIC, SUBMIT_OK, ERROR, LIVE_LIVE = (
        1, 2, 4, 8, 16, 32, 64, 128, 256, 512)


RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
HASH = re.compile(r"[0-9a-f]{8}")
CORE_PATH = re.compile(r"runs/[A-Za-z0-9._-]{1,120}\.core\.json")


class BundleError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------------------------------
# SafeRun
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SafeEvents:
    t: tuple[int, ...]
    d: tuple[int, ...]
    k: tuple[str, ...]        # EVENT_KINDS names
    a: tuple[int, ...]
    f: tuple[int, ...]
    s: tuple[int, ...]
    c: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.t)


@dataclass(frozen=True)
class SafeChapter:
    t: int
    k: str                    # CHAPTER_KINDS
    agent: int | None
    ev: int | None
    p: int
    merged: int


@dataclass(frozen=True)
class SafeSegment:
    t0: int
    t1: int
    title: str | None         # boot | onTask | free | wind | None (unknown title; skipped)


@dataclass(frozen=True)
class SafeAgent:
    id: int
    status: str | None
    startT: int | None
    endT: int | None
    solveT: int | None
    solveStep: int | None
    stepsUsed: int
    ended: str | None
    discovered: bool
    firstContact: int | None
    boardSteps: int
    shareAfterContact: float | None
    posts: int
    episodes: int
    secretLeak: bool


@dataclass(frozen=True)
class SafeRun:
    runId: str                # file names + manifest only, never cue text
    hash: str
    iface: str
    pop: str
    nAgents: int
    rep: int
    tEnd: int
    modelSpoken: str | None   # key of vocab.ENUMS['MODEL'] or None
    caveats: tuple[str, ...]
    events: SafeEvents
    chapters: tuple[SafeChapter, ...]
    segments: tuple[SafeSegment, ...]
    agents: tuple[SafeAgent, ...]
    binMs: int
    density: tuple[int, ...]


@dataclass(frozen=True)
class SafeCard:
    id: str
    hash: str
    core: str
    hidden: bool
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class SafeIndex:
    featured: str | None
    cards: tuple[SafeCard, ...]

    def card(self, run_id: str) -> SafeCard:
        for c in self.cards:
            if c.id == run_id:
                return c
        raise BundleError("E_RUN_UNKNOWN")


# --------------------------------------------------------------------------------------------------
# Typed accessors (fixed error codes; values never echoed)
# --------------------------------------------------------------------------------------------------

def _int(v, code: str, lo: int = -(2 ** 53), hi: int = 2 ** 53) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not (lo <= v <= hi):
        raise BundleError(code)
    return int(v)


def _opt_int(v, code: str) -> int | None:
    return None if v is None else _int(v, code)


def _enum(v, table, code: str | None = None):
    if isinstance(v, str) and v in table:
        return v
    if code:
        raise BundleError(code)
    return None


def _int_list(v, code: str) -> tuple[int, ...]:
    if not isinstance(v, list):
        raise BundleError(code)
    return tuple(_int(x, code) for x in v)


def _load_json(p: Path):
    try:
        with open(p, "rb") as fh:
            return json.loads(fh.read())
    except OSError:
        raise BundleError("E_READ") from None
    except ValueError:
        raise BundleError("E_JSON") from None


def load_index(public: Path = PUBLIC) -> SafeIndex:
    ix = _load_json(Path(public) / "data" / "index.json")
    if not isinstance(ix, dict) or ix.get("format") != "swarmpit.index/1" or not isinstance(ix.get("runs"), list):
        raise BundleError("E_INDEX_FORMAT")
    cards = []
    for r in ix["runs"]:
        if not isinstance(r, dict):
            raise BundleError("E_INDEX_CARD")
        rid, h, core = r.get("id"), r.get("hash"), r.get("core")
        if not (isinstance(rid, str) and RUN_ID.fullmatch(rid)):
            raise BundleError("E_INDEX_ID")
        if not (isinstance(h, str) and HASH.fullmatch(h)):
            raise BundleError("E_INDEX_HASH")
        if not (isinstance(core, str) and CORE_PATH.fullmatch(core)):
            raise BundleError("E_INDEX_CORE")
        cav = r.get("caveats") if isinstance(r.get("caveats"), list) else []
        cards.append(SafeCard(rid, h, core, r.get("hidden") is True,
                              tuple(c for c in cav if isinstance(c, str) and c in CAVEAT_CODES)))
    feat = ix.get("featured")
    featured = feat if isinstance(feat, str) and any(c.id == feat for c in cards) else None
    return SafeIndex(featured, tuple(cards))


def project(card: SafeCard, core: dict, models: dict) -> SafeRun:
    """Project a parsed core bundle into a SafeRun. Reads only whitelisted numeric / enum fields."""
    if not isinstance(core, dict) or core.get("format") != "swarmpit.run/1":
        raise BundleError("E_CORE_FORMAT")
    if core.get("hash") != card.hash:
        raise BundleError("E_CORE_HASH")
    run = core.get("run")
    if not isinstance(run, dict):
        raise BundleError("E_CORE_RUN")
    iface = _enum(run.get("iface"), IFACE, "E_IFACE")
    pop = _enum(run.get("pop"), POP, "E_POP")
    n_agents = _int(run.get("nAgents"), "E_NAGENTS", 0, 999)
    rep = _int(run.get("rep"), "E_REP", 0, 999)
    t_end = _int(run.get("tEndMs"), "E_TEND", 1, 3_600_000)
    model = run.get("model")
    model_spoken = model if isinstance(model, str) and model in models else None

    ev = core.get("events")
    if not isinstance(ev, dict):
        raise BundleError("E_EVENTS")
    cols = {c: _int_list(ev.get(c), "E_EVENTS") for c in ("t", "d", "k", "a", "f", "s", "c")}
    n = len(cols["t"])
    if any(len(v) != n for v in cols.values()):
        raise BundleError("E_EVENTS_LEN")
    kinds = []
    for k in cols["k"]:
        if not 0 <= k < len(EVENT_KINDS):
            raise BundleError("E_EVENT_KIND")
        kinds.append(EVENT_KINDS[k])
    events = SafeEvents(cols["t"], cols["d"], tuple(kinds), cols["a"], cols["f"], cols["s"], cols["c"])

    chapters = []
    for ch in core.get("chapters") or []:
        if not isinstance(ch, dict):
            raise BundleError("E_CHAPTER")
        k = _enum(ch.get("k"), CHAPTER_KINDS)
        if k is None:
            continue
        ag = ch.get("agent")
        agent = _int(ag, "E_CHAPTER", 0, 999) if ag is not None else None
        evi = ch.get("ev")
        evi = _int(evi, "E_CHAPTER", -1, n - 1) if evi is not None else None
        merged = ch.get("merged")
        chapters.append(SafeChapter(_int(ch.get("t"), "E_CHAPTER"), k, agent,
                                    evi if evi is not None and evi >= 0 else None,
                                    _int(ch.get("p"), "E_CHAPTER", 1, 3),
                                    _int(merged, "E_CHAPTER", 0, 999) if merged is not None else 1))

    segments = []
    for sg in core.get("segments") or []:
        if not isinstance(sg, dict):
            raise BundleError("E_SEGMENT")
        title = sg.get("title")
        segments.append(SafeSegment(_int(sg.get("t0"), "E_SEGMENT"), _int(sg.get("t1"), "E_SEGMENT"),
                                    SEGMENT_TITLES.get(title) if isinstance(title, str) else None))

    agents = []
    for i, a in enumerate(core.get("agents") or []):
        if not isinstance(a, dict):
            raise BundleError("E_AGENT")
        m = a.get("metrics") if isinstance(a.get("metrics"), dict) else {}
        sac = m.get("shareAfterContact")
        if sac is not None:
            if isinstance(sac, bool) or not isinstance(sac, (int, float)) or not 0 <= sac <= 1:
                raise BundleError("E_AGENT")
            sac = float(sac)
        agents.append(SafeAgent(
            id=_int(a.get("id", i), "E_AGENT", 0, 999),
            status=_enum(a.get("status"), AGENT_STATUS),
            startT=_opt_int(a.get("startT"), "E_AGENT"), endT=_opt_int(a.get("endT"), "E_AGENT"),
            solveT=_opt_int(a.get("solveT"), "E_AGENT"), solveStep=_opt_int(a.get("solveStep"), "E_AGENT"),
            stepsUsed=_int(a.get("stepsUsed", 0), "E_AGENT", 0),
            ended=_enum(a.get("ended"), ENDED),
            discovered=m.get("discovered") is True,
            firstContact=_opt_int(m.get("firstContact"), "E_AGENT"),
            boardSteps=_int(m.get("boardSteps", 0), "E_AGENT", 0),
            shareAfterContact=sac,
            posts=_int(m.get("posts", 0), "E_AGENT", 0),
            episodes=_int(m.get("episodes", 0), "E_AGENT", 0),
            secretLeak=m.get("secretLeak") is True))

    series = core.get("series") if isinstance(core.get("series"), dict) else {}
    return SafeRun(
        runId=card.id, hash=card.hash, iface=iface, pop=pop, nAgents=n_agents, rep=rep, tEnd=t_end,
        modelSpoken=model_spoken, caveats=card.caveats, events=events, chapters=tuple(chapters),
        segments=tuple(segments), agents=tuple(agents),
        binMs=_int(series.get("binMs", 1000), "E_SERIES", 1),
        density=_int_list(series.get("density", []), "E_SERIES"))


def load_run(index: SafeIndex, run_id: str, models: dict, public: Path = PUBLIC) -> SafeRun:
    card = index.card(run_id)
    core = _load_json(Path(public) / "data" / card.core)
    return project(card, core, models)


def select_runs(index: SafeIndex, ids: list[str] | None, featured: bool, all_: bool) -> list[str]:
    out: list[str] = []
    if featured:
        if index.featured is None:
            raise BundleError("E_NO_FEATURED")
        out.append(index.featured)
    if all_:
        out.extend(c.id for c in index.cards if not c.hidden)
    for i in ids or []:
        index.card(i)
        out.append(i)
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]
