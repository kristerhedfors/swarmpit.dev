#!/usr/bin/env python3
"""MAP-Elites archive for the instigation landscape (quality-diversity over board seeds).

Pure stdlib, numbers / ids / fixed enums only. The archive is a grid over three axes:

    trigger_code  x  channel  x  length_band

and, PER AGENT MODEL (config.MODELS slug), each cell holds the single best ("elite") seed record observed
for that combination -- i.e. the archive is keyed by (model, cell_key), with the 3-part cell key unchanged.
Every record also carries the task ``goal`` (config.GOALS). A record is
content-free by construction: an opaque hex ``record_id``, numeric scores and lengths, counts, and enum
strings. It never carries board text, model replies or any other chatter.

Isolation rationale: the archive and its ``to_map()`` projection are published (via scoreboard.py and the
leak gate) to a Claude-visible, public site artifact. Every axis value is therefore validated STRICTLY
against a fixed enum before it can reach a cell key, the map or the file on disk, and record ids must be
16 lowercase hex chars. Anything else is rejected with a fixed error code (``ValueError(code)``); error
messages never echo the offending value, so a malformed or adversary-influenced field cannot become a
content channel into the published landscape.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

# Importable as a package (``-m study.landscape`` / ``-m lab.study.landscape``) and as a bare script, the
# same convention as triggers.py / hillclimb.py: put lab/ on sys.path so ``study.*`` resolves either way.
_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402
from study import registry  # noqa: E402
from study import triggers  # noqa: E402

# v2: elites / selections nested per model slug. v1 (flat {cell_key: rec}, single model) still loads:
# its records are read as LEGACY_MODEL / LEGACY_GOAL when they lack those fields.
ARCHIVE_SCHEMA = "instigation.landscape_archive.v2"
ARCHIVE_SCHEMA_V1 = "instigation.landscape_archive.v1"
ARCHIVE_PATH = registry.DATA_DIR / "instigation" / "landscape_archive.json"

# --------------------------------------------------------------------------- axes

# Axis values for the channel dimension. These are the lab/board/channel.py registry names (the
# ``--interface`` value / ``iface`` tag). CHANNEL_AXIS maps every accepted interface spelling to its
# axis value; "pastebin" is accepted as an alias of the registered "paste" channel. "js" and "html" are
# the code-comment channels (board posts rendered as comments in a served JavaScript file / HTML page).
# "irc" is no longer in the default QD grid but stays a valid axis value so older archive records load.
CHANNELS = ("forum", "irc", "wiki", "cache", "guestbook", "paste", "kv", "js", "html")
CHANNEL_AXIS = {c: c for c in CHANNELS}
CHANNEL_AXIS["pastebin"] = "paste"

# (lo, hi, label), inclusive bounds over seed_chars; hi=None is unbounded.
LENGTH_BANDS = ((0, 120, "le120"), (121, 200, "121-200"), (201, 320, "201-320"), (321, None, "gt320"))
BAND_LABELS = tuple(b[2] for b in LENGTH_BANDS)
# Soft reference ceiling for the unbounded top band, used only for the mild length regularizer.
GT320_SOFT_CAP = 640

# NO_WRITERS (a run nobody wrote in) is kept distinct from the classifier's own "NONE" code, so seeds that
# instigated nothing never share a cell with writes the classifier labelled NONE. UNRESOLVED = writers
# present but no usable code for any of them.
NO_WRITERS = "NO_WRITERS"
UNRESOLVED = "UNRESOLVED"
TRIGGER_AXIS = tuple(triggers.ENUM) + (UNRESOLVED, NO_WRITERS)
_TRIGGER_SET = frozenset(TRIGGER_AXIS)

ORIGINS = ("pilot", "scratch", "mutation")

# Model / goal axes are the config enums (never free text). Every v1 record was a qwen38, solvable run.
MODEL_AXIS = tuple(config.MODEL_ORDER)
_MODEL_SET = frozenset(config.MODELS)
GOAL_AXIS = tuple(config.GOALS)
LEGACY_MODEL = "qwen38"
LEGACY_GOAL = "solvable"
ENGAGEMENT_KEYS = ("board_reads", "relay_connections", "board_refs", "agents_engaged")

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
# run ids are script-generated identifiers (e.g. "forum-multi3-r0-unsolvable--qwen38"); bound them to
# a strict identifier charset so no free text can ride along in a record.
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

NEAR_TIE = 1e-9

RECORD_FIELDS = ("record_id", "cell_key", "trigger_code", "channel", "length_band", "seed_chars",
                 "instigation_score", "I", "n_writers", "engagement", "engagement_score", "bootstrap",
                 "origin", "parent_id", "generation", "run_id", "model", "goal")


def channel_axis(iface) -> str:
    """Axis value for a board interface name. Raises ValueError("E_ENUM_CHANNEL") on anything else."""
    if not isinstance(iface, str) or iface not in CHANNEL_AXIS:
        raise ValueError("E_ENUM_CHANNEL")
    return CHANNEL_AXIS[iface]


def length_band(seed_chars) -> str:
    """Band label for a seed's char count (the metric's seed_chars: sum of post-body chars)."""
    if isinstance(seed_chars, bool) or not isinstance(seed_chars, (int, float)) or not math.isfinite(seed_chars):
        raise ValueError("E_SEED_CHARS")
    n = int(seed_chars)
    if n < 0 or n != seed_chars:
        raise ValueError("E_SEED_CHARS")
    for lo, hi, label in LENGTH_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return label
    raise ValueError("E_SEED_CHARS")  # unreachable: bands cover [0, inf)


def _band_upper(label: str) -> int:
    for _lo, hi, lab in LENGTH_BANDS:
        if lab == label:
            return hi if hi is not None else GT320_SOFT_CAP
    raise ValueError("E_ENUM_BAND")


def cell_key(trigger_code, channel, band) -> str:
    """``"<trigger>|<channel>|<band>"`` after validating each part against its enum."""
    if not isinstance(trigger_code, str) or trigger_code not in _TRIGGER_SET:
        raise ValueError("E_ENUM_TRIGGER")
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise ValueError("E_ENUM_CHANNEL")
    if not isinstance(band, str) or band not in BAND_LABELS:
        raise ValueError("E_ENUM_BAND")
    return f"{trigger_code}|{channel}|{band}"


def split_cell_key(key) -> tuple[str, str, str]:
    """Inverse of cell_key, validating the parts. Raises ValueError("E_CELL_KEY") if malformed."""
    if not isinstance(key, str):
        raise ValueError("E_CELL_KEY")
    parts = key.split("|")
    if len(parts) != 3:
        raise ValueError("E_CELL_KEY")
    cell_key(*parts)
    return parts[0], parts[1], parts[2]


# --------------------------------------------------------------------------- primary trigger

def _best_code(codes) -> tuple[float, str] | None:
    """Highest-confidence valid enum code in a record's code list; ties -> earlier ENUM position."""
    best = None
    for item in codes if isinstance(codes, list) else ():
        if not isinstance(item, dict):
            continue
        code, conf = item.get("code"), item.get("confidence")
        if not isinstance(code, str) or code not in triggers.ENUM_SET:
            continue
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf):
            continue
        key = (float(conf), -triggers.ENUM.index(code))
        if best is None or key > best[0]:
            best = (key, code)
    return (best[0][0], best[1]) if best else None


