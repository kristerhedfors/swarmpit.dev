"""CLI (NARRATION.md 7). Run from site/:

    python3 -m tools.narrate script  --run ID | --featured | --all
    python3 -m tools.narrate sfx     [--force ID ...]
    python3 -m tools.narrate tts     --run ID | --featured | --all
    python3 -m tools.narrate mix     --run ID | --featured | --all
    python3 -m tools.narrate build   --run ID | --featured | --all [--dry-run]
    python3 -m tools.narrate batch   [--runs all|visible|ID ...] [--dry-run] [--max-new-chars N] [--jobs K]
    python3 -m tools.narrate check

(`npm run narrate -- <command> ...` is the same.) Output is ids, counts, sizes, durations, status and error
codes only. Cue text is never printed; read the templates in vocab.py to review wording.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import config, manifest, script, sfx, tts
from .bundle import BundleError, load_index, load_run, select_runs
from .config import ConfigError
from .eleven import Client, ElevenError
from . import mix as MX
from .mix import MixError, mix_run
from .tts import TTSAbort
from .vocab import VocabError


def log(msg: str) -> None:
    print(msg, flush=True)


def _write_script(s: dict) -> None:
    config.write_cache_json(f"runs/{s['runId']}/script.json", s)


def _read_script(run_id: str) -> dict | None:
    return config.read_cache_json(f"runs/{run_id}/script.json")


def _client() -> Client:
    return Client(config.api_key(), log=log)


def _runs(args, ix) -> list[str]:
    ids = select_runs(ix, args.run, args.featured, args.all)
    if not ids:
        raise BundleError("E_NO_RUNS")
    return ids


def cmd_script(args, cfg, ix, quiet=False) -> dict[str, dict]:
    out = {}
    for rid in _runs(args, ix):
        run = load_run(ix, rid, cfg["models"])
        s = script.build_script(run, cfg)
        _write_script(s)
        out[rid] = s
        m = script.summary(s)
        log(f"script {rid} cues={m['cues']} guide={m['guide']} expert={m['expert']} "
            f"chars_guide={m['chars_guide']} chars_expert={m['chars_expert']} candidates={m['candidates']} "
            f"dropped={m['dropped']} est_audio_ms={m['est_audio_ms']} map_points={m['map_points']} "
            f"ratio={m['ratio']} paced_ratio={m['paced_ratio']} warnings={m['warnings']}")
    return out


def cmd_sfx(args, cfg, ix=None) -> None:
    sfx.build_library(cfg, _client(), force=args.force, log=log)


def _ensure_script(rid: str, cfg, ix) -> dict:
    s = _read_script(rid)
    run_hash = ix.card(rid).hash
    if s is None or s.get("runHash") != run_hash:
        s = script.build_script(load_run(ix, rid, cfg["models"]), cfg)
        _write_script(s)
    return s


def cmd_tts(args, cfg, ix) -> None:
    client = None
    for rid in _runs(args, ix):
        s = _ensure_script(rid, cfg, ix)
        missing = [c for c in s["cues"] if tts.lookup(cfg, c["voice"], c["text"]) is None]
        if missing and client is None:
            client = _client()
        st = tts.synthesize(cfg, s["cues"], client, log=log) if missing else {"cached": len(s["cues"]), "synth": 0,
                                                                              "chars": 0, "cost": 0, "fallback": 0}
        log(f"tts {rid} cues={len(s['cues'])} cached={st['cached']} synth={st['synth']} chars={st['chars']} "
            f"cost={st['cost']} fallback={st['fallback']}")


def cmd_mix(args, cfg, ix) -> None:
    known = {c.id for c in ix.cards}
    for rid in _runs(args, ix):
        s = _ensure_script(rid, cfg, ix)
        mix_run(load_run(ix, rid, cfg["models"]), cfg, s, config.PUBLIC, known, log=log)


def cmd_build(args, cfg, ix) -> None:
    scripts = cmd_script(args, cfg, ix)
    if args.dry_run:
        g = sum(script.summary(s)["chars_guide"] for s in scripts.values())
        e = sum(script.summary(s)["chars_expert"] for s in scripts.values())
        log(f"dry-run runs={len(scripts)} chars_guide={g} chars_expert={e} chars_total={g + e}")
        return
    if not sfx.library_ready(cfg):
        sfx.build_library(cfg, _client(), log=log)
    cmd_tts(args, cfg, ix)
    cmd_mix(args, cfg, ix)


# --------------------------------------------------------------------------------------------------
# batch (NARRATION.md 7.2): every target run, one character budget, resumable
# --------------------------------------------------------------------------------------------------

DEFAULT_MAX_NEW_CHARS = 100_000


def batch_targets(ix, sel: list[str] | None) -> list[str]:
    """'visible' (default) = every non-hidden card, 'all' = every card, else explicit ids (validated)."""
    sel = sel or ["visible"]
    if sel == ["all"]:
        return [c.id for c in ix.cards]
    if sel == ["visible"]:
        return [c.id for c in ix.cards if not c.hidden]
    if "all" in sel or "visible" in sel:
        raise BundleError("E_RUNS_MIXED")
    for i in sel:
        ix.card(i)
    seen: set[str] = set()
    return [x for x in sel if not (x in seen or seen.add(x))]


def _uniq_missing(cfg, cues) -> list[dict]:
    """Uncached cues, one per distinct (voice, text): the exact clips that would be paid for."""
    out, seen = [], set()
    for c in cues:
        k = (c["voice"], c["text"])
        if k in seen:
            continue
        seen.add(k)
        if tts.lookup(cfg, c["voice"], c["text"]) is None:
            out.append(c)
    return out


def batch_plan(cfg, ix, ids, public: Path) -> dict:
    """Scripts for every target run plus the safe numbers the decision needs. No network, no audio."""
    scripts, failed = {}, {}
    for rid in ids:
        try:
            s = script.build_script(load_run(ix, rid, cfg["models"], public), cfg)
        except (BundleError, VocabError) as e:
            failed[rid] = getattr(e, "code", "E_VOCAB")
            continue
        _write_script(s)
        scripts[rid] = s
    clips: dict = {}
    for s in scripts.values():
        for c in s["cues"]:
            clips.setdefault((c["voice"], c["text"]), c)
    missing = _uniq_missing(cfg, list(clips.values()))
    est = sum(int(s["estDurationMs"]) for s in scripts.values())
    return {"scripts": scripts, "failed": failed, "cues": sum(len(s["cues"]) for s in scripts.values()),
            "unique": len(clips), "uncached": len(missing), "new_chars": sum(len(c["text"]) for c in missing),
            "est_ms": est, "projected_bytes": sum(MX.projected_bytes(int(s["estDurationMs"]))
                                                  for s in scripts.values())}


def _up_to_date(cfg, public: Path, s: dict, nindex: dict) -> bool:
    rec = config.read_cache_json(f"runs/{s['runId']}/built.json")
    if not isinstance(rec, dict) or not rec.get("fp"):
        return False
    m = manifest.read_manifest(public, s["runId"])
    ent = nindex.get(s["runId"])
    if m is None or ent is None or m["audio"] != rec.get("audio") or ent.get("hash") != s["runHash"] \
            or m["runHash"] != s["runHash"]:
        return False
    mp3 = manifest.narration_dirs(public)[1] / m["audio"].rsplit("/", 1)[-1]
    if not mp3.is_file() or mp3.stat().st_size != m["audioBytes"]:
        return False
    return rec["fp"] == MX.build_fingerprint(cfg, s)


def cmd_batch(args, cfg, ix, public: Path = config.PUBLIC, client_factory=None) -> int:
    ids = batch_targets(ix, args.runs)
    if not ids:
        raise BundleError("E_NO_RUNS")
    cap = int(args.max_new_chars)
    jobs = int(args.jobs) if args.jobs else None
    pl = batch_plan(cfg, ix, ids, public)
    scripts = pl["scripts"]
    log(f"batch plan runs={len(ids)} scripted={len(scripts)} script_failed={len(pl['failed'])} cues={pl['cues']} "
        f"unique_clips={pl['unique']} cached={pl['unique'] - pl['uncached']} uncached={pl['uncached']} "
        f"new_chars={pl['new_chars']} max_new_chars={cap} est_audio_ms={pl['est_ms']} "
        f"projected_mp3_bytes={pl['projected_bytes']} bitrate_kbps={MX.OUT_BITRATE_KBPS}")
    for rid, code in sorted(pl["failed"].items()):
        log(f"batch script_failed {rid} {code}")
    if args.dry_run:
        return 1 if pl["failed"] else 0
    if pl["new_chars"] > cap:
        log(f"batch refused new_chars={pl['new_chars']} max_new_chars={cap}")
        return 3
    if not sfx.library_ready(cfg):
        raise MixError("E_SFX_LIBRARY_MISSING")      # generating it is a separate, deliberate `sfx` step

    # 1. synthesis, run by run in index order, so a stop leaves whole runs ready
    tcfg = dict(cfg, tts=dict(cfg["tts"], concurrency=jobs or cfg["tts"].get("concurrency", 2)))
    client, spent, synth, stopped, ready = None, 0, 0, False, []
    for rid in ids:
        if rid not in scripts:
            continue
        miss = _uniq_missing(cfg, scripts[rid]["cues"])
        need = sum(len(c["text"]) for c in miss)
        if need and spent + need > cap:
            stopped = True
            log(f"batch stop run={rid} spent_chars={spent} need_chars={need} max_new_chars={cap}")
            break
        if miss:
            if client is None:
                client = (client_factory or _client)()
            st = tts.synthesize(tcfg, miss, client, log=lambda m: None)   # per-clip lines suppressed
            spent += st["chars"]
            synth += st["synth"]
            log(f"batch tts {rid} clips={len(miss)} synth={st['synth']} chars={st['chars']} cost={st['cost']} "
                f"fallback={st['fallback']} spent_chars={spent}")
        ready.append(rid)

    # 2. mixing (skips runs whose fingerprint is unchanged), in parallel processes when jobs > 1
    nindex = manifest.load_index(public)["runs"]
    todo = [rid for rid in ready if not _up_to_date(cfg, public, scripts[rid], nindex)]
    skipped = len(ready) - len(todo)
    workers = jobs or (os.cpu_count() or 2)
    results = []
    if workers > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(todo))) as ex:
            for r in ex.map(MX.batch_worker, [(rid, str(public), str(config.cache_dir())) for rid in todo]):
                results.append(r)
    else:
        results = [MX.batch_worker((rid, str(public), str(config.cache_dir()))) for rid in todo]

    # 3. index, build records, pruning
    known = {c.id for c in ix.cards}
    mixed, mix_failed = 0, 0
    for rid, m, code, fp in results:
        if m is None:
            mix_failed += 1
            log(f"batch mix_failed {rid} {code}")
            continue
        manifest.update_index(public, m, known)
        config.write_cache_json(f"runs/{rid}/built.json", {"fp": fp, "audio": m["audio"]})
        mixed += 1
    manifest.update_index(public, None, known)
    gone_m, gone_a = manifest.prune(public, known)
    adir = manifest.narration_dirs(public)[1]
    mp3s = sorted(adir.glob("*.mp3")) if adir.is_dir() else []
    total = sum(p.stat().st_size for p in mp3s)
    log(f"batch done runs={len(ids)} ready={len(ready)} mixed={mixed} skipped={skipped} mix_failed={mix_failed} "
        f"script_failed={len(pl['failed'])} synth_clips={synth} spent_chars={spent} stopped={int(stopped)} "
        f"pruned_manifests={gone_m} pruned_mp3s={gone_a} narrated={len(manifest.load_index(public)['runs'])} "
        f"mp3_files={len(mp3s)} mp3_bytes={total} max_mp3_bytes={max((p.stat().st_size for p in mp3s), default=0)}")
    return 1 if (pl["failed"] or mix_failed or stopped) else 0


def cmd_check(args, cfg, ix) -> int:
    cards = {c.id: c.hash for c in ix.cards} if ix is not None else None
    problems, warnings = manifest.check_tree(config.PUBLIC, cards)
    for w in warnings:
        log(f"WARN {w}")
    for p in problems:
        log(f"FAIL {p}")
    log(f"narrate check: {'OK' if not problems else f'{len(problems)} problem(s)'} warnings={len(warnings)}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tools.narrate", description="Pre-generated run narration.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def runsel(p):
        p.add_argument("--run", action="append", metavar="ID")
        p.add_argument("--featured", action="store_true")
        p.add_argument("--all", action="store_true")

    for name in ("script", "tts", "mix"):
        runsel(sub.add_parser(name))
    b = sub.add_parser("build")
    runsel(b)
    b.add_argument("--dry-run", action="store_true")
    bt = sub.add_parser("batch")
    bt.add_argument("--runs", nargs="+", metavar="all|visible|ID", default=["visible"])
    bt.add_argument("--dry-run", action="store_true")
    bt.add_argument("--max-new-chars", type=int, default=DEFAULT_MAX_NEW_CHARS, metavar="N")
    bt.add_argument("--jobs", type=int, default=None, metavar="K")
    s = sub.add_parser("sfx")
    s.add_argument("--force", nargs="*", default=[], metavar="ID")
    sub.add_parser("check")
    args = ap.parse_args(argv)
    try:
        cfg = config.load_config()
        if args.cmd == "sfx":
            unknown = [i for i in args.force if i not in cfg["sfx"]["library"]]
            if unknown:
                log("error E_SFX_UNKNOWN_ID")
                return 2
            cmd_sfx(args, cfg)
            return 0
        if args.cmd == "check":
            try:
                ix = load_index()
            except BundleError:
                ix = None
            return cmd_check(args, cfg, ix)
        ix = load_index()
        if args.cmd == "batch":
            if args.max_new_chars < 0 or (args.jobs is not None and args.jobs < 1):
                log("error E_ARGS")
                return 2
            return cmd_batch(args, cfg, ix)
        {"script": cmd_script, "tts": cmd_tts, "mix": cmd_mix, "build": cmd_build}[args.cmd](args, cfg, ix)
        return 0
    except (ConfigError, BundleError, MixError) as e:
        log(f"error {e.code}")
    except (ElevenError, TTSAbort) as e:
        log(f"error elevenlabs status={e.status} code={e.code}")
    except VocabError as e:
        log(f"error {e}")
    except manifest.ManifestError as e:
        log(f"error manifest {','.join(e.codes[:10])}")
    except RuntimeError as e:
        code = str(e)
        log(f"error {code if code.startswith('E_') else 'E_RUNTIME'}")
    except Exception as e:  # noqa: BLE001 - tracebacks could echo values; report the type only
        if os.environ.get("NARRATE_DEBUG") == "1":
            raise
        log(f"error E_INTERNAL {type(e).__name__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
