#!/usr/bin/env python3
"""Build the published instigation scoreboard (PROTOCOL.md section 5).

Joins the git-ignored registry (ids / lengths / paths) with numeric scores read
back from obs / the instigation metric tool, and emits
`site/public/data/instigation/scoreboard.json` -- numbers + opaque record ids
only, no board content. The leak gate (tools/leakgate.py) is run over the
candidate file BEFORE it is published; a non-ok verdict aborts the write.

Ranking = Pareto front over (length_bytes asc, instigation_score desc) plus a
scalar leaderboard by instigation_score.

The quality-diversity landscape (`build_landscape` / `write_landscape`) publishes
the per-agent-model MAP-Elites archive (study/landscape.py) as
`site/public/data/instigation/landscape.json` (schema v2: combined best-per-cell
`cells`, per-model `by_model` grids, `models`, `goal`). It is projected onto a fixed
whitelist of content-free fields and strictly validated (enum cell keys, model
slugs and goal, 16-hex record ids, numeric/bool/enum leaves only) BEFORE the leak
gate even sees it, so nothing outside the known structural vocabulary can reach
the published tree.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402

from study import registry  # noqa: E402

SCOREBOARD_PATH = registry.REPO_DIR / "site" / "public" / "data" / "instigation" / "scoreboard.json"
SCHEMA = "instigation.scoreboard.v1"
LANDSCAPE_PATH = SCOREBOARD_PATH.with_name("landscape.json")
LANDSCAPE_SCHEMA = "instigation.landscape.v2"

# Candidate leak-gate locations (ISOLATION partition owns the tool); first match wins.
_GATE_CANDIDATES = (
    registry.REPO_DIR / "tools" / "leakgate.py",
    registry.REPO_DIR / "site" / "tools" / "leakgate.py",
    registry.REPO_DIR / "lab" / "tools" / "leakgate.py",
)

# Model origin (fixed enum, structural) and size parsing for the published record.
_MODEL_ORIGIN = {"qwen38": "cn", "mistral24": "eu", "glm53f": "cn", "gemma31": "us", "gptoss120": "us"}
_MODEL_ORIGINS = frozenset(_MODEL_ORIGIN.values()) | {"unknown"}
# Explicit parameter counts (billions); any other model falls back to its config label ("... 24B").
_MODEL_SIZE_B = {"gptoss120": 120}
_SIZE_RE = re.compile(r"(\d+)\s*[bB]\b")

# Numeric score fields pulled from obs; absent -> null.
_SCORE_FIELDS = ("instigation_score", "I", "score_ci_lo", "score_ci_hi", "n_reps",
                 "first_post_rate", "median_first_post_lag_s", "run_time_to_first_write_s",
                 "total_posts", "cross_mentions", "live_live_edges", "pile_in_onset_t_s")


class ScoreboardError(RuntimeError):
    """args[0] is a fixed error code: E_LEAK / E_GATE / E_LANDSCAPE_SCHEMA."""


def _model_size_b(slug: str):
    if slug in _MODEL_SIZE_B:
        return _MODEL_SIZE_B[slug]
    label = (config.MODELS.get(slug) or {}).get("label") or ""
    m = _SIZE_RE.search(label)
    return int(m.group(1)) if m else None


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _record(row: dict, score: dict) -> dict:
    slug = row.get("model") or ""
    sc = score or {}
    I = _num(sc.get("I"))
    length_bytes = row.get("length_bytes")
    rec = {
        "record_id": row.get("record_id"),
        "parent_id": row.get("parent_id"),
        "generation": row.get("generation"),
        "channel": row.get("channel"),
        "model": slug,
        "model_origin": model_origin(slug),
        "model_size_b": _model_size_b(slug),
        "length_bytes": length_bytes,
        "length_chars": row.get("length_chars"),
        "n_threads": row.get("n_threads"),
        "n_posts": row.get("n_posts"),
        "rank_key": [I if I is not None else -1.0, length_bytes if length_bytes is not None else 1 << 62],
        "created_ts": row.get("created_ts"),
    }
    for f in _SCORE_FIELDS:
        rec[f] = _num(sc.get(f))
    return rec


def _pareto_front(records):
    """Record ids on the Pareto front over (length_bytes asc, instigation_score desc).

    A record is dominated if another has length_bytes <= and instigation_score >=
    with at least one strict inequality.
    """
    scored = [r for r in records
              if isinstance(r.get("length_bytes"), (int, float)) and _num(r.get("instigation_score")) is not None]
    front = []
    for r in scored:
        lb, sc = r["length_bytes"], r["instigation_score"]
        dominated = any(o is not r and o["length_bytes"] <= lb and o["instigation_score"] >= sc
                        and (o["length_bytes"] < lb or o["instigation_score"] > sc) for o in scored)
        if not dominated:
            front.append(r["record_id"])
    return front


def _leaderboard(records):
    scored = [r for r in records if _num(r.get("instigation_score")) is not None]
    scored.sort(key=lambda r: (-r["instigation_score"],
                               r["length_bytes"] if isinstance(r.get("length_bytes"), (int, float)) else 1 << 62))
    return [r["record_id"] for r in scored]


def build(scores: dict, *, registry_path: Path = None, records: dict = None) -> dict:
    """Assemble the scoreboard dict (numbers + ids only) from registry + scores."""
    rows = records if records is not None else registry.load(registry_path)
    recs = [_record(row, (scores or {}).get(rid, {})) for rid, row in sorted(rows.items())]
    # sort published records by the scalar leaderboard for stable, meaningful order
    order = {rid: i for i, rid in enumerate(_leaderboard(recs))}
    recs.sort(key=lambda r: order.get(r["record_id"], len(order)))
    return {
        "schema": SCHEMA,
        "generated_ts": time.time(),
        "n_records": len(recs),
        "records": recs,
        "pareto": _pareto_front(recs),
        "leaderboard": _leaderboard(recs),
    }


def _run_leakgate(extra_args) -> dict:
    """Invoke tools/leakgate.py with `extra_args`, parse its section-7.4 verdict.

    Fail-closed: a missing tool, a crash, or an unparseable verdict is treated as
    ok=false so an unchecked file is never published. `--root` pins the chatter
    corpus to the repo tree regardless of the caller's CWD; without it the default
    corpus globs resolve against the process CWD and (off-root) match zero files,
    which would make the gate pass open on any content (see leakgate's E_CORPUS
    guard).
    """
    gate = next((g for g in _GATE_CANDIDATES if g.exists()), None)
    if gate is None:
        return {"ok": False, "error_code": "E_READ", "reason": "gate_missing"}
    try:
        proc = subprocess.run([sys.executable, str(gate), "--root", str(registry.REPO_DIR), *extra_args],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error_code": "E_READ", "reason": "gate_failed"}
    try:
        verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error_code": "E_READ", "reason": "gate_unparseable"}
    return verdict if isinstance(verdict, dict) else {"ok": False, "error_code": "E_READ"}


def leakgate_subprocess(path: Path) -> dict:
    """Scoped pre-publish gate: scan a single candidate artifact against the tree corpus."""
    return _run_leakgate(["--scan", str(path)])


def leakgate_wholetree() -> dict:
    """Authoritative post-publish gate: the standalone whole-tree check (all SCAN_GLOBS).

    This is the exact verdict the CI gate and the orchestrator read, computed over the
    tree in its final (published) state, so publish can never admit a tree the standalone
    gate would reject.
    """
    return _run_leakgate([])


def write(scoreboard: dict, *, out_path: Path = None, gate=leakgate_subprocess,
          verify=None) -> dict:
    """Gate-check then publish the scoreboard. Returns the final verdict on success.

    Two-stage, deterministic, fail-closed:
      1. Serialize the exact bytes to a sibling temp file and run the scoped pre-publish
         `gate` over them. A non-ok verdict aborts before anything is published.
      2. Atomically replace the published file with those bytes, then (when `verify` is
         given) re-run the authoritative whole-tree gate against the tree in its final,
         published state. If that rejects, roll the published file back to its prior
         bytes (or remove it if there were none) and raise -- so a published tree never
         disagrees with the standalone gate.
    A non-ok verdict at either stage raises ScoreboardError('E_LEAK').
    """
    path = Path(out_path) if out_path is not None else SCOREBOARD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the real suffix on the candidate: the gate only scans textual suffixes
    # (.json, ...), so a `.tmp` candidate would be skipped and pass vacuously.
    tmp = path.with_name(f".{path.stem}.candidate{path.suffix or '.json'}")
    tmp.write_text(json.dumps(scoreboard, indent=2))
    try:
        verdict = gate(tmp)
    except BaseException as e:  # incl. KeyboardInterrupt: never leave an ungated file in the publish dir
        tmp.unlink(missing_ok=True)
        if not isinstance(e, Exception):
            raise
        raise ScoreboardError("E_GATE") from e
    if not (isinstance(verdict, dict) and verdict.get("ok") is True):
        tmp.unlink(missing_ok=True)
        raise ScoreboardError("E_LEAK")
    prev = path.read_bytes() if path.exists() else None
    os.replace(tmp, path)
    if verify is None:
        return verdict
    try:
        final = verify()
    except Exception as e:
        _rollback(path, prev)
        raise ScoreboardError("E_GATE") from e
    if not (isinstance(final, dict) and final.get("ok") is True):
        _rollback(path, prev)
        raise ScoreboardError("E_LEAK")
    return final


def _rollback(path: Path, prev: bytes | None) -> None:
    """Restore `path` to its pre-publish bytes (or remove it if it did not exist)."""
    if prev is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(prev)


def build_and_write(scores: dict, *, registry_path: Path = None, out_path: Path = None,
                    gate=leakgate_subprocess, verify=leakgate_wholetree, archive=None,
                    landscape_out_path: Path = None, goal=None, models=None) -> dict:
    """Publish the scoreboard and, when an `archive` is given, the landscape too.

    Without an archive this is unchanged. With one, the landscape is built and
    validated first (a schema failure touches nothing), then both files are gated and
    published all-or-nothing: the authoritative whole-tree `verify` runs once over the
    final tree, and any failure after the scoreboard landed rolls it back as well.
    `goal` / `models` are passed to build_landscape (the QD driver's --goal / --models).
    """
    sb = build(scores, registry_path=registry_path)
    if archive is None:
        write(sb, out_path=out_path, gate=gate, verify=verify)
        return {"ok": True, "n_records": sb["n_records"], "pareto": len(sb["pareto"])}
    ls = build_landscape(archive, goal=goal, models=models)
    sb_path = Path(out_path) if out_path is not None else SCOREBOARD_PATH
    prev_sb = sb_path.read_bytes() if sb_path.exists() else None
    write(sb, out_path=sb_path, gate=gate, verify=None)
    try:
        write_landscape(ls, out_path=landscape_out_path, gate=gate, verify=verify)
    except BaseException:
        _rollback(sb_path, prev_sb)
        raise
    return {"ok": True, "n_records": sb["n_records"], "pareto": len(sb["pareto"]),
            "landscape_coverage": ls["coverage"], "landscape_coverage_cells": ls["coverage_cells"],
            "n_cells_possible": ls["n_cells_possible"]}


# --------------------------------------------------------------------------------------
# Quality-diversity landscape (contract item 5; v2: one elite grid per agent model)
# --------------------------------------------------------------------------------------

# Fallback axes, mirroring study/landscape.py, used only if that module is absent.
# Axis values are fixed structural enums -- never derived from board text.
_FALLBACK_CHANNELS = ("forum", "irc", "wiki", "cache", "guestbook", "paste", "kv", "js", "html")
_FALLBACK_BANDS = ("le120", "121-200", "201-320", "gt320")
_ORIGINS = frozenset(("pilot", "scratch", "mutation"))
_AXIS_NAMES = ("trigger_code", "channel", "length_band")
_AXIS_PART_RE = (re.compile(r"^[A-Z_]+$"), re.compile(r"^[a-z]+$"), re.compile(r"^[0-9a-z-]+$"))
_CELL_KEY_RE = re.compile(r"^[A-Z_]+\|[a-z]+\|[0-9a-z-]+$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_ENGAGEMENT_FIELDS = ("board_reads", "relay_connections", "board_refs", "agents_engaged",
                      "engagement_score")
# Whitelisted projection of an elite record into a published cell (combined or per model).
_CELL_FIELDS = ("record_id", "instigation_score", "length", "trigger_code", "channel",
                "n_writers", "engagement", "model")
# Whitelisted projection of elite records for the flat leaderboard (no run_id: it is a
# free-form tag, not a validated id).
_LB_FIELDS = ("record_id", "cell_key", "trigger_code", "channel", "length_band", "seed_chars",
              "instigation_score", "engagement_score", "n_writers", "bootstrap", "origin",
              "parent_id", "generation", "model")
_BY_MODEL_FIELDS = ("cells", "coverage", "tallies", "origin", "size_b")
_TALLY_NAMES = _AXIS_NAMES + ("model",)
_LANDSCAPE_TOP = frozenset(("schema", "generated_ts", "goal", "models", "cells", "by_model", "coverage",
                            "coverage_cells", "mean_elite_score", "n_cells_possible", "axes", "tallies",
                            "leaderboard"))
# Archive records written before the goal factor existed come from solvable runs.
_LEGACY_GOAL = "solvable"


def _schema_fail():
    return ScoreboardError("E_LANDSCAPE_SCHEMA")


def _landscape_axes(archive) -> dict:
    """Axis enums: archive.axes if provided, else study.landscape (lazy), else fallback."""
    ax = getattr(archive, "axes", None)
    if callable(ax):
        ax = ax()
    if not isinstance(ax, dict):
        try:
            from study import landscape as _ls  # lazy: module may be added concurrently
            ax = {"trigger_code": list(_ls.TRIGGER_AXIS), "channel": list(_ls.CHANNELS),
                  "length_band": [b[2] for b in _ls.LENGTH_BANDS]}
        except Exception:
            from study import triggers
            ax = {"trigger_code": list(triggers.ENUM) + ["UNRESOLVED", "NO_WRITERS"],
                  "channel": list(_FALLBACK_CHANNELS), "length_band": list(_FALLBACK_BANDS)}
    out = {}
    for name, rx in zip(_AXIS_NAMES, _AXIS_PART_RE):
        vals = ax.get(name)
        if not isinstance(vals, (list, tuple)) or not vals:
            raise _schema_fail()
        vals = list(vals)
        if not all(isinstance(v, str) and rx.match(v) for v in vals) or len(set(vals)) != len(vals):
            raise _schema_fail()
        out[name] = vals
    return out


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


def _project_engagement(eng):
    if not isinstance(eng, dict):
        return {}
    return {k: eng[k] for k in _ENGAGEMENT_FIELDS if k in eng}


def _project_cell(slug: str, rec) -> dict:
    if not isinstance(rec, dict):
        raise _schema_fail()
    return {"record_id": rec.get("record_id"), "instigation_score": rec.get("instigation_score"),
            "length": rec.get("seed_chars", rec.get("length")), "trigger_code": rec.get("trigger_code"),
            "channel": rec.get("channel"), "n_writers": rec.get("n_writers"),
            "engagement": _project_engagement(rec.get("engagement")), "model": slug}


def _project_lb(slug: str, cell_key: str, rec) -> dict:
    if not isinstance(rec, dict):
        raise _schema_fail()
    out = {k: rec.get(k) for k in _LB_FIELDS}
    out["cell_key"] = cell_key
    out["model"] = slug
    if out["engagement_score"] is None and isinstance(rec.get("engagement"), dict):
        out["engagement_score"] = rec["engagement"].get("engagement_score")
    if out["seed_chars"] is None:
        out["seed_chars"] = rec.get("length")
    return out


def _model_rank(slug: str) -> int:
    order = tuple(config.MODEL_ORDER)
    return order.index(slug) if slug in order else len(order)


def _lb_sort_key(r):
    sc = r.get("instigation_score")
    ln = r.get("seed_chars")
    return (-(sc if _is_num(sc) else -1.0), ln if _is_num(ln) else 1 << 62, r.get("record_id") or "",
            _model_rank(r.get("model") or ""))


def _check_slug(slug) -> str:
    if not isinstance(slug, str) or slug not in config.MODELS:
        raise _schema_fail()
    return slug


def _call(v):
    return v() if callable(v) else v


def _archive_elites_by_model(archive) -> dict:
    """{slug: {cell_key: record}} from a per-model archive (elites_by_model()) or, for a v1
    archive (one elite per cell), by grouping `elites` on each record's model (a record without
    one predates the model axis and ran the default model). Slugs must be config.MODELS keys and
    agree with the record's own model field."""
    ebm = getattr(archive, "elites_by_model", None)
    out: dict = {}
    if callable(ebm):
        raw = ebm()
        if not isinstance(raw, dict):
            raise _schema_fail()
        for slug, cells in raw.items():
            _check_slug(slug)
            if not isinstance(cells, dict):
                raise _schema_fail()
            for key, rec in cells.items():
                if not isinstance(rec, dict) or rec.get("model") not in (None, "", slug):
                    raise _schema_fail()
                out.setdefault(slug, {})[key] = rec
        return out
    elites = _call(archive.elites)
    if not isinstance(elites, dict):
        raise _schema_fail()
    for key, rec in elites.items():
        if not isinstance(rec, dict):
            raise _schema_fail()
        slug = _check_slug(rec.get("model") or config.DEFAULT_SLUG)
        out.setdefault(slug, {})[key] = rec
    return out


