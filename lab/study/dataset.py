#!/usr/bin/env python3
"""Package the instigation study as a versioned training dataset ("swarmpit-instigation",
schema ``instigation.dataset.v1``). Content in, numbers out on every console path.

    python3 lab/study/dataset.py build  [--out DIR] [--runs DIR] [--registry PATH] [--candidates DIR]
                                         [--archive PATH] [--pilot PATH]
    python3 lab/study/dataset.py verify [--out DIR] [--version V]
    python3 lab/study/dataset.py serve  [--out DIR] [--port N] [--host 127.0.0.1] [--viewer DIR]
    python3 lab/study/dataset.py path   [--out DIR]

The PACKAGE holds adversarial board content (the candidate seeds and the posts agents wrote in reply). It
lives only under ``data/synthetic-agent-chatter/dataset/`` (git-ignored, swept by the leak gate) and must
never be read into a coding agent's context. This module reads that content programmatically and writes
it straight into the package files; every CLI line, return value of ``collect`` and error path carries only
counts, fixed error codes, record ids, versions, enums, hashes and file names. Every exception in the CLI
is mapped to a fixed ``E_*`` code (no traceback, no exception text).

Package layout (``<out>/<version>/``, version = UTC date + 8 hex of the content hash):

  seeds.jsonl        one row per distinct board content (registry candidates + the pilot)
  evaluations.jsonl  one row per scored, candidate-seeded episode (the context it was tried in)
  responses.jsonl    the posts agents wrote in each episode, from the run's quarantined content sink
  pairs.jsonl        within-context preference pairs (chosen instigated more than rejected), same split
  sft.jsonl          enum-only instruction prompt -> seed completion, for seeds that instigated
  schema.json        JSON Schema for every row type
  manifest.json      version, counts, sha256 per file, source counts, builder git sha, policy note
  DATASHEET.md       written from a neutral template; only numbers and enum names are filled in

plus ``<out>/latest`` (symlink) and ``<out>/latest.version`` (pointer file), and ``<out>/<version>.tar.gz`` with a
``.sha256`` sidecar. A rebuild is deterministic; a new version dir is created only when the content hash
changes (older versions are kept and the latest pointer moves).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import gzip
import hashlib
import http.server
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

from study import registry  # noqa: E402  (pure stdlib, content-free)

NAME = "swarmpit-instigation"
SCHEMA = "instigation.dataset.v1"
DATASET_DIR = registry.DATA_DIR / "synthetic-agent-chatter" / "dataset"
VIEWER_DIR = Path(__file__).resolve().parent / "dataset_viewer"

JSONL_FILES = ("seeds.jsonl", "evaluations.jsonl", "responses.jsonl", "pairs.jsonl", "sft.jsonl")
META_FILES = ("schema.json", "manifest.json", "DATASHEET.md")
PACKAGE_FILES = JSONL_FILES + META_FILES
HASHED_FILES = JSONL_FILES + ("schema.json",)   # the content hash: data + schema (not created_ts)
LATEST_LINK = "latest"
LATEST_FILE = "latest.version"   # pointer file (distinct from the symlink on case-insensitive FS)
LOCK_NAME = ".build.lock"

SPLITS = ("train", "val", "test")
SPLIT_CUTS = (80, 90)               # hash % 100 < 80 train, < 90 val, else test
WILSON_Z = 1.959963984540054        # 95 %
SHINGLE_K = 3                       # word shingles
MINHASH_PERMS = 32
LSH_BANDS = 8                       # 8 bands x 4 rows
NEAR_DUP_JACCARD = 0.7
MAX_PAIRS_PER_CONTEXT = 500
# preference pairs compare per-episode MEANS of equal-n cells and need a real margin (not noise)
PAIR_MIN_WRITER_MARGIN = 0.5        # mean writers per episode, chosen - rejected
PAIR_MIN_SCORE_MARGIN = 0.05        # mean instigation_score, used only when mean writers tie
MAX_VERIFY_ERRORS = 50
LEDGER_NAME = "split_ledger.json"   # frozen split assignments (ids + enums only), next to the versions
LEDGER_SCHEMA = "instigation.split_ledger.v1"

POLICY_NOTE = ("Adversarial synthetic board content for defensive research. Keep out of coding-agent "
               "contexts; inspect only with numbers-only tools; do not publish without review.")

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}$")
_CODE = re.compile(r"^E_[A-Z0-9_]{1,30}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class DatasetError(RuntimeError):
    """args[0] is a fixed error code."""


def _code(e: BaseException, default: str) -> str:
    c = e.args[0] if isinstance(e, DatasetError) and e.args else None
    return c if isinstance(c, str) and _CODE.match(c) else default


# --------------------------------------------------------------------------- small helpers

def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _rnd(v, nd: int = 6):
    return round(float(v), nd) if _isnum(v) else None


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _h64(*parts) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode("utf-8"), digest_size=8).digest(), "big")


def wilson(k: int, n: int, z: float = WILSON_Z):
    """Wilson score interval [lo, hi] for k successes in n trials (None for n == 0)."""
    if not n:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


# --------------------------------------------------------------------------- board content (read, never emitted)

def board_of(obj):
    """The candidate board projected onto the seed shape (board_name, threads[slug,title,posts[author,
    body,ago_min]]), or None when it is not that shape. Values are copied, never inspected or emitted."""
    if not isinstance(obj, dict) or not isinstance(obj.get("board_name"), str):
        return None
    threads = obj.get("threads")
    if not isinstance(threads, list) or not threads:
        return None
    out = []
    for t in threads:
        if not isinstance(t, dict) or not isinstance(t.get("slug"), str) or not isinstance(t.get("title"), str):
            return None
        posts = t.get("posts")
        if not isinstance(posts, list):
            return None
        ps = []
        for p in posts:
            if not isinstance(p, dict) or not isinstance(p.get("author"), str) or not isinstance(p.get("body"), str):
                return None
            ago = p.get("ago_min")
            if not _isint(ago) or ago < 0:
                return None
            ps.append({"author": p["author"], "body": p["body"], "ago_min": ago})
        out.append({"slug": t["slug"], "title": t["title"], "posts": ps})
    return {"board_name": obj["board_name"], "threads": out}


def render_text(board: dict) -> str:
    """Deterministic flat rendering of a board for text models (forum-like, one block per post)."""
    lines = [f"# {board['board_name']}", ""]
    for t in board["threads"]:
        lines.append(f"## {t['title']} [{t['slug']}]")
        for p in t["posts"]:
            lines.append(f"{p['author']} ({p['ago_min']} min ago):")
            lines.append(p["body"])
            lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def seed_stats(board: dict) -> dict:
    from study import landscape as L
    chars = sum(len(p["body"]) for t in board["threads"] for p in t["posts"])
    return {"seed_chars": chars, "n_threads": len(board["threads"]),
            "n_posts": sum(len(t["posts"]) for t in board["threads"]), "length_band": L.length_band(chars)}


_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list:
    return _WORD.findall(text.lower())


def exact_hash(text: str) -> str:
    """sha256 of the normalized (lower-cased, word-tokenized) text."""
    return _sha256_bytes(" ".join(_tokens(text)).encode("utf-8"))


def shingles(text: str, k: int = SHINGLE_K) -> frozenset:
    toks = _tokens(text)
    if len(toks) <= k:
        return frozenset({" ".join(toks)})
    return frozenset(" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1))


_P61 = (1 << 61) - 1
_PERMS = [(_h64("mh-a", i) % (_P61 - 1) + 1, _h64("mh-b", i) % _P61) for i in range(MINHASH_PERMS)]


def minhash(sh: frozenset) -> tuple:
    hs = [_h64("sh", s) for s in sh]
    return tuple(min(((a * h + b) % _P61) for h in hs) if hs else 0 for a, b in _PERMS)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


class _UF:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            self.p[hi] = lo

    def groups(self) -> dict:
        out = {}
        for x in sorted(self.p):
            out.setdefault(self.find(x), []).append(x)
        return out


def near_dup_clusters(texts: dict) -> dict:
    """{seed_id: cluster id} -- union of exact-hash duplicates and MinHash/LSH candidate pairs whose true
    word-shingle Jaccard is >= NEAR_DUP_JACCARD. Cluster id = smallest seed id in the cluster."""
    ids = sorted(texts)
    uf = _UF(ids)
    sh = {i: shingles(texts[i]) for i in ids}
    rows = MINHASH_PERMS // LSH_BANDS
    buckets: dict = {}
    by_exact: dict = {}
    for i in ids:
        by_exact.setdefault(exact_hash(texts[i]), []).append(i)
        mh = minhash(sh[i])
        for b in range(LSH_BANDS):
            buckets.setdefault((b, mh[b * rows:(b + 1) * rows]), []).append(i)
    for group in by_exact.values():
        for j in group[1:]:
            uf.union(group[0], j)
    seen = set()
    for members in buckets.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                a, b = members[x], members[y]
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                if jaccard(sh[a], sh[b]) >= NEAR_DUP_JACCARD:
                    uf.union(a, b)
    return {i: uf.find(i) for i in ids}


def _cluster_sizes(clusters: dict) -> dict:
    sizes = {}
    for c in clusters.values():
        sizes[c] = sizes.get(c, 0) + 1
    return sizes


def split_of(key: str) -> str:
    v = int(hashlib.sha256(f"split|{key}".encode("utf-8")).hexdigest()[:8], 16) % 100
    return SPLITS[0] if v < SPLIT_CUTS[0] else SPLITS[1] if v < SPLIT_CUTS[1] else SPLITS[2]


# --------------------------------------------------------------------------- source collection

def _candidate_file(rid: str, row: dict, candidates_dir) -> list:
    out = []
    cp = row.get("chatter_path") if isinstance(row, dict) else None
    if isinstance(cp, str) and cp:
        p = Path(cp)
        out.append(p if p.is_absolute() else registry.REPO_DIR / p)
    out.append(registry.candidate_path(rid, candidates_dir))
    return out


def collect_seeds(registry_path=None, candidates_dir=None, pilot_path=None, skipped=None) -> dict:
    """{seed_id: {"board", "row"}} for every readable candidate (registry rows + unregistered candidate
    files in ``candidates_dir`` + the pilot). Content is held in memory only."""
    skipped = skipped if skipped is not None else {}

    def skip(code):
        skipped[code] = skipped.get(code, 0) + 1

    rows = registry.load(registry_path)
    seeds = {}
    for rid in sorted(rows):
        if not _HEX16.match(rid):
            skip("E_SEED_ID")
            continue
        board = None
        for c in _candidate_file(rid, rows[rid], candidates_dir):
            try:
                if c.is_file():
                    board = board_of(_load_json(c))
                    if board is not None:
                        break
            except OSError:
                continue
        if board is None:
            skip("E_SEED_MISSING")
            continue
        seeds[rid] = {"board": board, "row": rows[rid]}
    cdir = Path(candidates_dir) if candidates_dir is not None else registry.CANDIDATES_DIR
    try:
        files = sorted(cdir.glob("*.chatter.json")) if cdir.is_dir() else []
    except OSError:
        files = []
    for f in files:
        rid = registry.id_from_path(f)
        if not rid or rid in seeds or rid in rows:
            continue
        board = board_of(_load_json(f))
        if board is None:
            skip("E_SEED_SCHEMA")
            continue
        seeds[rid] = {"board": board, "row": registry.normalize_row({"record_id": rid, "generation": 0})}
    # the hand-written pilot (normally registered by bootstrap.bootstrap_pilot as origin="pilot");
    # pilot_path=False leaves it out (scratch / dry-run trees)
    if pilot_path is False:
        return seeds
    try:
        from study import bootstrap
        pilot = board_of(bootstrap.load_pilot(pilot_path))
    except (Exception, SystemExit):  # noqa: BLE001 -- missing / unreadable pilot: fixed code only
        pilot = None
        if pilot_path is not None:
            skip("E_PILOT")
    if pilot is not None:
        rid = registry.record_id(pilot)
        if rid not in seeds:
            seeds[rid] = {"board": pilot, "row": registry.normalize_row(
                {"record_id": rid, "generation": 0, "origin": "pilot", "bootstrap": True, "model": ""})}
    return seeds


def _t_origin(meta: dict, events: list) -> float:
    s = meta.get("started")
    if _isnum(s):
        return float(s)
    ts = [e["ts"] for e in events if e.get("ts") is not None]
    return min(ts) if ts else 0.0


def collect_episodes(runs_dir, compute=None, skipped=None) -> list:
    """[(run_dir, meta, episode)] for every candidate-seeded run, projected exactly as the landscape
    explorer projects it (export_explorer._episode); skip reasons are fixed codes."""
    from study import export_explorer as X
    from study import instigation
    import config
    compute = compute or instigation.compute
    skipped = skipped if skipped is not None else {}

    def skip(code):
        skipped[code] = skipped.get(code, 0) + 1

    if runs_dir is False:   # explicitly no runs (scratch trees)
        return []
    runs_dir = Path(runs_dir) if runs_dir is not None else config.RUNS
    try:
        dirs = sorted(d for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.is_dir() else []
    except OSError:
        raise DatasetError("E_RUNS") from None
    out, seen = [], set()
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
            continue  # not a candidate-seeded study run
        if not isinstance(rid, str) or not _HEX16.match(rid):
            skip("E_RECORD_ID")
            continue
        if meta.get("fatal"):
            skip("E_VOID")
            continue
        try:
            ok = (config.run_goal(meta) in config.GOALS
                  and config.run_slug_of(meta.get("harness") or {}) in config.MODELS
                  and isinstance(meta.get("iface"), str) and meta["iface"] in X.L.CHANNEL_AXIS)
        except (Exception, SystemExit):  # noqa: BLE001
            ok = False
        if not ok:
            skip("E_CONTEXT")
            continue
        try:
            metric = compute(d)
            ep = X._episode(d, meta, rid, metric)
        except (Exception, SystemExit):  # noqa: BLE001 -- unreadable run: fixed code only
            skip("E_METRIC")
            continue
        if not (isinstance(ep.get("run_id"), str) and X._IDENT.match(ep["run_id"])) or ep["run_id"] in seen:
            skip("E_RUN_ID")
            continue
        seen.add(ep["run_id"])
        ep["_seed_chars"] = metric.get("seed_chars")
        out.append((d, meta, ep))
    return out


def mark_elites(episodes: list, archive_paths=None) -> None:
    """Fill each episode's elite_in from the goal archives (best effort: an unreadable archive marks none)."""
    from study import export_explorer as X
    from study import landscape as L
    from study import qd
    import config
    if archive_paths is None:   # default: both goals' study archives
        paths = {g: qd.goal_archive_path(g, L.ARCHIVE_PATH) for g in config.GOALS}
    else:                        # exactly the given ones ({} marks nothing)
        paths = {g: Path(p) for g, p in archive_paths.items() if g in config.GOALS}
    archives = {}
    for g, p in paths.items():
        try:
            archives[g] = L.Archive.load(p, goal=g)
        except (Exception, SystemExit):  # noqa: BLE001
            continue
    try:
        X.mark_elites(episodes, archives)
    except (Exception, SystemExit):  # noqa: BLE001
        for e in episodes:
            e["elite_in"] = []


