#!/usr/bin/env python3
"""Bootstrap the instigation landscape archive with generation-0 candidates.

Two sources seed the MAP-Elites archive (lab/study/qd.py) before mutation starts:

  * pilot   -- the hand-written pilot seed (lab/seeds/seed.json), normalized into the
               candidate schema and persisted as a candidate record (bootstrap=True,
               origin="pilot", generation=0), plus optional gen-0 variants of it drawn
               from the generator model (generate.generate_mutation) with the same labels
               and parent_id = the pilot's record id.
  * scratch -- fresh generate.generate_candidate() draws per channel (bootstrap=False,
               origin="scratch", generation=0).

ISOLATION: the pilot seed is board text. This SCRIPT opens it only to coerce it into
the seed shape and write it straight to
`data/synthetic-agent-chatter/candidates/<record_id>.chatter.json`; its text goes
nowhere else except (for variants) the generator model's request, exactly as in
generate_mutation. Every function here returns numbers-only metas: the opaque record
id, char length, post count, the bootstrap flag and fixed enums (origin, channel), or
a fixed error code. Nothing here prints, logs or returns field values of the seed.

    python3 lab/study/bootstrap.py pilot   [--channels forum] [--variants N] [--model SLUG]
    python3 lab/study/bootstrap.py scratch --channels forum,irc --per-channel N [--model SLUG]

prints one numbers-only JSON object: {ok, n_ok, n_err, metas: [...]}.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

from board.channel import CHANNELS as _BOARD_CHANNELS  # noqa: E402

from study import generate, modelio, registry  # noqa: E402

PILOT_PATH = _LAB / "seeds" / "seed.json"
PILOT_MODEL = ""  # the pilot is hand-written: no generator model slug


class BootstrapError(RuntimeError):
    """args[0] is a fixed error code: E_PILOT_READ / E_SCHEMA / E_CHANNEL / E_DUPLICATE."""


def _check_channels(channels) -> tuple:
    chans = tuple(channels or ())
    if not chans or any(c not in _BOARD_CHANNELS for c in chans):
        raise BootstrapError("E_CHANNEL")
    return chans


def _meta(row: dict, *, channel: str) -> dict:
    """Numbers / ids / enums only, read off a registry row (never the candidate)."""
    return {"ok": True, "record_id": row["record_id"], "length_chars": row["length_chars"],
            "n_posts": row["n_posts"], "bootstrap": bool(row["bootstrap"]),
            "origin": row["origin"], "channel": channel,
            "generation": row["generation"], "parent_id": row.get("parent_id")}


def _err(code: str, *, channel: str = "", index: int = 0, origin: str = "") -> dict:
    return {"ok": False, "error": code, "channel": channel, "index": int(index), "origin": origin}


def _code(e: BaseException) -> str:
    """The fixed code carried by a known error; anything else collapses to E_UNKNOWN."""
    if isinstance(e, (generate.GenError, modelio.ModelError, BootstrapError)) and e.args \
            and isinstance(e.args[0], str) and e.args[0].startswith("E_"):
        return e.args[0]
    return "E_UNKNOWN"


def normalize_pilot(obj) -> dict:
    """Coerce a loaded pilot seed into the strict candidate schema (generate._normalize shape).

    The pilot is the store.Store seed shape (board_name, threads[slug, title,
    posts[author, body, ago_min]]); this mapping is structural only, so drift in optional
    fields does not break the bootstrap: a missing slug -> "thread-<i>", missing title ->
    the slug, missing author -> "anon", missing / non-integer / negative ago_min -> 0,
    unknown extra keys dropped, threads with no posts skipped. A non-string body or
    board_name, or nothing left to seed, raises BootstrapError("E_SCHEMA"). Values are
    copied, never inspected or emitted.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("board_name"), str):
        raise BootstrapError("E_SCHEMA")
    threads_in = obj.get("threads")
    if not isinstance(threads_in, list):
        raise BootstrapError("E_SCHEMA")
    threads = []
    for i, t in enumerate(threads_in):
        if not isinstance(t, dict):
            raise BootstrapError("E_SCHEMA")
        slug = t.get("slug") if isinstance(t.get("slug"), str) and t.get("slug") else f"thread-{i}"
        title = t.get("title") if isinstance(t.get("title"), str) else slug
        posts = []
        for p in t.get("posts") or []:
            if not isinstance(p, dict) or not isinstance(p.get("body"), str):
                raise BootstrapError("E_SCHEMA")
            author = p.get("author") if isinstance(p.get("author"), str) else "anon"
            ago = p.get("ago_min", 0)
            try:
                ago = max(0, int(ago))
            except (TypeError, ValueError):
                ago = 0
            posts.append({"author": author, "body": p["body"], "ago_min": ago})
        if posts:
            threads.append({"slug": slug, "title": title, "posts": posts})
    if not threads:
        raise BootstrapError("E_SCHEMA")
    try:  # final strict pass through the generator's own validator: one schema for all candidates
        return generate._normalize({"board_name": obj["board_name"], "threads": threads})
    except generate.GenError:
        raise BootstrapError("E_SCHEMA")