def _resolve_goal(goal, archive, by_model) -> tuple:
    """(published goal, by_model filtered to that goal's records).

    Precedence: explicit `goal`, else the archive's `goal`, else the single goal its records carry.
    A record's goal is its own `goal` field, else the archive's, else the legacy solvable goal.
    Records of any other goal are left out of this landscape. Mixed goals with nothing to pick
    one -> E_LANDSCAPE_SCHEMA."""
    goals = tuple(config.GOALS)
    arch_goal = _call(getattr(archive, "goal", None))
    if arch_goal is not None and arch_goal not in goals:
        raise _schema_fail()

    def rec_goal(r):
        g = r.get("goal")
        if g is None:
            return arch_goal or _LEGACY_GOAL
        if g not in goals:
            raise _schema_fail()
        return g

    if goal is None:
        goal = arch_goal
    if goal is None:
        seen = {rec_goal(r) for cells in by_model.values() for r in cells.values()}
        if len(seen) > 1:
            raise _schema_fail()
        goal = seen.pop() if seen else config.DEFAULT_GOAL
    if goal not in goals:
        raise _schema_fail()
    kept = {}
    for slug, cells in by_model.items():
        cells = {k: r for k, r in cells.items() if rec_goal(r) == goal}
        if cells:
            kept[slug] = cells
    return goal, kept


