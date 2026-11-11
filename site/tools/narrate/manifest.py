"""swarmpit.narration/1 manifests and the narration index (NARRATION.md 2, 10).

`validate` / `validate_index` / `check_tree` are shared by the pipeline (before writing), the `check` CLI and
tools/check_public.py. They return fixed error codes with numeric positions only; never field values.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import config
from . import timemap as TM
from . import vocab
from .sfx import SPEC as SFX_SPEC

SCHEMA = "swarmpit.narration/1"
INDEX_SCHEMA = "swarmpit.narration-index/1"
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
HASH8 = re.compile(r"[0-9a-f]{8}")
MP3_NAME = re.compile(r"([A-Za-z0-9._-]{1,80})\.([0-9a-f]{8})\.mp3")
GENERATOR = re.compile(r"narrate/\d+\.\d+\.\d+")
MAX_CUES = 80
MAX_MANIFEST_BYTES = 64 * 1024
# Sized for every run in the index at mix.OUT_BITRATE_KBPS (24 kbps = 3 KB/s): the longest runs (~6 min of audio)
# are ~1.1 MB, all ~335 runs ~110 MiB (NARRATION.md 6). Keep in sync with tools/check_public.py BUDGET.
MAX_MP3_BYTES = 2 * 1024 * 1024
MAX_AUDIO_TOTAL = 120 * 1024 * 1024
KEYS = {"schema", "generator", "runId", "runHash", "audio", "audioBytes", "durationMs", "runEndMs", "timeMap",
        "cues", "sfx"}
CUE_KEYS = {"audioMs", "endMs", "runMs", "voice", "tpl", "text"}
VOICES = ("guide", "expert")


def _nat(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def audio_url(run_id: str, h8: str) -> str:
    return f"../../audio/narration/{run_id}.{h8}.mp3"


def validate(m) -> list[str]:
    """Contract invariants of NARRATION.md 2.1. Returns [] when valid."""
    if not isinstance(m, dict):
        return ["E_MANIFEST_TYPE"]
    errs: list[str] = []
    if set(m) != KEYS:
        errs.append("E_KEYS")
    if m.get("schema") != SCHEMA:
        return errs + ["E_SCHEMA"]
    if not (isinstance(m.get("generator"), str) and GENERATOR.fullmatch(m["generator"])):
        errs.append("E_GENERATOR")
    rid = m.get("runId")
    if not (isinstance(rid, str) and RUN_ID.fullmatch(rid)):
        return errs + ["E_RUN_ID"]
    if not (isinstance(m.get("runHash"), str) and HASH8.fullmatch(m["runHash"])):
        errs.append("E_RUN_HASH")
    audio = m.get("audio")
    if not (isinstance(audio, str) and re.fullmatch(r"\.\./\.\./audio/narration/" + re.escape(rid)
                                                    + r"\.[0-9a-f]{8}\.mp3", audio)):
        errs.append("E_AUDIO_URL")
    for k in ("audioBytes", "durationMs", "runEndMs"):
        if not _nat(m.get(k)):
            errs.append(f"E_{k.upper()}")
    if errs:
        return errs
    dur, rend = m["durationMs"], m["runEndMs"]
    if dur <= 0 or rend <= 0:
        return ["E_DURATION"]
    pts = m.get("timeMap")
    terrs = TM.check(pts, dur, rend)
    errs += terrs
    cues = m.get("cues")
    if not isinstance(cues, list) or len(cues) > MAX_CUES:
        return errs + ["E_CUES"]
    prev_end = 0
    for i, c in enumerate(cues):
        if not isinstance(c, dict) or set(c) != CUE_KEYS:
            errs.append(f"E_CUE_KEYS cue={i}")
            continue
        a, e, r = c["audioMs"], c["endMs"], c["runMs"]
        if not (_nat(a) and _nat(e) and _nat(r)) or not a < e <= dur:
            errs.append(f"E_CUE_TIME cue={i}")
            continue
        if a < prev_end:
            errs.append(f"E_CUE_OVERLAP cue={i}")
        prev_end = e
        if not terrs and abs(TM.a2r(pts, a) - r) > 1:
            errs.append(f"E_CUE_RUNMS cue={i}")
        if c["voice"] not in VOICES:
            errs.append(f"E_CUE_VOICE cue={i}")
            continue
        try:
            vocab.validate_cue(c["tpl"], c["text"], index=i, voice=c["voice"])
        except vocab.VocabError as ve:
            errs.append(f"{ve.code} cue={i}")
    s = m.get("sfx")
    if not isinstance(s, dict) or set(s) != {"counts", "dropped"} or not _nat(s.get("dropped")) \
            or not isinstance(s.get("counts"), dict):
        errs.append("E_SFX")
    else:
        for k, v in s["counts"].items():
            if k not in SFX_SPEC or not _nat(v):
                errs.append("E_SFX_COUNTS")
                break
    return errs


def validate_index(ix) -> list[str]:
    if not isinstance(ix, dict) or set(ix) != {"schema", "runs"} or ix.get("schema") != INDEX_SCHEMA \
            or not isinstance(ix.get("runs"), dict):
        return ["E_NINDEX_SCHEMA"]
    errs = []
    for n, (rid, ent) in enumerate(sorted(ix["runs"].items())):
        if not (isinstance(rid, str) and RUN_ID.fullmatch(rid)):
            errs.append(f"E_NINDEX_ID entry={n}")
            continue
        if (not isinstance(ent, dict) or set(ent) != {"hash", "manifest", "durationMs"}
                or not (isinstance(ent["hash"], str) and HASH8.fullmatch(ent["hash"]))
                or ent["manifest"] != f"{rid}.json" or not _nat(ent["durationMs"])):
            errs.append(f"E_NINDEX_ENTRY entry={n}")
    return errs


# --------------------------------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------------------------------

def _dump(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class ManifestError(Exception):
    def __init__(self, codes: list[str]):
        self.codes = codes
        super().__init__("manifest invalid: " + ",".join(codes[:10]))


def narration_dirs(public: Path) -> tuple[Path, Path]:
    return Path(public) / "data" / "narration", Path(public) / "audio" / "narration"


def write_manifest(public: Path, m: dict) -> Path:
    errs = validate(m)
    if errs:
        raise ManifestError(errs)
    data = _dump(m)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(["E_MANIFEST_SIZE"])
    p = narration_dirs(public)[0] / f"{m['runId']}.json"
    config.write_atomic(p, data)
    return p


def load_index(public: Path) -> dict:
    p = narration_dirs(public)[0] / "index.json"
    try:
        with open(p, "rb") as fh:
            ix = json.loads(fh.read())
    except (OSError, ValueError):
        return {"schema": INDEX_SCHEMA, "runs": {}}
    return ix if not validate_index(ix) else {"schema": INDEX_SCHEMA, "runs": {}}


def update_index(public: Path, m: dict | None, known_ids: set[str]) -> dict:
    """Merge one manifest into the index and drop entries for runs no longer in data/index.json."""
    ix = load_index(public)
    runs = {k: v for k, v in ix["runs"].items() if k in known_ids}
    if m is not None:
        runs[m["runId"]] = {"hash": m["runHash"], "manifest": f"{m['runId']}.json", "durationMs": m["durationMs"]}
    ix = {"schema": INDEX_SCHEMA, "runs": dict(sorted(runs.items()))}
    errs = validate_index(ix)
    if errs:
        raise ManifestError(errs)
    config.write_atomic(narration_dirs(public)[0] / "index.json", _dump(ix))
    return ix


def read_manifest(public: Path, run_id: str, strict: bool = True) -> dict | None:
    """A committed manifest if it exists (and, if strict, validates), else None. Used by `batch`."""
    if not RUN_ID.fullmatch(run_id or ""):
        return None
    try:
        with open(narration_dirs(public)[0] / f"{run_id}.json", "rb") as fh:
            m = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    return m if not (strict and validate(m)) else None


def prune(public: Path, known_ids: set[str]) -> tuple[int, int]:
    """Delete manifests of runs no longer in data/index.json and mp3s no manifest references. Only files with the
    contract's name patterns inside the two narration dirs are touched. Returns (manifests, mp3s) removed."""
    ddir, adir = narration_dirs(public)
    gone_m = gone_a = 0
    keep: set[str] = set()
    for p in sorted(ddir.glob("*.json")) if ddir.is_dir() else []:
        if p.name == "index.json" or not RUN_ID.fullmatch(p.stem):
            continue
        if p.stem not in known_ids:
            p.unlink()
            gone_m += 1
            continue
        m = read_manifest(public, p.stem, strict=False)   # an outdated manifest still owns its mp3
        if m is not None and isinstance(m.get("audio"), str):
            keep.add(m["audio"].rsplit("/", 1)[-1])
    for p in sorted(adir.glob("*.mp3")) if adir.is_dir() else []:
        if MP3_NAME.fullmatch(p.name) and p.name not in keep:
            p.unlink()
            gone_a += 1
    return gone_m, gone_a