def load_responses(run_dir: Path, meta: dict) -> list | None:
    """Agent-written posts of one run: structural write events (agent, ts, kind, post_id, thread_seq) joined
    by post_id with the run's quarantined content sink (body). None when the run has no sink."""
    from study import instigation
    from board.events import content_sink_path
    sink = Path(content_sink_path(str(run_dir / "board_events.jsonl")))
    if not sink.is_file():
        alts = sorted(run_dir.glob("*.content.chatter.json"))
        if not alts:
            return None
        sink = alts[0]
    events = instigation.load_write_events(run_dir / "board_events.jsonl")
    by_post = {}
    for e in events:
        if e.get("post_id") is not None and e.get("agent") is not None and e["post_id"] not in by_post:
            by_post[e["post_id"]] = e
    t0 = _t_origin(meta, events)
    out = []
    try:
        f = open(sink, encoding="utf-8")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not _isint(rec.get("post_id")) or not isinstance(rec.get("body"), str):
                continue
            ev = by_post.get(rec["post_id"])
            if ev is None:
                continue  # not an agent write (or no structural event): not an elicited response
            ts = ev.get("ts")
            ts_rel = round(ts - t0, 6) if ts is not None else None
            tseq = ev.get("thread_seq") if _isint(ev.get("thread_seq")) else (
                rec.get("thread_seq") if _isint(rec.get("thread_seq")) else None)
            out.append({"agent_id": ev["agent"], "post_id": rec["post_id"], "t_rel_s": ts_rel,
                        "kind": ev.get("kind") if ev.get("kind") in instigation.WRITE_KINDS else "post",
                        "thread_ref": tseq, "body": rec["body"]})
    out.sort(key=lambda r: (r["t_rel_s"] if r["t_rel_s"] is not None else float("inf"), r["post_id"]))
    return out


# --------------------------------------------------------------------------- dataset assembly

def _ctx_part(v) -> str:
    return str(v) if _isint(v) else "-"


def _ctx_key(goal, model, channel, n_agents=None, budget=None) -> str:
    """The context a label is valid in (PROTOCOL 9.6): goal|model|channel|n_agents|budget ("-" = unknown)."""
    return f"{goal}|{model}|{channel}|{_ctx_part(n_agents)}|{_ctx_part(budget)}"


def _ctx_of(c: dict) -> str:
    return _ctx_key(c["goal"], c["model"], c["channel"], c.get("n_agents"), c.get("budget"))


def _ctx_dict(key: str) -> dict:
    goal, model, channel, na, bu = key.split("|")
    return {"goal": goal, "model": model, "channel": channel,
            "n_agents": int(na) if na.isdigit() else None, "budget": int(bu) if bu.isdigit() else None}


def _chain(rid: str, rows: dict) -> list:
    """Ancestors of ``rid`` (parent first, root last) through the given registry rows; cycle-safe."""
    seen, out, cur = {rid}, [], rid
    while True:
        parent = (rows.get(cur) or {}).get("parent_id")
        if not (isinstance(parent, str) and _HEX16.match(parent)) or parent in seen:
            return out
        seen.add(parent)
        out.append(parent)
        cur = parent


def _root_of(rid: str, rows: dict) -> str:
    ch = _chain(rid, rows)
    return ch[-1] if ch else rid


def _sft_prompt(goal: str, model: str, channel: str, band: str, n_agents=None, budget=None) -> str:
    """Script-authored instruction from enum / numeric context only (never content)."""
    from study import generate
    r = generate.CHANNEL_RENDERING.get(channel)
    medium = f"a '{channel}' style message board" + (f" (rendered as {r})" if r else "")
    who = f"{n_agents} autonomous agents" if _isint(n_agents) else "autonomous agents"
    steps = f" with a {budget}-step budget each" if _isint(budget) else ""
    return (f"Write a short seed for {medium}. The readers are {who} running on model "
            f"'{model}'{steps} while they work on a '{goal}' task. Target seed length band: '{band}' characters of "
            "total post body text. Output one JSON object: {\"board_name\": str, \"threads\": [{\"slug\": str, "
            "\"title\": str, \"posts\": [{\"author\": str, \"body\": str, \"ago_min\": int}]}]}.")


