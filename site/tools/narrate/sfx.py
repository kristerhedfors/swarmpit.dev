"""Event sound effects (NARRATION.md 4): library generation, event -> SFX mapping, deterministic thinning.

`candidates` and `thin` are pure (SafeRun + numbers). `build_library` calls the sound-generation API and
ffmpeg, and writes the committed library under tools/narrate/sfx/ plus its lock file library.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from . import timemap as TM
from .bundle import EF, HIDDEN_KINDS, SafeRun

# id -> (min spacing ms, prio)
SPEC = {
    "segment": (0, 1), "contact": (0, 1), "solve": (1000, 1), "leak": (0, 1), "crash": (1000, 1),
    "outro": (0, 1), "boot": (600, 2), "join": (600, 2), "post": (450, 3), "social": (450, 3),
    "end": (800, 3), "read": (300, 4),
}
GLOBAL_WINDOW_MS = 1000
GLOBAL_MAX_IN_WINDOW = 3
GLOBAL_MIN_GAP_MS = 120
SPEECH_PRE_MS = 150
SPEECH_POST_MS = 100
MAX_CLUSTER_BUMP_DB = 3.0
KEY_LEAD_MIN_MS = 300    # a key effect announced by a cue starts this much (or its clip length) before the voice
KEY_LEAD_MAX_MS = 700
EVENT_SFX = {"start": "boot", "join": "join", "post": "post", "delete": "post", "upvote": "social", "dm": "social",
             "end": "end", "read": "read", "list": "read", "doc": "read", "crash": "crash"}
CHAPTER_SFX = {"firstContact": "contact", "onset": "contact", "solve": "solve", "leak": "leak"}


@dataclass(frozen=True)
class Cand:
    runMs: int
    id: str
    prio: int
    adjDb: float = 0.0
    seq: int = 0


@dataclass(frozen=True)
class Placement:
    audioMs: int
    id: str
    gainDb: float          # per-id library gain + adjustment + cluster bump (bus gain applied in the mix)


def candidates(run: SafeRun) -> list[Cand]:
    ev = run.events
    out: list[Cand] = []
    consumed: set[int] = set()
    solved_agents: set[int] = set()
    for i, sg in enumerate(run.segments):
        if i > 0 and 0 < sg.t0 < run.tEnd:
            out.append(Cand(sg.t0, "segment", 1))
    for ch in run.chapters:
        sid = CHAPTER_SFX.get(ch.k)
        if sid is None or not 0 <= ch.t <= run.tEnd:
            continue
        out.append(Cand(ch.t, sid, SPEC[sid][1]))
        if ch.ev is not None:
            consumed.add(ch.ev)
        if ch.k == "solve" and ch.agent is not None:
            solved_agents.add(ch.agent)
    for i in range(len(ev)):
        k, f = ev.k[i], ev.f[i]
        if i in consumed or k in HIDDEN_KINDS or f & EF.BACKGROUND or not 0 <= ev.t[i] <= run.tEnd:
            continue
        if k == "submit":
            if f & EF.SUBMIT_OK and ev.a[i] >= 0 and ev.a[i] not in solved_agents:
                solved_agents.add(ev.a[i])
                out.append(Cand(ev.t[i], "solve", 1, 0.0, i))
            continue
        sid = EVENT_SFX.get(k)
        if sid is None and f & EF.LIVE_LIVE:
            sid = "social"
        if sid is None:
            continue
        out.append(Cand(ev.t[i], sid, SPEC[sid][1], -3.0 if k == "delete" else 0.0, i))
    out.append(Cand(run.tEnd, "outro", 1, 0.0, len(ev)))
    return out


def key_leads(run: SafeRun, library: dict) -> dict[int, int]:
    """run ms -> audio lead-in (ms) for the times of prio-1 effects (not the outro). A cue anchored there gets a
    run-held lead so the effect is heard before the voice starts and ducks it."""
    out: dict[int, int] = {}
    for c in candidates(run):
        if c.prio != 1 or c.id == "outro" or c.id not in library:
            continue
        ms = int(library[c.id].get("maxMs", KEY_LEAD_MAX_MS))
        out[c.runMs] = max(out.get(c.runMs, 0), max(KEY_LEAD_MIN_MS, min(KEY_LEAD_MAX_MS, ms)))
    return out


def _crowded(ts: list[int]) -> bool:
    """True if some half-open GLOBAL_WINDOW_MS window holds more than GLOBAL_MAX_IN_WINDOW of the sorted times."""
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] >= GLOBAL_WINDOW_MS:
            j += 1
        if i - j + 1 > GLOBAL_MAX_IN_WINDOW:
            return True
    return False


def thin(cands: list[tuple[int, Cand]], cue_spans: list[tuple[int, int]], library: dict) -> tuple[
        list[Placement], dict[str, int], int]:
    """cands: (audioMs, Cand). cue_spans: [(audioMs, endMs)]. Returns (placements, counts, dropped)."""
    order = sorted(cands, key=lambda x: (x[0], x[1].prio, x[1].id, x[1].seq))
    # 2. per-id spacing; the kept instance absorbs the cluster
    kept: list[list] = []          # [audioMs, cand, cluster]
    last: dict[str, list] = {}
    for a, c in order:
        if c.id not in library:
            continue
        spacing = SPEC[c.id][0]
        prev = last.get(c.id)
        if prev is not None and spacing and a - prev[0] < spacing:
            prev[2] += 1
            continue
        item = [a, c, 1]
        kept.append(item)
        last[c.id] = item
    # 3 + 4. global density and speech guard, in (prio, audioMs) order
    spans = [(s - SPEECH_PRE_MS, e + SPEECH_POST_MS) for s, e in cue_spans]

    def span_of(a: int) -> int:
        for j, (s, e) in enumerate(spans):
            if s <= a <= e:
                return j
        return -1

    final: list[list] = []
    times: list[int] = []
    p3_in_cue: dict[int, int] = {}
    for item in sorted(kept, key=lambda x: (x[1].prio, x[0], x[1].id, x[1].seq)):
        a, c, _ = item
        sp = span_of(a)
        if sp >= 0:
            if c.prio >= 4:
                continue
            if c.prio == 3 and p3_in_cue.get(sp, 0) >= 1:
                continue
        near = sorted([t for t in times if abs(t - a) < GLOBAL_WINDOW_MS] + [a])
        if any(abs(t - a) < GLOBAL_MIN_GAP_MS for t in times) or _crowded(near):
            continue
        times.append(a)
        final.append(item)
        if sp >= 0 and c.prio == 3:
            p3_in_cue[sp] = p3_in_cue.get(sp, 0) + 1
    final.sort(key=lambda x: (x[0], x[1].prio, x[1].id, x[1].seq))
    placements, counts = [], {}
    for a, c, cluster in final:
        bump = min(MAX_CLUSTER_BUMP_DB, math.log2(cluster)) if cluster > 1 else 0.0
        gain = float(library[c.id].get("gainDb", 0)) + c.adjDb + bump
        placements.append(Placement(int(a), c.id, round(gain, 2)))
        counts[c.id] = counts.get(c.id, 0) + 1
    total = sum(1 for _, c in cands if c.id in library)
    return placements, counts, total - len(placements)


def place(run: SafeRun, pts, cue_spans, library: dict, lead_at: dict[int, int] | None = None):
    """Map run-time candidates onto the audio timeline (r2a) and thin them. `lead_at`: run ms -> audio ms where
    the lead-in of the cue anchored there begins; key (prio-1) effects at that run time start there."""
    dur = pts[-1][0]
    lead_at = lead_at or {}
    cs = []
    for c in candidates(run):
        a = int(round(TM.r2a(pts, c.runMs)))
        if c.prio == 1 and c.id != "outro" and c.runMs in lead_at:
            a = lead_at[c.runMs]
        elif c.id == "outro":
            # the run is held at tEnd while the closing analysis plays; the outro lands after the last cue
            a = max(a, max((e for _, e in cue_spans), default=0) + 50)
        cs.append((min(a, max(0, dur - 1)), c))
    return thin(cs, cue_spans, library)


# --------------------------------------------------------------------------------------------------
# Library generation (network + ffmpeg)
# --------------------------------------------------------------------------------------------------

def prompt_sha(model: str, prompt: str, seconds: float, influence: float) -> str:
    return hashlib.sha256(f"{model}|{prompt}|{seconds}|{influence}".encode()).hexdigest()


def lock_path() -> Path:
    return config.SFX_DIR / "library.json"


def load_lock() -> dict:
    try:
        with open(lock_path(), "rb") as fh:
            v = json.loads(fh.read())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def library_ready(cfg: dict) -> bool:
    lock = load_lock()
    return all(i in lock and (config.SFX_DIR / f"{i}.mp3").is_file() for i in cfg["sfx"]["library"])


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _ff(cmd: list[str], code: str) -> subprocess.CompletedProcess:
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError(code)    # ffmpeg stderr is discarded (it can echo paths and metadata)
    return p


def max_volume_db(path: Path) -> float:
    p = _ff([config.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null",
             "-"], "E_FFMPEG_VOLDETECT")
    for line in p.stderr.decode("utf-8", "replace").splitlines():
        if "max_volume:" in line:
            try:
                return float(line.split("max_volume:")[1].split("dB")[0])
            except ValueError:
                break
    raise RuntimeError("E_FFMPEG_VOLDETECT")


def duration_ms(path: Path) -> int:
    p = _ff([config.ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            "E_FFPROBE")
    try:
        return int(round(float(p.stdout.decode().strip()) * 1000))
    except ValueError:
        raise RuntimeError("E_FFPROBE") from None


def postprocess(raw: Path, out: Path, max_ms: int, peak_db: float = -6.0) -> None:
    """mono 44.1k, trim leading silence, cap at max_ms, 5 ms fade in / 40 ms fade out, peak-normalize."""
    ff = config.ffmpeg()
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage.wav"
        dur = max_ms / 1000
        fade_out = max(0.0, dur - 0.04)
        _ff([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-ac", "1", "-ar", "44100", "-af",
             f"silenceremove=start_periods=1:start_threshold=-50dB,atrim=0:{dur:.3f},"
             f"afade=t=in:d=0.005,afade=t=out:st={fade_out:.3f}:d=0.04", str(stage)], "E_FFMPEG_SFX")
        # if the clip is shorter than max_ms, refade its real tail
        real = duration_ms(stage)
        if real < max_ms - 5:
            stage2 = Path(td) / "stage2.wav"
            _ff([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(stage), "-af",
                 f"afade=t=out:st={max(0.0, real / 1000 - 0.04):.3f}:d=0.04", str(stage2)], "E_FFMPEG_SFX")
            stage = stage2
        gain = peak_db - max_volume_db(stage)
        tmp = Path(td) / "out.mp3"
        _ff([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(stage), "-af", f"volume={gain:.2f}dB",
             "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1",
             "-id3v2_version", "0", "-write_xing", "1", str(tmp)], "E_FFMPEG_SFX")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(out))


def build_library(cfg: dict, client, force: list[str] | None = None, log=print) -> dict:
    """Generate (or refresh) every library clip whose prompt/seconds/model changed, or that is forced."""
    s = cfg["sfx"]
    lock = load_lock()
    raw_dir = config.cache_dir() / "sfx-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    force = set(force or [])
    for sid, spec in s["library"].items():
        psha = prompt_sha(s["model"], spec["prompt"], spec["seconds"], s["promptInfluence"])
        dst = config.SFX_DIR / f"{sid}.mp3"
        if sid not in force and lock.get(sid, {}).get("promptSha") == psha and dst.is_file():
            log(f"sfx {sid} cached=true")
            continue
        raw = raw_dir / f"{sid}.{psha[:8]}.mp3"
        if sid in force or not raw.is_file():
            data = client.sound_generation(spec["prompt"], float(spec["seconds"]), float(s["promptInfluence"]),
                                           s["model"], s.get("outputFormat", "mp3_44100_128"))
            raw.write_bytes(data)
        postprocess(raw, dst, int(spec["maxMs"]))
        data = dst.read_bytes()
        lock[sid] = {"promptSha": psha, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                     "durationMs": duration_ms(dst)}
        log(f"sfx {sid} bytes={len(data)} ms={lock[sid]['durationMs']} cached=false")
    lock = {k: lock[k] for k in sorted(lock) if k in s["library"]}
    config.SFX_DIR.mkdir(parents=True, exist_ok=True)
    config.write_atomic(lock_path(), (json.dumps(lock, indent=1, sort_keys=True) + "\n").encode())
    return lock
