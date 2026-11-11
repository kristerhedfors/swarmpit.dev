#!/usr/bin/env python3
"""Candidate registry for the instigation study (PROTOCOL.md section 5).

Pure stdlib, content-in / numbers-out. Every helper here takes a candidate
board-seed dict (which contains adversarial board *text*) and returns only
structural metadata: an opaque hex `record_id`, byte/char lengths and
thread/post counts. The candidate text is never printed, logged or returned.

Registry rows live in `data/instigation/registry.json` (git-ignored, since the
rows reference the git-ignored chatter files). A row holds ids / lengths /
paths only (PROTOCOL.md section 5), no board content.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parent
LAB_DIR = STUDY_DIR.parent
REPO_DIR = LAB_DIR.parent
DATA_DIR = REPO_DIR / "data"
REGISTRY_PATH = DATA_DIR / "instigation" / "registry.json"
CANDIDATES_DIR = DATA_DIR / "synthetic-agent-chatter" / "candidates"

# Serializes every registry read-modify-write (upsert / annotate are load -> modify -> atomic
# save, so two threads writing at once could drop a row). Re-entrant so callers may hold it
# around a sequence of registry calls (generate.REGISTRY_LOCK is this same object).
REGISTRY_LOCK = threading.RLock()

RECORD_ID_LEN = 16  # hex chars of the blake2b digest kept as the opaque record id

# Row fields persisted per PROTOCOL.md section 5 (ids / numbers / paths only).
ROW_FIELDS = ("record_id", "parent_id", "generation", "channel", "model",
              "chatter_path", "length_bytes", "length_chars", "n_threads",
              "n_posts", "created_ts", "bootstrap", "origin")

# Where a candidate came from (fixed enum, structural -- never derived from its text):
#   pilot    -- the hand-written pilot seed (lab/seeds/seed.json) or a gen-0 variant of it
#   scratch  -- a fresh generate_candidate() draw with no parent
#   mutation -- a generate_mutation() child of an existing candidate
ORIGINS = ("pilot", "scratch", "mutation")


def default_origin(parent_id) -> str:
    """Origin for a row that did not state one: a parented row is a mutation, else scratch.

    This is what lets generate._persist (which does not pass origin=) and rows written
    before the field existed load with a sensible, structural value.
    """
    return "mutation" if parent_id else "scratch"


def _check_origin(origin) -> str:
    if origin not in ORIGINS:
        raise ValueError("E_ORIGIN")
    return origin


def normalize_row(row: dict) -> dict:
    """Project a row onto ROW_FIELDS, filling the bootstrap / origin defaults for old rows.

    Rows persisted before `bootstrap` / `origin` existed load with bootstrap=False and
    origin derived from parent_id (see default_origin). An unknown stored origin is
    re-derived rather than trusted, so the field always holds an ORIGINS value.
    """
    clean = {k: row.get(k) for k in ROW_FIELDS}
    clean["bootstrap"] = bool(row.get("bootstrap", False))
    if clean["origin"] not in ORIGINS:
        clean["origin"] = default_origin(clean.get("parent_id"))
    return clean


def canonical_json(candidate) -> str:
    """Deterministic serialization used both for hashing and for on-disk writes.

    `sort_keys` only orders dict keys; list order (threads, posts) is preserved,
    so the board semantics are unchanged. The same string is what generate.py
    writes to the candidate file, so lengths recorded here match the file.
    """
    return json.dumps(candidate, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_id(candidate) -> str:
    """Opaque content id = first RECORD_ID_LEN hex of blake2b(canonical_json)."""
    digest = hashlib.blake2b(canonical_json(candidate).encode("utf-8")).hexdigest()
    return digest[:RECORD_ID_LEN]


def counts(candidate) -> dict:
    """Structural sizes of a candidate: numbers only, never its text.

    length_bytes / length_chars are measured on the canonical serialization (the
    exact bytes generate.py writes to the candidate file).
    """
    s = canonical_json(candidate)
    threads = (candidate or {}).get("threads") or []
    n_posts = 0
    for t in threads:
        if isinstance(t, dict):
            n_posts += len(t.get("posts") or [])
    return {
        "length_bytes": len(s.encode("utf-8")),
        "length_chars": len(s),
        "n_threads": len(threads),
        "n_posts": n_posts,
    }


def candidate_path(rid: str, candidates_dir: Path = None) -> Path:
    """Path of the candidate chatter file for a record id."""
    base = Path(candidates_dir) if candidates_dir is not None else CANDIDATES_DIR
    return base / f"{rid}.chatter.json"


def id_from_path(path) -> str:
    """The record id named by a candidate file path (its stem), without reading it.

    Candidate files are named `<record_id>.chatter.json`, so the id is the file
    name with that suffix stripped. Returns "" if the name is not in that form.
    """
    name = Path(path).name
    suffix = ".chatter.json"
    if not name.endswith(suffix):
        return ""
    stem = name[: -len(suffix)]
    if len(stem) == RECORD_ID_LEN and all(c in "0123456789abcdef" for c in stem):
        return stem
    return ""


def load(registry_path: Path = None) -> dict:
    """Load the registry as {record_id: row}. Missing / unreadable file -> {}."""
    p = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}
    return {rid: normalize_row(row) for rid, row in records.items() if isinstance(row, dict)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(records: dict, registry_path: Path = None) -> None:
    p = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    _atomic_write(p, json.dumps({"records": records}, indent=2, sort_keys=True))


def rel_chatter_path(chatter_path) -> str:
    """Store a candidate path as REPO_DIR-relative (e.g. ``data/synthetic-agent-chatter/...``).

    Candidate files live under REPO_DIR, so a repo-relative string keeps any absolute ``/Users/...``
    (or other home-dir) prefix out of registry.json -- which the leak gate scans as a published,
    numbers-only artifact and whose literal rule trips on such prefixes. A path outside REPO_DIR (e.g.
    a test tempdir) is returned unchanged; there is nothing repo-local to anchor it to.
    """
    s = str(chatter_path)
    if not s:
        return s
    try:
        return str(Path(s).resolve().relative_to(REPO_DIR))
    except (ValueError, OSError):
        return s


def make_row(candidate, *, parent_id=None, generation=0, channel="", model="",
             chatter_path="", created_ts=None, bootstrap=False, origin=None) -> dict:
    """Build a section-5 registry row (numbers / ids / path only) for a candidate.

    `bootstrap` marks seeds that seeded the landscape archive (the pilot and its gen-0
    variants); `origin` is one of ORIGINS and defaults from parent_id (default_origin)
    so callers that predate the field keep producing well-formed rows.
    """
    rid = record_id(candidate)
    c = counts(candidate)
    return {
        "record_id": rid,
        "parent_id": parent_id,
        "generation": int(generation),
        "channel": channel,
        "model": model,
        "chatter_path": rel_chatter_path(chatter_path),
        "length_bytes": c["length_bytes"],
        "length_chars": c["length_chars"],
        "n_threads": c["n_threads"],
        "n_posts": c["n_posts"],
        "created_ts": float(created_ts if created_ts is not None else time.time()),
        "bootstrap": bool(bootstrap),
        "origin": _check_origin(origin) if origin is not None else default_origin(parent_id),
    }


def upsert(row: dict, registry_path: Path = None) -> dict:
    """Insert or replace a row by record_id; returns the stored row."""
    p = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    clean = normalize_row(row)
    with REGISTRY_LOCK:
        records = load(p)
        records[clean["record_id"]] = clean
        save(records, p)
    return clean


def get(rid: str, registry_path: Path = None) -> dict:
    """The row for a record id, or {} if absent."""
    return load(registry_path).get(rid, {})


# Fields annotate() may change on an existing row: provenance only, never the id / sizes / path.
_ANNOTATABLE = ("parent_id", "generation", "channel", "model", "bootstrap", "origin")


def annotate(rid: str, registry_path: Path = None, **fields) -> dict:
    """Update provenance fields of an existing row in place; returns the stored row ({} if absent).

    Used by bootstrap.py to re-label a row that generate._persist wrote with defaults
    (e.g. a gen-0 pilot variant persisted as origin="mutation") as bootstrap / origin="pilot".
    Unknown field names raise ValueError("E_FIELD"); an invalid origin raises ValueError("E_ORIGIN").
    """
    bad = [k for k in fields if k not in _ANNOTATABLE]
    if bad:
        raise ValueError("E_FIELD")
    if "origin" in fields:
        _check_origin(fields["origin"])
    p = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    with REGISTRY_LOCK:
        records = load(p)
        row = records.get(rid)
        if not row:
            return {}
        row = dict(row)
        row.update(fields)
        if "generation" in fields:
            row["generation"] = int(fields["generation"])
        clean = normalize_row(row)
        records[rid] = clean
        save(records, p)
    return clean