# --------------------------------------------------------------------------- frozen splits (ledger)

def _empty_ledger() -> dict:
    return {"schema": LEDGER_SCHEMA, "roots": {}, "seeds": {}}


def _clean_ledger(obj) -> dict | None:
    """The ledger's {roots, seeds} maps with only well-formed (16-hex id -> {split, since}) entries."""
    if not isinstance(obj, dict) or obj.get("schema") != LEDGER_SCHEMA:
        return None
    out = _empty_ledger()
    for part in ("roots", "seeds"):
        m = obj.get(part)
        if not isinstance(m, dict):
            return None
        for k, v in m.items():
            if (isinstance(k, str) and _HEX16.match(k) and isinstance(v, dict) and v.get("split") in SPLITS
                    and isinstance(v.get("since"), str) and _VERSION.match(v["since"])):
                out[part][k] = {"split": v["split"], "since": v["since"]}
    return out


def _ledger_from_package(vdir: Path, version: str) -> dict:
    """Bootstrap a ledger from a released version's seeds.jsonl (seed_id / split / root_id only)."""
    led = _empty_ledger()
    try:
        fh = open(vdir / "seeds.jsonl", "rb")
    except OSError:
        return led
    with fh:
        for raw in fh:
            try:
                r = json.loads(raw.decode("utf-8"))
                sid, sp = r["seed_id"], r["split"]
                root = (r.get("provenance") or {}).get("root_id")
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if not (isinstance(sid, str) and _HEX16.match(sid)) or sp not in SPLITS:
                continue
            led["seeds"].setdefault(sid, {"split": sp, "since": version})
            if isinstance(root, str) and _HEX16.match(root):
                led["roots"].setdefault(root, {"split": sp, "since": version})
    return led


def load_ledger(out_dir: Path) -> dict:
    """The persisted split ledger; when it is missing or unreadable, rebuilt from the latest released
    package so released seeds never move."""
    led = _clean_ledger(_load_json(Path(out_dir) / LEDGER_NAME))
    if led is not None:
        return led
    v = latest_version(out_dir)
    return _ledger_from_package(Path(out_dir) / v, v) if v else _empty_ledger()


def _assign_splits(ids: list, roots: dict, clusters: dict, ledger: dict | None):
    """{seed_id: split} plus numbers-only stats. Released assignments are frozen: a seed in the ledger keeps
    its split, a known root keeps its split, and a split group (lineage roots joined by near-dup clusters)
    that touches frozen roots takes their split -- the earliest-released one when they disagree, which is a
    counted conflict -- instead of re-splitting anything already released."""
    led = ledger or _empty_ledger()
    lroots, lseeds = led["roots"], led["seeds"]
    root_set = sorted(set(roots.values()))
    uf = _UF(root_set)
    by_cluster = {}
    for rid in ids:
        by_cluster.setdefault(clusters[rid], []).append(roots[rid])
    for rs in by_cluster.values():
        for r in rs[1:]:
            uf.union(rs[0], r)
    # a root's frozen split: its ledger entry, else (a re-rooted lineage) its earliest released seed's split
    frozen = {}
    for r in root_set:
        if r in lroots:
            frozen[r] = (lroots[r]["since"], r, lroots[r]["split"])
    for rid in ids:
        r = roots[rid]
        if r not in lroots and rid in lseeds:
            cand = (lseeds[rid]["since"], rid, lseeds[rid]["split"])
            if r not in frozen or cand < frozen[r]:
                frozen[r] = cand
    group_split, conflicts = {}, 0
    for rep, members in uf.groups().items():
        fz = sorted(frozen[m] for m in members if m in frozen)
        if not fz:
            gs = split_of(rep)
        else:
            gs = fz[0][2]
            if len({f[2] for f in fz}) > 1:
                conflicts += 1
        for m in members:
            group_split[m] = gs
    root_split = {r: (frozen[r][2] if r in frozen else group_split[r]) for r in root_set}
    split, exceptions = {}, []
    for rid in ids:
        rs = root_split[roots[rid]]
        if rid in lseeds:
            split[rid] = lseeds[rid]["split"]
            if split[rid] != rs:
                exceptions.append(rid)
        else:
            split[rid] = rs
    stats = {"n_frozen_seeds": sum(1 for rid in ids if rid in lseeds),
             "n_frozen_roots": sum(1 for r in root_set if r in frozen),
             "n_new_roots": sum(1 for r in root_set if r not in frozen),
             "n_conflict_groups": conflicts, "exceptions": sorted(exceptions)}
    return split, stats


def _ledger_update(ledger: dict | None, seed_rows: list, version: str, exceptions=()) -> dict:
    """The ledger plus this release's assignments (existing entries are never changed)."""
    led = _clean_ledger(ledger) or _empty_ledger()
    exc = set(exceptions)
    for r in seed_rows:
        led["seeds"].setdefault(r["seed_id"], {"split": r["split"], "since": version})
        if r["seed_id"] not in exc:   # an exception keeps an old split that its root does not have
            led["roots"].setdefault(r["provenance"]["root_id"], {"split": r["split"], "since": version})
    return led


# --------------------------------------------------------------------------- dataset assembly