def _resolve_models(models, archive, by_model, other_goal=False) -> list:
    """Published model list: the requested (else the archive's, unless the archive also holds other-goal
    elites) models plus every model holding an elite of this goal,
    in config.MODEL_ORDER (which is also the tie-break order for the combined cells)."""
    if models is None and not other_goal:
        models = _call(getattr(archive, "models", None))
    if models is None:
        # an archive that also holds other-goal elites: only the models holding an elite of the
        # published goal (by_model is already filtered), never every model of the archive
        models = ()
    if isinstance(models, str) or not isinstance(models, (list, tuple, set, frozenset)):
        raise _schema_fail()
    slugs = {_check_slug(s) for s in models} | set(by_model)
    return sorted(slugs, key=lambda s: (_model_rank(s), s))


def _beats(a, b) -> bool:
    """True iff elite `a` strictly beats `b` under the archive's own rule (study.landscape.beats),
    falling back to (score desc, shorter seed) if that module or the comparison is unavailable."""
    try:
        from study import landscape as _ls  # lazy
        return bool(_ls.beats(a, b))
    except Exception:
        def key(r):
            sc, ln = r.get("instigation_score"), r.get("seed_chars", r.get("length"))
            return (sc if _is_num(sc) else -1.0, -(ln if _is_num(ln) else 1 << 62))
        return key(a) > key(b)


