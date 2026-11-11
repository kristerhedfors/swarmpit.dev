"""File ingester (incremental, idempotent; CONTRACT section 3.1, 3.2, 3.6), seeds, pilot.log, live-chunk
application + reconcile (section 3.4), and the single-writer IngestWorker thread (section 3.7).

Never writes under runs_dir: every open is "rb", only stat/listdir otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import derive as D
from .db import Database, jdump
from .parse import (DEFAULT_GOAL, VM_RE, canonical_json, merge_rows, norm_board_record, norm_relay_record,
                    norm_steps_record, norm_submission_line, parse_pilot_line, parse_run_id, run_goal, sha1_hex,
                    split_lines, vm_agent_id)

log = logging.getLogger("obs.ingest")

AGENT_DIR_RE = re.compile(r"^a(\d+)$")
CFG_RE = re.compile(r"^cfg-a(\d+)\.json$")
IMAGE = "python:3.12-bookworm"
PENDING_ABANDON_S = 3600
COLLECT_GRACE_S = 300
DIAG_MAX = 64 * 1024

# stream -> [(table, extra predicate)] used for resets / reconcile
STREAM_TABLES = {
    "steps": [("agent_records", ""), ("model_calls", ""), ("tool_calls", ""), ("steps", ""),
              ("submissions", "line_off < 0")],
    "board": [("board_events", "")],
    "relay": [("relay_events", "")],
    "subm": [("submissions", "line_off >= 0")],
}
AGENT_SCOPED = {"steps", "relay", "subm"}


def _ref(table: str, r: dict) -> str | None:
    if table == "steps":
        return f"steps:{r['agent_id']}:{r['line_off']}"
    if table == "model_calls":
        return f"model:{r['agent_id']}:{r['line_off']}"
    if table == "agent_records":
        return f"rec:{r['agent_id']}:{r['line_off']}"
    if table == "board_events":
        return f"board:{r['line_off']}"
    if table == "relay_events" and r.get("ev") != "data":
        return f"relay:{r['agent_id']}:{r['line_off']}"
    return None


def read_range(path: Path, start: int, end: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(max(0, end - start))


def line_at(path: Path, off: int) -> bytes | None:
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.readline()
    except OSError:
        return None
    return data[:-1] if data.endswith(b"\n") else None


def _looks_like_run(d: Path) -> bool:
    try:
        return any(CFG_RE.match(p.name) or p.name in ("run.json", "board_events.jsonl") or
                   (AGENT_DIR_RE.match(p.name) and p.is_dir()) for p in d.iterdir())
    except OSError:
        return False


@dataclass
class LiveChunk:
    run_id: str
    vm: str
    agent_id: int  # -1 for board VM
    stream: str  # steps|relay|subm|board|summary|diag:board.out
    guest_path: str
    epoch: str
    size: int  # -1 = file missing
    off: int
    data: bytes
    done: bool = False


@dataclass
class TickResult:
    changed: set = field(default_factory=set)
    events: list = field(default_factory=list)  # (event, payload) to broadcast


class Ingester:
    def __init__(self, db: Database, cfg, emit: Callable[[str, dict], None] | None = None):
        self.db = db
        self.cfg = cfg
        self.emit_cb = emit
        self.live_vms: dict[str, set[str]] = {}
        self.pending_refs: dict[str, set] = defaultdict(set)
        self.emitted_det: dict[str, set] = defaultdict(set)
        self.quiet = False
        self.last_ingest_ts: float | None = None
        self._flags: dict[str, str | None] = {}
        self._notes: dict[str, list] = defaultdict(list)
        self._pending_emits: list = []
        self._reconciled: dict[str, str] = {}
        self._fp: dict[str, tuple] = {}

    # ------------------------------------------------------------------ small helpers
    def emit(self, event: str, payload: dict):
        if self.quiet or self.emit_cb is None:
            return
        self._pending_emits.append((event, payload))

    def flush_emits(self):
        items, self._pending_emits = self._pending_emits, []
        if self.emit_cb:
            for ev, p in items:
                try:
                    self.emit_cb(ev, p)
                except Exception:  # never let SSE break ingest
                    log.exception("emit failed")

    def get_state(self, run_id, stream, agent_id, source) -> dict | None:
        r = self.db.con.execute(
            "SELECT * FROM ingest_state WHERE run_id=? AND stream=? AND agent_id=? AND source=?",
            [run_id, stream, agent_id, source])
        cols = [d[0] for d in r.description]
        row = r.fetchone()
        return dict(zip(cols, row)) if row else None

    def put_state(self, **kw):
        kw.setdefault("updated", time.time())
        self.db.bulk_insert("ingest_state", [kw], conflict="replace")

    def run_row(self, run_id) -> dict | None:
        r = self.db.con.execute("SELECT * FROM runs WHERE run_id = ?", [run_id])
        cols = [d[0] for d in r.description]
        row = r.fetchone()
        return dict(zip(cols, row)) if row else None

    def flag_for(self, run_id) -> str | None:
        if run_id not in self._flags or self._flags[run_id] is None:
            r = self.db.con.execute("SELECT ctf->>'flag' FROM runs WHERE run_id = ?", [run_id]).fetchone()
            self._flags[run_id] = r[0] if r else None
        return self._flags[run_id]

    def note(self, run_id, text):
        if text not in self._notes[run_id]:
            self._notes[run_id].append(text)

    def _rollback_emits(self, mark: int):
        """Drop SSE emits queued inside a transaction that rolled back (e.g. a phantom run_started)."""
        del self._pending_emits[mark:]

    def record_error(self, run_id, err: str):
        """Persist an ingest error outside the failed transaction (its runs row may have been rolled back)."""
        try:
            with self.db.lock:
                self.db.con.execute("DELETE FROM ingest_errors WHERE run_id = ?", [run_id])
                self.db.con.execute("INSERT INTO ingest_errors VALUES (?, ?, ?)", [run_id, time.time(), err[:500]])
        except Exception:
            log.exception("could not record ingest error")

    def touch(self, run_id):
        self.db.con.execute("UPDATE runs SET dirty = TRUE, data_version = data_version + 1, updated = ? "
                            "WHERE run_id = ?", [time.time(), run_id])

    def _flush_notes(self, run_id):
        new = self._notes.pop(run_id, [])
        if not new:
            return
        r = self.db.con.execute("SELECT notes FROM runs WHERE run_id = ?", [run_id]).fetchone()
        cur = json.loads(r[0]) if r and r[0] else []
        merged = list(dict.fromkeys(cur + new))[:200]
        self.db.con.execute("UPDATE runs SET notes = ? WHERE run_id = ?", [jdump(merged), run_id])

    # ------------------------------------------------------------------ runs + seeds
    def ensure_run(self, run_id: str, source_dir: Path | None, source: str = "files") -> bool:
        if self.run_row(run_id):
            return False
        p = parse_run_id(run_id)
        now = time.time()
        row = {"run_id": run_id, "experiment_id": self.cfg.experiment, "iface": p["iface"], "pop": p["pop"],
               "n_agents": p["n_agents"], "rep": p["rep"], "tag": D.run_tag(run_id, None, p["tag"]), "status": "pending",
               "source_dir": str(source_dir) if source_dir else None, "first_seen": now, "updated": now,
               "params": {"image": IMAGE, **({"goal": p["goal"]} if p["goal"] not in (None, DEFAULT_GOAL) else {})},
               "notes": [] if p["matched"] else ["run_id does not match grammar"],
               "dirty": True}
        self.db.bulk_insert("runs", [row])
        self.snapshot_seed(run_id, source_dir)
        self.emit("run_started", {"run_id": run_id, "iface": p["iface"], "n_agents": p["n_agents"], "model": None,
                                  "goal": p["goal"] or DEFAULT_GOAL,
                                  "t_origin": None, "source": source})
        return True

    def snapshot_seed(self, run_id: str, source_dir: Path | None):
        path, inferred = None, True
        if source_dir and (source_dir / "seed.json").is_file():
            path, inferred = source_dir / "seed.json", False
        elif self.cfg.seed_file.is_file():
            path = self.cfg.seed_file
        if path is None:
            return
        try:
            with open(path, "rb") as f:
                content = json.loads(f.read())
        except (OSError, ValueError):
            self.note(run_id, f"seed unreadable: {path}")
            return
        h = sha1_hex(canonical_json(content))[:16]
        if not self.db.con.execute("SELECT 1 FROM seeds WHERE seed_hash = ?", [h]).fetchone():
            self.db.bulk_insert("seeds", [{"seed_hash": h, "source": str(path), "inferred": inferred,
                                           "board_name": content.get("board_name"), "content": content}])
            rows, pid = [], 0
            for t in content.get("threads", []):
                for p in t.get("posts", []):
                    pid += 1
                    ago = int(p.get("ago_min") or 0)
                    rows.append({"seed_hash": h, "post_id": pid, "thread": t.get("slug"), "thread_title": t.get("title"),
                                 "author": p.get("author"), "ago_min": ago, "upvotes0": max(1, 12 - ago // 40),
                                 "body": p.get("body")})
            self.db.bulk_insert("seed_posts", rows)
        r = self.run_row(run_id)
        params = json.loads(r["params"]) if r and r["params"] else {}
        params.update({"seed_hash": h, "seed_inferred": inferred})
        self.db.con.execute("UPDATE runs SET seed_hash = ?, params = ? WHERE run_id = ?", [h, jdump(params), run_id])

    def set_status(self, run_id: str, status: str, extra: dict | None = None):
        r = self.run_row(run_id)
        if r is None or r["status"] == status:
            return
        self.db.con.execute("UPDATE runs SET status = ?, updated = ?, data_version = data_version + 1 "
                            "WHERE run_id = ?", [status, time.time(), run_id])
        D.refresh_run_status_in_mat(self.db, run_id)
        payload = {"run_id": run_id, "status": status, "t_origin": r["t_origin"],
                   "data_version": (r["data_version"] or 0) + 1, **(extra or {})}
        self.emit("run_finished" if status == "done" else "run_updated", payload)

    # ------------------------------------------------------------------ whole-file JSON
    def _whole(self, run_id, stream, agent_id, path: Path, source="file") -> bytes | None:
        try:
            st = path.stat()
        except OSError:
            return None
        s = self.get_state(run_id, stream, agent_id, source)
        if s and s["size"] == st.st_size and s["mtime"] == st.st_mtime:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        self.put_state(run_id=run_id, stream=stream, agent_id=agent_id, source=source, path=str(path),
                       epoch=str(st.st_ino), size=st.st_size, mtime=st.st_mtime, byte_off=len(data), lines=0)
        return data

    def apply_cfg(self, run_id, agent_id, data: bytes):
        try:
            c = json.loads(data)
        except ValueError:
            self.note(run_id, f"cfg-a{agent_id}.json unparseable")
            return
        harness = c.get("harness") or {}
        model = harness.get("model")
        self.db.bulk_insert("agents", [{"run_id": run_id, "agent_id": agent_id, "model": model, "cfg": c,
                                        "status": "pending"}])
        self.db.con.execute("UPDATE agents SET cfg = ?, model = coalesce(model, ?) WHERE run_id = ? AND agent_id = ?",
                            [jdump(c), model, run_id, agent_id])
        r = self.run_row(run_id)
        params = json.loads(r["params"]) if r["params"] else {}
        for k in ("board_url", "board_port"):
            if k in c:
                params[k] = c[k]
        n_cfg = self.db.con.execute("SELECT count(*) FROM agents WHERE run_id = ?", [run_id]).fetchone()[0]
        self.db.con.execute(
            "UPDATE runs SET harness = coalesce(harness, ?), ctf = coalesce(ctf, ?), model = coalesce(model, ?), "
            "n_agents = greatest(coalesce(n_agents, 0), ?), params = ? WHERE run_id = ?",
            [jdump(harness), jdump(c.get("ctf") or {}), model, n_cfg, jdump(params), run_id])
        self._flags.pop(run_id, None)

    def apply_run_json(self, run_id, data: bytes):
        try:
            m = json.loads(data)
        except ValueError:
            self.note(run_id, "run.json unparseable")
            return
        p = parse_run_id(run_id)
        r = self.run_row(run_id)
        params = json.loads(r["params"]) if r["params"] else {}
        extra = m.get("params") or {}
        params.update(extra)
        # the run id's goal marker is what the live view knows before run.json exists; run.json is authoritative
        # (its params.goal alone: absent = solvable, even when the id-derived goal was registered before)
        goal = run_goal(extra)
        if goal == DEFAULT_GOAL:
            params.pop("goal", None)
        else:
            params["goal"] = goal
        if p["matched"] and goal != p["goal"]:
            self.note(run_id, f"goal mismatch: run id says {p['goal']}, run.json says {goal}")
        if extra:
            params["_explicit"] = sorted(set(params.get("_explicit", [])) | set(extra))
        n = m.get("n_agents") or p["n_agents"]
        harness = m.get("harness") or {}
        timed = [vm_agent_id(x) if isinstance(x, str) else x for x in (m.get("timed_out") or [])]
        timed = [t for t in timed if t is not None]
        # same rule as derive_run, applied here too so a browser-lab run's tag never passes through '' (a failed
        # derive after this would otherwise leave it pooled in the default roll-ups)
        tag = D.run_tag(run_id, params, m.get("tag", p["tag"]) or "")
        self.db.con.execute(
            "UPDATE runs SET experiment_id = ?, iface = ?, n_agents = ?, pop = ?, rep = ?, tag = ?, model = ?, "
            "harness = ?, ctf = ?, params = ?, started = ?, finished = ?, t_origin = coalesce(?, t_origin), "
            "t_origin_final = (? IS NOT NULL), timed_out = ? WHERE run_id = ?",
            [extra.get("experiment") or r["experiment_id"] or self.cfg.experiment, m.get("iface") or p["iface"], n,
             None if n is None else ("solo" if n == 1 else "multi"), m.get("rep", p["rep"]), tag,
             harness.get("model") or m.get("model"), jdump(harness), jdump(m.get("ctf") or {}), jdump(params),
             m.get("started"), m.get("finished"), m.get("started"), m.get("started"), jdump(timed), run_id])
        self._flags.pop(run_id, None)

    def apply_summary(self, run_id, agent_id, data: bytes):
        try:
            s = json.loads(data)
        except ValueError:
            self.note(run_id, f"a{agent_id}/summary.json unparseable")
            return
        self.db.bulk_insert("agents", [{"run_id": run_id, "agent_id": agent_id, "status": "pending"}])
        self.db.con.execute("UPDATE agents SET summary = ? WHERE run_id = ? AND agent_id = ?",
                            [jdump(s), run_id, agent_id])

    def apply_diag(self, run_id, agent_id, name, path: Path | None = None, data: bytes | None = None, size=None):
        if data is None:
            try:
                size = path.stat().st_size
                with open(path, "rb") as f:
                    data = f.read(DIAG_MAX)
            except OSError:
                return
        self.db.bulk_insert("diag_files", [{"run_id": run_id, "agent_id": agent_id, "name": name,
                                            "size": size if size is not None else len(data),
                                            "content": data[:DIAG_MAX].decode("utf-8", "replace")}], conflict="replace")

    # ------------------------------------------------------------------ JSONL
    def consume(self, run_id, stream, agent_id, buf: bytes, base_off: int, base_lines: int, src: str):
        lines, consumed = split_lines(buf, base_off)
        rows: dict[str, list[dict]] = {}
        flag = self.flag_for(run_id)
        for k, (off, raw) in enumerate(lines):
            seq = base_lines + k
            if stream == "subm":
                text = raw.decode("utf-8", "replace")
                if text.strip():
                    merge_rows(rows, norm_submission_line(text, run_id, agent_id, off, src, flag))
                continue
            try:
                rec = json.loads(raw)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except ValueError:
                if raw.strip():
                    self.note(run_id, f"parse error {stream} a{agent_id} @{off}: {raw[:200].decode('utf-8', 'replace')}")
                continue
            try:
                if stream == "steps":
                    new = norm_steps_record(rec, run_id, agent_id, off, seq, src, flag)
                elif stream == "board":
                    new = norm_board_record(rec, run_id, off, seq, src)
                elif stream == "relay":
                    new = norm_relay_record(rec, run_id, agent_id, off, seq, src)
                else:
                    new = {}
            except Exception as e:  # malformed record shape (e.g. another provider's usage/tool_calls): skip line
                self.note(run_id, f"record error {stream} a{agent_id} @{off}: {e!r}"[:300])
                continue
            merge_rows(rows, new)
        last_off = lines[-1][0] if lines else None
        last_sha = sha1_hex(lines[-1][1]) if lines else None
        return rows, consumed, len(lines), last_off, last_sha

    def insert_rows(self, run_id, rows: dict[str, list[dict]]):
        for table in ("agent_records", "model_calls", "tool_calls", "steps", "submissions", "board_events",
                      "relay_events"):
            rs = rows.get(table)
            if not rs:
                continue
            self.db.bulk_insert(table, rs)
            if not self.quiet:
                for r in rs:
                    ref = _ref(table, r)
                    if ref:
                        self.pending_refs[run_id].add(ref)

    def delete_stream(self, run_id, stream, agent_id, only_src: str | None = None):
        for table, pred in STREAM_TABLES.get(stream, []):
            sql = f"DELETE FROM {table} WHERE run_id = ?"
            params = [run_id]
            if stream in AGENT_SCOPED:
                sql += " AND agent_id = ?"
                params.append(agent_id)
            if pred:
                sql += f" AND {pred}"
            if only_src and table != "tool_calls":
                sql += " AND src = ?"
                params.append(only_src)
            self.db.con.execute(sql, params)

    def ingest_jsonl_file(self, run_id, stream, agent_id, path: Path, source="file", src="final",
                          final: bool = False) -> int:
        """final=True: the writer is done (run finished/errored), so an unterminated last line is a complete
        record or a truncated one, never a line still being written: parse it if possible, else note it."""
        try:
            st = path.stat()
        except OSError:
            return 0
        epoch = str(st.st_ino)
        s = self.get_state(run_id, stream, agent_id, source)
        if s is None and source == "file":
            s = self.reconcile(run_id, stream, agent_id, path, st, epoch)
        if s and s["size"] == st.st_size and s["mtime"] == st.st_mtime and s["epoch"] == epoch:
            return 0
        if s:
            reset = s["epoch"] != epoch or st.st_size < s["byte_off"]
            if not reset and s["byte_off"] and s["last_line_off"] is not None:
                ln = line_at(path, s["last_line_off"])
                reset = ln is None or sha1_hex(ln) != s["last_line_sha1"]
            if reset:
                self.note(run_id, f"{stream} a{agent_id}: file truncated/replaced, re-ingested")
                self.delete_stream(run_id, stream, agent_id)
                s = None
        off = s["byte_off"] if s else 0
        nlines = s["lines"] if s else 0
        buf = read_range(path, off, st.st_size)
        rows, consumed, n, last_off, last_sha = self.consume(run_id, stream, agent_id, buf, off, nlines, src)
        tail = buf[consumed:]
        if final and tail.strip():
            t_rows, _c, t_n, _lo, _ls = self.consume(run_id, stream, agent_id, tail + b"\n", off + consumed,
                                                     nlines + n, src)
            if t_n and any(t_rows.values()):
                merge_rows(rows, t_rows)
                consumed += len(tail)
                n += t_n  # last_line_off/sha stay on the last newline-terminated line (line_at needs a newline)
            else:
                self.note(run_id, f"{stream} a{agent_id}: {len(tail)} trailing bytes without newline unparsed")
        self.insert_rows(run_id, rows)
        self.put_state(run_id=run_id, stream=stream, agent_id=agent_id, source=source, path=str(path), epoch=epoch,
                       size=st.st_size, mtime=st.st_mtime, byte_off=off + consumed, lines=nlines + n,
                       last_line_off=last_off if last_off is not None else (s or {}).get("last_line_off"),
                       last_line_sha1=last_sha if last_sha is not None else (s or {}).get("last_line_sha1"))
        return n or (1 if not s else 0)

    def reconcile(self, run_id, stream, agent_id, path: Path, st, epoch) -> dict | None:
        """First file ingest of a stream that was live-tapped: reuse live progress when the host copy is a
        byte-exact superset, else drop the live rows and ingest from 0 (CONTRACT 3.4 Reconcile)."""
        L = self.get_state(run_id, stream, agent_id, "live")
        if L is None:
            return None
        ok = st.st_size >= (L["byte_off"] or 0)
        if ok and L["byte_off"] and L["last_line_off"] is not None:
            ln = line_at(path, L["last_line_off"])
            ok = ln is not None and sha1_hex(ln) == L["last_line_sha1"]
        if ok:
            self.db.con.execute("UPDATE ingest_state SET source = 'live-reconciled' WHERE run_id=? AND stream=? "
                                "AND agent_id=? AND source='live'", [run_id, stream, agent_id])
            for table, pred in STREAM_TABLES.get(stream, []):
                if table == "tool_calls":
                    continue
                sql = f"UPDATE {table} SET src = 'final' WHERE run_id = ? AND src = 'live'"
                params = [run_id]
                if stream in AGENT_SCOPED:
                    sql += " AND agent_id = ?"
                    params.append(agent_id)
                if pred:
                    sql += f" AND {pred}"
                self.db.con.execute(sql, params)
            s = {"run_id": run_id, "stream": stream, "agent_id": agent_id, "source": "file", "path": str(path),
                 "epoch": epoch, "size": -1, "mtime": -1.0, "byte_off": L["byte_off"], "lines": L["lines"],
                 "last_line_off": L["last_line_off"], "last_line_sha1": L["last_line_sha1"]}
            self.put_state(**s)
            self._reconciled[run_id] = "superset"
            return s
        self.note(run_id, f"{stream} a{agent_id}: host copy differs from live tap; re-ingested from file")
        self.delete_stream(run_id, stream, agent_id, only_src="live")
        self.db.con.execute("UPDATE ingest_state SET source = 'live-discarded' WHERE run_id=? AND stream=? "
                            "AND agent_id=? AND source='live'", [run_id, stream, agent_id])
        self._reconciled[run_id] = "replaced"
        return None

    # ------------------------------------------------------------------ run directory scan
    def scan(self, only: set[str] | None = None) -> TickResult:
        res = TickResult()
        rd = self.cfg.runs_dir
        if not rd.is_dir():
            return res
        self._reconciled = {}
        try:
            self.ingest_pilot_log(res)
        except Exception:
            log.exception("pilot.log ingest failed")
        seen = set()
        dirs = self.discover_run_dirs()
        for rid, d, archive in dirs:
            if only and rid not in only:
                continue
            seen.add(rid)
            try:
                if self.ingest_run_dir(d, rid, archive):
                    res.changed.add(rid)
            except Exception as e:
                log.exception("ingest of %s failed", rid)
                try:
                    self.db.con.execute("ROLLBACK")
                except Exception:
                    pass
                self.note(rid, f"ingest error: {e!r}"[:300])
                self.record_error(rid, f"ingest error: {e!r}")
        try:
            self.attribute_pilot_markers(dirs)
        except Exception:
            log.exception("pilot marker attribution failed")
        try:
            res.changed |= self.ingest_mirrors(only)
        except Exception:
            log.exception("mirror ingest failed")
        if not only:
            for (rid, sdir, status, lt) in self.db.con.execute(
                    "SELECT run_id, source_dir, status, live_tapped FROM runs WHERE run_id <> '_pilot'").fetchall():
                if rid not in seen and status != "missing" and rid not in self.live_vms and not lt:
                    self.set_status(rid, "missing")
        self.last_ingest_ts = time.time()
        return res

    def discover_run_dirs(self) -> list[tuple[str, Path, str | None]]:
        """Top-level run dirs, plus run dirs nested one level inside '_<archive>' dirs (e.g. runs the lab
        moved aside as invalid). Archived runs get run_id '<name>-<archive>' so the archive becomes the tag."""
        out = []
        for d in sorted(self.cfg.runs_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name.startswith("_") and not _looks_like_run(d):
                arch = d.name.strip("_")
                for sub in sorted(d.iterdir()):
                    if sub.is_dir() and not sub.name.startswith(".") and _looks_like_run(sub):
                        out.append((f"{sub.name}-{arch}", sub, d.name))
                continue
            out.append((d.name, d, None))
        return out

    def _fingerprint(self, d: Path):
        out = []
        try:
            for e in os.scandir(d):
                st = e.stat()
                out.append((e.name, st.st_size, st.st_mtime_ns))
                if e.is_dir() and AGENT_DIR_RE.match(e.name):
                    for e2 in os.scandir(e.path):
                        st2 = e2.stat()
                        out.append((e.name + "/" + e2.name, st2.st_size, st2.st_mtime_ns))
        except OSError:
            return None
        return tuple(sorted(out))

    def _run_files(self, d: Path):
        cfgs, adirs, files = {}, {}, []
        for p in d.iterdir():
            m = CFG_RE.match(p.name)
            if m:
                cfgs[int(m.group(1))] = p
            m = AGENT_DIR_RE.match(p.name)
            if m and p.is_dir():
                adirs[int(m.group(1))] = p
                files.extend(p.iterdir())
            files.append(p)
        newest = 0.0
        for p in files:
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
        return cfgs, adirs, newest

    def ingest_run_dir(self, d: Path, rid: str | None = None, archive: str | None = None) -> bool:
        rid = rid or d.name
        # fast path: a finished run whose files did not change since the last successful pass needs no DB work
        fp = self._fingerprint(d)
        hit = self._fp.get(rid)
        if fp is not None and hit is not None and hit == (fp, "done") and rid not in self.live_vms:
            return False
        changed = self._ingest_run_dir(d, rid, archive)
        st = self.db.con.execute("SELECT status FROM runs WHERE run_id = ?", [rid]).fetchone()
        if fp is not None and st:
            self._fp[rid] = (fp, st[0])
        return changed

    def _ingest_run_dir(self, d: Path, rid: str, archive: str | None) -> bool:
        cfgs, adirs, newest = self._run_files(d)
        has_run = (d / "run.json").is_file()
        has_logs = bool(adirs) or (d / "board_events.jsonl").exists()
        live = rid in self.live_vms
        now = time.time()
        err_marker = self.db.con.execute(
            "SELECT 1 FROM run_markers WHERE run_id = ? AND kind IN ('error', 'abort') AND NOT coalesce(superseded, FALSE) "
            "LIMIT 1",
            [rid]).fetchone() is not None
        if has_run:
            status = "done"
        elif live:
            status = "collecting" if has_logs else "live"
        elif has_logs:
            status = "collecting" if (now - newest) < COLLECT_GRACE_S and not err_marker else "errored"
        elif err_marker:
            status = "errored"
        else:
            status = "pending" if (now - newest) < PENDING_ABANDON_S else "abandoned"

        changed = False
        con = self.db.con
        mark = len(self._pending_emits)
        with self.db.lock:
            con.execute("BEGIN TRANSACTION")
            try:
                created = self.ensure_run(rid, d)
                if created and archive:
                    r = self.run_row(rid)
                    params = json.loads(r["params"]) if r["params"] else {}
                    params["archive"] = archive
                    con.execute("UPDATE runs SET params = ? WHERE run_id = ?", [jdump(params), rid])
                changed |= created
                for aid, p in sorted(cfgs.items()):
                    data = self._whole(rid, "cfg", aid, p)
                    if data is not None:
                        self.apply_cfg(rid, aid, data)
                        changed = True
                if status in ("done", "errored"):
                    if has_run:
                        data = self._whole(rid, "run", -1, d / "run.json")
                        if data is not None:
                            self.apply_run_json(rid, data)
                            changed = True
                    elif status == "errored":
                        self.note(rid, "no run.json")
                    seedp = d / "seed.json"
                    if seedp.is_file() and self._whole(rid, "seed", -1, seedp) is not None:
                        self.snapshot_seed(rid, d)
                        changed = True
                    bp = d / "board_events.jsonl"
                    if bp.is_file() and self.ingest_jsonl_file(rid, "board", -1, bp, final=True):
                        changed = True
                    for name in ("board.out",):
                        p = d / name
                        if p.is_file() and self._whole(rid, f"diag:{name}", -1, p) is not None:
                            self.apply_diag(rid, -1, name, p)
                            changed = True
                    for aid, ad in sorted(adirs.items()):
                        con.execute("INSERT INTO agents (run_id, agent_id, status) VALUES (?, ?, 'pending') "
                                    "ON CONFLICT DO NOTHING", [rid, aid])
                        for stream, fname in (("steps", "steps.jsonl"), ("relay", "relay.jsonl")):
                            p = ad / fname
                            if p.is_file() and self.ingest_jsonl_file(rid, stream, aid, p, final=True):
                                changed = True
                        if has_run and (ad / "steps.jsonl").is_file():
                            # /var/tmp/submissions rows (line_off >= 0) are live-only provisional copies of what
                            # steps.jsonl records per step; drop them once the final steps stream is ingested.
                            n_live = con.execute("SELECT count(*) FROM submissions WHERE run_id = ? AND agent_id = ? "
                                                 "AND line_off >= 0", [rid, aid]).fetchone()[0]
                            if n_live:
                                con.execute("DELETE FROM submissions WHERE run_id = ? AND agent_id = ? AND line_off >= 0",
                                            [rid, aid])
                                changed = True
                        p = ad / "summary.json"
                        if p.is_file():
                            data = self._whole(rid, "summary", aid, p)
                            if data is not None:
                                self.apply_summary(rid, aid, data)
                                changed = True
                        for name in ("harness.out", "vaultd.out"):
                            p = ad / name
                            if p.is_file() and self._whole(rid, f"diag:{name}", aid, p) is not None:
                                self.apply_diag(rid, aid, name, p)
                                changed = True
                    if has_run:
                        lr = con.execute("SELECT count(*) FROM ingest_state WHERE run_id = ? AND source = 'live'",
                                         [rid]).fetchone()[0]
                        lt = con.execute("SELECT live_tapped FROM runs WHERE run_id = ?", [rid]).fetchone()[0]
                        if lt and lr and not adirs and not (d / "board_events.jsonl").exists():
                            con.execute("UPDATE runs SET final_missing = TRUE WHERE run_id = ?", [rid])
                if changed:
                    self.touch(rid)
                self._flush_notes(rid)
                prev = self.run_row(rid)["status"]
                if prev != status:
                    extra = {}
                    if status == "done":
                        r = self.run_row(rid)
                        extra = {"finished": r["finished"], "reconciled": self._reconciled.get(rid)}
                    self.set_status(rid, status, extra)
                    changed = True
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                self._rollback_emits(mark)
                raise
        with self.db.lock:
            con.execute("DELETE FROM ingest_errors WHERE run_id = ?", [rid])
        return changed

    # ------------------------------------------------------------------ live-tap mirrors (rebuild source)
    MIRROR_STREAMS = {"steps.jsonl": "steps", "relay.jsonl": "relay", "submissions": "subm", "events.jsonl": "board"}
    HOST_FILES = {"steps": "a{i}/steps.jsonl", "relay": "a{i}/relay.jsonl", "board": "board_events.jsonl"}

    def ingest_mirrors(self, only: set | None = None) -> set:
        """dash/data/live/<run>/<vm>/<file> are byte-exact copies of live-tapped guest logs. They are the
        source of truth for streams whose host copy never arrived (final_missing), e.g. after --rebuild."""
        changed = set()
        root = self.cfg.live_dir
        if not root.is_dir():
            return changed
        for rd in sorted(p for p in root.iterdir() if p.is_dir()):
            rid = rd.name
            if (only and rid not in only) or rid in self.live_vms:
                continue
            host = self.cfg.runs_dir / rid
            todo = []
            for vmd in sorted(p for p in rd.iterdir() if p.is_dir()):
                m = VM_RE.match(vmd.name)
                if not m:
                    continue
                aid = -1 if m["role"] == "board" else int(m["idx"])
                for f in vmd.iterdir():
                    stream = self.MIRROR_STREAMS.get(f.name)
                    if not stream:
                        continue
                    hf = self.HOST_FILES.get(stream)
                    if hf and (host / hf.format(i=aid)).is_file() and (host / "run.json").is_file():
                        continue  # the final copy exists: file ingest owns this stream
                    if stream == "subm" and (host / "run.json").is_file():
                        continue
                    todo.append((stream, aid, f))
            if not todo:
                continue
            mark = len(self._pending_emits)
            with self.db.lock:
                con = self.db.con
                con.execute("BEGIN TRANSACTION")
                try:
                    created = self.ensure_run(rid, host if host.is_dir() else None, source="mirror")
                    any_new = created
                    for stream, aid, f in todo:
                        if self.get_state(rid, stream, aid, "live") and not self.get_state(rid, stream, aid, "mirror"):
                            # rows already present from this process' live tap: adopt its progress
                            L = self.get_state(rid, stream, aid, "live")
                            self.put_state(**{**L, "source": "mirror", "path": str(f), "size": -1, "mtime": -1.0,
                                              "epoch": str(f.stat().st_ino)})
                        if self.ingest_jsonl_file(rid, stream, aid, f, source="mirror", src="mirror"):
                            any_new = True
                    if any_new:
                        con.execute("UPDATE runs SET live_tapped = TRUE, final_missing = ? WHERE run_id = ?",
                                    [not (host / "run.json").is_file() or not any(host.glob("a*/steps.jsonl")), rid])
                        r = self.run_row(rid)
                        if r["status"] in ("pending", "live", "collecting", "missing") and not host.is_dir():
                            self.note(rid, "final copy missing; rows rebuilt from live-tap mirror")
                            self.set_status(rid, "errored")
                        self.touch(rid)
                        changed.add(rid)
                    self._flush_notes(rid)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    self._rollback_emits(mark)
                    raise
        return changed

    # ------------------------------------------------------------------ pilot.log
    def ingest_pilot_log(self, res: TickResult):
        for p in sorted(self.cfg.runs_dir.glob("pilot*.log")):
            if p.is_file():
                self._ingest_one_pilot_log(p)

    def _ingest_one_pilot_log(self, p: Path):
        st = p.stat()
        stream = "pilotlog" if p.name == "pilot.log" else f"pilotlog:{p.name}"
        s = self.get_state("_pilot", stream, -1, "file")
        if s and s["size"] == st.st_size and s["mtime"] == st.st_mtime:
            return
        first = s is None
        if s and (s["epoch"] != str(st.st_ino) or st.st_size < s["byte_off"]):
            self.db.con.execute("DELETE FROM run_markers WHERE source = ?", [p.name])
            s, first = None, False
        off = s["byte_off"] if s else 0
        nl = s["lines"] if s else 0
        buf = read_range(p, off, st.st_size)
        lines, consumed = split_lines(buf, off)
        now = time.time()
        rows, runs = [], set()
        for line_off, raw in lines:
            m = parse_pilot_line(raw.decode("utf-8", "replace"))
            if not m:
                continue
            rows.append({"marker_id": f"{p.name}:{line_off}", "run_id": m["run_id"], "source": p.name,
                         "kind": m["kind"], "agent_id": m["agent_id"], "ts": None if first else now,
                         "detail": m["detail"], "data": m["data"], "log_run_id": m["run_id"], "superseded": False})
            runs.add(m["run_id"])
        with self.db.lock:
            self.db.con.execute("BEGIN TRANSACTION")
            try:
                self.db.bulk_insert("run_markers", rows)
                self.put_state(run_id="_pilot", stream=stream, agent_id=-1, source="file", path=str(p),
                               epoch=str(st.st_ino), size=st.st_size, mtime=st.st_mtime, byte_off=off + consumed,
                               lines=nl + len(lines), last_line_off=lines[-1][0] if lines else None,
                               last_line_sha1=sha1_hex(lines[-1][1]) if lines else None)
                for rid in runs:
                    self.db.con.execute("UPDATE runs SET dirty = TRUE WHERE run_id = ?", [rid])
                self.db.con.execute("COMMIT")
            except Exception:
                self.db.con.execute("ROLLBACK")
                raise
        for r in rows:
            if not self.quiet:
                self.emit("event", {"run_id": r["run_id"], "t": None, "lane": r["agent_id"] if r["agent_id"] is not None else -1,
                                    "kind": f"marker_{r['kind']}", "label": r["detail"] or r["kind"],
                                    "ref": r["marker_id"], "src": p.name})

    def attribute_pilot_markers(self, dirs: list[tuple[str, Path, str | None]]):
        """Map pilot*.log marker blocks to the run dir (attempt) they describe. A run id can have several
        attempts: e.g. the broken first IRC pass (archived as '<id>-invalid_irc_lf') and the rerun ('<id>').
        A block = one 'starting' .. next 'starting' of that id within one log. Blocks whose 'finished in Ns'
        matches a candidate dir's run.json (finished - started, +-5 s) go to that dir; the latest remaining
        block goes to '<id>' while it has no run.json (in progress); everything else is superseded (kept,
        labelled, ignored by derive and status)."""
        cur = self.db.con
        rows = cur.execute("SELECT marker_id, run_id, source, kind, detail, coalesce(log_run_id, run_id), "
                           "coalesce(superseded, FALSE) FROM run_markers WHERE source LIKE 'pilot%.log'").fetchall()
        if not rows:
            return
        sig = (len(rows), tuple(sorted((rid, (d / "run.json").is_file()) for rid, d, _ in dirs)))
        if getattr(self, "_attr_sig", None) == sig:
            return
        cands: dict[str, list[tuple]] = defaultdict(list)  # log id -> [(run_id, duration, has_run_json, top_level)]
        for rid, d, archive in dirs:
            base = d.name
            dur = None
            rj = d / "run.json"
            if rj.is_file():
                try:
                    with open(rj, "rb") as f:
                        m = json.loads(f.read())
                    if m.get("started") is not None and m.get("finished") is not None:
                        dur = float(m["finished"]) - float(m["started"])
                except (OSError, ValueError, TypeError):
                    pass
            cands[base].append((rid, dur, rj.is_file(), archive is None))
        mtimes = {}
        for p in self.cfg.runs_dir.glob("pilot*.log"):
            try:
                mtimes[p.name] = p.stat().st_mtime
            except OSError:
                pass

        def off(mid):
            try:
                return int(mid.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                return 0
        blocks: dict[str, list[dict]] = defaultdict(list)
        open_blk: dict[tuple[str, str], dict] = {}
        for mid, _rid, src, kind, detail, lid, _sup in sorted(rows, key=lambda r: (mtimes.get(r[2], 0), r[2], off(r[0]))):
            key = (src, lid)
            if kind == "skip":
                blocks[lid].append({"ids": [mid], "dur": None, "skip": True, "order": (mtimes.get(src, 0), off(mid))})
                continue
            if kind == "starting" or key not in open_blk:
                b = {"ids": [], "dur": None, "skip": False, "order": (mtimes.get(src, 0), off(mid))}
                blocks[lid].append(b)
                open_blk[key] = b
            b = open_blk[key]
            b["ids"].append(mid)
            if kind == "finished":
                try:
                    b["dur"] = float(detail)
                except (TypeError, ValueError):
                    pass
        assign: dict[str, tuple[str, bool]] = {}
        for lid, bl in blocks.items():
            cs = cands.get(lid, [])
            live_id = next((c[0] for c in cs if c[3]), lid)  # the top-level dir (or the bare id if none yet)
            target: dict[int, tuple[str, bool]] = {}
            used = set()
            for i, b in enumerate(bl):
                if b["skip"]:
                    target[i] = (live_id, False)
                    continue
                if b["dur"] is None:
                    continue
                best = None
                for rid, dur, _has, _top in cs:
                    if dur is None or rid in used:
                        continue
                    if abs(dur - b["dur"]) <= 5 and (best is None or abs(dur - b["dur"]) < best[1]):
                        best = (rid, abs(dur - b["dur"]))
                if best:
                    used.add(best[0])
                    target[i] = (best[0], False)
            live_has_run = any(c[2] for c in cs if c[0] == live_id)
            rest = [i for i in range(len(bl)) if i not in target]
            if rest and live_id not in used and not live_has_run:
                last = max(rest, key=lambda i: bl[i]["order"])
                target[last] = (live_id, False)
            if not cs and rest:  # no run dir at all (yet): keep every block on the logged id
                for i in rest:
                    target[i] = (lid, False)
            for i, b in enumerate(bl):
                rid, sup = target.get(i, (lid, True))
                for mid in b["ids"]:
                    assign[mid] = (rid, sup)
        changes, dirty = [], set()
        for mid, rid, _src, _k, _d, lid, sup in rows:
            new = assign.get(mid, (rid, bool(sup)))
            if new != (rid, bool(sup)):
                changes.append({"marker_id": mid, "run_id": new[0], "superseded": new[1], "log_run_id": lid})
                dirty.update({rid, new[0]})
        if changes:
            with self.db.lock:
                self.db.bulk_update("run_markers", ["marker_id"], changes)
                for rid in dirty:
                    cur.execute("UPDATE runs SET dirty = TRUE WHERE run_id = ?", [rid])
        self._attr_sig = sig

    # ------------------------------------------------------------------ live tap
    def set_live_vms(self, vms_by_run: dict[str, set[str]]):
        """Called by the worker with the latest discovery result. Emits vm_seen/vm_gone markers."""
        now = time.time()
        old = self.live_vms
        rows = []
        for rid, vms in vms_by_run.items():
            for vm in vms - old.get(rid, set()):
                rows.append({"marker_id": f"livetap:{vm}:{now:.3f}:seen", "run_id": rid, "source": "livetap",
                             "kind": "vm_seen", "agent_id": vm_agent_id(vm), "ts": now, "detail": vm, "data": {}})
        for rid, vms in old.items():
            for vm in vms - vms_by_run.get(rid, set()):
                rows.append({"marker_id": f"livetap:{vm}:{now:.3f}:gone", "run_id": rid, "source": "livetap",
                             "kind": "vm_gone", "agent_id": vm_agent_id(vm), "ts": now, "detail": vm, "data": {}})
        self.live_vms = {k: set(v) for k, v in vms_by_run.items()}
        if rows:
            with self.db.lock:
                self.db.bulk_insert("run_markers", rows)
        for rid in vms_by_run:
            if rid not in old and not (self.cfg.runs_dir / rid / "run.json").exists():
                d = self.cfg.runs_dir / rid
                with self.db.lock:
                    self.ensure_run(rid, d if d.is_dir() else None, source="livetap")
                    self.db.con.execute("UPDATE runs SET live_tapped = TRUE, n_agents = greatest(coalesce(n_agents,0), ?) "
                                        "WHERE run_id = ?",
                                        [sum(1 for v in vms_by_run[rid] if vm_agent_id(v) is not None), rid])
                    self.set_status(rid, "live")
                    if d.is_dir():
                        for p in d.glob("cfg-a*.json"):
                            m = CFG_RE.match(p.name)
                            data = self._whole(rid, "cfg", int(m.group(1)), p)
                            if data is not None:
                                self.apply_cfg(rid, int(m.group(1)), data)
                    self.touch(rid)

    def live_offsets(self, run_id: str, vm: str) -> dict[str, tuple[int, str | None]]:
        cur = self.db.reader()
        rows = cur.execute("SELECT path, byte_off, epoch FROM ingest_state WHERE run_id = ? AND source = 'live' "
                           "AND path LIKE ?", [run_id, f"vm:{vm}:%"]).fetchall()
        return {p.split(":", 2)[2]: (bo or 0, ep) for p, bo, ep in rows}

    def apply_live(self, ch: LiveChunk) -> bool:
        rid = ch.run_id
        if (self.cfg.runs_dir / rid / "run.json").exists():
            return False
        path_key = f"vm:{ch.vm}:{ch.guest_path}"
        mark = len(self._pending_emits)
        with self.db.lock:
            con = self.db.con
            con.execute("BEGIN TRANSACTION")
            try:
                if not self.run_row(rid):
                    d = self.cfg.runs_dir / rid
                    self.ensure_run(rid, d if d.is_dir() else None, source="livetap")
                con.execute("UPDATE runs SET live_tapped = TRUE WHERE run_id = ?", [rid])
                changed = self._apply_live_locked(ch, path_key)
                if changed:
                    self.touch(rid)
                self._flush_notes(rid)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                self._rollback_emits(mark)
                raise
        return changed

    def _apply_live_locked(self, ch: LiveChunk, path_key: str) -> bool:
        rid, aid = ch.run_id, ch.agent_id
        if ch.size < 0:
            return False
        if ch.stream == "summary":
            if ch.data:
                self.apply_summary(rid, aid, ch.data)
                self.put_state(run_id=rid, stream="summary", agent_id=aid, source="live", path=path_key,
                               epoch=ch.epoch, size=ch.size, mtime=time.time(), byte_off=len(ch.data), lines=0)
                return True
            return False
        if ch.stream.startswith("diag:"):
            s = self.get_state(rid, ch.stream, aid, "live")
            prev = b""
            if s and s["epoch"] == ch.epoch and ch.off == s["byte_off"]:
                r = self.db.con.execute("SELECT content FROM diag_files WHERE run_id=? AND agent_id=? AND name=?",
                                        [rid, aid, ch.stream[5:]]).fetchone()
                prev = (r[0] or "").encode() if r else b""
            elif ch.off != 0:
                return False
            if not ch.data and s and s["size"] == ch.size:
                return False
            self.apply_diag(rid, aid, ch.stream[5:], data=(prev + ch.data)[:DIAG_MAX], size=ch.size)
            self.put_state(run_id=rid, stream=ch.stream, agent_id=aid, source="live", path=path_key, epoch=ch.epoch,
                           size=ch.size, mtime=time.time(), byte_off=ch.off + len(ch.data), lines=0)
            return True
        s = self.get_state(rid, ch.stream, aid, "live")
        if s and (s["epoch"] != ch.epoch or ch.size < (s["byte_off"] or 0)):
            self.note(rid, f"live {ch.stream} a{aid}: guest file replaced/truncated; live rows reset")
            self.delete_stream(rid, ch.stream, aid, only_src="live")
            self.db.con.execute("DELETE FROM ingest_state WHERE run_id=? AND stream=? AND agent_id=? AND source='live'",
                                [rid, ch.stream, aid])
            return True
        off = s["byte_off"] if s else 0
        if ch.off != off or not ch.data:
            return False
        nl = s["lines"] if s else 0
        rows, consumed, n, last_off, last_sha = self.consume(rid, ch.stream, aid, ch.data, off, nl, "live")
        if consumed == 0:
            return False
        self.insert_rows(rid, rows)
        self.put_state(run_id=rid, stream=ch.stream, agent_id=aid, source="live", path=path_key, epoch=ch.epoch,
                       size=ch.size, mtime=time.time(), byte_off=off + consumed, lines=nl + n,
                       last_line_off=last_off, last_line_sha1=last_sha)
        self._mirror(rid, ch.vm, ch.guest_path, off, ch.data[:consumed])
        return True

    def _mirror(self, rid, vm, guest_path, off, data: bytes):
        d = self.cfg.live_dir / rid / vm
        d.mkdir(parents=True, exist_ok=True)
        p = d / os.path.basename(guest_path)
        mode = "r+b" if p.exists() else "wb"
        with open(p, mode) as f:
            f.seek(off)
            f.write(data)
            f.truncate()

    # ------------------------------------------------------------------ derive + broadcast
    def derive_dirty(self, min_interval: dict | None = None, only: set | None = None) -> list[str]:
        rows = self.db.con.execute("SELECT run_id FROM runs WHERE dirty AND run_id <> '_pilot'").fetchall()
        done = []
        now = time.time()
        for (rid,) in rows:
            if only and rid not in only:
                continue
            if min_interval is not None and now - min_interval.get(rid, 0) < 1.0:
                continue
            try:
                r = D.derive_run(self.db, rid, self.cfg.runs_dir)
            except Exception as e:
                log.exception("derive of %s failed", rid)
                with self.db.lock:
                    # fail closed: a browser-lab run whose derive raised still gets its tag (e.g. a DB from before
                    # the tag rule, re-derived for the DERIVE_VERSION bump), so it stays out of the default roll-ups
                    row = self.db.con.execute("SELECT params, tag FROM runs WHERE run_id = ?", [rid]).fetchone()
                    try:
                        rparams = json.loads(row[0]) if row and row[0] else {}
                    except ValueError:
                        rparams = {}
                    tag = D.run_tag(rid, rparams, row[1] if row else "")
                    self.db.con.execute("UPDATE runs SET dirty = FALSE, tag = ?, notes = list_append(CAST(notes AS VARCHAR[]), ?)::JSON "
                                        "WHERE run_id = ?", [tag, f"derive error: {e!r}"[:300], rid])
                continue
            if min_interval is not None:
                min_interval[rid] = now
            done.append(rid)
            self._after_derive(rid, r)
        return done

    def _after_derive(self, rid: str, r: D.DeriveResult):
        if self.quiet:
            self.emitted_det[rid].update(d["key"] for d in r.detections)
            self.pending_refs.pop(rid, None)
            return
        refs = self.pending_refs.pop(rid, set())
        if refs:
            cur = self.db.reader()
            res = cur.execute("SELECT t, lane, kind, step, call, cat, ref, label, is_board, phase, t_end FROM v_timeline "
                              "WHERE run_id = ? ORDER BY t NULLS LAST", [rid]).fetchall()
            posts = {f"board:{lo}": (uid, flags) for uid, flags, lo in cur.execute(
                "SELECT uid, flags, line_off FROM posts WHERE run_id = ? AND NOT is_seed", [rid]).fetchall()}
            for t, lane, kind, step, call, cat, ref, label, is_board, phase, t_end in res:
                if ref not in refs or kind.startswith("relay"):
                    continue
                ev = {"run_id": rid, "t": None if t is None else round(t, 3), "lane": lane, "kind": kind,
                      "label": label, "ref": ref, "src": "live" if rid in self.live_vms else "final"}
                if step is not None:
                    ev["step"] = step
                if call is not None:
                    ev["call"] = call
                if kind == "model":
                    ev["phase"] = phase or cat
                else:
                    if cat is not None:
                        ev["cat"] = cat
                    if phase is not None:  # same shape as /timeline rows (client derives free phase from it)
                        ev["phase"] = phase
                if is_board is not None:
                    ev["is_board"] = is_board
                if t_end is not None:
                    ev["t_end"] = round(t_end, 3)
                if ref in posts:
                    ev["uid"] = posts[ref][0]
                    ev["flags"] = json.loads(posts[ref][1] or "[]")
                self.emit("event", ev)
        for d in r.detections:
            if d["key"] in self.emitted_det[rid]:
                continue
            self.emitted_det[rid].add(d["key"])
            ev = {k: v for k, v in d.items() if k != "key"}
            if ev.get("t") is not None:
                ev["t"] = round(ev["t"], 3)
            self.emit("event", {"run_id": rid, **ev, "src": "derived"})
        self.emit("metrics_update", {"run_id": rid, "data_version": r.data_version, "metrics": r.metrics,
                                     "t_origin": r.t_origin})


# ---------------------------------------------------------------------- worker thread

class IngestWorker(threading.Thread):
    """Owns the single DuckDB writer: file scans, live-tap chunk application, derive."""

    def __init__(self, ingester: Ingester, cfg, watch: bool = True):
        super().__init__(name="obs-ingest", daemon=True)
        self.ing = ingester
        self.cfg = cfg
        self.watch = watch
        self.q: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        self.stop_ev = threading.Event()
        self.last_derive: dict[str, float] = {}
        self.ready = threading.Event()
        self.errors = 0
        self.fs_event = threading.Event()

    def submit(self, item):
        self.q.put(item)
        self.wake.set()

    def stop(self):
        self.stop_ev.set()
        self.wake.set()

    def _watch_loop(self):
        try:
            from watchfiles import watch
            for _ in watch(str(self.cfg.runs_dir), stop_event=self.stop_ev, recursive=True, debounce=500,
                           rust_timeout=5000, yield_on_timeout=False):
                self.fs_event.set()
                self.wake.set()
        except Exception:
            log.warning("watchfiles unavailable; relying on %ss polling", self.cfg.poll_s)

    def run(self):
        if self.watch and self.cfg.runs_dir.is_dir():
            threading.Thread(target=self._watch_loop, name="obs-watch", daemon=True).start()
        # initial catch-up: quiet (no SSE flood for history)
        try:
            self.ing.quiet = True
            self.ing.scan()
            self.ing.derive_dirty()
        except Exception:
            log.exception("initial ingest failed")
            self.errors += 1
        finally:
            self.ing.quiet = False
            self.ing._pending_emits.clear()
        self.ready.set()
        next_scan = time.time() + self.cfg.poll_s
        while not self.stop_ev.is_set():
            self.wake.wait(timeout=0.5)
            self.wake.clear()
            try:
                while True:
                    try:
                        item = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(item, LiveChunk):
                        self.ing.apply_live(item)
                    elif isinstance(item, tuple) and item[0] == "vms":
                        self.ing.set_live_vms(item[1])
                        next_scan = 0  # status may have changed
                    elif callable(item):
                        item()
                now = time.time()
                if now >= next_scan or self.fs_event.is_set():
                    self.fs_event.clear()
                    self.ing.scan()
                    next_scan = now + self.cfg.poll_s
                self.ing.derive_dirty(min_interval=self.last_derive)
            except Exception:
                log.exception("ingest tick failed")
                self.errors += 1
            finally:
                self.ing.flush_emits()