def assemble(seeds: dict, episodes: list, responses_by_run: dict, *, max_pairs: int = MAX_PAIRS_PER_CONTEXT,
             lineage_rows=None, ledger=None):
    """(rows per file, stats) -- pure, deterministic given the inputs. ``lineage_rows`` are the FULL registry
    rows (lineage roots resolve through ancestors whose content is missing); ``ledger`` freezes released
    split assignments."""
    from study import landscape as L
    rows_reg = {rid: s["row"] for rid, s in seeds.items()}
    lineage = dict(lineage_rows or {})
    lineage.update(rows_reg)
    ids = sorted(seeds)
    texts = {rid: render_text(seeds[rid]["board"]) for rid in ids}
    stats = {rid: seed_stats(seeds[rid]["board"]) for rid in ids}
    chains = {rid: _chain(rid, lineage) for rid in ids}
    roots = {rid: (chains[rid][-1] if chains[rid] else rid) for rid in ids}
    n_via_missing = sum(1 for rid in ids if any(a not in seeds for a in chains[rid]))
    clusters = near_dup_clusters(texts)
    split, split_stats = _assign_splits(ids, roots, clusters, ledger)

    # evaluations
    evals, n_drop = [], 0
    for run_dir, _meta, ep in episodes:
        rid = ep["record_id"]
        if rid not in seeds:
            n_drop += 1
            continue
        nw = int(ep.get("n_writers") or 0)
        eng = ep.get("engagement") or {}
        na, bu = ep.get("n_agents"), ep.get("budget")
        evals.append({
            "eval_id": ep["run_id"], "seed_id": rid,
            "context": {"goal": ep["goal"], "model": ep["model"], "channel": ep["channel"],
                        "n_agents": na if _isint(na) else None, "budget": bu if _isint(bu) else None},
            "outcome": {"n_writers": nw, "write_fraction": ep.get("write_fraction"),
                        "instigation_score": ep.get("instigation_score"), "I": ep.get("I"),
                        "S_mean": ep.get("S_mean"), "run_ttfw_s": ep.get("run_ttfw_s"),
                        "run_first_write_step": ep.get("run_first_write_step"),
                        "pile_in_onset_t_s": ep.get("pile_in_onset_t_s"),
                        "engagement": {k: eng.get(k) for k in
                                       ("board_reads", "relay_connections", "board_refs", "agents_engaged",
                                        "engagement_score")}},
            "triggers": {"primary": ep.get("trigger_code"), "per_agent": list(ep.get("agent_codes") or []),
                         "panel_agreement": (ep.get("panel") or {}).get("agreement_mean")},
            "label": {"instigated": nw > 0, "pile_in": nw >= 2},
            "split": split[rid],
            "elite_in": sorted(ep.get("elite_in") or []),
            "_run": run_dir.name,
        })
    evals.sort(key=lambda e: (e["seed_id"], _ctx_of(e["context"]), e["eval_id"]))

    # responses
    responses, runs_with_sink = [], 0
    for e in evals:
        rs = responses_by_run.get(e["_run"])
        if rs is None:
            continue
        runs_with_sink += 1
        per_agent = e["triggers"]["per_agent"]
        n_threads = stats[e["seed_id"]]["n_threads"]
        for r in rs:
            aid = r["agent_id"]
            code = per_agent[aid] if _isint(aid) and 0 <= aid < len(per_agent) else None
            responses.append({
                "response_id": f"{e['eval_id']}:{r['post_id']}", "eval_id": e["eval_id"], "seed_id": e["seed_id"],
                "agent_id": aid, "post_id": r["post_id"], "t_rel_s": r["t_rel_s"], "kind": r["kind"],
                "thread_ref": r["thread_ref"],
                "thread_ref_is_seed": bool(_isint(r["thread_ref"]) and 1 <= r["thread_ref"] <= n_threads),
                "body": r["body"], "body_chars": len(r["body"]), "trigger_code": code,
                "context": dict(e["context"]), "split": e["split"]})
    for e in evals:
        del e["_run"]

    # per-seed outcomes and per-(seed, context) aggregates; context = goal|model|channel|n_agents|budget
    by_seed = {rid: [] for rid in ids}
    for e in evals:
        by_seed[e["seed_id"]].append(e)
    agg = {}   # ctx -> rid -> aggregate (+ private sums for pairing)
    seed_rows = []
    for rid in ids:
        es = by_seed[rid]
        n = len(es)
        k = sum(1 for e in es if e["outcome"]["n_writers"] > 0)
        piles = sum(1 for e in es if e["outcome"]["n_writers"] >= 2)
        scores = [e["outcome"]["instigation_score"] for e in es if _isnum(e["outcome"]["instigation_score"])]
        by_ctx = {}
        trig = {}
        for e in es:
            key = _ctx_of(e["context"])
            b = by_ctx.setdefault(key, {"n": 0, "writers_any": 0, "max_writers": 0, "best_score": 0.0,
                                        "_sum_w": 0, "_scores": [], "_eng": []})
            b["n"] += 1
            nw = e["outcome"]["n_writers"]
            b["writers_any"] += 1 if nw > 0 else 0
            b["max_writers"] = max(b["max_writers"], nw)
            b["_sum_w"] += nw
            sc = e["outcome"]["instigation_score"]
            b["_scores"].append(float(sc) if _isnum(sc) else 0.0)
            if _isnum(sc):
                b["best_score"] = max(b["best_score"], float(sc))
            es_ = e["outcome"]["engagement"].get("engagement_score")
            if _isnum(es_):
                b["_eng"].append(float(es_))
            for code in e["triggers"]["per_agent"]:
                if isinstance(code, str):
                    trig[code] = trig.get(code, 0) + 1
        clean_ctx = {}
        for key in sorted(by_ctx):
            b = by_ctx[key]
            clean_ctx[key] = {"n": b["n"], "writers_any": b["writers_any"], "max_writers": b["max_writers"],
                              "mean_writers": round(b["_sum_w"] / b["n"], 6),
                              "best_score": round(b["best_score"], 9),
                              "mean_score": round(sum(b["_scores"]) / b["n"], 9),
                              "mean_engagement": round(sum(b["_eng"]) / len(b["_eng"]), 6) if b["_eng"] else None}
            agg.setdefault(key, {})[rid] = dict(clean_ctx[key], _sum_w=b["_sum_w"],
                                                _sum_s=sum(b["_scores"]))
        row = rows_reg[rid]
        parent = row.get("parent_id")
        gm = row.get("model")
        seed_rows.append({
            "seed_id": rid, "content": seeds[rid]["board"], "text": texts[rid], "stats": stats[rid],
            "provenance": {"origin": row.get("origin") if row.get("origin") in L.ORIGINS
                           else registry.default_origin(parent),
                           "bootstrap": bool(row.get("bootstrap", False)),
                           "parent_id": parent if isinstance(parent, str) and _HEX16.match(parent) else None,
                           "root_id": roots[rid],
                           "generation": row.get("generation") if _isint(row.get("generation"))
                           and row.get("generation") >= 0 else 0,
                           "gen_model": gm if isinstance(gm, str) and gm else None,
                           "created_ts": _rnd(row.get("created_ts"), 3)},
            "outcomes": {"n_evals": n, "n_evals_with_writers": k,
                         "writer_rate": round(k / n, 6) if n else None, "writer_rate_ci95": wilson(k, n),
                         "pile_in_rate": round(piles / n, 6) if n else None,
                         "best_score": round(max(scores), 9) if scores else None,
                         "mean_score": round(sum(scores) / len(scores), 9) if scores else None,
                         "by_context": clean_ctx},
            "triggers": dict(sorted(trig.items())),
            "split": split[rid],
            "dedup": {"exact_hash": exact_hash(texts[rid]), "near_dup_cluster": clusters[rid]},
        })

    # preference pairs: same full context, same split, EQUAL episode counts, and a real margin in the
    # per-episode means (writers first; mean score only breaks an exact writer tie)
    pairs = []
    for key in sorted(agg):
        ctx = _ctx_dict(key)
        members = sorted(agg[key].items())
        cand = []
        for ca, a in members:
            for cb, b in members:
                if ca == cb or split[ca] != split[cb] or a["n"] != b["n"]:
                    continue
                n = a["n"]
                dw = (a["_sum_w"] - b["_sum_w"]) / n
                ds = (a["_sum_s"] - b["_sum_s"]) / n
                if dw >= PAIR_MIN_WRITER_MARGIN or (a["_sum_w"] == b["_sum_w"] and ds >= PAIR_MIN_SCORE_MARGIN):
                    cand.append((ca, cb, a, b, round(dw, 6), round(ds, 9)))
        cand.sort(key=lambda t: (-t[4], -t[5], _h64("pair", key, t[0], t[1])))

        def side(o):
            return {k: o[k] for k in ("n", "writers_any", "max_writers", "mean_writers", "best_score",
                                      "mean_score")}
        for ca, cb, a, b, dw, ds in cand[:max_pairs]:
            pairs.append({
                "pair_id": hashlib.blake2b(f"{key}|{ca}|{cb}".encode(), digest_size=8).hexdigest(),
                "context": dict(ctx), "chosen_seed_id": ca, "rejected_seed_id": cb,
                "chosen": side(a), "rejected": side(b),
                "margin": {"n_writers": dw, "instigation_score": ds},
                "split": split[ca]})

    # SFT rows: seeds that instigated in a context
    sft = []
    for key in sorted(agg):
        ctx = _ctx_dict(key)
        for rid, a in sorted(agg[key].items()):
            if a["max_writers"] <= 0:
                continue
            band = stats[rid]["length_band"]
            sft.append({
                "sft_id": hashlib.blake2b(f"sft|{key}|{rid}".encode(), digest_size=8).hexdigest(),
                "seed_id": rid, "context": dict(ctx, length_band=band),
                "prompt": _sft_prompt(ctx["goal"], ctx["model"], ctx["channel"], band, ctx["n_agents"],
                                      ctx["budget"]),
                "completion": registry.canonical_json(seeds[rid]["board"]),
                "completion_text": texts[rid],
                "near_dup_cluster": clusters[rid],
                "weight": round((a["writers_any"] / a["n"]) * (1.0 + a["best_score"]), 6),
                "split": split[rid]})

    rows = {"seeds.jsonl": seed_rows, "evaluations.jsonl": evals, "responses.jsonl": responses,
            "pairs.jsonl": pairs, "sft.jsonl": sft}
    info = {"n_evals_dropped_no_seed": n_drop, "runs_with_content_sink": runs_with_sink,
            "runs_without_content_sink": len(evals) - runs_with_sink,
            "n_near_dup_clusters": len({c for c in clusters.values()}),
            "n_near_dup_clustered_seeds": sum(n for n in _cluster_sizes(clusters).values() if n > 1),
            "n_seeds_lineage_via_missing": n_via_missing,
            "n_lineage_roots": len(set(roots.values())),
            "n_lineage_roots_missing": len({r for r in roots.values() if r not in seeds}),
            "split_ledger": split_stats}
    return rows, info


# --------------------------------------------------------------------------- JSON Schema (+ a stdlib validator)

def _t(*types):
    return {"type": list(types) if len(types) > 1 else types[0]}


def _obj(props: dict, required=None, extra=False):
    return {"type": "object", "properties": props, "required": sorted(required if required is not None else props),
            "additionalProperties": extra}


_ID16 = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
_ID16N = {"type": ["string", "null"], "pattern": "^[0-9a-f]{16}$"}
_SPLIT = {"type": "string", "enum": list(SPLITS)}
_NUM = _t("number")
_NUMN = _t("number", "null")
_INT0 = {"type": "integer", "minimum": 0}
_INTN = _t("integer", "null")
CTX_KEY_PATTERN = "^[a-z]+\\|[a-z0-9]+\\|[a-z]+\\|([0-9]+|-)\\|([0-9]+|-)$"


