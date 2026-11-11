#!/usr/bin/env python3
"""Whole-file public gate for swarmpit.dev (SPEC section 4.9). Python 3 stdlib only.

    python3 tools/check_public.py public [--data-only] [--no-budget] [--allow FILE] [--deny WORD ...]
    python3 tools/check_public.py --selftest

Fails (exit 1, listing file:offset rule) on residual leaks (redact.py rules 1-6 in detect mode, literal
'/Users/', 'krister', '.env', denylisted names in path context), size budgets, bare/extensionless import
specifiers in public/js, and external URLs in public/js or public/css. --data-only limits budgets and
scans to public/data (used by export.py so a JS over budget does not block a data export).

Narration track (NARRATION.md section 10): `.mp3` is the only binary type allowed, and only as
audio/narration/<run-id>.<sha8>.mp3 with a stripped header; data/narration/*.json must be valid
swarmpit.narration/1 manifests (strict keys, monotonic time map, cue text fullmatching its template via
tools/narrate/vocab.py) referenced by the narration index. Errors are fixed codes and paths only.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import Redactor, default_denylist  # noqa: E402

KB = 1024
BUDGET = {"core": 400 * KB, "core_synth": 900 * KB, "detail": 600 * KB, "index": 256 * KB, "js": 150 * KB, "css": 32 * KB,
          "mp3": 2 * 1024 * KB, "audio": 120 * 1024 * KB, "narration": 64 * KB}
# Narration audio (NARRATION.md 6): every run in the index is narrated at 24 kbps mono (3 KB/s). ~650 min of audio
# projects to ~111 MiB, the longest run (~6 min) to ~1.1 MB. Kept equal to tools/narrate/manifest.py
# MAX_MP3_BYTES / MAX_AUDIO_TOTAL; test_check_public asserts the two stay in sync.
TEXT_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".xml", ".webmanifest", ".map", ".md", ""}
BINARY_EXT = {".mp3"}
LITERALS = ["/Users/", "krister", ".env"]
# API keys that can end up in binaries (the mp3 byte scan does not run the text rules)
_BIN_KEY = re.compile(rb"sk_[A-Za-z0-9]{32,}|sk-[A-Za-z0-9_-]{16,}")

_IMPORT = re.compile(r"""(?:^|[;\s}])(?:import|export)\s[^'"`;]*?\bfrom\s*(['"])([^'"]+)\1"""
                     r"""|(?:^|[;\s}])import\s*(['"])([^'"]+)\3"""
                     r"""|\bimport\s*\(\s*(['"])([^'"]+)\5\s*\)""", re.M)
_URL = re.compile(r"https?://[^\s'\"`)]+")
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:'\"\\])//[^\n]*", re.S)

# ------------------------------------------------------------------------------------------------
# Instigation training dataset (lab/study/dataset.py, lab/study/PROTOCOL.md section 9). The package
# holds adversarial board content and is NEVER publishable. Any file that looks like part of it -- by
# name, by path segment, by archive magic, by schema marker or by row shape -- hard-fails with a fixed
# code and its path only. A flagged file is not scanned any further, so no redaction fragment of it is
# ever printed.
# ------------------------------------------------------------------------------------------------
DATASET_FILES = {"seeds.jsonl", "evaluations.jsonl", "responses.jsonl", "pairs.jsonl", "sft.jsonl",
                 "datasheet.md"}
DATASET_DIRS = {"synthetic-agent-chatter", "dataset_package", "instigation-dataset"}
# a package version directory: <UTC date>-<8 hex content hash>
_DATASET_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}(?:\.tar\.gz|\.tgz|\.tar)?", re.I)
_DATASET_NAME = re.compile(r".*\.chatter\.json|swarmpit-instigation.*|.*\.jsonl(?:\.gz)?|.*\.(?:tar|tgz|tar\.gz|gz)",
                           re.I)
_DATASET_MARKERS = ("instigation.dataset.", "swarmpit-instigation")
# key pairs that only dataset rows carry (seeds / evaluations / responses / pairs / sft)
_DATASET_SHAPES = (frozenset({"seed_id", "content"}), frozenset({"seed_id", "text"}),
                   frozenset({"seed_id", "outcomes"}), frozenset({"eval_id", "seed_id"}),
                   frozenset({"eval_id", "body"}), frozenset({"chosen_seed_id", "rejected_seed_id"}),
                   frozenset({"prompt", "completion", "weight"}))
