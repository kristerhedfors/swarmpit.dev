"""Cached synthesis of a script's cues (NARRATION.md 5.2).

Cache: .narration-cache/tts/<sha256>.mp3 + .json (meta), .narration-cache/pcm/<sha256>.s16 (trimmed mono PCM).
The key covers voice id, model, settings, seed, output format and text, so a hit never touches the network.
Log lines carry ids, counts, sizes and codes only; never cue text, response bodies, or the key.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

from . import config
from .eleven import ElevenError

SAMPLE_RATE = 44100
TRIM_AF = ("silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse")


class TTSAbort(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(f"tts aborted status={status} code={code}")


def request_spec(cfg: dict, voice: str, text: str, model: str) -> dict:
    v = cfg["voices"][voice]
    return {"v": 1, "voiceId": v["voiceId"], "model": model, "settings": v["settings"],
            "seed": cfg["tts"].get("seed"), "outputFormat": cfg["tts"]["outputFormat"], "text": text}


def cache_key(spec: dict) -> str:
    canon = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _paths(key: str) -> tuple[Path, Path, Path]:
    c = config.cache_dir()
    return c / "tts" / f"{key}.mp3", c / "tts" / f"{key}.json", c / "pcm" / f"{key}.s16"


def _models(cfg: dict) -> list[str]:
    m = [cfg["tts"]["model"]]
    fb = cfg["tts"].get("fallbackModel")
    if fb and fb not in m:
        m.append(fb)
    return m


def lookup(cfg: dict, voice: str, text: str) -> dict | None:
    """Cached entry for a cue (primary model first, then fallback): {key, pcm, durationMs, model}."""
    for model in _models(cfg):
        key = cache_key(request_spec(cfg, voice, text, model))
        _, meta_p, pcm_p = _paths(key)
        if meta_p.is_file() and pcm_p.is_file():
            try:
                with open(meta_p, "rb") as fh:
                    meta = json.loads(fh.read())
                return {"key": key, "pcm": pcm_p, "durationMs": int(meta["durationMs"]), "model": model}
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return None


def decode_to_pcm(mp3: Path, pcm: Path) -> int:
    """Decode + trim leading/trailing silence to mono 44.1 kHz s16le. Returns the duration in ms."""
    pcm.parent.mkdir(parents=True, exist_ok=True)
    tmp = pcm.with_suffix(".s16.part")
    p = subprocess.run([config.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3), "-ac", "1",
                        "-ar", str(SAMPLE_RATE), "-af", TRIM_AF, "-f", "s16le", str(tmp)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if p.returncode != 0 or not tmp.is_file():
        raise RuntimeError("E_FFMPEG_DECODE")
    tmp.replace(pcm)
    return int(round(pcm.stat().st_size / 2 / SAMPLE_RATE * 1000))


def _write_entry(key: str, res: dict, model: str) -> dict:
    mp3_p, meta_p, pcm_p = _paths(key)
    mp3_p.parent.mkdir(parents=True, exist_ok=True)
    mp3_p.write_bytes(res["audio"])
    dur = decode_to_pcm(mp3_p, pcm_p)
    meta = {"durationMs": dur, "characterCost": res.get("characterCost"), "requestId": res.get("requestId"),
            "model": model, "bytes": len(res["audio"]), "status": 200}
    meta_p.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def _needs_fallback(e: ElevenError) -> bool:
    return e.status == 422 or e.code in ("model_not_found", "invalid_model", "model_not_available")


def synthesize(cfg: dict, cues: list[dict], client, log=print) -> dict:
    """Synthesize every uncached cue ({voice, tpl, text}). Returns safe stats. Raises TTSAbort on fatal errors."""
    models = _models(cfg)
    state = {"primary_ok": True}
    lock = threading.Lock()
    stats = {"cached": 0, "synth": 0, "chars": 0, "cost": 0, "fallback": 0}

    def one(i: int, cue: dict) -> None:
        voice, text, tpl = cue["voice"], cue["text"], cue["tpl"]
        if lookup(cfg, voice, text) is not None:
            with lock:
                stats["cached"] += 1
            log(f"tts {tpl} {voice} cue={i} cached=true")
            return
        tried = models if state["primary_ok"] else models[1:]
        for j, model in enumerate(tried):
            key = cache_key(request_spec(cfg, voice, text, model))
            v = cfg["voices"][voice]
            try:
                res = client.tts_with_timestamps(v["voiceId"], text, model, v["settings"], cfg["tts"].get("seed"),
                                                 cfg["tts"]["outputFormat"], cfg["tts"]["endpoint"])
            except ElevenError as e:
                log(f"tts {tpl} {voice} cue={i} status={e.status} code={e.code}")
                if j + 1 < len(tried) and _needs_fallback(e):
                    with lock:
                        state["primary_ok"] = False
                        stats["fallback"] += 1
                    continue
                # fatal (401/403/422/quota) or retries exhausted: abort the whole build
                raise TTSAbort(e.status, e.code) from None
            meta = _write_entry(key, res, model)
            with lock:
                stats["synth"] += 1
                stats["chars"] += len(text)
                stats["cost"] += meta["characterCost"] or 0
            log(f"tts {tpl} {voice} cue={i} status=200 bytes={meta['bytes']} ms={meta['durationMs']} "
                f"cost={meta['characterCost'] if meta['characterCost'] is not None else '?'} cached=false")
            return

    conc = max(1, int(cfg["tts"].get("concurrency", 2)))
    ex = ThreadPoolExecutor(max_workers=conc)
    try:
        futs = [ex.submit(one, i, c) for i, c in enumerate(cues)]
        wait(futs, return_when=FIRST_EXCEPTION)
        for f in futs:
            if f.done() and f.exception() is not None:
                raise f.exception()
        for f in futs:
            f.result()
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return stats