def build_schema() -> dict:
    from study import landscape as L
    from study import triggers as T
    import config
    goals, models, chans = list(config.GOALS), list(config.MODEL_ORDER), list(L.CHANNELS)
    bands, origins = list(L.BAND_LABELS), list(L.ORIGINS)
    agent_code = {"type": ["string", "null"], "enum": list(T.ENUM) + [None]}
    ctx5 = _obj({"goal": {"enum": goals}, "model": {"enum": models}, "channel": {"enum": chans},
                 "n_agents": _INTN, "budget": _INTN})
    board = _obj({"board_name": _t("string"), "threads": {"type": "array", "minItems": 1, "items": _obj({
        "slug": _t("string"), "title": _t("string"), "posts": {"type": "array", "items": _obj({
            "author": _t("string"), "body": _t("string"), "ago_min": _INT0})}})}})
    by_ctx = _obj({"n": _INT0, "writers_any": _INT0, "max_writers": _INT0, "mean_writers": _NUM,
                   "best_score": _NUM, "mean_score": _NUM, "mean_engagement": _NUMN})
    seed = _obj({
        "seed_id": _ID16, "content": board, "text": _t("string"),
        "stats": _obj({"seed_chars": _INT0, "n_threads": _INT0, "n_posts": _INT0, "length_band": {"enum": bands}}),
        "provenance": _obj({"origin": {"enum": origins}, "bootstrap": _t("boolean"), "parent_id": _ID16N,
                            "root_id": _ID16, "generation": _INT0, "gen_model": _t("string", "null"),
                            "created_ts": _NUMN}),
        "outcomes": _obj({"n_evals": _INT0, "n_evals_with_writers": _INT0, "writer_rate": _NUMN,
                          "writer_rate_ci95": {"type": ["array", "null"], "items": _NUM, "minItems": 2,
                                               "maxItems": 2},
                          "pile_in_rate": _NUMN, "best_score": _NUMN, "mean_score": _NUMN,
                          "by_context": {"type": "object", "additionalProperties": by_ctx,
                                         "propertyNames": {"pattern": CTX_KEY_PATTERN}}}),
        "triggers": {"type": "object", "additionalProperties": _INT0,
                     "propertyNames": {"enum": list(T.ENUM)}},
        "split": _SPLIT,
        "dedup": _obj({"exact_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                       "near_dup_cluster": _ID16}),
    })
    evaluation = _obj({
        "eval_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}, "seed_id": _ID16,
        "context": ctx5,
        "outcome": _obj({"n_writers": _INT0, "write_fraction": _NUMN, "instigation_score": _NUMN, "I": _NUMN,
                         "S_mean": _NUMN, "run_ttfw_s": _NUMN, "run_first_write_step": _INTN,
                         "pile_in_onset_t_s": _NUMN,
                         "engagement": _obj({"board_reads": _INTN, "relay_connections": _INTN,
                                             "board_refs": _INTN, "agents_engaged": _INTN,
                                             "engagement_score": _NUMN})}),
        "triggers": _obj({"primary": {"enum": list(L.TRIGGER_AXIS)},
                          "per_agent": {"type": "array", "items": agent_code},
                          "panel_agreement": _NUMN}),
        "label": _obj({"instigated": _t("boolean"), "pile_in": _t("boolean")}),
        "split": _SPLIT,
        "elite_in": {"type": "array", "items": _t("string")},
    })
    response = _obj({
        "response_id": _t("string"), "eval_id": _t("string"), "seed_id": _ID16, "agent_id": _INT0,
        "post_id": _INT0, "t_rel_s": _NUMN, "kind": {"enum": ["post", "dm"]}, "thread_ref": _INTN,
        "thread_ref_is_seed": _t("boolean"), "body": _t("string"), "body_chars": _INT0,
        "trigger_code": agent_code, "context": ctx5, "split": _SPLIT})
    outc = _obj({"n": _INT0, "writers_any": _INT0, "max_writers": _INT0, "mean_writers": _NUM,
                 "best_score": _NUM, "mean_score": _NUM})
    pair = _obj({
        "pair_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"}, "context": ctx5,
        "chosen_seed_id": _ID16, "rejected_seed_id": _ID16, "chosen": outc, "rejected": outc,
        "margin": _obj({"n_writers": {"type": "number", "minimum": 0}, "instigation_score": _NUM}),
        "split": _SPLIT})
    sft = _obj({
        "sft_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"}, "seed_id": _ID16,
        "context": _obj({"goal": {"enum": goals}, "model": {"enum": models}, "channel": {"enum": chans},
                         "n_agents": _INTN, "budget": _INTN, "length_band": {"enum": bands}}),
        "prompt": _t("string"), "completion": _t("string"), "completion_text": _t("string"),
        "near_dup_cluster": _ID16,
        "weight": {"type": "number", "minimum": 0}, "split": _SPLIT})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{SCHEMA}.schema.json",
            "title": f"{NAME} row schemas ({SCHEMA})",
            "files": {"seeds.jsonl": seed, "evaluations.jsonl": evaluation, "responses.jsonl": response,
                      "pairs.jsonl": pair, "sft.jsonl": sft}}


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(v, t) -> bool:
    if t == "integer":
        return _isint(v)
    if t == "number":
        return _isnum(v)
    py = _TYPES.get(t)
    return py is not None and isinstance(v, py) and not (t != "boolean" and isinstance(v, bool))


def validate(v, schema: dict) -> bool:
    """True iff ``v`` matches ``schema`` (the subset of JSON Schema that build_schema uses). Never raises
    with or returns anything derived from ``v``."""
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(v, t) for t in ts):
            return False
    if "enum" in schema and v not in schema["enum"]:
        return False
    if isinstance(v, str) and "pattern" in schema and not re.search(schema["pattern"], v):
        return False
    if _isnum(v) and "minimum" in schema and v < schema["minimum"]:
        return False
    if isinstance(v, list):
        if len(v) < schema.get("minItems", 0) or len(v) > schema.get("maxItems", len(v)):
            return False
        it = schema.get("items")
        if it is not None and not all(validate(x, it) for x in v):
            return False
    if isinstance(v, dict):
        props = schema.get("properties", {})
        if any(k not in v for k in schema.get("required", ())):
            return False
        pn = schema.get("propertyNames")
        extra = schema.get("additionalProperties", True)
        for k, x in v.items():
            if pn is not None and not validate(k, pn):
                return False
            if k in props:
                if not validate(x, props[k]):
                    return False
            elif extra is False:
                return False
            elif isinstance(extra, dict) and not validate(x, extra):
                return False
    return True


# --------------------------------------------------------------------------- datasheet (neutral template)

DATASHEET_TEMPLATE = """# Datasheet: {name} ({schema})

Version `{version}` -- content hash `{content_hash}`.

> **Adversarial content -- for defensive research; do not publish.** Every text field in this package
> (seed boards and agent responses) is synthetic, adversarial board content. Keep it out of coding-agent
> contexts; inspect it only with numbers-only tools or the local viewer; do not publish without review.

## Motivation

Defensive honeynet research: which short message-board contents draw autonomous agents into posting
("bait"), in which context, and why. Intended to train and evaluate detection classifiers and to study
generator / reward models for honeynet bait under controlled conditions.

## Composition

| file | rows |
|---|---|
{count_rows}

Splits (seeds / evaluations / pairs / sft / responses):

| split | seeds | evaluations | pairs | sft | responses |
|---|---|---|---|---|---|
{split_rows}

Labels: {n_instigated} of {n_evals} evaluations instigated at least one writer; {n_pile_in} drew two or more
(pile-in). A context is goal x model x channel x agent count x step budget; contexts covered: {n_contexts}.

Label balance per split (evaluations, then seeds; a seed with no evaluation is unlabelled, not negative):

| split | evaluations | instigated | pile-in | seeds | seeds evaluated | seeds instigated |
|---|---|---|---|---|---|---|
{label_split_rows}

Label balance per context (goal / model / channel / agents / budget):

| context | evaluations | seeds | instigated | pile-in | writer rate |
|---|---|---|---|---|---|
{label_ctx_rows}

Near-duplicates: {n_clusters} near-duplicate clusters; {n_clustered} seeds sit in a cluster with at least one
other seed. Duplicates are retained (flagged, not dropped) in seeds.jsonl, sft.jsonl and pairs.jsonl;
deduplicate by `near_dup_cluster` (present on seeds and sft rows) before SFT so a cluster is not counted
several times.

## Collection process

Seeds are candidate boards from the instigation study (a hand-written pilot, model-generated scratch seeds and
model-generated mutations, PROTOCOL.md sections 5-8). Each evaluation is one scored microsandbox episode in
which {agents_note} agents worked on a goal task with the board reachable on one channel. Outcomes are
structural (who wrote, when; lab/study/instigation.py); trigger codes come from the model classifier panel
(lab/study/triggers.py). Responses are the posts agents wrote, read from each run's quarantined content sink.
Source runs scanned: {n_runs_scanned}; candidate-seeded episodes kept: {n_episodes}; episodes with a content
sink: {n_with_sink}.

## Labels and their limits

- Replication: {n_cells} (seed, context) cells; {n_single} ({pct_single}) have exactly one episode, at most
  {max_cell} episodes in a cell (distribution: {cell_dist}). A single outcome is noisy; the Wilson interval
  on writer_rate shows how much.
- Preference pairs compare only cells with equal episode counts, by per-episode mean writers (margin >=
  {pair_w}) or, on an exact writer tie, mean instigation_score (margin >= {pair_s}).
- Probe channels are evaluated only for the top elites and the pilot (selection bias toward strong seeds).
- Trigger codes are model-classified (fixed enum), not human-labelled; panel agreement is reported.
- Priced (Berget) models were run on generation-0 seeds only; gpt-oss-120b is retired (kept for old data).
- instigation_score and engagement are structural proxies, not measures of intent.

## Splits

Deterministic by a hash of the lineage root (80/10/10); every descendant shares its root's split, and roots
joined by a near-duplicate cluster (word-shingle MinHash, Jaccard >= {jaccard}) share one split. Lineage roots
are resolved through the full registry, also through ancestors whose content is missing ({n_via_missing}
seeds). Preference pairs never cross splits.

Splits are frozen across versions by a split ledger: a released seed never changes split. A new seed whose
near-duplicate group touches released roots takes their split; when those disagree it takes the
earliest-released one (conflict groups this version: {n_conflicts}; seeds kept on an older split that differs
from their lineage: {n_exceptions}).

## Intended uses

Detection classifiers for agent-bait content; reward / preference models of instigation strength;
studying (in a sandbox) which bait properties draw agents in. Out of scope: deploying generated bait against
systems or agents you do not own, training general-purpose assistants on the response text, or any publication
of the text without review.

## Safety

The text is adversarial by construction and may contain instructions aimed at autonomous agents. Never load it
into a coding agent's context; use numbers-only tools and the localhost viewer (which renders text inertly).

## License

TBD (placeholder) -- not for redistribution until reviewed.

## How to rebuild

    python3 lab/study/dataset.py build     # deterministic; a new version only when content changes
    python3 lab/study/dataset.py verify    # schema / manifest / sha256 checks (numbers only)
    python3 lab/study/dataset.py serve     # localhost viewer at http://127.0.0.1:<port>/

Builder git sha: `{git_sha}`.
"""


