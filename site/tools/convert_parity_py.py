#!/usr/bin/env python3
"""Python side of the converter parity check (docs/browser-swarm.md §5; driven by tools/convert_parity.mjs).

    dash/server/.venv/bin/python tools/convert_parity_py.py --runs DIR [--db PATH] [--run ID ...]

Ingests DIR into a scratch obs DB (the same `obs ingest --rebuild` export.py runs), builds every finished run
with export.py's RunBuilder + finalize + make_card, and writes ONE JSON object to stdout:

    {"seed": <default seed path obs used, or null>, "runs": {<run_id>: {"dir": <path rel. to DIR>, "proj": {...}}},
     "errors": {<run_id>: <fixed code>}}

`proj` is a numbers-only projection of the bundles (counts, times, indices, flags, enum codes, sha1 prefixes of
ids); it never carries labels, bodies, commands or any other run text. tools/convert_parity.mjs computes the same
projection from the TS converter and compares them; neither side prints run content (CLAUDE.md chatter policy).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SITE = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import export as X  # noqa: E402
from redact import Redactor, default_denylist  # noqa: E402


def h8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def projection(core: dict, detail: dict, card: dict) -> dict:
    """Mirror of proj() in tools/convert_parity.mjs (keep both in lockstep)."""
    run = core["run"]
    nodes = core["nodes"]
    ev = core["events"]
    items = []
    for it in detail["items"]:
        t = it["t"]
        if t == "step":
            items.append([t, it.get("exit"), it.get("outLen"), it.get("via"), len(it.get("submit") or [])])
        elif t == "think":
            items.append([t, it.get("nTools")] + list(it.get("tok") or []))
        elif t == "req":
            items.append([t, it.get("status"), it.get("bytes"), it.get("method")])
        else:
            items.append([t])
    pk = sorted(detail["posts"])
    return {
        "run": {"nAgents": run["nAgents"], "rep": run["rep"], "tEndMs": run["tEndMs"], "wallMs": run["wallMs"],
                "prehistoryMs": run["prehistoryMs"], "caveats": [c["code"] for c in run["caveats"]],
                "harness": run["harness"], "pop": run["pop"], "iface": run["iface"], "goal": run.get("goal"),
                "oldHarness": str(run["codeVersion"]).startswith("pre-"), "tagged": run["tag"] != "",
                "boardName": h8(run["boardName"])},
        "nodeIds": [h8(n["id"]) for n in nodes],
        "nodes": [[n["k"], n["t0"], n.get("t1"), n.get("agent"), n.get("thread"), n.get("author"), n.get("postId"),
                   1 if n.get("seed") else 0, n.get("tSeed"), n.get("bodyLen"), n.get("upvotes0"),
                   "|".join(n.get("flags") or []), len(n["names"]) if "names" in n else None] for n in nodes],
        "edges": [[e["s"], e["d"], e["k"], e.get("w", 1), 1 if e.get("inferred") else 0, 1 if e.get("liveLive") else 0,
                   1 if "ev" in e else 0] for e in core["edges"]],
        "events": {**{k: ev[k] for k in "tdkanefsc"}, "x": [1 if x >= 0 else 0 for x in ev["x"]]},
        "fires": core["fires"],
        "agents": [[a["id"], a["node"], len(a["names"]), a["status"], a["startT"], a["endT"], a["solveT"], a["solveStep"],
                    a["stepsUsed"], a["modelCalls"], a["ended"], a["tokens"]["prompt"], a["tokens"]["completion"],
                    a["tokens"]["cached"], a["tokens"]["reasoning"], a["metrics"]["discovered"],
                    a["metrics"]["firstContact"], a["metrics"]["boardSteps"], a["metrics"]["shareAfterContact"],
                    a["metrics"]["posts"], a["metrics"]["episodes"], a["metrics"]["secretLeak"], a["drift"]["t"],
                    a["drift"]["a"]] for a in core["agents"]],
        "chapters": [[c["t"], c["k"], c.get("agent"), c.get("node"), c.get("ev"), c["p"], c.get("merged", 1)]
                     for c in core["chapters"]],
        "segments": [[s["t0"], s["t1"], s["title"]] for s in core["segments"]],
        "series": core["series"],
        "card": {"stats": card["stats"], "density": card["density"], "score": card["score"], "caveats": card["caveats"],
                 "durationMs": card["durationMs"], "nAgents": card["nAgents"], "pop": card["pop"], "iface": card["iface"],
                 "goal": card.get("goal"), "blurb": h8(card["blurb"])},
        "detail": {"items": items, "postKeys": [h8(k) for k in pk],
                   "leakSpans": [len(detail["posts"][k]["leakSpans"]) for k in pk],
                   "mentions": [len(detail["posts"][k]["mentions"]) for k in pk]},
        "nStrings": len(core["strings"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=SITE / ".cache" / "parity.duckdb")
    ap.add_argument("--run", nargs="*")
    a = ap.parse_args()
    X.ensure_duckdb(sys.argv[1:])
    import duckdb

    runs = a.runs.resolve()
    out = {"seed": None, "runs": {}, "errors": {}}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            X.run_ingest(runs, a.db.resolve())
    except Exception:
        out["errors"]["_ingest"] = "ingest_failed"
        print(json.dumps(out))
        return 1
    sys.path.insert(0, str(X.DASH_SERVER))
    from obs.config import Config  # noqa: E402
    sf = Config(runs_dir=runs).seed_file
    out["seed"] = str(sf) if sf.is_file() else None

    con = duckdb.connect(str(a.db.resolve()), read_only=True)
    meta = {k: v for k, v in con.execute("SELECT key, value FROM schema_meta").fetchall()}
    meta = {"obsSchema": int(meta.get("schema_version", 1)), "obsDerive": int(meta.get("derive_version", 0))}
    deny = default_denylist([])
    for r in X.rows(con, "SELECT run_id, status, ctf, source_dir FROM runs WHERE run_id <> '_pilot' ORDER BY run_id"):
        rid = r["run_id"]
        if a.run and rid not in a.run:
            continue
        if r["status"] != "done":
            continue
        try:
            rel = str(Path(r["source_dir"]).resolve().relative_to(runs))
        except (TypeError, ValueError):
            out["errors"][rid] = "no_source_dir"
            continue
        try:
            ctf = X.jl(r["ctf"], {}) or {}
            R = Redactor(deny=deny, allow=X.ctf_allow(ctf), mask=None)
            rb = X.RunBuilder(con, rid, R, runs, False, meta)
            with contextlib.redirect_stdout(io.StringIO()):
                core, detail = rb.build()
                core, detail, cj, dj = X.finalize(core, detail, R)
                card = X.make_card(core, len(cj.encode()), len(dj.encode()), False, rb.runm)
            out["runs"][rid] = {"dir": rel, "proj": projection(core, detail, card)}
        except Exception as e:  # fixed code only: exception messages can echo run content
            out["errors"][rid] = "build_error:" + type(e).__name__
    print(json.dumps(out, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
