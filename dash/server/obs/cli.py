"""`obs ingest|serve|derive|stats|tap-once` (CONTRACT section 7)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .config import Config
from .db import Database, DatabaseLocked, SchemaChanged


def _cfg(a) -> Config:
    cfg = Config()
    kw = {}
    if getattr(a, "runs", None):
        kw["runs_dir"] = Path(a.runs).expanduser()
    if getattr(a, "db", None):
        kw["db_path"] = Path(a.db).expanduser()
    if getattr(a, "no_live_tap", False):
        kw["live_tap"] = False
    elif getattr(a, "live_tap", False):
        kw["live_tap"] = True
    return cfg.override(**kw)


def _open(cfg: Config, check_schema=True) -> Database:
    try:
        db = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    except DatabaseLocked as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    try:
        db.setup(check_schema=check_schema)
    except SchemaChanged as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    return db


def cmd_ingest(a):
    cfg = _cfg(a)
    if a.rebuild:
        for suffix in ("", ".wal"):
            p = Path(str(cfg.db_path) + suffix)
            if p.exists():
                p.unlink()
    from .ingest import Ingester
    db = _open(cfg)
    ing = Ingester(db, cfg)
    ing.quiet = True
    t0 = time.time()
    if a.prune:
        with db.lock:
            gone = [r for (r,) in db.con.execute("SELECT run_id FROM runs WHERE status = 'missing'").fetchall()]
            for rid in gone:
                for t in ("runs", "agents", "agent_records", "model_calls", "tool_calls", "steps", "board_events",
                          "relay_events", "submissions", "posts", "threads", "interactions", "diag_files",
                          "ingest_state", "mat_agent_metrics", "mat_run_metrics"):
                    db.con.execute(f"DELETE FROM {t} WHERE run_id = ?", [rid])
            print(f"pruned {len(gone)} missing runs")
    res = ing.scan(only=set(a.run) if a.run else None)
    derived = [] if a.no_derive else ing.derive_dirty(only=set(a.run) if a.run else None)
    n = db.con.execute("SELECT count(*) FROM runs WHERE run_id <> '_pilot'").fetchone()[0]
    print(f"ingested {len(res.changed)} changed run(s), derived {len(derived)}; {n} runs in {cfg.db_path} "
          f"({time.time() - t0:.2f}s) from {cfg.runs_dir}")
    db.close()


def cmd_derive(a):
    cfg = _cfg(a)
    from . import derive as D
    db = _open(cfg)
    with db.lock:
        if a.all:
            db.con.execute("UPDATE runs SET dirty = TRUE")
        elif a.run:
            for r in a.run:
                db.con.execute("UPDATE runs SET dirty = TRUE WHERE run_id = ?", [r])
    rows = db.con.execute("SELECT run_id FROM runs WHERE dirty AND run_id <> '_pilot'").fetchall()
    t0 = time.time()
    for (rid,) in rows:
        D.derive_run(db, rid, cfg.runs_dir)
    print(f"derived {len(rows)} run(s) in {time.time() - t0:.2f}s")
    db.close()


def cmd_stats(a):
    cfg = _cfg(a)
    db = _open(cfg)
    con = db.con
    tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                                        "ORDER BY 1").fetchall()]
    print(f"db: {cfg.db_path}\nruns_dir: {cfg.runs_dir}")
    print("\nrow counts:")
    for t in tables:
        print(f"  {t:20s} {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>10,}")
    print("\nrun statuses:")
    for s, n in con.execute("SELECT status, count(*) FROM runs GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  {s or '?':12s} {n}")
    print("\nnotes:")
    for rid, notes in con.execute("SELECT run_id, notes FROM runs WHERE json_array_length(notes) > 0 ORDER BY run_id").fetchall():
        for n in json.loads(notes):
            print(f"  {rid}: {n}")
    last = con.execute("SELECT max(updated) FROM ingest_state").fetchone()[0]
    if last:
        print(f"\nlast ingest state update: {time.time() - last:.1f}s ago")
    db.close()


def cmd_tap_once(a):
    cfg = _cfg(a)
    from .livetap import tap_once
    ing = None
    if a.write:
        from .ingest import Ingester
        db = _open(cfg)
        ing = Ingester(db, cfg)
    for row in tap_once(cfg, vm=a.vm, write=a.write, ingester=ing):
        print(json.dumps(row))


def cmd_serve(a):
    cfg = _cfg(a)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    import uvicorn
    from .api import create_app
    from .ingest import Ingester, IngestWorker
    from .livetap import LiveTap
    from .sse import Broadcaster

    db = _open(cfg)
    bc = Broadcaster()
    ing = Ingester(db, cfg, emit=bc.publish)
    worker = IngestWorker(ing, cfg, watch=not a.no_watch)
    tap = LiveTap(cfg, submit=worker.submit, offsets_fn=ing.live_offsets)
    if not (cfg.web_dist / "index.html").is_file():
        print("Frontend not built. Run: cd dash/web && npm i && npm run build", file=sys.stderr)
    print(f"obs serve: runs_dir={cfg.runs_dir} db={cfg.db_path} live_tap={'on' if tap.enabled else 'off'} "
          f"-> http://{a.host}:{a.port}", file=sys.stderr)
    worker.start()
    tap.start()
    app = create_app(cfg, db, bc, worker=worker, tap=tap)
    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        tap.stop()
        worker.stop()
        worker.join(timeout=5)
        db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="obs", description="Swarm Observatory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--runs", help="runs dir (OBS_RUNS_DIR)")
        p.add_argument("--db", help="DuckDB path (OBS_DB)")

    p = sub.add_parser("ingest", help="one-shot incremental ingest + derive")
    common(p)
    p.add_argument("--run", action="append", help="limit to run id (repeatable)")
    p.add_argument("--rebuild", action="store_true", help="delete the DB file and rebuild from files")
    p.add_argument("--prune", action="store_true", help="delete rows of runs whose dir disappeared")
    p.add_argument("--no-derive", action="store_true")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("serve", help="API + UI + continuous ingest + live tap")
    common(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-watch", action="store_true")
    p.add_argument("--live-tap", action="store_true",
                   help="exec-tail running agent VMs (off by default: visible to the agents, CONTRACT 3.4)")
    p.add_argument("--no-live-tap", action="store_true")
    p.add_argument("--reload", action="store_true", help="(accepted for compatibility; not supported with the "
                                                          "single-writer worker)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("derive", help="re-run the derive pass")
    common(p)
    p.add_argument("--run", action="append")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_derive)

    p = sub.add_parser("stats", help="row counts, statuses, notes")
    common(p)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("tap-once", help="debug: discovery + one read-only tail tick")
    common(p)
    p.add_argument("--vm")
    p.add_argument("--write", action="store_true")
    p.add_argument("--no-live-tap", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_tap_once)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