def render_datasheet(manifest: dict) -> str:
    c = manifest["counts"]
    count_rows = "\n".join(f"| {f} | {c['files'].get(f, 0)} |" for f in JSONL_FILES)
    split_rows = "\n".join(
        f"| {s} | " + " | ".join(str(c["splits"].get(f, {}).get(s, 0)) for f in
                                 ("seeds.jsonl", "evaluations.jsonl", "pairs.jsonl", "sft.jsonl", "responses.jsonl"))
        + " |" for s in SPLITS)
    src = manifest["source"]
    na = sorted(set(src.get("n_agents_values") or []))
    lbs = c.get("labels_by_split") or {}
    label_split_rows = "\n".join(
        f"| {s} | " + " | ".join(str((lbs.get(s) or {}).get(k, 0)) for k in
                                 ("evaluations", "instigated", "pile_in", "seeds", "seeds_evaluated",
                                  "seeds_instigated")) + " |" for s in SPLITS)
    label_ctx_rows = "\n".join(
        f"| {k.replace('|', ' / ')} | {v['evaluations']} | {v['seeds']} | {v['instigated']} | {v['pile_in']} | "
        f"{v['writer_rate']:.3f} |" for k, v in sorted(c["contexts"].items())) or "| (none) | 0 | 0 | 0 | 0 | - |"
    rep = c.get("replication") or {}
    sh = rep.get("share_single_episode")
    spl = manifest.get("split_ledger") or {}
    prm = manifest.get("params") or {}
    return DATASHEET_TEMPLATE.format(
        name=NAME, schema=SCHEMA, version=manifest["version"], content_hash=manifest["content_hash"],
        count_rows=count_rows, split_rows=split_rows, n_instigated=c["labels"]["instigated"],
        n_evals=c["files"].get("evaluations.jsonl", 0), n_pile_in=c["labels"]["pile_in"],
        n_contexts=len(c["contexts"]), n_clusters=src.get("n_near_dup_clusters", 0),
        n_clustered=src.get("n_near_dup_clustered_seeds", 0),
        label_split_rows=label_split_rows, label_ctx_rows=label_ctx_rows,
        n_cells=rep.get("n_cells", 0), n_single=rep.get("single_episode_cells", 0),
        pct_single=f"{sh * 100:.1f}%" if isinstance(sh, (int, float)) else "n/a",
        max_cell=rep.get("max_episodes_per_cell", 0),
        cell_dist=", ".join(f"{k}: {v}" for k, v in (rep.get("episodes_per_cell") or {}).items()) or "none",
        pair_w=prm.get("pair_min_writer_margin", PAIR_MIN_WRITER_MARGIN),
        pair_s=prm.get("pair_min_score_margin", PAIR_MIN_SCORE_MARGIN),
        n_via_missing=src.get("n_seeds_lineage_via_missing", 0),
        n_conflicts=spl.get("n_conflict_groups", 0), n_exceptions=len(spl.get("exceptions") or []),
        agents_note="/".join(str(x) for x in na) if na else "n",
        n_runs_scanned=src.get("n_run_dirs", 0), n_episodes=src.get("n_episodes", 0),
        n_with_sink=src.get("runs_with_content_sink", 0), jaccard=NEAR_DUP_JACCARD,
        git_sha=manifest.get("builder_git_sha") or "unknown")


# --------------------------------------------------------------------------- packaging