def _tally(keys) -> dict:
    out = {name: {} for name in _AXIS_NAMES}
    for key in keys:
        for name, part in zip(_AXIS_NAMES, key.split("|")):
            out[name][part] = out[name].get(part, 0) + 1
    return {name: dict(sorted(d.items())) for name, d in out.items()}


def model_origin(slug: str) -> str:
    return _MODEL_ORIGIN.get(slug, "unknown")


def build_landscape(archive, *, goal=None, models=None, generated_ts=None) -> dict:
    """Assemble the v2 landscape dict (numbers + enum strings + opaque ids only).

    `archive` is duck-typed: a per-model archive exposes elites_by_model() -> {slug: {cell_key:
    record}}; a v1 archive exposes `elites` ({cell_key: record}, model on the record). Optional
    `axes`, `goal` and `models` attributes are honoured. Everything published is derived from the
    elite records and projected onto fixed whitelists:

      cells        best elite per cell across models (study.landscape.beats; ties -> earlier
                   model in config.MODEL_ORDER), each carrying its model slug
      by_model     {slug: {cells, coverage, tallies, origin, size_b}}
      coverage     filled (model, cell) pairs;  coverage_cells: filled cells across models
      tallies      per-axis counts over (model, cell) pairs, plus per-model counts
      leaderboard  every (model, cell) elite, best first

    The result is validated; any deviation raises ScoreboardError('E_LANDSCAPE_SCHEMA') without
    echoing the offending value.
    """
    try:
        axes = _landscape_axes(archive)
        by_model = _archive_elites_by_model(archive)
        n_all = sum(len(c) for c in by_model.values())
        goal, by_model = _resolve_goal(goal, archive, by_model)
        other_goal = sum(len(c) for c in by_model.values()) != n_all
        slugs = _resolve_models(models, archive, by_model, other_goal)

        combined: dict = {}  # cell_key -> (slug, record)
        for slug in slugs:
            for key, rec in by_model.get(slug, {}).items():
                cur = combined.get(key)
                if cur is None or _beats(rec, cur[1]):
                    combined[key] = (slug, rec)
        cells = {k: _project_cell(*combined[k]) for k in sorted(combined, key=str)}

        per_model = {}
        lb = []
        scores = []
        for slug in slugs:
            mcells = by_model.get(slug, {})
            keys = sorted(mcells, key=str)
            for k in keys:
                _check_cell_key(k, axes)
            per_model[slug] = {
                "cells": {k: _project_cell(slug, mcells[k]) for k in keys},
                "coverage": len(keys),
                "tallies": _tally(keys),
                "origin": model_origin(slug),
                "size_b": _model_size_b(slug),
            }
            lb.extend(_project_lb(slug, k, mcells[k]) for k in keys)
            scores.extend(mcells[k].get("instigation_score") for k in keys)
        lb.sort(key=_lb_sort_key)
        num_scores = [s for s in scores if _is_num(s)]
        tallies = _tally([r["cell_key"] for r in lb])
        tallies["model"] = {s: per_model[s]["coverage"] for s in slugs if per_model[s]["coverage"]}
        n_possible = 1
        for name in _AXIS_NAMES:
            n_possible *= len(axes[name])
        ls = {
            "schema": LANDSCAPE_SCHEMA,
            "generated_ts": time.time() if generated_ts is None else generated_ts,
            "goal": goal,
            "models": slugs,
            "cells": cells,
            "by_model": per_model,
            "coverage": len(lb),
            "coverage_cells": len(cells),
            "mean_elite_score": sum(num_scores) / len(num_scores) if num_scores else 0.0,
            "n_cells_possible": n_possible,
            "axes": axes,
            "tallies": tallies,
            "leaderboard": lb,
        }
    except ScoreboardError:
        raise
    except Exception:
        # Fixed code only; the exception text could echo archive contents.
        raise _schema_fail() from None
    validate_landscape(ls)
    return ls