def load_pilot(pilot_path=None) -> dict:
    """Read + normalize the pilot seed file. Raises BootstrapError(E_PILOT_READ / E_SCHEMA)."""
    p = Path(pilot_path) if pilot_path is not None else PILOT_PATH
    try:
        with open(p) as f:
            obj = json.load(f)
    except (OSError, ValueError):
        raise BootstrapError("E_PILOT_READ") from None  # no parser message: it could echo content
    return normalize_pilot(obj)


def persist_pilot(candidate: dict, *, channel: str, candidates_dir=None, registry_path=None) -> dict:
    """Write the normalized pilot as a candidate file + bootstrap registry row; return the row."""
    rid = registry.record_id(candidate)
    path = registry.candidate_path(rid, candidates_dir)
    registry._atomic_write(path, registry.canonical_json(candidate))
    row = registry.make_row(candidate, parent_id=None, generation=0, channel=channel,
                            model=PILOT_MODEL, chatter_path=str(path), created_ts=time.time(),
                            bootstrap=True, origin="pilot")
    return registry.upsert(row, registry_path)


def _transport(transport, slug, allow_paid):
    return transport if transport is not None else modelio.make_transport(slug, allow_paid=allow_paid)


def bootstrap_pilot(pilot_path=None, channels=("forum",), n_variants: int = 0, *,
                    slug: str = modelio.DEFAULT_SLUG, transport=None, allow_paid: bool = False,
                    registry_path=None, candidates_dir=None, temperature: float = 0.9) -> list:
    """Seed the archive from the pilot: one pilot record + `n_variants` gen-0 variants.

    The pilot record is listed once per channel in `channels` (same record id; the registry
    row keeps the first channel) so the driver can evaluate it on every axis channel.
    Variants are spread round-robin over `channels` (variant i -> channels[i % len]); each is
    a generate_mutation() child of the persisted pilot file, re-labelled bootstrap=True,
    origin="pilot", generation=0, parent_id=<pilot id>. A variant failure yields an error
    meta and the loop continues; a pilot failure yields a single error meta.
    """
    try:
        chans = _check_channels(channels)
        cand = load_pilot(pilot_path)
        prow = persist_pilot(cand, channel=chans[0], candidates_dir=candidates_dir,
                             registry_path=registry_path)
    except (BootstrapError, OSError) as e:
        return [_err(_code(e), origin="pilot")]
    pid = prow["record_id"]
    metas = [_meta(prow, channel=c) for c in chans]
    n_variants = max(0, int(n_variants or 0))
    if not n_variants:
        return metas
    try:
        tp = _transport(transport, slug, allow_paid)
    except modelio.ModelError as e:
        return metas + [_err(_code(e), channel=chans[i % len(chans)], index=i, origin="pilot")
                        for i in range(n_variants)]
    pilot_file = registry.candidate_path(pid, candidates_dir)
    for i in range(n_variants):
        ch = chans[i % len(chans)]
        try:
            m = generate.generate_mutation(pilot_file, slug=slug, channel=ch, generation=0,
                                           transport=tp, allow_paid=allow_paid,
                                           candidates_dir=candidates_dir,
                                           registry_path=registry_path, temperature=temperature)
            if m["record_id"] == pid:
                # the model echoed the pilot verbatim: keep the pilot row as-is, count no variant
                registry.annotate(pid, registry_path, parent_id=None, channel=chans[0],
                                  model=PILOT_MODEL, bootstrap=True, origin="pilot")
                raise BootstrapError("E_DUPLICATE")
            row = registry.annotate(m["record_id"], registry_path, parent_id=pid, generation=0,
                                    channel=ch, bootstrap=True, origin="pilot")
            metas.append(_meta(row, channel=ch))
        except Exception as e:  # noqa: BLE001 -- reduced to a fixed code, never a message
            metas.append(_err(_code(e), channel=ch, index=i, origin="pilot"))
    return metas