_JSONL_PROBE_LINES = 64


def dataset_name_code(rel_parts: tuple[str, ...]) -> str | None:
    """Fixed code when a public path is named like a dataset file or lives under a dataset directory."""
    name = rel_parts[-1] if rel_parts else ""
    if name.lower() in DATASET_FILES or _DATASET_NAME.fullmatch(name):
        return "E_DATASET_FILE"
    for part in rel_parts[:-1]:
        if part.lower() in DATASET_DIRS or _DATASET_VERSION.fullmatch(part):
            return "E_DATASET_FILE"
    if _DATASET_VERSION.fullmatch(name):
        return "E_DATASET_FILE"
    return None


def dataset_magic_code(head: bytes) -> str | None:
    """gzip / tar archives (a packed dataset under any name) are never publishable."""
    if head[:2] == b"\x1f\x8b" or head[257:262] == b"ustar":
        return "E_DATASET_ARCHIVE"
    return None


def _has_dataset_shape(obj, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if isinstance(obj, dict):
        keys = set(obj)
        if any(sig <= keys for sig in _DATASET_SHAPES):
            return True
        return any(_has_dataset_shape(v, depth + 1) for v in obj.values() if isinstance(v, (dict, list)))
    if isinstance(obj, list):
        return any(_has_dataset_shape(v, depth + 1) for v in obj if isinstance(v, (dict, list)))
    return False


def dataset_text_code(text: str, obj=None) -> str | None:
    """Fixed code when text carries a dataset schema marker or dataset-shaped rows (JSON or JSON lines)."""
    low = text.lower()
    if any(m in low for m in _DATASET_MARKERS):
        return "E_DATASET_MARKER"
    if obj is not None and _has_dataset_shape(obj):
        return "E_DATASET_SHAPE"
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        n += 1
        if n > _JSONL_PROBE_LINES:
            break
        try:
            row = json.loads(s)
        except ValueError:
            continue
        if _has_dataset_shape(row):
            return "E_DATASET_SHAPE"
    return None


def _strings_of_json(obj, out: list[str]):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for v in obj:
            _strings_of_json(v, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _strings_of_json(v, out)


def scan_text(text: str, R: Redactor, rel: str, problems: list[str], literal_ci=True):
    for lit in LITERALS:
        hay = text.lower() if literal_ci else text
        i = hay.find(lit.lower() if literal_ci else lit)
        if i >= 0:
            problems.append(f"{rel}:{i}: literal {lit!r}")
    is_code = rel.endswith((".js", ".mjs", ".css"))
    for off, rule, frag in R.detect(text):
        # in compiled code, `obj.home` / `x.local` property access is not a hostname (data is still checked)
        if is_code and rule == "host":
            continue
        # data files may carry run payload text (CLAUDE.md chatter policy): report path + rule only
        problems.append(f"{rel}:{off}: {rule}" if rel.startswith("data/") else f"{rel}:{off}: {rule}: {frag[:60]!r}")


# ------------------------------------------------------------------------------------------------
# Narration track (NARRATION.md 2 and 10). Validators return fixed error codes only, never content.
# ------------------------------------------------------------------------------------------------
NARR_SCHEMA = "swarmpit.narration/1"
NARR_INDEX_SCHEMA = "swarmpit.narration-index/1"
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
_MP3_NAME = re.compile(r"([A-Za-z0-9._-]{1,80})\.([0-9a-f]{8})\.mp3")
_CODE = re.compile(r"E_[A-Z0-9_]{1,40}")
_NARR_KEYS = {"schema", "generator", "runId", "runHash", "audio", "audioBytes", "durationMs", "runEndMs",
              "timeMap", "cues", "sfx"}
_CUE_KEYS = {"audioMs", "endMs", "runMs", "voice", "tpl", "text"}
_VOICES = {"guide", "expert"}
MAX_POINTS, MAX_CUES, MAX_TEXT, MAX_SLOPE = 600, 80, 240, 8


def _u(v) -> bool:
    return type(v) is int and v >= 0


def a2r(tm: list, a: float) -> float:
    """Audio ms -> run ms (linear, clamped); same as src/player/timemap.ts a2r."""
    if not tm or a <= tm[0][0]:
        return tm[0][1] if tm else 0
    if a >= tm[-1][0]:
        return tm[-1][1]
    lo, hi = 0, len(tm) - 1
    while lo < hi:
        md = (lo + hi + 1) // 2
        if tm[md][0] <= a:
            lo = md
        else:
            hi = md - 1
    (a0, r0), (a1, r1) = tm[lo], tm[lo + 1]
    return r0 + (r1 - r0) * (a - a0) / (a1 - a0)


def sfx_ids() -> set[str] | None:
    """Library ids from the committed tools/narrate/voices.json (None if unavailable: any [a-z]+ id is accepted)."""
    try:
        v = json.loads((Path(__file__).resolve().parent / "narrate" / "voices.json").read_text(encoding="utf-8"))
        ids = set(v["sfx"]["library"].keys())
        return ids if all(isinstance(k, str) for k in ids) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def load_cue_validator():
    """tools/narrate/vocab.validate_cue(tpl, text, index, voice) -> raises VocabError(code). None if unavailable."""
    try:
        from narrate import vocab  # noqa: PLC0415 - optional, stdlib-only package owned by the pipeline
        return vocab.validate_cue
    except Exception:  # noqa: BLE001
        return None


def validate_narration(obj, cue_validator=None, sfx_lib: set[str] | None = None) -> list[str]:
    """swarmpit.narration/1 invariants (NARRATION.md 2.1 #1-7). Returns fixed codes (with cue indexes only)."""
    E: list[str] = []
    if not isinstance(obj, dict):
        return ["E_NOT_OBJECT"]
    if set(obj) != _NARR_KEYS:
        E.append("E_KEYS")
    if obj.get("schema") != NARR_SCHEMA:
        E.append("E_SCHEMA")
    rid = obj.get("runId")
    if not isinstance(rid, str) or not _RUN_ID.fullmatch(rid):
        return E + ["E_RUN_ID"]
    if not isinstance(obj.get("generator"), str) or not isinstance(obj.get("runHash"), str) \
            or not re.fullmatch(r"[0-9a-f]{8}", obj.get("runHash") or ""):
        E.append("E_HEADER")
    audio = obj.get("audio")
    if not isinstance(audio, str) or not re.fullmatch(r"\.\./\.\./audio/narration/" + re.escape(rid) + r"\.[0-9a-f]{8}\.mp3", audio):
        E.append("E_AUDIO_URL")
    dur, rend = obj.get("durationMs"), obj.get("runEndMs")
    if not (_u(obj.get("audioBytes")) and _u(dur) and _u(rend) and dur >= 1):
        return E + ["E_SIZES"]
    tm = obj.get("timeMap")
    if not isinstance(tm, list) or not (2 <= len(tm) <= MAX_POINTS) \
            or not all(isinstance(p, list) and len(p) == 2 and _u(p[0]) and _u(p[1]) for p in tm):
        return E + ["E_MAP_SHAPE"]
    if tm[0] != [0, 0] or tm[-1] != [dur, rend]:
        E.append("E_MAP_ENDS")
    for (a0, r0), (a1, r1) in zip(tm, tm[1:]):
        if a1 <= a0 or r1 < r0:
            E.append("E_MAP_MONOTONIC")
            break
        if r1 - r0 > MAX_SLOPE * (a1 - a0):
            E.append("E_MAP_SLOPE")
            break
    map_ok = not any(c.startswith("E_MAP") for c in E)
    cues = obj.get("cues")
    if not isinstance(cues, list) or len(cues) > MAX_CUES:
        return E + ["E_CUES"]
    prev_end = 0
    for i, c in enumerate(cues):
        if not isinstance(c, dict) or set(c) != _CUE_KEYS:
            E.append(f"E_CUE_KEYS cue={i}")
            continue
        a, e, r = c["audioMs"], c["endMs"], c["runMs"]
        if not (_u(a) and _u(e) and _u(r)) or not (a < e <= dur):
            E.append(f"E_CUE_RANGE cue={i}")
            continue
        if a < prev_end:
            E.append(f"E_CUE_OVERLAP cue={i}")
        prev_end = e
        if map_ok and abs(a2r(tm, a) - r) > 1:
            E.append(f"E_CUE_ANCHOR cue={i}")
        if not isinstance(c["voice"], str) or c["voice"] not in _VOICES:
            E.append(f"E_VOICE cue={i}")
        text, tpl = c["text"], c["tpl"]
        if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT) or not isinstance(tpl, str):
            E.append(f"E_TEXT_LEN cue={i}")
            continue
        if cue_validator is None:
            E.append(f"E_VALIDATOR_UNAVAILABLE cue={i}")
            continue
        try:
            cue_validator(tpl, text, i, c["voice"] if isinstance(c["voice"], str) and c["voice"] in _VOICES else None)
        except Exception as ex:  # noqa: BLE001 - report the fixed code only, never the message
            code = getattr(ex, "code", None)
            E.append(f"{code if isinstance(code, str) and _CODE.fullmatch(code) else 'E_INVALID'} cue={i}")
    sfx = obj.get("sfx")
    if not isinstance(sfx, dict) or set(sfx) != {"counts", "dropped"} or not _u(sfx.get("dropped")) \
            or not isinstance(sfx.get("counts"), dict):
        E.append("E_SFX")
    else:
        for k, v in sfx["counts"].items():
            if not re.fullmatch(r"[a-z]{1,16}", k) or (sfx_lib is not None and k not in sfx_lib) or not _u(v):
                E.append("E_SFX_COUNTS")
                break
    return E


def validate_narration_index(obj) -> list[str]:
    if not isinstance(obj, dict) or set(obj) != {"schema", "runs"} or obj.get("schema") != NARR_INDEX_SCHEMA \
            or not isinstance(obj.get("runs"), dict):
        return ["E_INDEX_SHAPE"]
    for rid, e in obj["runs"].items():
        if not _RUN_ID.fullmatch(rid) or not isinstance(e, dict) or set(e) != {"hash", "manifest", "durationMs"} \
                or not isinstance(e["hash"], str) or e["manifest"] != f"{rid}.json" or not _u(e["durationMs"]):
            return ["E_INDEX_ENTRY"]
    return []


def _scan_words(hay: str, words: list[str]) -> bool:
    return any(w and w.lower() in hay for w in words)


def check_narration(root: Path, files: list[Path], data_only: bool, budgets: bool, deny_words: list[str],
                    problems: list[str], warnings: list[str], cue_validator) -> None:
    ndir, adir = root / "data" / "narration", root / "audio" / "narration"
    manifests = sorted(p for p in files if p.parent == ndir and p.suffix == ".json" and p.name != "index.json")
    mp3s = sorted(p for p in files if p.suffix == ".mp3")
    for p in files:
        if (ndir in p.parents and p.parent != ndir) or (p.parent == ndir and p.suffix != ".json"):
            problems.append(f"{p.relative_to(root)}: unexpected file in data/narration")
    if not manifests and not mp3s and not (ndir / "index.json").exists():
        return
    run_hash: dict[str, str] = {}
    try:
        idx = json.loads((root / "data" / "index.json").read_text(encoding="utf-8"))
        run_hash = {r["id"]: r.get("hash") for r in idx.get("runs", []) if isinstance(r, dict) and isinstance(r.get("id"), str)}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    nindex = None
    ip = ndir / "index.json"
    if manifests and not ip.exists():
        problems.append("data/narration/index.json: missing narration index")
    if ip.exists():
        try:
            nindex = json.loads(ip.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            nindex = None
        codes = validate_narration_index(nindex) if nindex is not None else ["E_JSON"]
        for c in codes:
            problems.append(f"data/narration/index.json: {c}")
        if codes:
            nindex = None
    indexed = nindex["runs"] if nindex else {}
    sfx_lib = sfx_ids()
    referenced: dict[str, int] = {}
    for p in manifests:
        rel = str(p.relative_to(root))
        size = p.stat().st_size
        if size > BUDGET["narration"]:
            problems.append(f"{rel}: manifest {size} B > {BUDGET['narration']}")
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # reported by the generic JSON pass
        codes = validate_narration(obj, cue_validator, sfx_lib)
        for c in codes:
            problems.append(f"{rel}: {c}")
        if not isinstance(obj, dict) or not isinstance(obj.get("runId"), str) or not _RUN_ID.fullmatch(obj["runId"]):
            continue
        rid = obj["runId"]
        if p.name != f"{rid}.json":
            problems.append(f"{rel}: E_NAME")
        if rid not in indexed:
            problems.append(f"{rel}: E_NOT_INDEXED")
        if rid not in run_hash:
            problems.append(f"{rel}: E_UNKNOWN_RUN")
        elif obj.get("runHash") != run_hash[rid] or (rid in indexed and indexed[rid]["hash"] != run_hash[rid]):
            warnings.append(f"{rel}: stale narration (run re-exported; the player ignores it until rebuilt)")
        for i, c in enumerate(obj.get("cues") or []):
            t = c.get("text") if isinstance(c, dict) else None
            if isinstance(t, str) and _scan_words(t.lower(), LITERALS + deny_words):
                problems.append(f"{rel}: E_DENYLIST cue={i}")
        audio = obj.get("audio")
        if isinstance(audio, str) and audio.startswith("../../audio/narration/"):
            name = audio[len("../../audio/narration/"):]
            referenced[name] = referenced.get(name, 0) + 1
            if not data_only:
                ap = adir / name
                if not ap.is_file():
                    problems.append(f"{rel}: E_AUDIO_MISSING")
                elif obj.get("audioBytes") != ap.stat().st_size:
                    problems.append(f"{rel}: E_AUDIO_BYTES")
    for rid, e in indexed.items():
        if not (ndir / e["manifest"]).is_file():
            problems.append(f"data/narration/index.json: E_INDEX_DANGLING {rid}")
    if data_only:
        return
    audio_total = 0
    long_words = [w for w in deny_words if len(w) >= 5]
    for p in mp3s:
        rel = str(p.relative_to(root))
        if p.parent != adir:
            problems.append(f"{rel}: mp3 outside audio/narration")
            continue
        data = p.read_bytes()
        audio_total += len(data)
        m = _MP3_NAME.fullmatch(p.name)
        if not m:
            problems.append(f"{rel}: E_MP3_NAME")
        elif hashlib.sha256(data).hexdigest()[:8] != m.group(2):
            problems.append(f"{rel}: E_MP3_HASH")
        if data[:3] == b"ID3" or data[-128:-125] == b"TAG" or b"APETAGEX" in data:
            problems.append(f"{rel}: E_MP3_TAGS (strip metadata)")
        elif len(data) < 4 or ((data[0] << 8) | data[1]) & 0xFFE0 != 0xFFE0:
            problems.append(f"{rel}: E_MP3_SYNC")
        low = data.lower()
        if any(w.lower().encode() in low for w in LITERALS + long_words if w):
            problems.append(f"{rel}: E_MP3_LITERAL")
        if _BIN_KEY.search(data):
            problems.append(f"{rel}: E_MP3_SECRET")
        # audio budgets are enforced even with --no-budget (which only relaxes the legacy JS/CSS limits)
        if len(data) > BUDGET["mp3"]:
            problems.append(f"{rel}: mp3 {len(data)} B > {BUDGET['mp3']}")
        n = referenced.get(p.name, 0)
        if n != 1:
            problems.append(f"{rel}: E_MP3_{'ORPHAN' if n == 0 else 'SHARED'}")
    if audio_total > BUDGET["audio"]:
        problems.append(f"audio/**: {audio_total} B > {BUDGET['audio']} B budget")


DEFAULT_ALLOW = Path(__file__).resolve().parent.parent / ".cache" / "ctf-allow.json"


def load_allow(path: Path | None) -> list[str]:
    """The exported runs' synthetic CTF values (flag, passphrase, key, vault id, vault seal), written by
    export.py to site/.cache (never public). They are honey data shown on purpose, so the gate allows them
    exactly as the exporter's Redactor does."""
    p = path or DEFAULT_ALLOW
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in v if isinstance(x, str) and len(x) >= 6] if isinstance(v, list) else []


_UNSET = object()


def check(root: Path, data_only=False, deny: list[str] | None = None, allow: list[str] | None = None,
          budgets=True, warnings: list[str] | None = None, cue_validator=_UNSET) -> list[str]:
    root = root.resolve()
    problems: list[str] = []
    warnings = warnings if warnings is not None else []
    deny_words = default_denylist(deny)
    R = Redactor(deny=deny_words, allow=allow or [])
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if data_only:
        files = [p for p in files if (root / "data") in p.parents]
    js_total = css_total = 0
    for p in files:
        rel = str(p.relative_to(root))
        if p.name.endswith(".tmp"):
            problems.append(f"{rel}: stray temp file")
            continue
        size = p.stat().st_size
        if rel.startswith("data/runs/"):
            synth = p.name.startswith("_synth")
            if p.name.endswith(".core.json") and size > (BUDGET["core_synth"] if synth else BUDGET["core"]):
                problems.append(f"{rel}: core bundle {size} B > budget")
            if p.name.endswith(".detail.json") and size > BUDGET["detail"]:
                problems.append(f"{rel}: detail bundle {size} B > budget")
        if rel == "data/index.json" and size > BUDGET["index"]:
            problems.append(f"{rel}: index.json {size} B > {BUDGET['index']}")
        if rel.startswith("js/") and p.suffix == ".js":
            js_total += size
        if rel.startswith("css/"):
            css_total += size
        # dataset guard (section 9) runs before every other content check and stops the file there
        dcode = dataset_name_code(p.relative_to(root).parts)
        if dcode is None:
            try:
                with open(p, "rb") as fh:
                    dcode = dataset_magic_code(fh.read(512))
            except OSError:
                dcode = "E_DATASET_READ"
        if dcode:
            problems.append(f"{rel}: {dcode}")
            continue
        if p.suffix.lower() in BINARY_EXT:
            continue  # mp3: checked by check_narration
        if p.suffix.lower() not in TEXT_EXT:
            problems.append(f"{rel}: unexpected file type")
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not utf-8 text")
            continue
        obj = None
        if p.suffix == ".json":
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
        dcode = dataset_text_code(text, obj)
        if dcode:
            problems.append(f"{rel}: {dcode}")
            continue
        if p.suffix == ".json":
            if obj is None:
                try:
                    json.loads(text)
                except ValueError as e:
                    problems.append(f"{rel}: invalid JSON (line {getattr(e, 'lineno', 0)} col {getattr(e, 'colno', 0)})")
                    continue
            strs: list[str] = []
            _strings_of_json(obj, strs)
            # scan each string separately (offsets are string-relative) plus the raw file for literals
            for lit in LITERALS:
                i = text.lower().find(lit.lower())
                if i >= 0:
                    problems.append(f"{rel}:{i}: literal {lit!r}")
            for s in strs:
                for off, rule, frag in R.detect(s):
                    # JSON (run bundles, index, narration manifests) may carry run payload text: rule only, never
                    # an excerpt (CLAUDE.md chatter policy)
                    problems.append(f"{rel}: {rule}")
        else:
            scan_text(text, R, rel, problems)
        if rel.startswith("js/") and p.suffix == ".js":
            for m in _IMPORT.finditer(text):
                spec = m.group(2) or m.group(4) or m.group(6)
                if not (spec.startswith("./") or spec.startswith("../")) or not re.search(r"\.(m?js|json)$", spec):
                    problems.append(f"{rel}:{m.start()}: bad import specifier {spec!r}")
        if (rel.startswith("js/") and p.suffix == ".js") or rel.startswith("css/"):
            code = _COMMENT.sub("", text)
            for m in _URL.finditer(code):
                problems.append(f"{rel}: external URL {m.group(0)[:80]!r}")
    check_narration(root, files, data_only, budgets, deny_words, problems, warnings,
                    load_cue_validator() if cue_validator is _UNSET else cue_validator)
    if budgets and not data_only:
        if js_total > BUDGET["js"]:
            problems.append(f"js/**: {js_total} B > {BUDGET['js']} B budget")
        if css_total > BUDGET["css"]:
            problems.append(f"css/**: {css_total} B > {BUDGET['css']} B budget")
    return problems


class _StubVocabError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_STUB_TPL = {"G.test": ("guide", re.compile(r"Agent \d{1,3} posts\.")), "E.test": ("expert", re.compile(r"This matters\."))}


def stub_cue_validator(tpl, text, index=None, voice=None):
    """Selftest/unit-test stand-in for tools/narrate/vocab.validate_cue (neutral placeholder templates)."""
    if tpl not in _STUB_TPL:
        raise _StubVocabError("E_TPL_UNKNOWN")
    if voice is not None and voice != _STUB_TPL[tpl][0]:
        raise _StubVocabError("E_VOICE")
    if not _STUB_TPL[tpl][1].fullmatch(text):
        raise _StubVocabError("E_TPL_NOMATCH")


def write_narration_fixture(root: Path, run_id="r1", run_hash="0123abcd", mutate=None) -> dict:
    """A tiny synthetic narrated run under root (placeholder text, fake mp3 frames). Returns the manifest."""
    mp3 = b"\xff\xfb\x90\x64" + bytes(400)
    h8 = hashlib.sha256(mp3).hexdigest()[:8]
    (root / "data" / "narration").mkdir(parents=True, exist_ok=True)
    (root / "audio" / "narration").mkdir(parents=True, exist_ok=True)
    (root / "data" / "index.json").write_text(json.dumps({"runs": [{"id": run_id, "hash": run_hash}]}), encoding="utf-8")
    (root / "audio" / "narration" / f"{run_id}.{h8}.mp3").write_bytes(mp3)
    man = {"schema": NARR_SCHEMA, "generator": "narrate/1.0.0", "runId": run_id, "runHash": run_hash,
           "audio": f"../../audio/narration/{run_id}.{h8}.mp3", "audioBytes": len(mp3), "durationMs": 9000,
           "runEndMs": 6000, "timeMap": [[0, 0], [2000, 0], [8000, 6000], [9000, 6000]],
           "cues": [{"audioMs": 0, "endMs": 1800, "runMs": 0, "voice": "guide", "tpl": "G.test", "text": "Agent 3 posts."},
                    {"audioMs": 8000, "endMs": 8900, "runMs": 6000, "voice": "expert", "tpl": "E.test", "text": "This matters."}],
           "sfx": {"counts": {"post": 2}, "dropped": 1}}
    if mutate:
        mutate(man)
    (root / "data" / "narration" / f"{run_id}.json").write_text(json.dumps(man), encoding="utf-8")
    (root / "data" / "narration" / "index.json").write_text(json.dumps(
        {"schema": NARR_INDEX_SCHEMA, "runs": {run_id: {"hash": run_hash, "manifest": f"{run_id}.json", "durationMs": 9000}}}),
        encoding="utf-8")
    return man


def selftest() -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "js").mkdir()
        (root / "data" / "runs").mkdir(parents=True)

        def expect(name: str, content: str, should_fail: bool, needle: str = ""):
            nonlocal bad
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            probs = check(root, deny=["krister"])
            hit = [x for x in probs if x.startswith(name)]
            if needle:
                hit = [x for x in hit if needle in x]
            ok = bool(hit) == should_fail
            if not ok:
                bad += 1
                print(f"FAIL selftest {name}: expected {'failure' if should_fail else 'pass'}; problems={probs}")
            p.unlink()

        expect("data/a.json", json.dumps({"x": "cat /Users/someone/x"}), True, "/Users/")
        expect("data/b.json", json.dumps({"x": "whoami -> user; user@sandbox"}), False)
        expect("data/c.json", json.dumps({"x": "commit ccb83c0794516276bfc538649a96942f6352855c"}), False)
        expect("data/d.json", json.dumps({"x": "sk-ant-api03-AbCdEf0123456789xyzXYZ"}), True, "api_key")
        expect("data/e.json", json.dumps({"x": "resolver 8.8.8.8"}), True, "ip")
        expect("data/f.json", json.dumps({"x": "board 172.16.0.9 and 127.0.0.2"}), False)
        expect("js/g.js", "import { h } from './dom';\n", True, "bad import")
        expect("js/h.js", "import { h } from './dom.js';\nexport { x } from '../y.js';\n", False)
        expect("js/i.js", "import x from 'lodash';\n", True, "bad import")
        expect("js/j.js", "const u = 'https://cdn.example.com/x.js';\n", True, "external URL")
        expect("data/k.json", json.dumps({"x": "mail alice@example.com"}), True, "email")
        expect("data/l.json", json.dumps({"x": "signed krister"}), True)
        # index budget (256 KB: one card per run across every model lane and goal)
        expect("data/index.json", json.dumps({"x": ["run"] * 20000}), False)
        expect("data/index.json", json.dumps({"x": ["run"] * 40000}), True, "index.json")
        for i, probe in enumerate(("ssh 192.168.1.23", "gw 10.0.0.5", "/var/folders/8x/abcd1234/T/x",
                                   "postgres://admin:hunter2pass@db:5432", "Authorization: Basic dXNlcjpwYXNzd29yZA==",
                                   "LLM_KEY=abc123def456", "key: 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",
                                   "mail first.last@gmail.com", "ELEVEN=sk_" + "0123456789abcdef" * 3,
                                   "sk_" + "fedcba9876543210" * 3)):
            expect(f"data/probe{i}.json", json.dumps({"x": probe}), True)
        expect("js/probe_key.js", "const k = '" + "sk_" + "0123456789abcdef" * 3 + "';\n", True)
        # instigation dataset guard (PROTOCOL.md section 9): placeholder rows only
        row = {"seed_id": "0123456789abcdef", "content": {"board_name": "lorem", "threads": []}}
        expect("data/seeds.jsonl", json.dumps(row) + "\n", True, "E_DATASET_FILE")
        expect("data/x/sft.jsonl", "{}\n", True, "E_DATASET_FILE")
        expect("data/DATASHEET.md", "lorem\n", True, "E_DATASET_FILE")
        expect("data/2026-01-02-0123abcd/manifest.json", "{}", True, "E_DATASET_FILE")
        expect("data/synthetic-agent-chatter/a.json", "{}", True, "E_DATASET_FILE")
        expect("data/lorem.chatter.json", "{}", True, "E_DATASET_FILE")
        expect("data/rows.json", json.dumps([row]), True, "E_DATASET_SHAPE")
        expect("data/rows.txt", json.dumps({"chosen_seed_id": "a", "rejected_seed_id": "b"}) + "\n", True,
               "E_DATASET_SHAPE")
        expect("data/m.json", json.dumps({"schema": "instigation.dataset.v1"}), True, "E_DATASET_MARKER")
        expect("data/o.json", json.dumps({"rows": [{"prompt": "lorem", "completion": "ipsum", "weight": 1.0}]}), True,
               "E_DATASET_SHAPE")
        expect("data/q.json", json.dumps({"prompt": "lorem", "completion": "ipsum"}), False)
        expect("data/n.json", json.dumps({"record_id": "0123456789abcdef", "instigation_score": 0.5}), False)
        p = root / "data" / "lorem.svg"
        p.write_bytes(b"\x1f\x8b\x08\x00" + bytes(64))
        if not any(x.startswith("data/lorem.svg") and "E_DATASET_ARCHIVE" in x for x in check(root, deny=["krister"])):
            bad += 1
            print("FAIL selftest dataset archive magic")
        p.unlink()
    # Narration (NARRATION.md 10.5): one good manifest, a foreign-word cue, a non-monotonic map.
    def foreign(m):
        m["cues"][0]["text"] = "Agent 3 posts elsewhere."

    def nonmono(m):
        m["timeMap"][2] = [1500, 6000]
    for label, mut, needle in (("good", None, ""), ("foreign", foreign, "E_TPL_NOMATCH"), ("nonmono", nonmono, "E_MAP_MONOTONIC")):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_narration_fixture(root, mutate=mut)
            probs = check(root, deny=["krister"], cue_validator=stub_cue_validator)
            hit = [x for x in probs if needle in x] if needle else probs
            if bool(hit) != bool(needle):
                bad += 1
                print(f"FAIL selftest narration {label}: problems={probs}")
    print("check_public selftest:", "OK" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    deny: list[str] = []
    args = []
    allow_path: Path | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--deny" and i + 1 < len(argv):
            deny.append(argv[i + 1])
            i += 2
            continue
        if argv[i] == "--allow" and i + 1 < len(argv):
            allow_path = Path(argv[i + 1])
            i += 2
            continue
        args.append(argv[i])
        i += 1
    data_only = "--data-only" in args
    roots = [a for a in args if not a.startswith("--")]
    root = Path(roots[0] if roots else "public")
    if not root.is_dir():
        print(f"check_public: {root} is not a directory", file=sys.stderr)
        return 1
    warns: list[str] = []
    probs = check(root, data_only=data_only, deny=deny, allow=load_allow(allow_path),
                  budgets="--no-budget" not in args, warnings=warns)
    for w in warns[:50]:
        print("WARN " + w)
    if probs:
        for p in probs[:200]:
            print("FAIL " + p)
        print(f"check_public: {len(probs)} problem(s)")
        return 1
    print(f"check_public: OK ({'data only' if data_only else 'whole tree'}: {root})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
