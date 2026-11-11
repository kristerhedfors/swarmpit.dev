"""Mixing (NARRATION.md 6): stdlib PCM placement of voice cues and SFX, then one ffmpeg call that ducks the
SFX bus under the voice (sidechaincompress); then a measured static gain to the loudness target (two-pass, so the
SFX/voice balance is preserved), a peak limiter, and a metadata-free mono mp3.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from . import GENERATOR, config, manifest, script, sfx, tts
from . import timemap as TM
from .bundle import SafeRun

SR = 44100
# Delivery format (NARRATION.md 6). Speech-only commentary over sparse SFX, committed for every run in the index
# (~650 min of audio): 24 kbps CBR mono MPEG-2 Layer III at 24 kHz keeps the whole set near 110 MiB, about 0.18 MB
# per minute. These constants are authoritative; voices.json `mix.bitrate` / `mix.sampleRate` are not used for
# the encode (the 44.1 kHz PCM buses above are internal only).
OUT_BITRATE_KBPS = 24
OUT_SAMPLE_RATE = 24000


class MixError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class AudioPlan:
    plan: script.Plan
    placements: list[sfx.Placement]
    counts: dict[str, int]
    dropped: int


def plan_audio(run: SafeRun, cfg: dict, cues: list[script.Cue], dur_of) -> AudioPlan:
    lib = cfg["sfx"]["library"]
    p = script.plan(run, cues, dur_of, leads=sfx.key_leads(run, lib))
    spans = [(pl.audioMs, pl.endMs) for pl in p.placed]
    lead_at = {c.anchor: pl.audioMs - ld for c, pl, ld in zip(p.cues, p.placed, p.leads) if ld > 0}
    placements, counts, dropped = sfx.place(run, p.points, spans, lib, lead_at)
    return AudioPlan(p, placements, counts, dropped)


def manifest_dict(run: SafeRun, ap: AudioPlan, points, h8: str, audio_bytes: int) -> dict:
    p = ap.plan
    return {
        "schema": manifest.SCHEMA, "generator": GENERATOR, "runId": run.runId, "runHash": run.hash,
        "audio": manifest.audio_url(run.runId, h8), "audioBytes": int(audio_bytes),
        "durationMs": int(points[-1][0]), "runEndMs": int(run.tEnd),
        "timeMap": [[int(a), int(r)] for a, r in points],
        "cues": [{"audioMs": pl.audioMs, "endMs": pl.endMs, "runMs": pl.runMs, "voice": c.voice, "tpl": c.tpl,
                  "text": c.text} for c, pl in zip(p.cues, p.placed)],
        "sfx": {"counts": dict(sorted(ap.counts.items())), "dropped": int(ap.dropped)},
    }


def plan_only(run: SafeRun, cfg: dict, script_obj: dict) -> dict:
    """Manifest dict from estimated durations, without audio (tests / --plan-only)."""
    cues = script.cues_from_script(script_obj)
    ap = plan_audio(run, cfg, cues, lambda c: script.estimate_ms(c, cfg))
    return manifest_dict(run, ap, ap.plan.points, "0" * 8, 0)


# --------------------------------------------------------------------------------------------------
# PCM buses (stdlib)
# --------------------------------------------------------------------------------------------------

def _db(g: float) -> float:
    return 10 ** (g / 20)


def read_pcm(path: Path) -> array:
    a = array("h")
    a.frombytes(Path(path).read_bytes())
    return a


def place_voice(n: int, clips: list[tuple[int, array, float]]) -> array:
    """clips: (audioMs, pcm, gainDb). Serial by construction, so plain copies (with gain + clip)."""
    bus = array("h", bytes(2 * n))
    for at_ms, pcm, gain in clips:
        off = at_ms * SR // 1000
        m = min(len(pcm), n - off)
        if m <= 0:
            continue
        if gain == 0:
            bus[off:off + m] = pcm[:m]
        else:
            g = _db(gain)
            bus[off:off + m] = array("h", (max(-32768, min(32767, int(v * g))) for v in pcm[:m]))
    return bus


def place_sfx(n: int, hits: list[tuple[int, array, float]]) -> array:
    acc = array("i", bytes(4 * n))
    for at_ms, pcm, gain in hits:
        off = at_ms * SR // 1000
        m = min(len(pcm), n - off)
        if m <= 0:
            continue
        g = _db(gain)
        for i in range(m):
            acc[off + i] += int(pcm[i] * g)
    return array("h", (32767 if v > 32767 else -32768 if v < -32768 else v for v in acc))


def write_wav(path: Path, pcm: array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def sfx_pcm(sid: str) -> array:
    """Decode a committed library clip once into the PCM cache (keyed by the clip's sha256)."""
    src = config.SFX_DIR / f"{sid}.mp3"
    if not src.is_file():
        raise MixError("E_SFX_MISSING")
    h = hashlib.sha256(src.read_bytes()).hexdigest()
    dst = config.cache_dir() / "pcm" / f"sfx-{sid}-{h[:16]}.s16"
    if not dst.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.part")   # unique per process (parallel batch mixing)
        p = subprocess.run([config.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-ac", "1",
                            "-ar", str(SR), "-f", "s16le", str(tmp)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if p.returncode != 0:
            raise MixError("E_FFMPEG_DECODE")
        tmp.replace(dst)
    return read_pcm(dst)


# --------------------------------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------------------------------

def filter_graph(cfg: dict) -> str:
    """Voice + ducked SFX bus. No loudness stage here: normalization is a separate static gain (see encode)."""
    d = cfg["mix"]["duck"]
    return (f"[0:a]asplit=2[vk][vm];[1:a]volume={float(cfg['sfx']['busGainDb']):.2f}dB[s];"
            f"[s][vk]sidechaincompress=threshold={d['threshold']}:ratio={d['ratio']}:attack={d['attackMs']}"
            f":release={d['releaseMs']}[sd];"
            f"[vm][sd]amix=inputs=2:normalize=0[out]")


_LUFS_I = re.compile(r"^\s*I:\s+(-?\d+(?:\.\d+)?|-inf) LUFS\s*$")


def integrated_lufs(path: Path) -> float | None:
    """EBU R128 integrated loudness of a file (only the numeric summary line of ffmpeg's stderr is parsed)."""
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null",
                        "-"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if p.returncode != 0:
        raise MixError("E_FFMPEG_LOUDNESS")
    val = None
    for line in p.stderr.decode("utf-8", "replace").splitlines():
        m = _LUFS_I.match(line)
        if m:
            val = m.group(1)
    if val is None:
        raise MixError("E_FFMPEG_LOUDNESS")
    return None if val == "-inf" else float(val)


def norm_gain_db(cfg: dict, measured_lufs: float | None) -> float:
    """One static gain to the loudness target. Constant over the file, so the SFX/voice balance set by the mix
    is kept exactly (a single-pass loudnorm is an AGC and lifts the quiet SFX-only stretches)."""
    if measured_lufs is None:
        return 0.0
    return max(-30.0, min(30.0, float(cfg["mix"]["lufs"]) - measured_lufs))


def limiter(cfg: dict) -> str:
    """Peak safety after the static gain: only touches the rare peaks above the true-peak ceiling (with a 0.5 dB
    margin for inter-sample peaks); latency-compensated so the time map stays exact; no auto-level."""
    lim = 10 ** ((float(cfg["mix"]["truePeak"]) - 0.5) / 20)
    return f"alimiter=limit={lim:.4f}:attack=5:release=50:level=0:latency=1"


def encode(cfg: dict, voice_wav: Path, sfx_wav: Path, out: Path) -> None:
    """Two passes: (1) duck + sum to a float WAV, measure its integrated loudness; (2) static gain, peak limiter,
    resample, encode a metadata-free mp3."""
    mx = cfg["mix"]
    mixed = out.with_name("mixed.wav")
    p = subprocess.run([config.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(voice_wav),
                        "-i", str(sfx_wav), "-filter_complex", filter_graph(cfg), "-map", "[out]",
                        "-c:a", "pcm_f32le", str(mixed)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if p.returncode != 0 or not mixed.is_file():
        raise MixError("E_FFMPEG_MIX")
    gain = norm_gain_db(cfg, integrated_lufs(mixed))
    cmd = [config.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(mixed),
           "-af", f"volume={gain:.2f}dB,{limiter(cfg)},aresample={OUT_SAMPLE_RATE}", "-ac", str(mx["channels"]),
           "-ar", str(OUT_SAMPLE_RATE), "-c:a", "libmp3lame", "-b:a", f"{OUT_BITRATE_KBPS}k",
           "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0", "-write_xing", "1",
           "-fflags", "+bitexact", "-flags:a", "+bitexact", str(out)]
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if p.returncode != 0 or not out.is_file():
        raise MixError("E_FFMPEG_MIX")


def projected_bytes(duration_ms: int) -> int:
    """Expected mp3 size for a mix of this length (CBR payload plus one header frame)."""
    return int(duration_ms) * OUT_BITRATE_KBPS // 8 + 1024


def probe_ms(path: Path) -> int:
    return sfx.duration_ms(path)


def mix_to_mp3(cfg: dict, duration_ms: int, voice_clips, sfx_hits, work: Path) -> Path:
    n = duration_ms * SR // 1000 + 1
    work.mkdir(parents=True, exist_ok=True)
    vw, sw, out = work / "voice.wav", work / "sfx.wav", work / "out.mp3"
    write_wav(vw, place_voice(n, voice_clips))
    write_wav(sw, place_sfx(n, sfx_hits))
    encode(cfg, vw, sw, out)
    return out


# --------------------------------------------------------------------------------------------------
# Run-level mix
# --------------------------------------------------------------------------------------------------

def build_fingerprint(cfg: dict, script_obj: dict) -> str:
    """Digest of everything that determines a run's mp3 + manifest: the pipeline source, voices.json, the SFX
    library, the encode settings, the script (cues, anchors, run hash) and the TTS cache key of every cue. The batch
    command skips a run whose recorded fingerprint still matches (resumable). Returns '' if a clip is uncached."""
    h = hashlib.sha256()
    h.update(repr((GENERATOR, OUT_BITRATE_KBPS, OUT_SAMPLE_RATE)).encode())
    for p in sorted(config.PKG.glob("*.py")) + [config.VOICES_JSON] + sorted(config.SFX_DIR.glob("*.mp3")):
        if p.name == "__main__.py" or not p.is_file():
            continue
        h.update(p.name.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    h.update(_canon(script_obj).encode("utf-8"))
    for c in script_obj.get("cues", []):
        e = tts.lookup(cfg, c["voice"], c["text"])
        if e is None:
            return ""
        h.update(e["key"].encode())
    return h.hexdigest()


def _canon(v) -> str:
    """Deterministic text form of plain JSON-like data (stdlib only, no json import here)."""
    if isinstance(v, dict):
        return "{" + ",".join(repr(k) + ":" + _canon(v[k]) for k in sorted(v)) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_canon(x) for x in v) + "]"
    return repr(v)


def batch_worker(job: tuple) -> tuple:
    """Process-pool entry for `batch`: mix one run without touching the shared narration index.
    job = (runId, public, cache dir). Returns (runId, manifest | None, error code | None, fingerprint)."""
    from .bundle import BundleError, load_index, load_run
    rid, public, cache = job
    os.environ["NARRATE_CACHE"] = str(cache)      # same cache as the parent, whatever the start method
    try:
        cfg = config.load_config()
        ix = load_index(Path(public))
        s = config.read_cache_json(f"runs/{rid}/script.json")
        if s is None:
            return rid, None, "E_SCRIPT_MISSING", ""
        fp = build_fingerprint(cfg, s)
        m = mix_run(load_run(ix, rid, cfg["models"], Path(public)), cfg, s, Path(public), set(),
                    log=lambda msg: print(msg, flush=True), update_index=False)
        return rid, m, None, fp
    except (MixError, BundleError, config.ConfigError) as e:
        return rid, None, e.code, ""
    except manifest.ManifestError as e:
        return rid, None, (e.codes or ["E_MANIFEST"])[0].split(" ")[0], ""
    except RuntimeError as e:
        code = str(e)
        return rid, None, code if code.startswith("E_") else "E_RUNTIME", ""
    except Exception as e:  # noqa: BLE001 - report the type only (values could be echoed)
        return rid, None, f"E_INTERNAL_{type(e).__name__}", ""


def mix_run(run: SafeRun, cfg: dict, script_obj: dict, public: Path, known_ids: set[str], log=print,
            update_index: bool = True) -> dict:
    if script_obj.get("runId") != run.runId or script_obj.get("runHash") != run.hash:
        raise MixError("E_SCRIPT_STALE")
    cues = script.cues_from_script(script_obj)
    entries = {}
    for c in cues:
        e = tts.lookup(cfg, c.voice, c.text)
        if e is None:
            raise MixError("E_TTS_MISSING")
        entries[c.id] = e
    ap = plan_audio(run, cfg, cues, lambda c: entries[c.id]["durationMs"])
    p = ap.plan
    voice_clips = [(pl.audioMs, read_pcm(entries[c.id]["pcm"]), float(cfg["voices"][c.voice].get("gainDb", 0)))
                   for c, pl in zip(p.cues, p.placed)]
    lib_pcm = {sid: sfx_pcm(sid) for sid in sorted({pl.id for pl in ap.placements})}
    hits = [(pl.audioMs, lib_pcm[pl.id], pl.gainDb) for pl in ap.placements]
    work = config.cache_dir() / "runs" / run.runId
    out = mix_to_mp3(cfg, p.duration_ms, voice_clips, hits, work)
    measured = probe_ms(out)
    try:
        points = TM.finalize(p.points, measured)
    except ValueError:
        raise MixError("E_MAP_DURATION") from None
    data = out.read_bytes()
    h8 = hashlib.sha256(data).hexdigest()[:8]
    m = manifest_dict(run, ap, points, h8, len(data))
    errs = manifest.validate(m)
    if errs:
        raise manifest.ManifestError(errs)
    _, adir = manifest.narration_dirs(public)
    adir.mkdir(parents=True, exist_ok=True)
    dst = adir / f"{run.runId}.{h8}.mp3"
    config.write_atomic(dst, data)
    for old in adir.glob(f"{run.runId}.*.mp3"):
        if old != dst and manifest.MP3_NAME.fullmatch(old.name) and \
                manifest.MP3_NAME.fullmatch(old.name).group(1) == run.runId:
            old.unlink()
    manifest.write_manifest(public, m)
    if update_index:
        manifest.update_index(public, m, known_ids)
    log(f"mix {run.runId} cues={len(m['cues'])} sfx={sum(ap.counts.values())} sfx_dropped={ap.dropped} "
        f"bytes={len(data)} ms={measured} ratio={measured / run.tEnd:.2f} map_points={len(points)} "
        f"warnings={','.join(p.warnings) or '-'}")
    return m
