#!/usr/bin/env python3
"""Content-in / numbers-out leak gate for schizboard (PROTOCOL.md section 6).

Builds a blake2b shingle corpus over every known synthetic-chatter source, then scans every
Claude-visible / to-be-published artifact for membership. Any overlap is a leak. The gate reads board
and chatter text only inside this process; it emits numbers, fixed error codes, and tree-relative paths
only -- never matched text, excerpts, field values, or tracebacks. A coding agent may run it and read its
verdict because nothing it prints echoes the content.

    python3 tools/leakgate.py [TREE]                 # whole tree: known corpus globs vs published artifacts
    python3 tools/leakgate.py --root TREE             # same, explicit root
    python3 tools/leakgate.py --corpus DIR_OR_FILE ... --scan DIR_OR_FILE ...   # explicit sets (hermetic)
    python3 tools/leakgate.py ... --json              # (default) print the section 7.4 verdict JSON

Exit status: 0 when ok (no leak, no read/budget failure), 1 otherwise.

Verdict (section 7.4), numbers only:
  {"ok": bool, "scanned_files": int, "corpus_shingles": int, "leak_hits": int,
   "by_file": [{"path": str, "hits": int, "first_offset": int}], "error_code": str|null}

Default-glob mode (no --corpus) first lists the git index of TREE (paths only, no file is opened) and fails
closed with E_TRACKED when a chatter file, run artifact or instigation dataset file (PROTOCOL.md section 9) is
tracked or staged; by_file then carries those tree-relative paths with hits=1, first_offset=-1.

Standalone whole-tree CI invocation:
    python3 tools/leakgate.py --root "$(git rev-parse --show-toplevel)"
  (run alongside tools/check_public.py; a non-zero exit blocks publish -- see site/tools/export.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SHINGLE_N = 8                       # words per overlapping shingle
LONG_TOKEN = 12                     # distinctive single tokens (>= this many chars) also hashed
DIGEST_BYTES = 8                    # blake2b digest width for the corpus set
MAX_FILE_BYTES = 256 * 1024 * 1024  # skip any single source larger than this
CORPUS_BUDGET_BYTES = 2 * 1024 * 1024 * 1024  # stop building the corpus past this (fail with E_BUDGET)

E_READ, E_LEAK, E_BUDGET = "E_READ", "E_LEAK", "E_BUDGET"
# Default-glob mode produced an empty corpus: the root is wrong or the tree holds no chatter sources,
# so membership scanning is vacuous. Fail closed rather than pass every scanned file open.
E_CORPUS = "E_CORPUS"
# Default-glob mode only: a chatter / dataset file is tracked or staged in git (PROTOCOL.md section 9).
# Committing it would publish it with the repository, so this fails closed before any scan.
E_TRACKED = "E_TRACKED"

# Literal rules folded in from redact.py (case-insensitive substring leaks).
LITERALS = ("/users/", ".env")

# Corpus: every known synthetic-chatter / board-text source, relative to the tree root.
CORPUS_GLOBS = (
    "lab/seeds/*.json",
    "**/*.chatter.json",
    "runs/**/board_events.jsonl",
    "runs/**/steps.jsonl",
    "runs/**/relay.jsonl",
    "runs/**/summary.json",
    "runs/**/*.out",
    "results/**/*",
    "lab/web/dist/**/*",
    "site/public/data/runs/**/*",
    "dash/server/tests/fixtures/runs/**/*",
    "data/synthetic-agent-chatter/**/*",   # includes the instigation dataset package (section 9)
)

# Never-commit rules (section 9). The instigation training dataset lives under
# data/synthetic-agent-chatter/dataset/<version>/ (+ a .tar.gz and a `latest` pointer); it is git-ignored and
# part of the corpus above. These rules catch a forced `git add -f`, a copy under another directory, or a
# renamed package. Matching is on tree-relative paths only -- no file is opened.
NEVER_TRACKED_DIRS = ("data/synthetic-agent-chatter/", "runs/")
NEVER_TRACKED_NAMES = frozenset({"seeds.jsonl", "evaluations.jsonl", "responses.jsonl", "pairs.jsonl",
                                 "sft.jsonl", "datasheet.md"})
_NEVER_TRACKED_NAME = re.compile(r".*\.chatter\.json|swarmpit-instigation.*", re.I)
_DATASET_VERSION_DIR = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}(?:\.tar\.gz)?", re.I)

# Scan: Claude-visible / to-be-published numbers-only artifacts that must stay clean.
SCAN_GLOBS = (
    "site/public/data/instigation/*.json",
    "data/instigation/*.json",
    "runs/**/instigation/*.json",
)

_WORD = re.compile(r"[^0-9a-z]+")
# Opaque content-free ids (record_id = 16 hex, run_id tags that embed it, other blake2b/hex digests).
# PROTOCOL.md section 1/5/7 declares these Claude-safe structural metadata: they carry no board text,
# are derived from a candidate's hash (not its words), and legitimately appear in BOTH run artifacts
# (run_id tags) and the numbers-only published artifacts. Excluding them from the long-token feature
# set neutralizes the id collision without relaxing body-shingle detection (shingles still see them in
# context). A token is treated as an opaque id iff it is a pure lowercase-hex run of >= LONG_TOKEN
# chars -- the same class the gate would otherwise hash as a "distinctive long token".
_HEX_ID = re.compile(r"^[0-9a-f]{%d,}$" % LONG_TOKEN)
_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".out", ".md", ".html", ".css", ".js", ".svg", ".xml",
                  ".webmanifest", ".map", ".csv", ".tsv", ""}


class GateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _normalize(text: str) -> list[str]:
    """Casefold, collapse every non-alphanumeric run to a boundary, return the token list."""
    return [t for t in _WORD.split(text.casefold()) if t]


def _digest(s: str) -> bytes:
    return hashlib.blake2b(s.encode("utf-8"), digest_size=DIGEST_BYTES).digest()


def _features(tokens: list[str]):
    """Yield the hashable features of a token stream: long tokens, then overlapping n-gram shingles.

    Each feature is (digest, token_index) so the scan can report a numeric first_offset without text.
    """
    for i, tok in enumerate(tokens):
        if len(tok) >= LONG_TOKEN and not _HEX_ID.match(tok):
            yield _digest(tok), i
    if len(tokens) >= SHINGLE_N:
        for i in range(len(tokens) - SHINGLE_N + 1):
            yield _digest(" ".join(tokens[i:i + SHINGLE_N])), i


def _read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise GateError(E_BUDGET)
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _iter_paths(root: Path, globs) -> list[Path]:
    seen: dict[Path, None] = {}
    for g in globs:
        for p in root.glob(g):
            if p.is_file():
                try:
                    rp = p.resolve()
                except OSError:
                    rp = p
                seen.setdefault(rp, None)
    return list(seen)


def _is_textual(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES


def never_tracked(rel: str) -> bool:
    """True when a tree-relative path must never be committed (chatter, run artifacts, dataset files)."""
    rel = rel.replace("\\", "/")
    if any(rel.startswith(d) for d in NEVER_TRACKED_DIRS):
        return True
    parts = rel.split("/")
    name = parts[-1]
    if name.lower() in NEVER_TRACKED_NAMES or _NEVER_TRACKED_NAME.fullmatch(name):
        return True
    return any(_DATASET_VERSION_DIR.fullmatch(p) for p in parts)


def tracked_violations(root: Path) -> list[str] | None:
    """Tree-relative paths of tracked or staged files that `never_tracked` forbids.

    None when root is not inside a git work tree (nothing there can be committed). Lists paths only;
    never opens a file. Raises GateError(E_READ) if git is present but its listing fails mid-way."""
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--full-name"],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-prefix"],
                             capture_output=True, text=True, timeout=60)
        prefix = top.stdout.strip() if top.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        raise GateError(E_READ)
    out = []
    for raw in res.stdout.split(b"\0"):
        if not raw:
            continue
        full = raw.decode("utf-8", "replace")
        if never_tracked(full):
            out.append(full[len(prefix):] if prefix and full.startswith(prefix) else full)
    return sorted(out)


# Instigation dataset package (PROTOCOL.md section 9): its rows mix copied content with a lot of
# structural metadata (ids, run ids, enum keys, field names, numbers) that legitimately also appears in the
# numbers-only published artifacts. Hashing those rows raw makes every published landscape/explorer file
# "leak". So for package row files only the content-bearing string fields enter the corpus; the
# script-authored metadata files (manifest, schema, datasheet: neutral templates) contribute nothing.
# A row file that does not parse as JSON lines falls back to its raw text (fail closed).
DATASET_ROW_FILES = frozenset({"seeds.jsonl", "responses.jsonl", "sft.jsonl", "evaluations.jsonl",
                               "pairs.jsonl"})
DATASET_META_FILES = frozenset({"manifest.json", "schema.json", "datasheet.md"})
DATASET_CONTENT_KEYS = frozenset({"content", "text", "body", "completion", "completion_text"})
# The builder's split ledger (dataset/split_ledger.json): record ids -> split names only. It contributes
# nothing when it parses as that schema; anything else under the name is hashed raw (fail closed).
DATASET_LEDGER_FILE = "split_ledger.json"
DATASET_LEDGER_SCHEMA = "instigation.split_ledger.v1"


def _is_split_ledger(text: str) -> bool:
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    if not (isinstance(obj, dict) and obj.get("schema") == DATASET_LEDGER_SCHEMA
            and set(obj) == {"schema", "roots", "seeds"}):
        return False
    for part in ("roots", "seeds"):
        m = obj.get(part)
        if not isinstance(m, dict):
            return False
        for k, v in m.items():
            if not (re.fullmatch(r"[0-9a-f]{16}", k) and isinstance(v, dict) and set(v) == {"split", "since"}
                    and v["split"] in ("train", "val", "test") and isinstance(v["since"], str)
                    and _DATASET_VERSION_DIR.fullmatch(v["since"])):
                return False
    return True


def _in_dataset_package(path: Path) -> bool:
    parts = [x.lower() for x in path.parts]
    for i in range(len(parts) - 1):
        if parts[i] == "synthetic-agent-chatter" and parts[i + 1] == "dataset":
            return True
    return False


def _strings(v, out: list) -> None:
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _strings(x, out)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out)


def _dataset_content_text(text: str) -> str | None:
    """The content-bearing strings of a package row file, newline-joined; None if it is not JSON lines."""
    out: list = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        for k, v in row.items():
            if k in DATASET_CONTENT_KEYS:
                _strings(v, out)
    return "\n".join(out)


def build_corpus(sources: list[Path]) -> tuple[set, int]:
    """Hash every feature of every readable source into one set. Raises GateError on failure."""
    corpus: set = set()
    total = 0
    read_failed = False
    for p in sources:
        if not _is_textual(p):
            continue
        try:
            text = _read_text(p)
        except GateError:
            raise
        except OSError:
            read_failed = True
            continue
        total += len(text.encode("utf-8", "replace"))
        if total > CORPUS_BUDGET_BYTES:
            raise GateError(E_BUDGET)
        if _in_dataset_package(p):
            name = p.name.lower()
            if name in DATASET_META_FILES:
                continue
            if name == DATASET_LEDGER_FILE and _is_split_ledger(text):
                continue
            if name in DATASET_ROW_FILES:
                content = _dataset_content_text(text)
                if content is not None:
                    text = content
        for dg, _ in _features(_normalize(text)):
            corpus.add(dg)
    if read_failed and not corpus:
        raise GateError(E_READ)
    return corpus, len(corpus)


def _literal_hit(text: str) -> int | None:
    low = text.casefold()
    best = None
    for lit in LITERALS:
        i = low.find(lit)
        if i >= 0 and (best is None or i < best):
            best = i
    return best


def scan_file(path: Path, corpus: set) -> tuple[int, int | None]:
    """Return (hit_count, first_offset) for one artifact. first_offset is a token index (or char
    offset for a literal hit); None when no offset applies. Never returns matched text."""
    try:
        text = _read_text(path)
    except GateError:
        raise
    except OSError:
        raise GateError(E_READ)
    hits = 0
    first = None
    for dg, idx in _features(_normalize(text)):
        if dg in corpus:
            hits += 1
            if first is None:
                first = idx
    lit = _literal_hit(text)
    if lit is not None:
        hits += 1
        if first is None:
            first = lit
    return hits, first


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return path.name


def run_gate(root: Path, corpus_paths: list[Path] | None = None,
             scan_paths: list[Path] | None = None) -> tuple[dict, int]:
    """Core gate: returns (verdict_dict, exit_code). Catches everything into a fixed error code so no
    traceback or content can escape."""
    try:
        default_corpus = corpus_paths is None
        if default_corpus:
            bad = tracked_violations(root)
            if bad:
                return {"ok": False, "scanned_files": 0, "corpus_shingles": 0, "leak_hits": 0,
                        "by_file": [{"path": b, "hits": 1, "first_offset": -1} for b in bad[:200]],
                        "error_code": E_TRACKED}, 1
        sources = corpus_paths if corpus_paths is not None else _iter_paths(root, CORPUS_GLOBS)
        targets = scan_paths if scan_paths is not None else _iter_paths(root, SCAN_GLOBS)
        # A scanned artifact is never part of the corpus (it must not vacuously match itself).
        target_set = {p.resolve() if hasattr(p, "resolve") else p for p in targets}
        sources = [p for p in sources if (p.resolve() if hasattr(p, "resolve") else p) not in target_set]
        corpus, n_shingles = build_corpus(sources)
        # In default-glob mode an empty corpus means the globs matched no chatter (wrong root / cwd),
        # which would make every scan vacuously pass. Fail closed; hermetic mode (explicit --corpus)
        # may legitimately pass an empty corpus and is left to the caller.
        if default_corpus and n_shingles == 0:
            raise GateError(E_CORPUS)
        by_file = []
        total_hits = 0
        for p in sorted(targets):
            if not _is_textual(p):
                continue
            hits, first = scan_file(p, corpus)
            if hits:
                by_file.append({"path": _rel(p, root), "hits": int(hits),
                                "first_offset": int(first if first is not None else -1)})
                total_hits += hits
        by_file.sort(key=lambda r: (-r["hits"], r["path"]))
        error_code = E_LEAK if total_hits else None
        ok = total_hits == 0 and error_code is None
        verdict = {"ok": ok, "scanned_files": len(targets), "corpus_shingles": n_shingles,
                   "leak_hits": int(total_hits), "by_file": by_file, "error_code": error_code}
        return verdict, (0 if ok else 1)
    except GateError as e:
        return {"ok": False, "scanned_files": 0, "corpus_shingles": 0, "leak_hits": 0, "by_file": [],
                "error_code": e.code}, 1
    except Exception:
        # Fail closed with a fixed code; never surface the exception (it could echo content).
        return {"ok": False, "scanned_files": 0, "corpus_shingles": 0, "leak_hits": 0, "by_file": [],
                "error_code": E_READ}, 1


def _collect_cli(items: list[str]) -> list[Path]:
    out: list[Path] = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            out.extend(q for q in p.rglob("*") if q.is_file())
        elif p.is_file():
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="content-in / numbers-out leak gate")
    ap.add_argument("tree", nargs="?", default=None, help="tree root (default: cwd or --root)")
    ap.add_argument("--root", default=None, help="tree root for the default corpus/scan globs")
    ap.add_argument("--corpus", action="append", default=[], help="explicit corpus dir/file (repeatable)")
    ap.add_argument("--scan", action="append", default=[], help="explicit scan dir/file (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the verdict JSON (default)")
    a = ap.parse_args(argv)

    root = Path(a.root or a.tree or ".").resolve()
    corpus_paths = _collect_cli(a.corpus) if a.corpus else None
    scan_paths = _collect_cli(a.scan) if a.scan else None
    verdict, code = run_gate(root, corpus_paths, scan_paths)
    sys.stdout.write(json.dumps(verdict) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