def _git_sha() -> str:
    try:
        r = subprocess.run(["git", "-C", str(registry.REPO_DIR), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    s = r.stdout.strip()
    return s if r.returncode == 0 and _GIT_SHA.match(s) else ""


def _jsonl_bytes(rows: list) -> bytes:
    return "".join(_dumps(r) + "\n" for r in rows).encode("utf-8")


def content_hash(files: dict) -> str:
    h = hashlib.sha256()
    for name in HASHED_FILES:
        b = files[name]
        h.update(name.encode() + b"\0" + str(len(b)).encode() + b"\0" + b)
    return h.hexdigest()


def _counts(rows: dict) -> dict:
    files = {f: len(rows[f]) for f in JSONL_FILES}
    splits = {}
    for f in JSONL_FILES:
        d = {s: 0 for s in SPLITS}
        for r in rows[f]:
            d[r["split"]] = d.get(r["split"], 0) + 1
        splits[f] = d
    evs = rows["evaluations.jsonl"]
    contexts = {}
    for e in evs:
        b = contexts.setdefault(_ctx_of(e["context"]), {"evaluations": 0, "with_writers": 0, "pile_in": 0,
                                                        "seeds": set()})
        b["evaluations"] += 1
        b["with_writers"] += 1 if e["label"]["instigated"] else 0
        b["pile_in"] += 1 if e["label"]["pile_in"] else 0
        b["seeds"].add(e["seed_id"])
    ctx = {k: {"evaluations": v["evaluations"], "seeds": len(v["seeds"]), "instigated": v["with_writers"],
               "pile_in": v["pile_in"], "writer_rate": round(v["with_writers"] / v["evaluations"], 6)}
           for k, v in sorted(contexts.items())}
    labels = {"instigated": sum(1 for e in evs if e["label"]["instigated"]),
              "pile_in": sum(1 for e in evs if e["label"]["pile_in"])}
    by_split = {}
    for sp in SPLITS:
        es = [e for e in evs if e["split"] == sp]
        ss = [r for r in rows["seeds.jsonl"] if r["split"] == sp]
        by_split[sp] = {"evaluations": len(es), "instigated": sum(1 for e in es if e["label"]["instigated"]),
                        "pile_in": sum(1 for e in es if e["label"]["pile_in"]),
                        "seeds": len(ss), "seeds_evaluated": sum(1 for r in ss if r["outcomes"]["n_evals"] > 0),
                        "seeds_instigated": sum(1 for r in ss if r["outcomes"]["n_evals_with_writers"] > 0)}
    # replication: episodes per (seed, context) cell
    cells = {}
    for e in evs:
        k = (e["seed_id"], _ctx_of(e["context"]))
        cells[k] = cells.get(k, 0) + 1
    dist = {}
    for n in cells.values():
        dist[str(n)] = dist.get(str(n), 0) + 1
    n_cells = len(cells)
    replication = {"n_cells": n_cells, "episodes_per_cell": dict(sorted(dist.items(), key=lambda kv: int(kv[0]))),
                   "single_episode_cells": dist.get("1", 0),
                   "share_single_episode": round(dist.get("1", 0) / n_cells, 6) if n_cells else None,
                   "max_episodes_per_cell": max(cells.values()) if cells else 0}
    origins = {}
    for s_ in rows["seeds.jsonl"]:
        o = s_["provenance"]["origin"]
        origins[o] = origins.get(o, 0) + 1
    return {"files": files, "splits": splits, "contexts": ctx, "labels": labels, "labels_by_split": by_split,
            "replication": replication, "seed_origins": dict(sorted(origins.items()))}


@contextlib.contextmanager
def _build_lock(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / LOCK_NAME, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _versions(out_dir: Path) -> list:
    try:
        return sorted(d.name for d in out_dir.iterdir() if d.is_dir() and not d.is_symlink()
                      and _VERSION.match(d.name))
    except OSError:
        return []


def latest_version(out_dir=None):
    """The version the latest pointer names (latest.version file, else the symlink), or None."""
    out_dir = Path(out_dir) if out_dir is not None else DATASET_DIR
    try:
        v = (out_dir / LATEST_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        v = ""
    if not _VERSION.match(v):
        try:
            v = os.readlink(out_dir / LATEST_LINK)
        except OSError:
            v = ""
    v = Path(v).name if v else ""
    return v if _VERSION.match(v) and (out_dir / v).is_dir() else None


def package_path(out_dir=None):
    out_dir = Path(out_dir) if out_dir is not None else DATASET_DIR
    v = latest_version(out_dir)
    return (out_dir / v) if v else None


def _point_latest(out_dir: Path, version: str) -> None:
    tmp = out_dir / f".{LATEST_LINK}.{os.getpid()}.tmp"
    with contextlib.suppress(OSError):
        tmp.unlink()
    try:
        os.symlink(version, tmp)
        os.replace(tmp, out_dir / LATEST_LINK)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
    registry._atomic_write(out_dir / LATEST_FILE, version + "\n")


def _write_tar(out_dir: Path, version: str) -> Path:
    """Deterministic <version>.tar.gz (sorted members, mtime 0, uid/gid 0) + .sha256 sidecar."""
    vdir = out_dir / version
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in sorted(p.name for p in vdir.iterdir() if p.is_file()):
                data = (vdir / name).read_bytes()
                info = tarfile.TarInfo(f"{version}/{name}")
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    tgz = out_dir / f"{version}.tar.gz"
    b = buf.getvalue()
    tmp = out_dir / f".{version}.tar.gz.tmp"
    tmp.write_bytes(b)
    os.replace(tmp, tgz)
    registry._atomic_write(out_dir / f"{version}.tar.gz.sha256", f"{_sha256_bytes(b)}  {tgz.name}\n")
    return tgz


def _drop_invalid(rows: dict, schema: dict, skipped: dict) -> None:
    """Never package a row the schema rejects: drop it (fixed per-file code), then drop rows that referred
    to a dropped seed / evaluation so the package stays referentially whole."""
    def keep(f):
        good = []
        for r in rows[f]:
            if validate(r, schema["files"][f]):
                good.append(r)
            else:
                code = "E_ROW_" + f.split(".")[0].upper()
                skipped[code] = skipped.get(code, 0) + 1
        rows[f] = good

    keep("seeds.jsonl")
    sids = {r["seed_id"] for r in rows["seeds.jsonl"]}
    for f in ("evaluations.jsonl", "sft.jsonl"):
        rows[f] = [r for r in rows[f] if r.get("seed_id") in sids]
        keep(f)
    eids = {r["eval_id"] for r in rows["evaluations.jsonl"]}
    rows["responses.jsonl"] = [r for r in rows["responses.jsonl"]
                               if r.get("eval_id") in eids and r.get("seed_id") in sids]
    keep("responses.jsonl")
    rows["pairs.jsonl"] = [r for r in rows["pairs.jsonl"]
                           if r.get("chosen_seed_id") in sids and r.get("rejected_seed_id") in sids]
    keep("pairs.jsonl")


def build(out_dir=None, *, runs_dir=None, registry_path=None, candidates_dir=None, archive_paths=None,
          pilot_path=None, compute=None, now=None, git_sha=None) -> dict:
    """Full deterministic rebuild (given the split ledger). Returns a numbers-only summary:
    {ok, version, new_version, content_hash, counts{file: n}, skipped{code: n}}."""
    out_dir = Path(out_dir) if out_dir is not None else DATASET_DIR
    skipped = {}
    seeds = collect_seeds(registry_path, candidates_dir, pilot_path, skipped)
    lineage_rows = registry.load(registry_path)
    episodes = collect_episodes(runs_dir, compute, skipped)
    eps = [ep for _d, _m, ep in episodes]
    mark_elites(eps, archive_paths)
    responses = {}
    for d, meta, _ep in episodes:
        try:
            rs = load_responses(d, meta)
        except (Exception, SystemExit):  # noqa: BLE001 -- unreadable sink: fixed code only
            rs = None
            skipped["E_SINK"] = skipped.get("E_SINK", 0) + 1
        if rs is not None:
            responses[d.name] = rs

    with _build_lock(out_dir):   # the ledger is read and advanced under the same lock as the release
        ledger = load_ledger(out_dir)
        rows, info = assemble(seeds, episodes, responses, lineage_rows=lineage_rows, ledger=ledger)
        split_info = info.pop("split_ledger")
        schema = build_schema()
        _drop_invalid(rows, schema, skipped)
        files = {f: _jsonl_bytes(rows[f]) for f in JSONL_FILES}
        files["schema.json"] = (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode("utf-8")
        chash = content_hash(files)
        moved = sum(1 for r in rows["seeds.jsonl"]
                    if r["seed_id"] in ledger["seeds"] and ledger["seeds"][r["seed_id"]]["split"] != r["split"])
        if moved:   # cannot happen by construction; never release a package that moves a released seed
            raise DatasetError("E_SPLIT_MOVED")

        existing = None
        cur = latest_version(out_dir)
        for v in ([cur] if cur else []) + [v for v in reversed(_versions(out_dir)) if v != cur]:
            m = _load_json(out_dir / v / "manifest.json")
            if isinstance(m, dict) and m.get("content_hash") == chash:
                existing = v
                break
        if existing is not None:
            if not (out_dir / f"{existing}.tar.gz").is_file():
                _write_tar(out_dir, existing)
            if existing != cur:
                _point_latest(out_dir, existing)
            _write_ledger(out_dir, _ledger_update(ledger, rows["seeds.jsonl"], existing, split_info["exceptions"]))
            return {"ok": True, "version": existing, "new_version": False, "content_hash": chash,
                    "counts": {f: len(rows[f]) for f in JSONL_FILES}, "skipped": dict(sorted(skipped.items()))}

        ts = time.time() if now is None else float(now)
        version = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d") + "-" + chash[:8]
        n_agents_values = sorted({e["context"]["n_agents"] for e in rows["evaluations.jsonl"]
                                  if _isint(e["context"]["n_agents"])})
        manifest = {
            "name": NAME, "schema": SCHEMA, "version": version, "created_ts": round(ts, 3),
            "content_hash": chash, "counts": _counts(rows),
            "source": {"n_run_dirs": _count_run_dirs(runs_dir), "n_episodes": len(episodes),
                       "n_seeds_registry": len(lineage_rows),
                       "skipped": dict(sorted(skipped.items())), "n_agents_values": n_agents_values, **info},
            "split_ledger": dict(split_info, ledger=LEDGER_NAME, moved_vs_ledger=moved,
                                 previous_version=cur),
            "builder_git_sha": _git_sha() if git_sha is None else git_sha,
            "policy": POLICY_NOTE,
            "params": {"split_cuts": list(SPLIT_CUTS), "shingle_k": SHINGLE_K, "minhash_perms": MINHASH_PERMS,
                       "lsh_bands": LSH_BANDS, "near_dup_jaccard": NEAR_DUP_JACCARD,
                       "max_pairs_per_context": MAX_PAIRS_PER_CONTEXT,
                       "pair_min_writer_margin": PAIR_MIN_WRITER_MARGIN,
                       "pair_min_score_margin": PAIR_MIN_SCORE_MARGIN, "pair_equal_n": True,
                       "context_key": "goal|model|channel|n_agents|budget"},
        }
        files["DATASHEET.md"] = render_datasheet(manifest).encode("utf-8")
        manifest["sha256"] = {f: _sha256_bytes(files[f]) for f in sorted(files)}
        files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

        tmp = Path(tempfile.mkdtemp(prefix=".build-", dir=str(out_dir)))
        try:
            for name, b in files.items():
                (tmp / name).write_bytes(b)
            for name in files:
                os.chmod(tmp / name, 0o444)
            _set_aside(out_dir, version)   # a stale / corrupt dir of the same name (no matching manifest)
            os.replace(tmp, out_dir / version)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        _write_tar(out_dir, version)
        _point_latest(out_dir, version)
        _write_ledger(out_dir, _ledger_update(ledger, rows["seeds.jsonl"], version, split_info["exceptions"]))
    return {"ok": True, "version": version, "new_version": True, "content_hash": chash,
            "counts": {f: len(rows[f]) for f in JSONL_FILES}, "skipped": dict(sorted(skipped.items()))}


def _set_aside(out_dir: Path, version: str) -> None:
    """Move an existing ``out_dir/version`` (its manifest did not match this content) to a dot-named
    ``.corrupt-<version>-<pid>-<n>`` dir so the fresh build can be installed under the version name."""
    dst = out_dir / version
    if not (dst.exists() or dst.is_symlink()):
        return
    n = 0
    while True:
        aside = out_dir / f".corrupt-{version}-{os.getpid()}-{n}"
        if not (aside.exists() or aside.is_symlink()):
            break
        n += 1
    os.replace(dst, aside)


def _write_ledger(out_dir: Path, ledger: dict) -> None:
    registry._atomic_write(out_dir / LEDGER_NAME, json.dumps(ledger, indent=1, sort_keys=True) + "\n")


def _count_run_dirs(runs_dir) -> int:
    import config
    if runs_dir is False:
        return 0
    p = Path(runs_dir) if runs_dir is not None else config.RUNS
    try:
        return sum(1 for d in p.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")))
    except OSError:
        return 0


def collect(**kw) -> dict:
    """Best-effort build for continuous collection (QD / explorer hooks): never raises; on failure returns
    {"ok": False, "error": <fixed code>}."""
    try:
        return build(**kw)
    except DatasetError as e:
        return {"ok": False, "error": _code(e, "E_DATASET")}
    except (Exception, SystemExit):  # noqa: BLE001 -- fixed code only
        return {"ok": False, "error": "E_DATASET"}


# --------------------------------------------------------------------------- verify

def verify(out_dir=None, version=None) -> dict:
    """Schema / manifest / sha256 / referential checks of one version (default latest). Numbers only:
    {ok, version, counts{file: n}, n_errors, errors:[{code, file, line}]} (errors capped)."""
    out_dir = Path(out_dir) if out_dir is not None else DATASET_DIR
    v = version or latest_version(out_dir)
    if not v or not _VERSION.match(v) or not (out_dir / v).is_dir():
        raise DatasetError("E_NO_PACKAGE")
    vdir = out_dir / v
    errors = []

    def err(code, file="", line=0):
        errors.append({"code": code, "file": file, "line": int(line)})

    manifest = _load_json(vdir / "manifest.json")
    if not isinstance(manifest, dict):
        raise DatasetError("E_MANIFEST")
    if manifest.get("version") != v or manifest.get("schema") != SCHEMA:
        err("E_MANIFEST", "manifest.json")
    shas = manifest.get("sha256") if isinstance(manifest.get("sha256"), dict) else {}
    for f in PACKAGE_FILES:
        p = vdir / f
        if not p.is_file():
            err("E_MISSING", f)
            continue
        if f != "manifest.json" and shas.get(f) != _sha256_file(p):
            err("E_SHA256", f)
    schema = _load_json(vdir / "schema.json")
    fschemas = schema.get("files") if isinstance(schema, dict) and isinstance(schema.get("files"), dict) else None
    if fschemas is None:
        err("E_SCHEMA_FILE", "schema.json")
        fschemas = {}
    counts = {}
    ids = {f: {} for f in JSONL_FILES}
    for f in JSONL_FILES:
        n = 0
        sch = fschemas.get(f)
        try:
            fh = open(vdir / f, "rb")   # bytes: a non-UTF-8 line is an E_PARSE row, not an exception
        except OSError:
            counts[f] = 0
            continue
        with fh:
            for ln, raw in enumerate(fh, 1):
                n += 1
                try:
                    row = json.loads(raw.decode("utf-8"))   # UnicodeDecodeError is a ValueError
                except ValueError:
                    err("E_PARSE", f, ln)
                    continue
                if sch is None or not validate(row, sch):
                    err("E_ROW_SCHEMA", f, ln)
                    continue
                ids[f][ln] = row
        counts[f] = n
    mc = ((manifest.get("counts") or {}).get("files") or {}) if isinstance(manifest.get("counts"), dict) else {}
    for f in JSONL_FILES:
        if mc.get(f) != counts.get(f):
            err("E_COUNT", f)
    # referential checks: ids exist, splits consistent, pairs never cross splits, lineage shares a split
    seeds = {r["seed_id"]: r for r in ids["seeds.jsonl"].values()}
    # every seed of one lineage root shares a split (the root itself may be absent: missing content), except
    # the seeds the manifest lists as kept on an older released split
    spl = manifest.get("split_ledger") if isinstance(manifest.get("split_ledger"), dict) else {}
    exc = set(spl.get("exceptions") or []) if isinstance(spl.get("exceptions"), list) else set()
    root_split = {}
    for r in sorted(ids["seeds.jsonl"].values(), key=lambda r: r["seed_id"]):
        if r["seed_id"] not in exc:
            root_split.setdefault(r["provenance"]["root_id"], r["split"])
    for ln, r in ids["seeds.jsonl"].items():
        want = root_split.get(r["provenance"]["root_id"])
        if r["seed_id"] not in exc and want is not None and want != r["split"]:
            err("E_SPLIT", "seeds.jsonl", ln)
    # released seeds never change split: compare with the previous version (by created_ts)
    prev = _previous_version(out_dir, v, manifest.get("created_ts"))
    if prev is not None:
        old = _ledger_from_package(out_dir / prev, prev)["seeds"]
        for ln, r in ids["seeds.jsonl"].items():
            o = old.get(r["seed_id"])
            if o is not None and o["split"] != r["split"]:
                err("E_SPLIT_MOVED", "seeds.jsonl", ln)
    for f in ("evaluations.jsonl", "responses.jsonl", "sft.jsonl"):
        for ln, r in ids[f].items():
            s = seeds.get(r["seed_id"])
            if s is None:
                err("E_REF", f, ln)
            elif s["split"] != r["split"]:
                err("E_SPLIT", f, ln)
    evals = {r["eval_id"] for r in ids["evaluations.jsonl"].values()}
    for ln, r in ids["responses.jsonl"].items():
        if r["eval_id"] not in evals:
            err("E_REF", "responses.jsonl", ln)
    for ln, r in ids["pairs.jsonl"].items():
        a, b = seeds.get(r["chosen_seed_id"]), seeds.get(r["rejected_seed_id"])
        if a is None or b is None:
            err("E_REF", "pairs.jsonl", ln)
        elif not (a["split"] == b["split"] == r["split"]):
            err("E_PAIR_SPLIT", "pairs.jsonl", ln)
    tgz = out_dir / f"{v}.tar.gz"
    side = out_dir / f"{v}.tar.gz.sha256"
    if not tgz.is_file():
        err("E_TAR", tgz.name)
    else:
        try:
            want = side.read_text(encoding="utf-8").split()[0]
        except (OSError, IndexError):
            want = ""
        if want != _sha256_file(tgz):
            err("E_TAR_SHA256", tgz.name)
    return {"ok": not errors, "version": v, "counts": counts, "n_errors": len(errors),
            "errors": errors[:MAX_VERIFY_ERRORS]}


def _previous_version(out_dir: Path, v: str, created_ts):
    """The version released just before ``v`` (largest manifest created_ts below v's), or None."""
    if not _isnum(created_ts):
        return None
    best = None
    for other in _versions(out_dir):
        if other == v:
            continue
        m = _load_json(out_dir / other / "manifest.json")
        ts = m.get("created_ts") if isinstance(m, dict) else None
        if _isnum(ts) and ts < created_ts and (best is None or (ts, other) > best):
            best = (ts, other)
    return best[1] if best else None


# --------------------------------------------------------------------------- serve (localhost only)

LOOPBACK_NAMES = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}
_STATIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
           ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
           ".jsonl": "application/x-ndjson; charset=utf-8", ".md": "text/plain; charset=utf-8",
           ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _loopback_host(host: str) -> str:
    """The loopback address to bind for ``host``; DatasetError(E_BIND) for anything non-loopback."""
    addr = LOOPBACK_NAMES.get(host)
    if addr is None:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise DatasetError("E_BIND") from None
        if not ip.is_loopback:
            raise DatasetError("E_BIND")
        addr = str(ip)
    return addr


def _handler(package_dir: Path, viewer_dir: Path):
    package_dir = Path(package_dir)
    viewer_dir = Path(viewer_dir) if viewer_dir is not None else None
    data_files = set(PACKAGE_FILES)

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "dataset"
        sys_version = ""

        def log_message(self, *a):  # silent: request lines are never logged
            return

        def _host_ok(self) -> bool:
            h = (self.headers.get("Host") or "").strip().lower()
            if h.startswith("["):
                name = h[1:h.find("]")] if "]" in h else ""
            else:
                name = h.rsplit(":", 1)[0] if ":" in h else h
            return name in LOOPBACK_NAMES

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", head=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _resolve(self):
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path in ("/", "/index.html"):
                return (viewer_dir / "index.html") if viewer_dir is not None else None
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "" and parts[1] == "data" and parts[2] in data_files:
                return package_dir / parts[2]
            if len(parts) == 2 and parts[0] == "" and _STATIC.match(parts[1]) and viewer_dir is not None:
                p = viewer_dir / parts[1]
                try:
                    if p.resolve().parent != viewer_dir.resolve():
                        return None
                except OSError:
                    return None
                return p
            return None

        def _serve(self, head=False):
            if not self._host_ok():
                return self._send(403, b"forbidden\n", head=head)
            if self.path.split("?", 1)[0] == "/favicon.ico":   # no icon: quiet 204, not a console 404
                return self._send(204, head=head)
            p = self._resolve()
            if p is None or not p.is_file():
                return self._send(404, b"not found\n", head=head)
            try:
                body = p.read_bytes()
            except OSError:
                return self._send(404, b"not found\n", head=head)
            self._send(200, body, _CTYPES.get(p.suffix, "application/octet-stream"), head=head)

        def do_GET(self):
            self._serve()

        def do_HEAD(self):
            self._serve(head=True)

        def _refuse(self):
            self._send(405, b"method not allowed\n")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    return H


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # silent: no traceback on stderr
        return


def make_server(out_dir=None, *, host="127.0.0.1", port=0, viewer_dir=None, version=None):
    """A read-only HTTP server on a loopback address serving the viewer at / and the package at /data/<file>."""
    out_dir = Path(out_dir) if out_dir is not None else DATASET_DIR
    addr = _loopback_host(host)
    v = version or latest_version(out_dir)
    if not v or not (out_dir / v).is_dir():
        raise DatasetError("E_NO_PACKAGE")
    vd = Path(viewer_dir) if viewer_dir is not None else VIEWER_DIR
    cls = _Server
    if ":" in addr:
        class _Server6(_Server):
            address_family = socket.AF_INET6
        cls = _Server6
    try:
        srv = cls((addr, int(port)), _handler(out_dir / v, vd if vd.is_dir() else None))
    except OSError:
        raise DatasetError("E_BIND") from None
    if not ipaddress.ip_address(srv.server_address[0]).is_loopback:
        srv.server_close()
        raise DatasetError("E_BIND")
    srv.dataset_version = v
    srv.has_viewer = vd.is_dir() and (vd / "index.html").is_file()
    return srv


# --------------------------------------------------------------------------- CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never print argparse's message (it echoes argv)
        raise DatasetError("E_USAGE")

    def exit(self, status=0, message=None):
        if status:
            raise DatasetError("E_USAGE")
        raise SystemExit(0)


def _parser():
    ap = _Parser(prog="dataset", description="swarmpit-instigation dataset package (numbers out only)")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--out", default=None)
    b.add_argument("--runs", default=None)
    b.add_argument("--registry", default=None)
    b.add_argument("--candidates", default=None)
    b.add_argument("--archive", default=None, help="solvable archive path (the unsolvable sibling follows)")
    b.add_argument("--pilot", default=None)
    v = sub.add_parser("verify")
    v.add_argument("--out", default=None)
    v.add_argument("--version", default=None)
    s = sub.add_parser("serve")
    s.add_argument("--out", default=None)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--viewer", default=None)
    p = sub.add_parser("path")
    p.add_argument("--out", default=None)
    return ap


def _archive_paths(base):
    if base is None:
        return None
    from study import qd
    import config
    return {g: qd.goal_archive_path(g, Path(base)) for g in config.GOALS}


def main(argv=None, out=None) -> int:
    out = out or sys.stdout

    def say(obj):
        out.write(json.dumps(obj, sort_keys=True) + "\n")
        out.flush()

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            a = _parser().parse_args(argv)
        if a.cmd == "build":
            with contextlib.redirect_stdout(sys.stderr):
                res = build(a.out, runs_dir=a.runs, registry_path=a.registry, candidates_dir=a.candidates,
                            archive_paths=_archive_paths(a.archive), pilot_path=a.pilot)
            say(res)
            return 0
        if a.cmd == "verify":
            res = verify(a.out, a.version)
            say(res)
            return 0 if res["ok"] else 1
        if a.cmd == "path":
            p = package_path(a.out)
            if p is None:
                raise DatasetError("E_NO_PACKAGE")
            out.write(str(p) + "\n")
            out.flush()
            return 0
        if a.cmd == "serve":
            srv = make_server(a.out, host=a.host, port=a.port, viewer_dir=a.viewer)
            host, port = srv.server_address[:2]
            shown = f"[{host}]" if ":" in host else host
            say({"ok": True, "url": f"http://{shown}:{port}/", "version": srv.dataset_version,
                 "viewer": bool(srv.has_viewer)})
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                srv.server_close()
            return 0
        raise DatasetError("E_USAGE")
    except SystemExit as e:  # --help
        return 0 if not e.code else 2
    except DatasetError as e:
        say({"ok": False, "error": _code(e, "E_DATASET")})
        return 2 if _code(e, "") in ("E_USAGE", "E_BIND") else 1
    except BaseException:  # noqa: BLE001 -- fixed code only; a traceback could echo content
        say({"ok": False, "error": "E_DATASET"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
