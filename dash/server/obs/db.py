"""DuckDB connection management: one writer connection, per-request read cursors, schema apply, meta rows,
and fast bulk insert/update helpers (newline-delimited JSON temp file + read_json; DuckDB's Python list
parameter path is orders of magnitude slower)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import duckdb

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_tmp_counter = 0


class SchemaChanged(RuntimeError):
    pass


class DatabaseLocked(RuntimeError):
    pass


LOCKED_MSG = ("database is locked by a running 'obs serve' (it ingests continuously); stop it or use --db")


def jdump(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


class Database:
    def __init__(self, path: Path | str, tmp_dir: Path | None = None, read_only: bool = False):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir()) / "obs-tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.con = duckdb.connect(self.path, read_only=read_only)
        except duckdb.IOException as e:  # lock held by another process
            if "lock" in str(e).lower():
                raise DatabaseLocked(LOCKED_MSG) from e
            raise
        self.lock = threading.RLock()
        self._types: dict[str, dict[str, str]] = {}
        self.read_only = read_only

    # ------------------------------------------------------------------ setup
    def setup(self, check_schema: bool = True) -> dict:
        """Apply DDL idempotently, verify schema_version, seed meta rows. Returns info dict."""
        from . import derive, metrics  # local import: avoid cycles
        info = {"derive_bumped": False}
        with self.lock:
            existing = self.con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_meta'").fetchone()[0]
            if existing:
                row = self.con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                if row and check_schema and int(row[0]) != SCHEMA_VERSION:
                    raise SchemaChanged("schema changed: run 'uv run obs ingest --rebuild'")
            self.con.execute(SCHEMA_PATH.read_text())
            # Metric caches are CREATE TABLE AS view: when a view's columns change, rebuild the cache and re-derive.
            for mat, view in (("mat_agent_metrics", "v_agent_metrics"), ("mat_run_metrics", "v_run_metrics")):
                cols = [r[0] for r in self.con.execute(f"DESCRIBE {mat}").fetchall()]
                vcols = [r[0] for r in self.con.execute(f"DESCRIBE {view}").fetchall()]
                if cols != vcols:
                    self.con.execute(f"DROP TABLE {mat}")
                    self.con.execute(f"CREATE TABLE {mat} AS SELECT * FROM {view} LIMIT 0")
                    self.con.execute("UPDATE runs SET dirty = TRUE")
                    self._types.pop(mat, None)
                    info["derive_bumped"] = True
            now = time.time()
            self.con.execute("INSERT INTO schema_meta VALUES ('schema_version', ?) ON CONFLICT DO NOTHING",
                             [str(SCHEMA_VERSION)])
            self.con.execute("INSERT INTO schema_meta VALUES ('created', ?) ON CONFLICT DO NOTHING", [str(now)])
            row = self.con.execute("SELECT value FROM schema_meta WHERE key='derive_version'").fetchone()
            if row is None or int(row[0]) != derive.DERIVE_VERSION:
                if row is not None:
                    self.con.execute("UPDATE runs SET dirty = TRUE")
                    info["derive_bumped"] = True
                self.con.execute("INSERT OR REPLACE INTO schema_meta VALUES ('derive_version', ?)",
                                 [str(derive.DERIVE_VERSION)])
            self.con.execute(
                "INSERT INTO experiments (experiment_id, name, description, created, params) VALUES (?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                ["board-stickiness", "Board stickiness pilot",
                 "Do agents discover, return to and post on a planted agent message board, by interface and "
                 "population (lab/orchestrate.py pilot).", now, jdump({"pilot_rng_seed": 1234, "cells_per_rep": 8})])
            self.bulk_insert("metric_defs", metrics.metric_def_rows(), conflict="replace")
        return info

    def meta(self, key: str) -> str | None:
        r = self.reader().execute("SELECT value FROM schema_meta WHERE key=?", [key]).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: Any):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [key, str(value)])

    def reader(self) -> duckdb.DuckDBPyConnection:
        """A fresh cursor (own connection to the same DB). Read-only by convention."""
        return self.con.cursor()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    def col_types(self, table: str) -> dict[str, str]:
        t = self._types.get(table)
        if t is None:
            rows = self.con.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? "
                "ORDER BY ordinal_position", [table]).fetchall()
            t = {c: ty for c, ty in rows}
            self._types[table] = t
        return t

    def _write_tmp(self, rows: Iterable[dict], cols: list[str], types: dict[str, str]) -> tuple[str, int]:
        global _tmp_counter
        _tmp_counter += 1
        p = self.tmp_dir / f"bulk-{os.getpid()}-{threading.get_ident()}-{_tmp_counter}.jsonl"
        n = 0
        jcols = [c for c in cols if types[c] == "JSON"]
        enc = _ENC.encode
        with open(p, "w", encoding="utf-8") as f:
            write = f.write
            for r in rows:
                o = {c: r.get(c) for c in cols}
                for c in jcols:
                    v = o[c]
                    if isinstance(v, str):
                        # JSON columns accept raw JSON text; parse so read_json stores it verbatim
                        try:
                            o[c] = json.loads(v)
                        except ValueError:
                            pass
                try:
                    line = enc(o)
                except ValueError:  # NaN / inf -> NULL
                    line = enc(_clean_row(o))
                write(line)
                write("\n")
                n += 1
        return str(p), n

    def _read_json_expr(self, path: str, cols: list[str], types: dict[str, str]) -> str:
        spec = ", ".join(f"'{c}': '{types[c]}'" for c in cols)
        return (f"read_json('{path}', format='newline_delimited', maximum_object_size=268435456, "
                f"columns={{{spec}}})")

    def bulk_insert(self, table: str, rows: list[dict], conflict: str | None = "nothing",
                    cols: list[str] | None = None) -> int:
        """Insert rows (dicts keyed by column). conflict: 'nothing' | 'replace' | None."""
        if not rows:
            return 0
        types = self.col_types(table)
        if cols is None:
            seen: dict[str, None] = {}
            for r in rows:
                for k in r:
                    if k in types:
                        seen[k] = None
            cols = list(seen)
        path, n = self._write_tmp(rows, cols, types)
        try:
            clause = {"nothing": " ON CONFLICT DO NOTHING", "replace": "", None: ""}[conflict]
            verb = "INSERT OR REPLACE INTO" if conflict == "replace" else "INSERT INTO"
            collist = ", ".join(cols)
            with self.lock:
                self.con.execute(f"{verb} {table} ({collist}) SELECT {collist} FROM "
                                 f"{self._read_json_expr(path, cols, types)}{clause}")
        finally:
            os.unlink(path)
        return n

    def bulk_update(self, table: str, keys: list[str], rows: list[dict], where: str = "",
                    params: list | None = None) -> int:
        """UPDATE table SET <non-key cols> FROM rows WHERE keys match (and optional extra predicate)."""
        if not rows:
            return 0
        types = self.col_types(table)
        cols = [c for c in rows[0] if c in types]
        path, n = self._write_tmp(rows, cols, types)
        try:
            sets = ", ".join(f"{c} = u.{c}" for c in cols if c not in keys)
            cond = " AND ".join(f"{table}.{k} = u.{k}" for k in keys)
            if where:
                cond += f" AND {where}"
            with self.lock:
                self.con.execute(f"UPDATE {table} SET {sets} FROM {self._read_json_expr(path, cols, types)} u "
                                 f"WHERE {cond}", params or [])
        finally:
            os.unlink(path)
        return n


_ENC = json.JSONEncoder(ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":"))


def _clean_row(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            v = None
        out[k] = v
    return out


def open_db(cfg, check_schema: bool = True, setup: bool = True) -> Database:
    db = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    if setup:
        db.setup(check_schema=check_schema)
    return db