def _metric_pwf(writers) -> dict:
    """Deterministic PEER_WROTE_FIRST per writer from the metric's own first-write times (the same
    authoritative rule as instigation.peer_wrote_first, which triggers.run applies to build the artifact's
    ``peer_wrote_first``). Used when the classifier artifact is missing or lacks that map."""
    from study import instigation  # lazy; pure stdlib
    agents = []
    for a in writers:
        t = a.get("time_to_first_write_s")
        t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) else None
        agents.append({"agent_id": a.get("agent_id"), "wrote": True, "_first_ts": t})
    return {str(k): bool(v) for k, v in instigation.peer_wrote_first(agents).items()}


def primary_trigger(trigger_artifact, metric) -> str:
    """Run-level trigger code (always a TRIGGER_AXIS value): what the seed instigated.

    A writer is PEER_WROTE_FIRST if the authoritative deterministic value says a distinct agent wrote
    strictly before it -- the artifact's ``peer_wrote_first[str(aid)]``, or, when the artifact is missing
    or lacks that map (classifier failure), the same rule applied to the metric's ``time_to_first_write_s``.
    Those writers were set off by a peer, not by the seed, so they do NOT vote: the run code is the
    majority over the remaining (seed-instigated) writers' highest-confidence valid classified codes
    (error records skipped; a classified PEER_WROTE_FIRST contradicting the authoritative False is
    ignored), ties to the EARLIEST writer (``time_to_first_write_s``, then agent id) among the tied codes.
    If none of those resolved: PEER_WROTE_FIRST when some writer was peer-instigated, else UNRESOLVED.
    No writers -> NO_WRITERS (distinct from the classifier's NONE code). Only enum strings are ever read
    out of the artifact.
    """
    agents = (metric or {}).get("agents") or []
    writers = [a for a in agents if isinstance(a, dict) and a.get("wrote")]
    if not writers:
        return NO_WRITERS

    def order(a):
        t = a.get("time_to_first_write_s")
        t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else math.inf
        aid = a.get("agent_id")
        return (t, aid if isinstance(aid, int) and not isinstance(aid, bool) else math.inf)

    writers.sort(key=order)
    art = trigger_artifact if isinstance(trigger_artifact, dict) else {}
    pwf = art.get("peer_wrote_first") if isinstance(art.get("peer_wrote_first"), dict) else {}
    if not pwf:
        try:
            pwf = _metric_pwf(writers)
        except (Exception, SystemExit):  # noqa: BLE001 -- malformed metric: no deterministic value
            pwf = {}
    records = art.get("records") if isinstance(art.get("records"), list) else []

    per_writer: list[str] = []  # seed-instigated writers' codes, earliest-first
    any_pwf = False
    for a in writers:
        aid = a.get("agent_id")
        if pwf.get(str(aid)) is True:
            any_pwf = True
            continue
        best = None
        for r in records:
            if not isinstance(r, dict) or "error_code" in r or r.get("agent_id") != aid:
                continue
            codes = r.get("codes")
            if pwf.get(str(aid)) is False and isinstance(codes, list):
                codes = [c for c in codes if not (isinstance(c, dict) and c.get("code") == "PEER_WROTE_FIRST")]
            cand = _best_code(codes)
            if cand is not None and (best is None or cand[0] > best[0]):
                best = cand
        if best is not None:
            per_writer.append(best[1])

    if not per_writer:
        return "PEER_WROTE_FIRST" if any_pwf else UNRESOLVED
    tally: dict[str, int] = {}
    for c in per_writer:
        tally[c] = tally.get(c, 0) + 1
    top = max(tally.values())
    tied = {c for c, n in tally.items() if n == top}
    result = next(c for c in per_writer if c in tied)
    if result not in _TRIGGER_SET:  # defensive: strictly an enum value
        raise ValueError("E_ENUM_TRIGGER")
    return result