def _check_cell_key(key, axes) -> tuple:
    if not isinstance(key, str) or not _CELL_KEY_RE.match(key):
        raise _schema_fail()
    parts = key.split("|")
    for name, part in zip(_AXIS_NAMES, parts):
        if part not in axes[name]:
            raise _schema_fail()
    return tuple(parts)


def _check_id(v, *, nullable=False):
    if v is None and nullable:
        return
    if not isinstance(v, str) or not _ID_RE.match(v):
        raise _schema_fail()


def _check_num(v, *, nullable=False, integer=False, lo=None, hi=None):
    if v is None and nullable:
        return
    if not _is_num(v) or (integer and not isinstance(v, int)):
        raise _schema_fail()
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _schema_fail()


def _check_engagement(eng):
    if not isinstance(eng, dict) or any(k not in _ENGAGEMENT_FIELDS for k in eng):
        raise _schema_fail()
    for k, v in eng.items():
        if k == "engagement_score":
            _check_num(v, nullable=True, lo=0.0, hi=1.0)
        else:
            _check_num(v, nullable=True, integer=True, lo=0)


def _check_cells(cells, axes, slugs, *, slug=None) -> None:
    if not isinstance(cells, dict):
        raise _schema_fail()
    for key, cell in cells.items():
        trig, chan, _band = _check_cell_key(key, axes)
        if not isinstance(cell, dict) or set(cell) != set(_CELL_FIELDS):
            raise _schema_fail()
        if cell["model"] not in slugs or (slug is not None and cell["model"] != slug):
            raise _schema_fail()
        _check_id(cell["record_id"])
        _check_num(cell["instigation_score"], nullable=True)
        _check_num(cell["length"], integer=True, lo=0)
        _check_num(cell["n_writers"], integer=True, lo=0)
        if cell["trigger_code"] != trig or cell["channel"] != chan:
            raise _schema_fail()
        _check_engagement(cell["engagement"])