def bootstrap_scratch(channels, per_channel: int, *, slug: str = modelio.DEFAULT_SLUG,
                      transport=None, allow_paid: bool = False, registry_path=None,
                      candidates_dir=None, temperature: float = 0.9) -> list:
    """Draw `per_channel` fresh gen-0 candidates for each channel (bootstrap=False, origin="scratch").

    Each draw goes through generate.generate_candidate (model text -> candidate file); the row
    is then labelled origin="scratch" explicitly. Failures yield error metas and the loop continues.
    """
    try:
        chans = _check_channels(channels)
    except BootstrapError as e:
        return [_err(_code(e), origin="scratch")]
    per_channel = max(0, int(per_channel or 0))
    try:
        tp = _transport(transport, slug, allow_paid) if per_channel else None
    except modelio.ModelError as e:
        return [_err(_code(e), channel=c, index=i, origin="scratch")
                for c in chans for i in range(per_channel)]
    metas = []
    for ch in chans:
        for i in range(per_channel):
            try:
                m = generate.generate_candidate(slug=slug, channel=ch, parent_id=None, generation=0,
                                                transport=tp, allow_paid=allow_paid,
                                                candidates_dir=candidates_dir,
                                                registry_path=registry_path, temperature=temperature)
                row = registry.annotate(m["record_id"], registry_path, channel=ch, generation=0,
                                        bootstrap=False, origin="scratch")
                metas.append(_meta(row, channel=ch))
            except Exception as e:  # noqa: BLE001 -- reduced to a fixed code, never a message
                metas.append(_err(_code(e), channel=ch, index=i, origin="scratch"))
    return metas


def _load_env(slug: str) -> None:
    """Load the provider key via orchestrate.load_env (repo .env, or the main checkout's .env from a
    git worktree); never prints it. A missing key surfaces later as the Transport's E_KEY."""
    import config
    import orchestrate
    try:
        orchestrate.load_env(config.provider_spec(slug)["secret_env"])
    except (OSError, AssertionError):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="bootstrap gen-0 landscape candidates (numbers out only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pilot", "scratch"):
        sp = sub.add_parser(name)
        sp.add_argument("--channels", default="forum", help="comma-separated board channel names")
        sp.add_argument("--model", default=modelio.DEFAULT_SLUG, help="config.MODELS slug")
        sp.add_argument("--allow-paid", action="store_true", help="opt in to a priced (Berget) model")
        sp.add_argument("--temperature", type=float, default=0.9)
        sp.add_argument("--registry", default=None, help="override registry.json path")
        sp.add_argument("--candidates-dir", default=None)
        if name == "pilot":
            sp.add_argument("--pilot", default=None, help="pilot seed path (default lab/seeds/seed.json)")
            sp.add_argument("--variants", type=int, default=0)
        else:
            sp.add_argument("--per-channel", type=int, default=1)
    a = ap.parse_args(argv)
    chans = tuple(c.strip() for c in a.channels.split(",") if c.strip())
    common = dict(slug=a.model, allow_paid=a.allow_paid, temperature=a.temperature,
                  registry_path=a.registry, candidates_dir=a.candidates_dir)
    needs_model = (a.cmd == "pilot" and a.variants > 0) or (a.cmd == "scratch" and a.per_channel > 0)
    if needs_model:
        try:
            _load_env(a.model)
        except Exception:  # noqa: BLE001 -- unknown slug etc.; Transport reports a fixed code
            pass
    if a.cmd == "pilot":
        metas = bootstrap_pilot(a.pilot, chans, a.variants, **common)
    else:
        metas = bootstrap_scratch(chans, a.per_channel, **common)
    n_ok = sum(1 for m in metas if m.get("ok"))
    print(json.dumps({"ok": n_ok > 0 and n_ok == len(metas), "n_ok": n_ok,
                      "n_err": len(metas) - n_ok, "metas": metas}))
    return 0 if n_ok == len(metas) and n_ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