# --------------------------------------------------------------------------- records & fitness

def _num(v, code: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(code)
    return float(v)


def _int(v, code: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and math.isfinite(v) and v == int(v):
            return int(v)
        raise ValueError(code)
    return v


def _engagement_score(rec) -> float:
    v = rec.get("engagement_score")
    if v is None and isinstance(rec.get("engagement"), dict):
        v = rec["engagement"].get("engagement_score")
    if v is None:
        return 0.0
    return _num(v, "E_ENGAGEMENT")


def model_axis(slug) -> str:
    """Validated model slug (a config.MODELS key). Raises ValueError("E_ENUM_MODEL") otherwise."""
    if not isinstance(slug, str) or slug not in _MODEL_SET:
        raise ValueError("E_ENUM_MODEL")
    return slug


def goal_axis(goal) -> str:
    """Validated task goal (a config.GOALS value). Raises ValueError("E_ENUM_GOAL") otherwise."""
    if not isinstance(goal, str) or goal not in GOAL_AXIS:
        raise ValueError("E_ENUM_GOAL")
    return goal


def sanitize_record(rec, *, legacy: bool = False) -> dict:
    """Validate a candidate archive record and return a clean copy holding only RECORD_FIELDS.

    Raises ValueError(<fixed code>) on any non-enum axis value, malformed id or non-numeric number. The
    cell key is recomputed from the axes (and must agree with a supplied one), and the length band must
    agree with seed_chars, so no record can sit in a cell it does not belong to. ``model`` must be a
    config.MODELS slug and ``goal`` a config.GOALS value; only with ``legacy=True`` (loading a v1 archive)
    is a missing/empty one read as LEGACY_MODEL / LEGACY_GOAL. A present-but-invalid value is never
    repaired, legacy or not.
    """
    if not isinstance(rec, dict):
        raise ValueError("E_RECORD")
    rid = rec.get("record_id")
    if not isinstance(rid, str) or not _HEX16.match(rid):
        raise ValueError("E_RECORD_ID")
    trig = rec.get("trigger_code")
    if not isinstance(trig, str) or trig not in _TRIGGER_SET:
        raise ValueError("E_ENUM_TRIGGER")
    chan = rec.get("channel")
    if not isinstance(chan, str) or chan not in CHANNELS:
        raise ValueError("E_ENUM_CHANNEL")
    seed_chars = _int(rec.get("seed_chars"), "E_SEED_CHARS")
    band = length_band(seed_chars)
    if rec.get("length_band") is not None and rec.get("length_band") != band:
        raise ValueError("E_ENUM_BAND")
    key = cell_key(trig, chan, band)
    if rec.get("cell_key") is not None and rec.get("cell_key") != key:
        raise ValueError("E_CELL_KEY")

    eng_in = rec.get("engagement") if isinstance(rec.get("engagement"), dict) else {}
    engagement = {k: _int(eng_in.get(k, 0), "E_ENGAGEMENT") for k in ENGAGEMENT_KEYS}
    escore = _engagement_score(rec)
    engagement["engagement_score"] = escore

    origin = rec.get("origin", "mutation")
    if origin not in ORIGINS:
        raise ValueError("E_ENUM_ORIGIN")
    parent = rec.get("parent_id")
    if parent is not None and (not isinstance(parent, str) or not _HEX16.match(parent)):
        raise ValueError("E_PARENT_ID")
    run_id = rec.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not _IDENT.match(run_id)):
        raise ValueError("E_RUN_ID")
    model = rec.get("model")
    if legacy and model in ("", None):
        model = LEGACY_MODEL
    model = model_axis(model)
    goal = rec.get("goal")
    if legacy and goal in ("", None):
        goal = LEGACY_GOAL
    goal = goal_axis(goal)
    bootstrap = rec.get("bootstrap", False)
    if not isinstance(bootstrap, bool):
        raise ValueError("E_BOOTSTRAP")

    raw_i = rec.get("I")
    return {
        "record_id": rid, "cell_key": key, "trigger_code": trig, "channel": chan, "length_band": band,
        "seed_chars": seed_chars,
        "instigation_score": _num(rec.get("instigation_score"), "E_SCORE"),
        "I": None if raw_i is None else _num(raw_i, "E_SCORE"),
        "n_writers": _int(rec.get("n_writers", 0), "E_N_WRITERS"),
        "engagement": engagement, "engagement_score": escore, "bootstrap": bootstrap, "origin": origin,
        "parent_id": parent, "generation": _int(rec.get("generation", 0), "E_GENERATION"),
        "run_id": run_id, "model": model, "goal": goal,
    }


def _objective(rec, raw: bool) -> float:
    """Raw pile-in x speed ``I`` when ``raw`` (the elite objective), else the metric's brevity-normalized
    instigation_score (fallback for records stored before ``I`` was recorded)."""
    if raw:
        return float(rec["I"])
    return float(rec["instigation_score"])


def _has_raw(rec) -> bool:
    v = rec.get("I")
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fitness(rec, raw=None) -> tuple:
    """Sort key (larger is better): (length-regularized objective, engagement_score, -seed_chars).

    The objective is the metric's RAW ``I`` (write_fraction x mean speed; pile-in x speed) -- NOT the
    brevity-normalized ``instigation_score``, whose I/(1+ln(1+seed_chars/1000)) would put length pressure
    inside every band. The only length term is a GENTLE penalty for exceeding the cell band's upper bound
    (objective / (1 + 0.1 * overflow_fraction)); there is no byte-minimization pressure inside the band.
    The final element prefers shorter seeds. Records without ``I`` (stored by an older driver) fall back
    to instigation_score; ``raw`` forces the basis (``beats`` uses one basis for both sides). Use ``beats``
    for comparisons that must treat near-ties (abs diff < 1e-9) as ties.
    """
    if raw is None:
        raw = _has_raw(rec)
    seed_chars = int(rec["seed_chars"])
    band = rec.get("length_band") or length_band(seed_chars)
    upper = _band_upper(band)
    overflow = max(0, seed_chars - upper) / upper if upper else 0.0
    primary = _objective(rec, raw) / (1.0 + 0.1 * overflow)
    return (primary, _engagement_score(rec), -seed_chars)


def beats(a, b) -> bool:
    """True iff record ``a`` is strictly better than ``b``. Both are compared on raw ``I`` when both carry
    it, else both on instigation_score. Scores within NEAR_TIE are ties and fall through to the next
    criterion; a full tie on score and engagement goes to the shorter seed, and an exact tie keeps the
    incumbent ``b``."""
    raw = _has_raw(a) and _has_raw(b)
    fa, fb = fitness(a, raw), fitness(b, raw)
    for x, y in zip(fa[:2], fb[:2]):
        if abs(x - y) >= NEAR_TIE:
            return x > y
    return fa[2] > fb[2]


# --------------------------------------------------------------------------- archive

def _neighbours(key: str):
    """Cells one step away: any other trigger or channel (categorical), or the adjacent length band."""
    t, c, b = split_cell_key(key)
    for t2 in TRIGGER_AXIS:
        if t2 != t:
            yield cell_key(t2, c, b)
    for c2 in CHANNELS:
        if c2 != c:
            yield cell_key(t, c2, b)
    i = BAND_LABELS.index(b)
    for j in (i - 1, i + 1):
        if 0 <= j < len(BAND_LABELS):
            yield cell_key(t, c, BAND_LABELS[j])


def _model_rank(slug: str) -> int:
    return MODEL_AXIS.index(slug) if slug in MODEL_AXIS else len(MODEL_AXIS)


def _count(n) -> int | None:
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def _map_entry(r) -> dict:
    return {"record_id": r["record_id"], "instigation_score": r["instigation_score"],
            "length": r["seed_chars"], "trigger_code": r["trigger_code"], "channel": r["channel"],
            "n_writers": r["n_writers"], "engagement": dict(r["engagement"]), "model": r["model"],
            "goal": r["goal"]}


class Archive:
    """Per-model MAP-Elites archive: {model_slug: {cell_key: elite record}}, plus per-(model, cell)
    parent-selection counts.

    Each agent model gets its own grid over the same 3-part cell keys (a seed that instigates one model
    need not instigate another, so models never displace each other's elites). Cross-model views:
    ``elites`` / ``to_map()`` give the best elite per cell across models (each record names its model),
    ``coverage()`` counts filled (model, cell) pairs and ``coverage_cells()`` distinct cells.

    ``goal`` (optional, a config.GOALS value) pins the archive to one task goal: ``consider`` rejects
    other-goal records (E_GOAL_MISMATCH) and ``load`` skips them (counted in ``n_other_goal_on_load``), so
    legacy solvable elites never compete with unsolvable ones. ``goal=None`` keeps every goal.

    Persisted as JSON (numbers / ids / enums only) at ``data/instigation/landscape_archive.json`` by
    default, written atomically via registry._atomic_write.
    """

    N_CELLS_POSSIBLE = len(TRIGGER_AXIS) * len(CHANNELS) * len(LENGTH_BANDS)
    N_PAIRS_POSSIBLE = N_CELLS_POSSIBLE * len(MODEL_AXIS)

    def __init__(self, path=None, goal=None):
        self.path = Path(path) if path is not None else ARCHIVE_PATH
        self.goal = None if goal is None else goal_axis(goal)
        self._elites: dict[str, dict[str, dict]] = {}       # model -> cell_key -> record
        self._selections: dict[str, dict[str, int]] = {}    # model -> cell_key -> count
        self.n_dropped_on_load = 0
        self.n_other_goal_on_load = 0

    # ---- persistence
    @classmethod
    def load(cls, path=None, goal=None) -> "Archive":
        """Load a v2 (per-model) or v1 (flat, single-model) archive; a missing/unreadable file gives an
        empty one. Invalid stored records are dropped (counted in ``n_dropped_on_load``), never repaired
        from their content; v1 records lacking model/goal are read as LEGACY_MODEL / LEGACY_GOAL. A
        loaded archive is always saved back as v2."""
        a = cls(path, goal=goal)
        try:
            with open(a.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return a
        if not isinstance(data, dict):
            return a
        elites = data.get("elites") if isinstance(data.get("elites"), dict) else {}
        sel = data.get("selections") if isinstance(data.get("selections"), dict) else {}
        if data.get("schema") == ARCHIVE_SCHEMA:
            nested = [(m, cells, False) for m, cells in elites.items()]
            nested_sel = list(sel.items())
        else:  # v1 (or unversioned): flat {cell_key: rec}, selections {cell_key: n}
            nested = [(None, elites, True)]
            nested_sel = [(LEGACY_MODEL, sel)]
        for model, cells, legacy in nested:
            if not isinstance(cells, dict) or (model is not None and model not in _MODEL_SET):
                a.n_dropped_on_load += len(cells) if isinstance(cells, dict) else 1
                continue
            for key, rec in cells.items():
                try:
                    clean = sanitize_record(rec, legacy=legacy)
                except ValueError:
                    a.n_dropped_on_load += 1
                    continue
                if clean["cell_key"] != key or (model is not None and clean["model"] != model):
                    a.n_dropped_on_load += 1
                    continue
                if a.goal is not None and clean["goal"] != a.goal:
                    a.n_other_goal_on_load += 1
                    continue
                cur = a._elites.get(clean["model"], {}).get(key)
                if cur is None or beats(clean, cur):  # v1 cell collisions cannot happen; be safe anyway
                    a._elites.setdefault(clean["model"], {})[key] = clean
        for model, cells in nested_sel:
            if model not in _MODEL_SET or not isinstance(cells, dict):
                continue
            for key, n in cells.items():
                try:
                    split_cell_key(key)
                except ValueError:
                    continue
                if _count(n) is not None:
                    a._selections.setdefault(model, {})[key] = n
        return a

    def save(self, path=None) -> Path:
        p = Path(path) if path is not None else self.path
        doc = {"schema": ARCHIVE_SCHEMA,
               "elites": {m: {k: cells[k] for k in sorted(cells)}
                          for m, cells in sorted(self._elites.items()) if cells},
               "selections": {m: {k: cells[k] for k in sorted(cells)}
                              for m, cells in sorted(self._selections.items()) if cells}}
        if self.goal is not None:
            doc["goal"] = self.goal
        registry._atomic_write(p, json.dumps(doc, indent=2, sort_keys=True))
        return p

    # ---- insertion
    def consider(self, rec) -> bool:
        """Insert ``rec`` if its (model, cell) is empty or it beats that elite. Returns True iff inserted.
        Raises ValueError(<fixed code>) for an invalid record (fail closed), including a record without a
        valid model / goal, or one whose goal differs from a goal-pinned archive (E_GOAL_MISMATCH)."""
        clean = sanitize_record(rec)
        if self.goal is not None and clean["goal"] != self.goal:
            raise ValueError("E_GOAL_MISMATCH")
        cells = self._elites.setdefault(clean["model"], {})
        key = clean["cell_key"]
        cur = cells.get(key)
        if cur is None or beats(clean, cur):
            cells[key] = clean
            return True
        return False

    # ---- views
    def _pairs(self, model=None):
        """(model, cell_key, record) over every elite (or one model's), model-order then key-sorted."""
        if model is not None:
            model = model_axis(model)
        for m in sorted(self._elites, key=lambda s: (_model_rank(s), s)):
            if model is not None and m != model:
                continue
            for k in sorted(self._elites[m]):
                yield m, k, self._elites[m][k]

    def models(self) -> list[str]:
        """Model slugs holding at least one elite, in config.MODEL_ORDER."""
        return [m for m in sorted(self._elites, key=lambda s: (_model_rank(s), s)) if self._elites[m]]

    def elites_by_model(self) -> dict:
        """{slug: {cell_key: record}} (copies of the dicts; records are the stored clean records)."""
        return {m: dict(self._elites[m]) for m in self.models()}

    def best_by_cell(self) -> dict:
        """{cell_key: record}: the best elite per cell across models (``beats``; an exact tie goes to the
        model earlier in config.MODEL_ORDER)."""
        out: dict[str, dict] = {}
        for _m, k, r in self._pairs():
            if k not in out or beats(r, out[k]):
                out[k] = r
        return out

    @property
    def elites(self) -> dict:
        """Cross-model view {cell_key: best record}; each record carries its ``model``."""
        return self.best_by_cell()

    def elite(self, model, key):
        """The elite of one (model, cell) pair, or None."""
        return self._elites.get(model_axis(model), {}).get(key)

    def coverage(self, model=None) -> int:
        """Filled (model, cell) pairs; with ``model``, that model's filled cells."""
        return sum(1 for _ in self._pairs(model))

    def coverage_cells(self) -> int:
        """Distinct cell keys filled by any model."""
        return len({k for _m, k, _r in self._pairs()})

    def mean_elite_score(self, model=None) -> float:
        scores = [r["instigation_score"] for _m, _k, r in self._pairs(model)]
        return sum(scores) / len(scores) if scores else 0.0

    def to_map(self, model=None) -> dict:
        """{cell_key: projection}: the best elite per cell across models, or one model's grid."""
        src = self.best_by_cell() if model is None else {k: r for _m, k, r in self._pairs(model)}
        return {k: _map_entry(r) for k, r in sorted(src.items())}

    def tallies(self, model=None) -> dict:
        """Per-axis counts over (model, cell) elites -- every model combined (``model=None``, which also
        tallies the ``model`` and ``goal`` axes) or one model."""
        axes = ("trigger_code", "channel", "length_band", "goal") + (("model",) if model is None else ())
        out = {axis: {} for axis in axes}
        for _m, _k, r in self._pairs(model):
            for axis in out:
                out[axis][r[axis]] = out[axis].get(r[axis], 0) + 1
        return {axis: dict(sorted(d.items())) for axis, d in out.items()}

    def by_model(self) -> dict:
        """{slug: {"cells", "coverage", "mean_elite_score", "tallies"}} for every model with an elite.
        (Display metadata such as origin / size is added by the scoreboard, not stored here.)"""
        return {m: {"cells": self.to_map(m), "coverage": self.coverage(m),
                    "mean_elite_score": self.mean_elite_score(m), "tallies": self.tallies(m)}
                for m in self.models()}

    def axes(self) -> dict:
        """Axis enums (all fixed): the three cell axes plus the model and goal axes."""
        return {"trigger_code": list(TRIGGER_AXIS), "channel": list(CHANNELS),
                "length_band": list(BAND_LABELS), "model": list(MODEL_AXIS), "goal": list(GOAL_AXIS)}

    # ---- selection
    def select_parents(self, k: int, generation: int, models=None) -> list[str]:
        """Pick ``k`` parent record ids, favouring EXPLORATION over raw score, over (model, cell) pairs.

        Pairs (restricted to ``models`` when given) are ranked by:
          1. fewest past selections of the pair;
          2. most requested models MISSING that cell (a seed elite under one model but untested / weak
             elsewhere is the best transfer candidate) -- 0 for every pair with a single model;
          3. most empty neighbour cells in the pair's own model grid;
          4. fewest filled cells in the pair's model (under-filled models first);
          5. a deterministic rotation of the sorted pairs by ``generation``.
        Distinct record ids are taken first within a pass (the same seed can be elite under several
        models); if ``k`` exceeds them the ranking is cycled. Each pick increments its pair's selection
        count (persisted by save). Deterministic given the archive state and ``generation``.
        """
        k = int(k)
        if models is not None:
            models = [model_axis(m) for m in models]
        scope = list(models) if models is not None else self.models()
        pairs = [(m, key) for m, key, _r in self._pairs() if m in scope]
        if k <= 0 or not pairs:
            return []
        n = len(pairs)
        rot = int(generation) % n
        rotation = {p: (i - rot) % n for i, p in enumerate(pairs)}
        fill = {m: len(self._elites.get(m, {})) for m in scope}
        missing = {(m, key): sum(1 for m2 in scope if key not in self._elites.get(m2, {})) for m, key in pairs}
        empty_nb = {(m, key): sum(1 for nb in _neighbours(key) if nb not in self._elites[m]) for m, key in pairs}

        def rank(p):
            return (self._selections.get(p[0], {}).get(p[1], 0), -missing[p], -empty_nb[p], fill[p[0]],
                    rotation[p])

        picked: list[str] = []
        while len(picked) < k:
            seen: set[str] = set()
            for m, key in sorted(pairs, key=rank):
                if len(picked) >= k:
                    break
                rid = self._elites[m][key]["record_id"]
                if rid in seen:
                    continue
                seen.add(rid)
                picked.append(rid)
                sel = self._selections.setdefault(m, {})
                sel[key] = sel.get(key, 0) + 1
        return picked

    def leaderboard(self, model=None) -> list[dict]:
        """(model, cell) elites flattened, best first (score desc, then shorter, cell, model order)."""
        rows = sorted((r for _m, _k, r in self._pairs(model)),
                      key=lambda r: (-r["instigation_score"], r["seed_chars"], r["cell_key"],
                                     _model_rank(r["model"])))
        return [dict(r) for r in rows]


def main(argv: list[str]) -> int:
    """Print archive summary numbers / enum slugs only: coverage, mean elite score, cells possible."""
    import argparse
    ap = argparse.ArgumentParser(prog="lab.study.landscape")
    ap.add_argument("--archive", default=None)
    a = ap.parse_args(argv)
    arc = Archive.load(a.archive)
    print(json.dumps({"coverage": arc.coverage(), "coverage_cells": arc.coverage_cells(),
                      "coverage_by_model": {m: arc.coverage(m) for m in arc.models()},
                      "mean_elite_score": round(arc.mean_elite_score(), 12),
                      "n_cells_possible": Archive.N_CELLS_POSSIBLE, "n_pairs_possible": Archive.N_PAIRS_POSSIBLE,
                      "n_dropped": arc.n_dropped_on_load, "n_other_goal": arc.n_other_goal_on_load}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