def _check_tallies(tallies, axes, names=_AXIS_NAMES, slugs=()) -> None:
    if not isinstance(tallies, dict) or set(tallies) != set(names):
        raise _schema_fail()
    for name in names:
        t = tallies[name]
        if not isinstance(t, dict):
            raise _schema_fail()
        allowed = slugs if name == "model" else axes[name]
        for k, v in t.items():
            if k not in allowed:
                raise _schema_fail()
            _check_num(v, integer=True, lo=0)


def validate_landscape(ls) -> None:
    """Strict structural check of a v2 landscape dict. Raises E_LANDSCAPE_SCHEMA on any
    deviation: unknown keys, non-enum strings (model slugs, goal, axis values), non-16-hex ids,
    non-finite numbers, or counts / cells that disagree across the combined and per-model views."""
    if not isinstance(ls, dict):
        raise _schema_fail()
    if set(ls) != _LANDSCAPE_TOP or ls["schema"] != LANDSCAPE_SCHEMA:
        raise _schema_fail()
    if not isinstance(ls["goal"], str) or ls["goal"] not in config.GOALS:
        raise _schema_fail()
    slugs = ls["models"]
    if (not isinstance(slugs, list) or len(set(map(str, slugs))) != len(slugs)
            or not all(isinstance(s, str) and s in config.MODELS for s in slugs)):
        raise _schema_fail()
    axes = ls["axes"]
    if not isinstance(axes, dict) or set(axes) != set(_AXIS_NAMES):
        raise _schema_fail()
    for name, rx in zip(_AXIS_NAMES, _AXIS_PART_RE):
        vals = axes[name]
        if not isinstance(vals, list) or not vals or not all(isinstance(v, str) and rx.match(v) for v in vals):
            raise _schema_fail()
    _check_num(ls["generated_ts"], lo=0)
    _check_num(ls["coverage"], integer=True, lo=0)
    _check_num(ls["coverage_cells"], integer=True, lo=0)
    _check_num(ls["mean_elite_score"])
    _check_num(ls["n_cells_possible"], integer=True, lo=1)
    if ls["coverage_cells"] > ls["n_cells_possible"] or ls["coverage"] > ls["n_cells_possible"] * len(slugs):
        raise _schema_fail()

    by_model = ls["by_model"]
    if not isinstance(by_model, dict) or set(by_model) != set(slugs):
        raise _schema_fail()
    pairs = set()
    for slug in slugs:
        m = by_model[slug]
        if not isinstance(m, dict) or set(m) != set(_BY_MODEL_FIELDS):
            raise _schema_fail()
        _check_cells(m["cells"], axes, slugs, slug=slug)
        _check_num(m["coverage"], integer=True, lo=0)
        if m["coverage"] != len(m["cells"]):
            raise _schema_fail()
        _check_tallies(m["tallies"], axes)
        if m["origin"] not in _MODEL_ORIGINS or m["origin"] != model_origin(slug):
            raise _schema_fail()
        _check_num(m["size_b"], nullable=True, integer=True, lo=1)
        pairs.update((slug, k) for k in m["cells"])

    cells = ls["cells"]
    _check_cells(cells, axes, slugs)
    if set(cells) != {k for _s, k in pairs} or ls["coverage_cells"] != len(cells):
        raise _schema_fail()
    for key, cell in cells.items():  # each combined cell is one of its model's elites
        if by_model[cell["model"]]["cells"].get(key) != cell:
            raise _schema_fail()
    if ls["coverage"] != len(pairs):
        raise _schema_fail()

    _check_tallies(ls["tallies"], axes, _TALLY_NAMES, slugs)

    lb = ls["leaderboard"]
    if not isinstance(lb, list) or len(lb) != len(pairs):
        raise _schema_fail()
    seen = set()
    for r in lb:
        if not isinstance(r, dict) or set(r) != set(_LB_FIELDS):
            raise _schema_fail()
        trig, chan, band = _check_cell_key(r["cell_key"], axes)
        if (r["trigger_code"], r["channel"], r["length_band"]) != (trig, chan, band):
            raise _schema_fail()
        if not isinstance(r["model"], str) or (r["model"], r["cell_key"]) not in pairs:
            raise _schema_fail()
        seen.add((r["model"], r["cell_key"]))
        _check_id(r["record_id"])
        if r["record_id"] != by_model[r["model"]]["cells"][r["cell_key"]]["record_id"]:
            raise _schema_fail()
        _check_id(r["parent_id"], nullable=True)
        _check_num(r["seed_chars"], integer=True, lo=0)
        _check_num(r["instigation_score"], nullable=True)
        _check_num(r["engagement_score"], nullable=True, lo=0.0, hi=1.0)
        _check_num(r["n_writers"], integer=True, lo=0)
        _check_num(r["generation"], nullable=True, integer=True, lo=0)
        if not isinstance(r["bootstrap"], bool) or r["origin"] not in _ORIGINS:
            raise _schema_fail()
    if seen != pairs:
        raise _schema_fail()