# --------------------------------------------------------------------------------------------------
# Whole-tree check (CLI `check` and the public gate)
# --------------------------------------------------------------------------------------------------

def mp3_problems(p: Path) -> list[str]:
    errs = []
    m = MP3_NAME.fullmatch(p.name)
    data = p.read_bytes()
    if not m:
        errs.append("E_MP3_NAME")
    elif hashlib.sha256(data).hexdigest()[:8] != m.group(2):
        errs.append("E_MP3_HASH")
    if data[:3] == b"ID3":
        errs.append("E_MP3_ID3")
    elif len(data) < 2 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        errs.append("E_MP3_SYNC")
    if len(data) > MAX_MP3_BYTES:
        errs.append("E_MP3_SIZE")
    return errs


def check_tree(public: Path, cards: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Validate public/data/narration + public/audio/narration. `cards` = {runId: hash} from data/index.json.
    Returns (problems, warnings) as 'relpath: CODE' strings."""
    public = Path(public)
    ddir, adir = narration_dirs(public)
    problems: list[str] = []
    warnings: list[str] = []

    def rel(p: Path) -> str:
        return str(p.relative_to(public))

    mp3s = sorted(adir.glob("*")) if adir.is_dir() else []
    total = 0
    for p in mp3s:
        if not p.is_file():
            continue
        total += p.stat().st_size
        if p.suffix != ".mp3":
            problems.append(f"{rel(p)}: E_AUDIO_EXT")
            continue
        problems += [f"{rel(p)}: {c}" for c in mp3_problems(p)]
    if total > MAX_AUDIO_TOTAL:
        problems.append("audio/narration: E_AUDIO_TOTAL")
    if not ddir.is_dir():
        if mp3s:
            problems.append("audio/narration: E_ORPHAN_MP3")
        return problems, warnings

    ix_p = ddir / "index.json"
    index_runs: dict = {}
    if ix_p.is_file():
        try:
            ix = json.loads(ix_p.read_bytes())
        except ValueError:
            ix = None
        errs = validate_index(ix)
        problems += [f"{rel(ix_p)}: {c}" for c in errs]
        if not errs:
            index_runs = ix["runs"]
    else:
        problems.append(f"{rel(ix_p)}: E_NINDEX_MISSING")
    referenced: dict[str, int] = {}
    for p in sorted(ddir.glob("*")):
        if p.name == "index.json" or not p.is_file():
            continue
        if p.suffix != ".json":
            problems.append(f"{rel(p)}: E_NARRATION_EXT")
            continue
        if p.stat().st_size > MAX_MANIFEST_BYTES:
            problems.append(f"{rel(p)}: E_MANIFEST_SIZE")
        try:
            m = json.loads(p.read_bytes())
        except ValueError:
            problems.append(f"{rel(p)}: E_JSON")
            continue
        errs = validate(m)
        if errs:
            problems += [f"{rel(p)}: {c}" for c in errs]
            continue
        rid = m["runId"]
        if p.name != f"{rid}.json":
            problems.append(f"{rel(p)}: E_MANIFEST_NAME")
        ent = index_runs.get(rid)
        if ent is None:
            problems.append(f"{rel(p)}: E_NOT_IN_INDEX")
        elif ent["hash"] != m["runHash"] or ent["durationMs"] != m["durationMs"]:
            problems.append(f"{rel(p)}: E_INDEX_MISMATCH")
        mp3 = (p.parent / m["audio"]).resolve()
        name = mp3.name
        referenced[name] = referenced.get(name, 0) + 1
        if not mp3.is_file():
            problems.append(f"{rel(p)}: E_MP3_MISSING")
        elif mp3.stat().st_size != m["audioBytes"]:
            problems.append(f"{rel(p)}: E_MP3_BYTES")
        if cards is not None:
            if rid not in cards:
                problems.append(f"{rel(p)}: E_RUN_UNKNOWN")
            elif cards[rid] != m["runHash"]:
                warnings.append(f"{rel(p)}: W_STALE_HASH")
    for rid in index_runs:
        if not (ddir / f"{rid}.json").is_file():
            problems.append(f"{rel(ix_p)}: E_INDEX_DANGLING")
    for p in mp3s:
        if p.suffix == ".mp3" and referenced.get(p.name, 0) != 1:
            problems.append(f"{rel(p)}: E_ORPHAN_MP3" if referenced.get(p.name, 0) == 0 else f"{rel(p)}: E_MP3_SHARED")
    return problems, warnings
