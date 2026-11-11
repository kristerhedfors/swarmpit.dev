#!/usr/bin/env python3
"""Export every candidate-seeded study run as the landscape explorer's data file. Numbers, enums and ids only.

    python3 lab/study/export_explorer.py [--out PATH] [--runs DIR] [--registry PATH] [--archive PATH]

Writes ``site/public/data/instigation/explorer.json`` (schema ``instigation.explorer.v1``) so the site can
browse the data along every dimension: goal x model x channel x seed (length band, origin, generation) x
trigger code, plus both goals' MAP-Elites landscapes. Content-in / numbers-out:

  * one episode per run directory under ``--runs`` whose run.json names ``params.candidate_id`` (any goal,
    model or channel, including the earlier solvable validation runs). Runs with no run.json, a void run
    (run.json ``fatal``; aborted runs already live under ``runs/_aborted``) and ``_``/``.`` directories are
    skipped and counted under a fixed reason code;
  * the per-run metric comes from ``instigation.compute`` (structural keys only); the run's trigger code from
    ``landscape.primary_trigger`` over the consensus ``instigation/triggers.json`` artifact, of which only
    enum codes, numbers and validated slugs are read;
  * the seed table joins the registry (ids / numbers / enums) with the metric's ``seed_chars``; an ancestor
    seed that no episode ran is sized by ``len()`` of its post bodies only (the text is never emitted);
  * the landscapes are ``scoreboard.build_landscape`` over each goal's archive (``qd.goal_archive_path`` +
    ``landscape.Archive.load(goal=...)``).

The document is validated against a strict whitelist (fixed keys, enum strings, 16-hex record ids, an
identifier-charset run id, finite numbers) BEFORE anything is written; then the exact bytes are gated
fail-closed by tools/leakgate.py (``--scan`` the candidate, ``--root`` the repo) and by
site/tools/check_public.py (over a scratch public tree holding only this file), and atomically replace the
published file. A post-publish check that fails rolls the file back to its previous bytes. The CLI prints
one numbers-only summary line; every failure is a fixed error code.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402
from board.channel import CHANNELS as BOARD_CHANNELS  # noqa: E402
from study import instigation  # noqa: E402
from study import landscape as L  # noqa: E402
from study import qd  # noqa: E402
from study import registry  # noqa: E402
from study import scoreboard  # noqa: E402
from study import triggers  # noqa: E402

SCHEMA = "instigation.explorer.v1"
EXPLORER_PATH = scoreboard.SCOREBOARD_PATH.with_name("explorer.json")
CHECK_PUBLIC = registry.REPO_DIR / "site" / "tools" / "check_public.py"
# Retired models (no longer run) stay browsable; this mirrors config's per-model "retired" flag and
# keeps gpt-oss-120b marked even on a config that predates the flag.
_RETIRED_FALLBACK = frozenset({"gptoss120"})

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+-]{0,63}$")
_CODE = re.compile(r"^E_[A-Z0-9_]{1,30}$")

TOP_KEYS = ("schema", "generated_ts", "dims", "seeds", "episodes", "landscapes")
DIM_KEYS = ("model", "channel", "length_band", "trigger_code", "goal", "origin")
MODEL_DIM_KEYS = ("slug", "label", "origin", "size_b", "provider", "retired")
CHANNEL_DIM_KEYS = ("id", "label")
SEED_KEYS = ("seed_chars", "length_band", "origin", "bootstrap", "parent_id", "generation", "gen_model")
EPISODE_KEYS = ("run_id", "record_id", "goal", "model", "channel", "rep", "generation", "n_agents", "budget",
                "n_writers", "write_fraction", "S_mean", "I", "instigation_score", "run_ttfw_s",
                "run_first_write_step", "pile_in_onset_t_s", "posts_over_time", "engagement", "trigger_code",
                "agent_codes", "panel", "classifier_errors", "elite_in")
ENGAGEMENT_KEYS = ("board_reads", "relay_connections", "board_refs", "agents_engaged", "engagement_score")
PANEL_KEYS = ("slugs", "agreement_mean")
TRIGGER_CODES = tuple(L.TRIGGER_AXIS)
AGENT_CODES = frozenset(triggers.ENUM)


class ExportError(RuntimeError):
    """args[0] is a fixed error code (E_SCHEMA / E_LEAK / E_GATE / E_PUBLIC / E_LANDSCAPE / E_WRITE)."""


def _fail(code: str = "E_SCHEMA") -> ExportError:
    return ExportError(code)


# --------------------------------------------------------------------------- small helpers

def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _load_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _rnd(v, nd: int = 6):
    return None if v is None else round(float(v), nd)


def is_retired(slug: str) -> bool:
    fn = getattr(config, "is_retired", None)
    flag = bool(fn(slug)) if callable(fn) else bool((config.MODELS.get(slug) or {}).get("retired", False))
    return flag or slug in _RETIRED_FALLBACK


def _model_dims() -> list[dict]:
    out = []
    for slug in config.MODEL_ORDER:
        m = config.MODELS[slug]
        out.append({"slug": slug, "label": m.get("label") or slug, "origin": scoreboard.model_origin(slug),
                    "size_b": scoreboard._model_size_b(slug), "provider": m.get("provider"),
                    "retired": is_retired(slug)})
    return out


def _channel_dims(present) -> list[dict]:
    return [{"id": c, "label": BOARD_CHANNELS[c].label} for c in L.CHANNELS
            if c in present and c in BOARD_CHANNELS]


def _seed_chars_from_registry(rid: str, row: dict):
    """Sum of post-body len() of a registry seed (the metric's seed_chars definition), or None. The seed
    text is read only to be counted; nothing of it leaves this function."""
    cands = []
    cp = row.get("chatter_path") if isinstance(row, dict) else None
    if isinstance(cp, str) and cp:
        p = Path(cp)
        cands.append(p if p.is_absolute() else registry.REPO_DIR / p)
    cands.append(registry.candidate_path(rid))
    for c in cands:
        try:
            if not c.is_file():
                continue
        except OSError:
            continue
        seed = _load_json(c)
        if seed is None:
            continue
        return sum(len(b) for b in instigation._iter_seed_bodies(seed))
    return None


# --------------------------------------------------------------------------- per-run projection

def _agent_code(records, aid: int):
    """The classifier's (consensus) code for one writer: its highest-confidence valid enum code over
    non-error records (landscape's rule), else None."""
    best = None
    for r in records:
        if not isinstance(r, dict) or "error_code" in r or r.get("agent_id") != aid:
            continue
        cand = L._best_code(r.get("codes"))
        if cand is not None and (best is None or cand[0] > best[0]):
            best = cand
    return best[1] if best else None


def _panel_block(artifact):
    panel = artifact.get("panel") if isinstance(artifact, dict) else None
    if not isinstance(panel, dict):
        return None
    slugs = [s for s in (panel.get("slugs") or []) if isinstance(s, str) and s in config.MODELS] \
        if isinstance(panel.get("slugs"), list) else []
    agr = panel.get("agreement") if isinstance(panel.get("agreement"), dict) else {}
    vals = [float(v) for v in agr.values() if _isnum(v) and 0.0 <= v <= 1.0]
    return {"slugs": list(dict.fromkeys(slugs)),
            "agreement_mean": round(sum(vals) / len(vals), 6) if vals else None}


def _episode(run_dir: Path, meta: dict, rid: str, metric: dict) -> dict:
    """Project one run onto EPISODE_KEYS (elite_in / generation are filled by the caller)."""
    artifact = _load_json(run_dir / "instigation" / "triggers.json")
    if not isinstance(artifact, dict):
        artifact = None
    n_writers = int(metric.get("n_writers") or 0)
    try:
        code = L.primary_trigger(artifact, metric)
    except (Exception, SystemExit):  # noqa: BLE001 -- malformed artifact: no usable code
        code = L.UNRESOLVED if n_writers else L.NO_WRITERS
    records = artifact.get("records") if artifact and isinstance(artifact.get("records"), list) else []
    agent_codes = []
    for a in metric.get("agents") or []:
        aid = a.get("agent_id")
        agent_codes.append(_agent_code(records, aid) if a.get("wrote") else None)
    eng = metric.get("engagement") or {}
    pot = [[_rnd(t), int(p), int(w)] for t, p, w in (metric.get("posts_over_time") or [])]
    return {
        "run_id": metric.get("run_id") if isinstance(metric.get("run_id"), str) else run_dir.name,
        "record_id": rid,
        "goal": config.run_goal(meta),
        "model": config.run_slug_of(meta.get("harness") or {}),
        "channel": L.channel_axis(meta.get("iface")),
        "rep": meta.get("rep"),
        "generation": 0,
        "n_agents": metric.get("n_agents"),
        "budget": metric.get("budget"),
        "n_writers": n_writers,
        "write_fraction": _rnd(metric.get("write_fraction"), 9),
        "S_mean": _rnd(metric.get("S_mean"), 9),
        "I": _rnd(metric.get("I"), 9),
        "instigation_score": _rnd(metric.get("instigation_score"), 9),
        "run_ttfw_s": _rnd(metric.get("run_time_to_first_write_s")),
        "run_first_write_step": metric.get("run_first_write_step"),
        "pile_in_onset_t_s": _rnd(metric.get("pile_in_onset_t_s")),
        "posts_over_time": pot,
        "engagement": {k: (_rnd(eng.get(k), 9) if k == "engagement_score" else eng.get(k))
                       for k in ENGAGEMENT_KEYS},
        "trigger_code": code,
        "agent_codes": agent_codes,
        "panel": _panel_block(artifact),
        "classifier_errors": sum(1 for r in records if isinstance(r, dict) and "error_code" in r),
        "elite_in": [],
    }


def collect_episodes(runs_dir: Path, compute=instigation.compute) -> tuple[list[dict], dict, dict]:
    """(episodes, {record_id: seed_chars}, {skip_code: n}) over the top-level run dirs of ``runs_dir``."""
    episodes, chars, skipped = [], {}, {}

    def skip(code):
        skipped[code] = skipped.get(code, 0) + 1

    try:
        dirs = sorted(d for d in Path(runs_dir).iterdir() if d.is_dir())
    except OSError:
        raise _fail("E_RUNS") from None
    seen_ids = set()
    for d in dirs:
        if d.name.startswith(("_", ".")):
            continue
        meta = _load_json(d / "run.json")
        if not isinstance(meta, dict):
            skip("E_NO_RUNJSON")
            continue
        params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
        rid = params.get("candidate_id")
        if rid is None:
            continue  # not a candidate-seeded study run (pilot grid etc.): out of scope, not a skip
        if not isinstance(rid, str) or not _HEX16.match(rid):
            skip("E_RECORD_ID")
            continue
        if meta.get("fatal"):
            skip("E_VOID")
            continue
        if config.run_goal(meta) not in config.GOALS:
            skip("E_GOAL")
            continue
        if config.run_slug_of(meta.get("harness") or {}) not in config.MODELS:
            skip("E_MODEL")
            continue
        if not (isinstance(meta.get("iface"), str) and meta["iface"] in L.CHANNEL_AXIS):
            skip("E_CHANNEL")
            continue
        try:
            metric = compute(d)
        except (Exception, SystemExit):  # noqa: BLE001 -- unreadable run: fixed code only
            skip("E_METRIC")
            continue
        sc = metric.get("seed_chars")
        if not (_isint(sc) and sc >= 0):
            skip("E_SEED_CHARS")
            continue
        try:
            ep = _episode(d, meta, rid, metric)
        except (Exception, SystemExit):  # noqa: BLE001
            skip("E_EPISODE")
            continue
        if not (isinstance(ep["run_id"], str) and _IDENT.match(ep["run_id"])) or ep["run_id"] in seen_ids:
            skip("E_RUN_ID")
            continue
        try:  # per-episode whitelist now, so one odd run is skipped rather than failing the export
            _validate_episode(ep, {rid: {"generation": ep["generation"]}}, L.CHANNELS)
        except ExportError:
            skip("E_EPISODE_SCHEMA")
            continue
        seen_ids.add(ep["run_id"])
        chars.setdefault(rid, sc)
        episodes.append(ep)
    return episodes, chars, skipped


# --------------------------------------------------------------------------- seeds / landscapes

def build_seeds(chars: dict, rows: dict) -> dict:
    """{record_id: seed row} for every episode seed plus each one's registry ancestry (when sizable)."""
    out = {}

    def row_of(rid):
        return rows.get(rid) if isinstance(rows.get(rid), dict) else {}

    def seed(rid, sc):
        row = row_of(rid)
        parent = row.get("parent_id")
        gen_model = row.get("model")
        gen = row.get("generation")
        origin = row.get("origin") if row.get("origin") in L.ORIGINS else registry.default_origin(parent)
        return {"seed_chars": int(sc), "length_band": L.length_band(int(sc)), "origin": origin,
                "bootstrap": bool(row.get("bootstrap", False)),
                "parent_id": parent if isinstance(parent, str) and _HEX16.match(parent) else None,
                "generation": gen if _isint(gen) and gen >= 0 else 0,
                "gen_model": gen_model if isinstance(gen_model, str) and gen_model in config.MODELS else None}

    for rid in sorted(chars):
        out[rid] = seed(rid, chars[rid])
    queue = [s["parent_id"] for s in out.values() if s["parent_id"]]
    while queue:
        rid = queue.pop()
        if rid in out or rid not in rows:
            continue
        sc = _seed_chars_from_registry(rid, row_of(rid))
        if not (_isint(sc) and sc >= 0):
            continue
        out[rid] = seed(rid, sc)
        if out[rid]["parent_id"]:
            queue.append(out[rid]["parent_id"])
    return {k: out[k] for k in sorted(out)}


def load_archives(archive_base: Path = None) -> dict:
    """{goal: Archive} pinned to each goal, from qd.goal_archive_path."""
    base = Path(archive_base) if archive_base is not None else L.ARCHIVE_PATH
    return {g: L.Archive.load(qd.goal_archive_path(g, base), goal=g) for g in config.GOALS}


def build_landscapes(archives: dict) -> dict:
    out = {}
    for g in config.GOALS:
        try:
            out[g] = scoreboard.build_landscape(archives[g], goal=g)
        except (scoreboard.ScoreboardError, KeyError):
            raise _fail("E_LANDSCAPE") from None
    return out


def mark_elites(episodes: list[dict], archives: dict) -> None:
    """Fill each episode's ``elite_in``: the cell keys of its (model, goal) grid whose current elite is its
    record. An elite that names the run it was measured in marks that run only (when it is exported);
    otherwise every exported run of that record on the cell's channel is marked."""
    by_run = {e["run_id"]: e for e in episodes}
    for g, arc in archives.items():
        for slug, cells in arc.elites_by_model().items():
            for key, rec in cells.items():
                rid, chan = rec.get("record_id"), rec.get("channel")
                run = by_run.get(rec.get("run_id"))
                if run is not None and (run["record_id"], run["model"], run["goal"], run["channel"]) \
                        == (rid, slug, g, chan):
                    targets = [run]
                else:
                    targets = [e for e in episodes if (e["record_id"], e["model"], e["goal"], e["channel"])
                               == (rid, slug, g, chan)]
                for e in targets:
                    if key not in e["elite_in"]:
                        e["elite_in"].append(key)
    for e in episodes:
        e["elite_in"].sort()


def _ep_sort_key(e):
    return (config.GOALS.index(e["goal"]), config.MODEL_ORDER.index(e["model"]), L.CHANNELS.index(e["channel"]),
            e["record_id"], e["rep"] if _isint(e["rep"]) else -1, e["run_id"])


def build(runs_dir: Path = None, *, registry_path: Path = None, archive_base: Path = None,
          compute=instigation.compute, generated_ts=None) -> tuple[dict, dict]:
    """(explorer document, {skip_code: n}). The document is validated before it is returned."""
    runs_dir = Path(runs_dir) if runs_dir is not None else config.RUNS
    episodes, chars, skipped = collect_episodes(runs_dir, compute)
    rows = registry.load(registry_path)
    seeds = build_seeds(chars, rows)
    for e in episodes:
        e["generation"] = seeds[e["record_id"]]["generation"]
    archives = load_archives(archive_base)
    mark_elites(episodes, archives)
    episodes.sort(key=_ep_sort_key)
    landscapes = build_landscapes(archives)
    present = {e["channel"] for e in episodes}
    for ls in landscapes.values():
        present |= {c["channel"] for c in ls["cells"].values()}
    doc = {
        "schema": SCHEMA,
        "generated_ts": round(time.time() if generated_ts is None else generated_ts, 3),
        "dims": {"model": _model_dims(), "channel": _channel_dims(present),
                 "length_band": list(L.BAND_LABELS), "trigger_code": list(TRIGGER_CODES),
                 "goal": list(config.GOALS), "origin": list(L.ORIGINS)},
        "seeds": seeds,
        "episodes": episodes,
        "landscapes": landscapes,
    }
    validate(doc)
    return doc, skipped


# --------------------------------------------------------------------------- strict validation

def _num(v, *, nullable=False, integer=False, lo=None, hi=None):
    if v is None and nullable:
        return
    if not _isnum(v) or (integer and not _isint(v)):
        raise _fail()
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _fail()


def _keys(obj, keys):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise _fail()


def _hex(v, *, nullable=False):
    if v is None and nullable:
        return
    if not isinstance(v, str) or not _HEX16.match(v):
        raise _fail()


def _enum(v, allowed, *, nullable=False):
    if v is None and nullable:
        return
    if not isinstance(v, str) or v not in allowed:
        raise _fail()


def validate(doc) -> None:
    """Whitelist check of the whole document. Raises ExportError('E_SCHEMA') (or E_LANDSCAPE) on any
    deviation and never echoes the offending value."""
    _keys(doc, TOP_KEYS)
    if doc["schema"] != SCHEMA:
        raise _fail()
    _num(doc["generated_ts"], lo=0)

    dims = doc["dims"]
    _keys(dims, DIM_KEYS)
    if not isinstance(dims["model"], list) or [m.get("slug") if isinstance(m, dict) else None
                                               for m in dims["model"]] != list(config.MODEL_ORDER):
        raise _fail()
    for m in dims["model"]:
        _keys(m, MODEL_DIM_KEYS)
        slug = m["slug"]
        if not (isinstance(m["label"], str) and _LABEL.match(m["label"])
                and m["label"] == (config.MODELS[slug].get("label") or slug)):
            raise _fail()
        if m["origin"] != scoreboard.model_origin(slug) or m["provider"] not in config.PROVIDERS:
            raise _fail()
        if m["provider"] != config.MODELS[slug]["provider"] or not isinstance(m["retired"], bool):
            raise _fail()
        _num(m["size_b"], nullable=True, integer=True, lo=1)
    chans = dims["channel"]
    if not isinstance(chans, list) or not chans:
        raise _fail()
    chan_ids = []
    for c in chans:
        _keys(c, CHANNEL_DIM_KEYS)
        _enum(c["id"], L.CHANNELS)
        if c["id"] not in BOARD_CHANNELS or c["label"] != BOARD_CHANNELS[c["id"]].label \
                or not (isinstance(c["label"], str) and _LABEL.match(c["label"])):
            raise _fail()
        chan_ids.append(c["id"])
    if len(set(chan_ids)) != len(chan_ids):
        raise _fail()
    if (dims["length_band"] != list(L.BAND_LABELS) or dims["trigger_code"] != list(TRIGGER_CODES)
            or dims["goal"] != list(config.GOALS) or dims["origin"] != list(L.ORIGINS)):
        raise _fail()

    seeds = doc["seeds"]
    if not isinstance(seeds, dict):
        raise _fail()
    for rid, s in seeds.items():
        _hex(rid)
        _keys(s, SEED_KEYS)
        _num(s["seed_chars"], integer=True, lo=0)
        if s["length_band"] != L.length_band(s["seed_chars"]):
            raise _fail()
        _enum(s["origin"], L.ORIGINS)
        if not isinstance(s["bootstrap"], bool):
            raise _fail()
        _hex(s["parent_id"], nullable=True)
        _num(s["generation"], integer=True, lo=0)
        _enum(s["gen_model"], config.MODELS, nullable=True)

    eps = doc["episodes"]
    if not isinstance(eps, list):
        raise _fail()
    run_ids = set()
    for e in eps:
        _validate_episode(e, seeds, chan_ids)
        if e["run_id"] in run_ids:
            raise _fail()
        run_ids.add(e["run_id"])

    ls = doc["landscapes"]
    _keys(ls, config.GOALS)
    for g in config.GOALS:
        try:
            scoreboard.validate_landscape(ls[g])
        except scoreboard.ScoreboardError:
            raise _fail("E_LANDSCAPE") from None
        if ls[g]["goal"] != g:
            raise _fail("E_LANDSCAPE")
        if any(c["channel"] not in chan_ids for c in ls[g]["cells"].values()):
            raise _fail()


def _validate_episode(e, seeds, chan_ids) -> None:
    _keys(e, EPISODE_KEYS)
    if not (isinstance(e["run_id"], str) and _IDENT.match(e["run_id"])):
        raise _fail()
    _hex(e["record_id"])
    if e["record_id"] not in seeds or e["generation"] != seeds[e["record_id"]]["generation"]:
        raise _fail()
    _enum(e["goal"], config.GOALS)
    _enum(e["model"], config.MODELS)
    _enum(e["channel"], chan_ids)
    _num(e["rep"], integer=True, lo=0)
    _num(e["generation"], integer=True, lo=0)
    _num(e["n_agents"], integer=True, lo=1)
    _num(e["budget"], nullable=True, integer=True, lo=1)
    _num(e["n_writers"], integer=True, lo=0, hi=e["n_agents"])
    for k in ("write_fraction", "S_mean", "I"):
        _num(e[k], lo=0.0, hi=1.0)
    _num(e["instigation_score"], lo=0.0, hi=1.0)
    _num(e["run_ttfw_s"], nullable=True)
    _num(e["run_first_write_step"], nullable=True, integer=True, lo=0)
    _num(e["pile_in_onset_t_s"], nullable=True)
    pot = e["posts_over_time"]
    if not isinstance(pot, list):
        raise _fail()
    for pt in pot:
        if not isinstance(pt, list) or len(pt) != 3:
            raise _fail()
        _num(pt[0])
        _num(pt[1], integer=True, lo=1)
        _num(pt[2], integer=True, lo=1, hi=e["n_agents"])
    eng = e["engagement"]
    _keys(eng, ENGAGEMENT_KEYS)
    for k in ENGAGEMENT_KEYS:
        if k == "engagement_score":
            _num(eng[k], lo=0.0, hi=1.0)
        else:
            _num(eng[k], integer=True, lo=0)
    _enum(e["trigger_code"], TRIGGER_CODES)
    codes = e["agent_codes"]
    if not isinstance(codes, list) or len(codes) != e["n_agents"]:
        raise _fail()
    for c in codes:
        _enum(c, AGENT_CODES, nullable=True)
    if e["n_writers"] == 0 and e["trigger_code"] != L.NO_WRITERS:
        raise _fail()
    panel = e["panel"]
    if panel is not None:
        _keys(panel, PANEL_KEYS)
        if not isinstance(panel["slugs"], list) or len(set(panel["slugs"])) != len(panel["slugs"]):
            raise _fail()
        for s in panel["slugs"]:
            _enum(s, config.MODELS)
        _num(panel["agreement_mean"], nullable=True, lo=0.0, hi=1.0)
    _num(e["classifier_errors"], integer=True, lo=0)
    el = e["elite_in"]
    if not isinstance(el, list) or len(set(el)) != len(el):
        raise _fail()
    for key in el:
        try:
            _t, chan, _b = L.split_cell_key(key)
        except ValueError:
            raise _fail() from None
        if chan != e["channel"]:
            raise _fail()


# --------------------------------------------------------------------------- gates + publish

def leakgate(path: Path, root: Path = None) -> dict:
    """tools/leakgate.py --root <repo> --scan <path>; fail-closed (any crash -> ok false)."""
    if root is None:
        return scoreboard.leakgate_subprocess(path)
    gate = Path(root) / "tools" / "leakgate.py"
    if not gate.is_file():
        gate = registry.REPO_DIR / "tools" / "leakgate.py"
    try:
        proc = subprocess.run([sys.executable, str(gate), "--root", str(root), "--scan", str(path)],
                              capture_output=True, text=True, timeout=600)
        verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"ok": False, "error_code": "E_READ"}
    return verdict if isinstance(verdict, dict) else {"ok": False, "error_code": "E_READ"}


def check_public(path: Path) -> dict:
    """site/tools/check_public.py over a scratch public tree holding only this file (as
    data/instigation/explorer.json), so the verdict concerns these bytes alone. Its output is discarded
    (only the exit status is used): the FAIL lines can quote fragments."""
    if not CHECK_PUBLIC.is_file():
        return {"ok": False, "error_code": "E_READ"}
    with tempfile.TemporaryDirectory(prefix="explorer-public-") as tmp:
        dest = Path(tmp) / "public" / "data" / "instigation" / "explorer.json"
        dest.parent.mkdir(parents=True)
        shutil.copyfile(path, dest)
        try:
            proc = subprocess.run([sys.executable, str(CHECK_PUBLIC), str(Path(tmp) / "public"), "--data-only"],
                                  capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error_code": "E_READ"}
    return {"ok": proc.returncode == 0, "error_code": None if proc.returncode == 0 else "E_PUBLIC"}


def default_gate(path: Path) -> dict:
    v = leakgate(path)
    if not (isinstance(v, dict) and v.get("ok") is True):
        return {"ok": False, "error_code": "E_LEAK"}
    p = check_public(path)
    if not p.get("ok"):
        return {"ok": False, "error_code": "E_PUBLIC"}
    return {"ok": True, "error_code": None}


def serialize(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=False, allow_nan=False).encode("utf-8")


_DEFAULT = object()  # resolve the module-level gates at call time (patchable)


def publish(doc: dict, *, out_path: Path = None, gate=_DEFAULT, verify=_DEFAULT) -> int:
    """Validate, gate the exact bytes (fail-closed), atomically replace ``out_path``, then re-check the
    published file with ``verify`` and roll back to the previous bytes on failure. Returns bytes written."""
    gate = default_gate if gate is _DEFAULT else gate
    verify = check_public if verify is _DEFAULT else verify
    validate(doc)
    path = Path(out_path) if out_path is not None else EXPLORER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = serialize(doc)
    # Keep the .json suffix on the candidate: the gate only scans textual suffixes.
    tmp = path.with_name(f".{path.stem}.candidate{path.suffix or '.json'}")
    try:
        tmp.write_bytes(data)
        verdict = gate(tmp)
    except BaseException as e:  # never leave an ungated candidate in the publish dir
        tmp.unlink(missing_ok=True)
        if not isinstance(e, Exception):
            raise
        raise _fail("E_GATE") from None
    if not (isinstance(verdict, dict) and verdict.get("ok") is True):
        tmp.unlink(missing_ok=True)
        code = verdict.get("error_code") if isinstance(verdict, dict) else None
        raise _fail(code if isinstance(code, str) and _CODE.match(code) else "E_LEAK")
    prev = path.read_bytes() if path.exists() else None
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise _fail("E_WRITE") from None
    if verify is not None:
        try:
            final = verify(path)
        except Exception:
            final = None
        if not (isinstance(final, dict) and final.get("ok") is True):
            scoreboard._rollback(path, prev)
            raise _fail("E_VERIFY")
    return len(data)


def summary(doc: dict, skipped: dict, nbytes: int = None) -> dict:
    def count(field):
        out = {}
        for e in doc["episodes"]:
            out[e[field]] = out.get(e[field], 0) + 1
        return out

    res = {"ok": True, "n_episodes": len(doc["episodes"]),
           "by_goal": count("goal"), "by_model": count("model"), "by_channel": count("channel"),
           "n_seeds": len(doc["seeds"]),
           "landscape_coverage": {g: doc["landscapes"][g]["coverage"] for g in config.GOALS},
           "n_skipped": {k: skipped[k] for k in sorted(skipped) if _CODE.match(k)}}
    if nbytes is not None:
        res["bytes"] = nbytes
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="export the landscape explorer data (numbers out only)")
    ap.add_argument("--out", default=None, help="output path (default site/public/data/instigation/explorer.json)")
    ap.add_argument("--runs", default=None, help="runs directory (default: this checkout's runs/)")
    ap.add_argument("--registry", default=None, help="override registry.json path")
    ap.add_argument("--archive", default=None, help="override the solvable archive path (goal siblings follow)")
    ap.add_argument("--dataset", action="store_true",
                    help="also rebuild the local training-data package (study/dataset.py; best effort, numbers only)")
    a = ap.parse_args(argv)
    try:
        doc, skipped = build(a.runs, registry_path=a.registry, archive_base=a.archive)
        nbytes = publish(doc, out_path=a.out)
    except ExportError as e:
        code = e.args[0] if e.args and isinstance(e.args[0], str) and _CODE.match(e.args[0]) else "E_EXPORT"
        print(json.dumps({"ok": False, "error": code}))
        return 1
    except Exception:  # noqa: BLE001 -- fixed code only; a traceback could echo a value
        print(json.dumps({"ok": False, "error": "E_EXPORT"}))
        return 1
    summ = summary(doc, skipped, nbytes)
    if a.dataset:
        summ["dataset"] = collect_dataset(a.runs, a.registry, a.archive)
    print(json.dumps(summ))
    return 0


def collect_dataset(runs=None, registry_path=None, archive=None) -> dict:
    """Optional hook: rebuild the training-data package from the same sources. Never raises; returns only
    {ok, version, new_version} or {ok: False, error: <fixed code>}."""
    try:
        from study import dataset
        archive_paths = None if archive is None else {g: qd.goal_archive_path(g, Path(archive))
                                                      for g in config.GOALS}
        res = dataset.collect(runs_dir=runs, registry_path=registry_path, archive_paths=archive_paths)
    except Exception:  # noqa: BLE001 -- fixed code only
        return {"ok": False, "error": "E_DATASET"}
    if not isinstance(res, dict) or not res.get("ok"):
        err = res.get("error") if isinstance(res, dict) else None
        return {"ok": False, "error": err if isinstance(err, str) and _CODE.match(err) else "E_DATASET"}
    v = res.get("version")
    return {"ok": True, "version": v if isinstance(v, str) and len(v) <= 32 else None,
            "new_version": bool(res.get("new_version"))}


if __name__ == "__main__":
    sys.exit(main())