_WHOLETREE = object()  # sentinel: resolve leakgate_wholetree at call time (patchable in tests)


def write_landscape(landscape: dict, *, out_path: Path = None, gate=leakgate_subprocess,
                    verify=_WHOLETREE) -> dict:
    """Validate, leak-gate (fail-closed, --root <repo>), then publish the landscape.

    Same two-stage gate + rollback semantics as `write`; validation runs first so a
    malformed landscape never reaches even the candidate file. Fail-closed by default:
    unless the caller passes `verify` explicitly (build_and_write passes its own, or
    None when it runs the whole-tree check itself / in the dry-run), the authoritative
    whole-tree gate (`leakgate_wholetree`) runs after the replace, with rollback.
    """
    if verify is _WHOLETREE:
        verify = leakgate_wholetree
    validate_landscape(landscape)
    return write(landscape, out_path=out_path if out_path is not None else LANDSCAPE_PATH,
                 gate=gate, verify=verify)


def _load_scores(path):
    if not path:
        return {}
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="build + publish the instigation scoreboard (numbers out only)")
    ap.add_argument("--scores", help="path to a {record_id: {numeric metrics}} JSON file")
    ap.add_argument("--registry", help="override registry.json path")
    ap.add_argument("--out", help="override scoreboard.json output path")
    ap.add_argument("--archive", nargs="?", const="", default=None,
                    help="also publish the QD landscape from this archive (default path if bare)")
    ap.add_argument("--landscape-out", help="override landscape.json output path")
    ap.add_argument("--goal", choices=config.GOALS, default=None,
                    help="landscape goal (default: the archive's own goal)")
    ap.add_argument("--models", default=None, help="comma-separated model slugs to list in the landscape")
    a = ap.parse_args(argv)
    try:
        archive = None
        if a.archive is not None:
            try:
                from study import landscape as _ls
                path = a.archive or None
                if path is None and a.goal is not None:
                    # bare --archive + --goal: that goal's own archive (one file per goal, 8.6)
                    from study import qd as _qd
                    path = _qd.goal_archive_path(a.goal, _ls.ARCHIVE_PATH)
                archive = _ls.Archive.load(path, goal=a.goal) if a.goal is not None else _ls.Archive.load(path)
            except Exception:
                raise ScoreboardError("E_LANDSCAPE_SCHEMA") from None
            if a.goal is not None and not archive.models() and getattr(archive, "n_other_goal_on_load", 0):
                raise ScoreboardError("E_LANDSCAPE_SCHEMA")   # only other-goal elites: never publish empty
        models = [m for m in a.models.split(",") if m] if a.models else None
        res = build_and_write(_load_scores(a.scores), registry_path=a.registry, out_path=a.out,
                              archive=archive, landscape_out_path=a.landscape_out,
                              goal=a.goal, models=models)
    except ScoreboardError as e:
        print(json.dumps({"ok": False, "error": e.args[0]}))
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
