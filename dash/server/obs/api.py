"""Starlette app: read-only JSON API (CONTRACT section 5), SSE live feed, static frontend."""
from __future__ import annotations

import bisect
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, metrics as M, sse
from . import state as S
from .db import SCHEMA_VERSION, Database
from .parse import DEFAULT_GOAL, run_goal

DEFAULT_LIMIT, MAX_LIMIT = 500, 10000
TIMELINE_KINDS = ["step", "idle", "model", "start", "end", "crash", "api_retry", "req", "post", "upvote", "delete",
                  "dm", "irc_cmd", "irc_join", "connect", "disconnect", "board_start", "relay_open", "relay_close"]
DEFAULT_TL_KINDS = [k for k in TIMELINE_KINDS if not k.startswith("relay_")]
SERIES_METRICS = ["board_share_rolling5", "cum_board_steps", "cum_tokens", "cum_posts", "requests", "board_steps",
                  "posts", "model_calls"]
PLANNED_CONTROL = ["GET  /api/control/schema", "POST /api/control/experiments", "POST /api/control/conditions",
                   "POST /api/control/runs  {condition_id, reps, tag}", "POST /api/control/runs/{run_id}/stop",
                   "PATCH /api/control/runs/{run_id}/board  {affordances, seed_patch}",
                   "PATCH /api/control/runs/{run_id}/agents/{agent_id}  {nudge, params}"]
RUN_SORT = {"run_id", "started", "finished", "duration_s", "status", "iface", "pop", "n_agents", "rep", "tag", "model",
            "discovery_rate", "solve_rate", "total_posts", "requests", "cross_mentions", "mention_edges_live",
            "live_live_edges", "swarm_onset_t", "tokens_prompt", "condition_id", "data_version", "first_seen", "updated"}
JSON_COLS = {"harness", "params", "ctf", "timed_out", "notes", "cfg", "summary", "display_names", "factors", "content",
             "submissions", "usage", "data", "flags", "mentions", "evidence", "run_params"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail


def _default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


class JSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(_clean(content), ensure_ascii=False, separators=(",", ":"), default=_default).encode()


def r3(v):
    return None if v is None else round(v, 3)


def _j(v, d=None):
    if v is None:
        return d
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def dicts(res) -> list[dict]:
    cols = [c[0] for c in res.description]
    out = []
    for row in res.fetchall():
        d = dict(zip(cols, row))
        for k in JSON_COLS & d.keys():
            d[k] = _j(d[k])
        out.append(d)
    return out


def multi(req: Request, key: str) -> list[str] | None:
    vals = req.query_params.getlist(key)
    if not vals:
        return None
    out = []
    for v in vals:
        out.extend(x.strip() for x in v.split(",")) if v != "" else out.append("")
    return out


def qint(req, key, default, lo=None, hi=None):
    v = req.query_params.get(key)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ApiError(400, "bad_param", f"{key} must be an integer")
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def qfloat(req, key, default=None):
    v = req.query_params.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        raise ApiError(400, "bad_param", f"{key} must be a number")


def build_filters(req: Request, grain: str = "run", default_tagged: bool = False,
                  default_status: str | None = None, default_goal: str | None = None) -> tuple[str, list]:
    """WHERE clause for mat_run_metrics (grain='run') or mat_agent_metrics (grain='agent').

    default_goal (aggregate endpoints): when no goal / param.goal filter is given, keep only that goal, so cells
    never pool solvable and unsolvable runs unless asked to (grouping by goal counts as asking)."""
    status_col = "status" if grain == "run" else "run_status"
    params_col = "params" if grain == "run" else "run_params"
    clauses, params = [], []
    mapping = {"model": "model", "iface": "iface", "pop": "pop", "n_agents": "n_agents", "tag": "tag",
               "status": status_col, "experiment": "experiment_id", "condition": "condition_id", "run": "run_id"}
    for key, col in mapping.items():
        vals = multi(req, key)
        if vals is None:
            continue
        if key == "n_agents":
            try:
                vals = [int(v) for v in vals]
            except ValueError:
                raise ApiError(400, "bad_param", "n_agents must be integers")
        clauses.append(f"{col} IN ({', '.join('?' for _ in vals)})")
        params.extend(vals)
    inc = req.query_params.get("include_tagged")
    include_tagged = default_tagged if inc is None else inc in ("1", "true", "yes")
    if multi(req, "tag") is None and not include_tagged:
        clauses.append("coalesce(tag, '') = ''")
    if default_status and multi(req, "status") is None:
        clauses.append(f"{status_col} = ?")
        params.append(default_status)
    goal_vals = [v for k in M.GOAL_KEYS for v in (multi(req, k) or [])]
    if goal_vals:
        clauses.append(f"{M.goal_sql(params_col)} IN ({', '.join('?' for _ in goal_vals)})")
        params.extend(goal_vals)
    elif default_goal and not set(multi(req, "group") or []) & set(M.GOAL_KEYS):
        clauses.append(f"{M.goal_sql(params_col)} = ?")
        params.append(default_goal)
    for k in req.query_params.keys():
        if k in M.GOAL_KEYS:
            continue
        for prefix, col in (("harness.", "harness"), ("param.", params_col)):
            if k.startswith(prefix):
                sub = k[len(prefix):]
                if not M.KEY_RE.match(sub):
                    raise ApiError(400, "bad_param", f"bad key {k}")
                vals = multi(req, k) or []
                clauses.append(f"json_extract_string({col}, '$.{sub}') IN ({', '.join('?' for _ in vals)})")
                params.extend(vals)
    if req.query_params.get("exclude_broken") in ("1", "true"):
        clauses.append(f"coalesce(json_extract_string({params_col}, '$.irc_inbound_broken'), 'false') <> 'true'")
    return (" AND ".join(clauses) or "TRUE"), params


def create_app(cfg, db: Database, bc: sse.Broadcaster | None = None, worker=None, tap=None) -> Starlette:
    bc = bc or sse.Broadcaster()

    def reader():
        return db.reader()

    def run_meta(cur, run_id):
        r = cur.execute("SELECT run_id, data_version, t_origin, status, iface, t_end FROM runs WHERE run_id = ?",
                        [run_id]).fetchone()
        if not r:
            raise ApiError(404, "unknown_run", f"no run {run_id!r}")
        return {"run_id": r[0], "data_version": r[1] or 0, "t_origin": r[2], "status": r[3], "iface": r[4], "t_end": r[5]}

    def etagged(req, meta, build):
        tag = f'"{meta["run_id"]}:{meta["data_version"]}"'
        if req.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        resp = JSON(build())
        resp.headers["ETag"] = tag
        return resp

    # ------------------------------------------------------------------ 5.1 health
    def health(req):
        cur = reader()
        n_runs = cur.execute("SELECT count(*) FROM runs").fetchone()[0]
        n_live = cur.execute("SELECT count(*) FROM runs WHERE status IN ('live','collecting')").fetchone()[0]
        last = getattr(getattr(worker, "ing", None), "last_ingest_ts", None)
        return JSON({"ok": True, "version": __version__, "schema_version": SCHEMA_VERSION,
                     "derive_version": int(db.meta("derive_version") or 0), "db": db.path,
                     "runs_dir": str(cfg.runs_dir), "runs_dir_exists": cfg.runs_dir.is_dir(), "n_runs": n_runs,
                     "n_live": n_live, "last_ingest_ts": last,
                     "ingest_lag_s": None if last is None else round(time.time() - last, 3),
                     "ingest_errors": getattr(worker, "errors", 0),
                     "run_ingest_errors": [{"run_id": r, "ts": t, "error": e} for r, t, e in cur.execute(
                         "SELECT run_id, ts, error FROM ingest_errors ORDER BY ts DESC LIMIT 20").fetchall()],
                     "live_tap": tap.status() if tap else {"enabled": False, "msb": cfg.msb, "live_vms": 0,
                                                           "last_discover_ts": None, "errors": 0},
                     "web_built": (cfg.web_dist / "index.html").is_file()})

    # ------------------------------------------------------------------ 5.2 facets
    def facets(req):
        cur = reader()
        # The tag dimension lists every tag (so tagged runs can be picked); every other dimension counts with the
        # same tagged-run rule as the run lists / tiles, so a chip's count equals the runs a click returns.
        where_tag, params_tag = build_filters(req, "run", default_tagged=True)
        where, params = build_filters(req, "run")
        base = f"FROM runs r LEFT JOIN (SELECT run_id FROM mat_run_metrics) m USING (run_id) WHERE r.run_id <> '_pilot'"
        sub = f"(SELECT r.* {base}) x"
        dims = []
        for key, col in (("model", "model"), ("iface", "iface"), ("pop", "pop"), ("n_agents", "n_agents"),
                         ("tag", "tag"), ("status", "status"), ("experiment", "experiment_id"),
                         ("condition", "condition_id")):
            w, pr = (where_tag, params_tag) if key == "tag" else (where, params)
            rows = cur.execute(f"SELECT {col}, count(*) FROM {sub} WHERE {w} GROUP BY 1 ORDER BY 2 DESC, 1",
                               pr).fetchall()
            dims.append({"key": key, "kind": "num" if key == "n_agents" else "cat",
                         "values": [{"value": v if v is not None or key != "tag" else "", "n_runs": n}
                                    for v, n in rows]})
        for prefix, col in (("harness", "harness"), ("param", "params")):
            keys = [k for (k,) in cur.execute(
                f"SELECT DISTINCT unnest(json_keys({col})) FROM {sub} WHERE {where} AND {col} IS NOT NULL ORDER BY 1",
                params).fetchall() if not k.startswith("_")]
            if prefix == "param":  # goal is always a dim: runs without one are the historical solvable CTF
                keys = sorted(set(keys) | {"goal"})
            for k in keys:
                if not M.KEY_RE.match(k):
                    continue
                vexpr = M.goal_sql(col) if (prefix, k) == ("param", "goal") else f"json_extract_string({col}, '$.{k}')"
                rows = cur.execute(
                    f"SELECT {vexpr} v, json_type(json_extract({col}, '$.{k}')) ty, "
                    f"count(*) FROM {sub} WHERE {where} GROUP BY 1, 2 ORDER BY 3 DESC, 1", params).fetchall()
                if (prefix, k) == ("param", "goal"):  # merge the NULL ('solvable' by default) and explicit buckets
                    merged: dict = {}
                    for v, _ty, n in rows:
                        merged[v] = merged.get(v, 0) + n
                    rows = [(v, "VARCHAR", n) for v, n in sorted(merged.items(), key=lambda x: (-x[1], x[0]))]
                num = all(ty in ("BIGINT", "UBIGINT", "DOUBLE", None) for _, ty, _ in rows)
                vals = []
                for v, ty, n in rows:
                    if num and v is not None:
                        v = float(v) if ty == "DOUBLE" else int(v)
                    elif ty == "BOOLEAN":
                        v = v == "true"
                    vals.append({"value": v, "n_runs": n})
                dims.append({"key": f"{prefix}.{k}", "kind": "num" if num else "cat", "values": vals})
        mdefs = dicts(cur.execute("SELECT * FROM metric_defs ORDER BY grain, metric_id"))
        return JSON({"dims": dims, "metrics": mdefs})

    # ------------------------------------------------------------------ 5.3 runs
    RUN_SELECT = """
      SELECT r.run_id, r.status, r.experiment_id, r.condition_id, c.label AS condition_label, r.model, r.iface, r.pop,
             r.n_agents, r.rep, coalesce(r.tag, '') AS tag, r.started, r.finished, r.finished - r.started AS duration_s,
             r.harness, r.params, r.notes, r.live_tapped, r.data_version, r.t_origin, r.t_end, r.first_seen, r.updated,
             m.discovery_rate, m.solve_rate, m.total_posts, m.requests, m.cross_mentions, m.mention_edges_live, m.live_live_edges,
             m.swarm_onset_t, m.any_leak, m.tokens_prompt, m.tokens_completion
      FROM runs r LEFT JOIN mat_run_metrics m USING (run_id) LEFT JOIN conditions c ON c.condition_id = r.condition_id
      WHERE r.run_id <> '_pilot'
    """
    MCOLS = ["discovery_rate", "solve_rate", "total_posts", "requests", "cross_mentions", "mention_edges_live",
             "live_live_edges",
             "swarm_onset_t", "any_leak", "tokens_prompt", "tokens_completion"]

    def shape_run(d):
        d["metrics"] = {k: d.pop(k) for k in MCOLS}
        d["params"] = {k: v for k, v in (d.get("params") or {}).items() if not k.startswith("_")}
        d["goal"] = run_goal(d["params"])
        return d

    def runs(req):
        cur = reader()
        where, params = build_filters(req, "run")
        q = req.query_params.get("q")
        if q:
            where += " AND x.run_id ILIKE ?"
            params.append(f"%{q}%")
        sort = req.query_params.get("sort", "started")
        if sort not in RUN_SORT:
            raise ApiError(400, "bad_param", f"sort must be one of {sorted(RUN_SORT)}")
        order = "ASC" if req.query_params.get("order", "desc").lower() == "asc" else "DESC"
        limit = qint(req, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = qint(req, "offset", 0, 0)
        total = cur.execute(f"SELECT count(*) FROM ({RUN_SELECT}) x WHERE {where}", params).fetchone()[0]
        rows = dicts(cur.execute(f"SELECT * FROM ({RUN_SELECT}) x WHERE {where} ORDER BY {sort} {order} NULLS LAST, "
                                 f"run_id LIMIT {limit} OFFSET {offset}", params))
        return JSON({"total": total, "runs": [shape_run(d) for d in rows]})

    def run_detail(req):
        run_id = req.path_params["run_id"]
        cur = reader()
        meta = run_meta(cur, run_id)

        def build():
            d = dicts(cur.execute(f"SELECT * FROM ({RUN_SELECT}) x WHERE run_id = ?", [run_id]))[0]
            extra = dicts(cur.execute("SELECT ctf, t_origin_final, board_offset_s, seed_hash, timed_out, final_missing, "
                                      "code_version, source_dir FROM runs WHERE run_id = ?", [run_id]))[0]
            run = {**shape_run(d), **extra}
            cond = dicts(cur.execute("SELECT condition_id, label, factors, params FROM conditions WHERE condition_id = ?",
                                     [run.get("condition_id")]))
            exp = dicts(cur.execute("SELECT experiment_id, name, description, params FROM experiments "
                                    "WHERE experiment_id = ?", [run.get("experiment_id")]))
            o = run.get("t_origin") or 0.0
            agents = dicts(cur.execute("SELECT * FROM agents WHERE run_id = ? ORDER BY agent_id", [run_id]))
            am = {a["agent_id"]: a for a in dicts(cur.execute("SELECT * FROM mat_agent_metrics WHERE run_id = ?", [run_id]))}
            out_agents = []
            for a in agents:
                m = am.get(a["agent_id"], {})
                out_agents.append({
                    "agent_id": a["agent_id"], "model": a["model"], "status": a["status"], "solved": a["solved"],
                    "solve_step": a["solve_step"], "solve_t": r3(a["solve_ts"] - o) if a["solve_ts"] else None,
                    "steps_used": a["steps_used"], "model_calls": a["model_calls"], "ended": a["ended"],
                    "crashed": a["crashed"], "start_t": r3(a["start_ts"] - o) if a["start_ts"] else None,
                    "end_t": r3(a["end_ts"] - o) if a["end_ts"] else None, "clock_offset_s": a["clock_offset_s"],
                    "display_names": a["display_names"] or [],
                    "tokens": {"prompt": a["tok_prompt"], "completion": a["tok_completion"], "cached": a["tok_cached"],
                               "reasoning": a["tok_reasoning"]},
                    "metrics": {k: m.get(k) for k in M.AGENT_METRICS if k in m},
                })
            markers = [{"kind": k, "t": r3(ts - o) if ts is not None else None, "source": s, "agent_id": aid,
                        "detail": det, "superseded": bool(sup)} for k, ts, s, aid, det, sup in cur.execute(
                "SELECT kind, ts, source, agent_id, detail, coalesce(superseded, FALSE) FROM run_markers "
                "WHERE run_id = ? ORDER BY superseded NULLS FIRST, ts NULLS FIRST, "
                "source, TRY_CAST(split_part(marker_id, ':', -1) AS BIGINT), marker_id", [run_id]).fetchall()]
            diag = [{"agent_id": a, "name": n, "size": s} for a, n, s in cur.execute(
                "SELECT agent_id, name, size FROM diag_files WHERE run_id = ? ORDER BY agent_id, name", [run_id]).fetchall()]
            threads = dicts(cur.execute("SELECT thread, title, is_seed, created_t, created_by FROM threads "
                                        "WHERE run_id = ? ORDER BY is_seed DESC, created_t", [run_id]))
            return {"run": run, "condition": cond[0] if cond else None, "experiment": exp[0] if exp else None,
                    "agents": out_agents, "markers": markers, "diag": diag, "threads": threads}
        return etagged(req, meta, build)

    # ------------------------------------------------------------------ 5.5 timeline
    def timeline(req):
        run_id = req.path_params["run_id"]
        cur = reader()
        meta = run_meta(cur, run_id)
        kinds = multi(req, "kinds") or DEFAULT_TL_KINDS
        bad = [k for k in kinds if k not in TIMELINE_KINDS]
        if bad:
            raise ApiError(400, "bad_param", f"unknown kinds {bad}")
        include = set(multi(req, "include") or ["posts", "phases"])
        lanes_f = multi(req, "agents")
        t0, t1 = qfloat(req, "t0"), qfloat(req, "t1")
        fmt = req.query_params.get("format", "rows")

        def build():
            where = ["v.run_id = ?", "list_contains(?, v.kind)"]
            params = [run_id, kinds]
            if lanes_f:
                where.append("list_contains(?, v.lane)")
                params.append([int(x) for x in lanes_f])
            if t0 is not None:
                where.append("v.t >= ?")
                params.append(t0)
            if t1 is not None:
                where.append("v.t <= ?")
                params.append(t1)
            rows = cur.execute(
                "SELECT v.t, v.t_end, v.lane, v.kind, v.step, v.call, v.cat, v.label, v.ref, v.is_board, v.phase "
                f"FROM v_timeline v WHERE {' AND '.join(where)}", params).fetchall()
            pmap = {f"board:{lo}": (uid, fl) for uid, fl, lo in cur.execute(
                "SELECT uid, flags, line_off FROM posts WHERE run_id = ? AND line_off IS NOT NULL", [run_id]).fetchall()}
            cmap = {}
            if "model" in kinds:
                mq = "SELECT agent_id, line_off, completion_tokens FROM model_calls WHERE run_id = ?"
                mp = [run_id]
                if t0 is not None:
                    mq += " AND t >= ?"
                    mp.append(t0)
                if t1 is not None:
                    mq += " AND t <= ?"
                    mp.append(t1)
                cmap = {f"model:{a}:{lo}": ct for a, lo, ct in cur.execute(mq, mp).fetchall()}

            def kr(kind, ref):
                if kind == "model":
                    return 0
                if kind in ("step", "idle"):
                    return 1
                if kind.startswith("relay"):
                    return 2
                return 3 if ref.startswith("board:") else 4
            rows.sort(key=lambda r: (r[0] is not None, r[0] if r[0] is not None else 0, r[2], kr(r[3], r[8]), r[8]))
            events = []
            for (t, t_end, lane, kind, step, call, cat, label, ref, is_board, phase) in rows:
                e = {"t": r3(t), "lane": lane, "kind": kind, "label": label, "ref": ref}
                if t_end is not None:
                    e["t_end"] = r3(t_end)
                if step is not None:
                    e["step"] = step
                if call is not None:
                    e["call"] = call
                if kind == "model":
                    e["phase"] = phase or cat
                    ctok = cmap.get(ref)
                    if ctok is not None:
                        e["completion_tokens"] = ctok
                else:
                    if cat is not None:
                        e["cat"] = cat
                    if phase is not None:
                        e["phase"] = phase
                if is_board is not None:
                    e["is_board"] = is_board
                pm = pmap.get(ref)
                if pm is not None:
                    e["uid"] = pm[0]
                    e["flags"] = _j(pm[1], [])
                events.append(e)
            if "relay_data" in include:
                rr = cur.execute("SELECT t, agent_id, dir, n, step, line_off FROM relay_events WHERE run_id = ? AND "
                                 "ev = 'data'" + (" AND list_contains(?, agent_id)" if lanes_f else "") +
                                 " ORDER BY t", [run_id] + ([[int(x) for x in lanes_f]] if lanes_f else [])).fetchall()
                for t, aid, dr, n, step, lo in rr:
                    if (t0 is not None and (t is None or t < t0)) or (t1 is not None and (t is None or t > t1)):
                        continue
                    events.append({"t": r3(t), "lane": aid, "kind": "relay_data", "label": f"{dr} {n}B",
                                   "ref": f"relay:{aid}:{lo}", "step": step, "dir": dr, "n": n})
                events.sort(key=lambda e: (e["t"] is not None, e["t"] or 0))
            agents = cur.execute("SELECT agent_id, display_names, start_ts, solve_ts, end_ts FROM agents "
                                 "WHERE run_id = ? ORDER BY agent_id", [run_id]).fetchall()
            o = meta["t_origin"] or 0.0
            out = {"run_id": run_id, "t_origin": meta["t_origin"], "t_end": r3(meta["t_end"]), "status": meta["status"],
                   "iface": meta["iface"], "data_version": meta["data_version"],
                   "lanes": [{"lane": -1, "label": "board"}] + [
                       {"lane": a, "label": f"a{a}", "names": _j(n, [])} for a, n, *_ in agents]}
            if "phases" in include:
                out["phases"] = [{"agent_id": a, "start": r3(s - o) if s else None, "solve": r3(sv - o) if sv else None,
                                  "end": r3(e - o) if e else None} for a, _n, s, sv, e in agents]
            if "posts" in include:
                out["posts"] = [{**p, "t": r3(p["t"])} for p in dicts(cur.execute(
                    "SELECT uid, post_id, kind, thread, author, author_node, agent_id, is_seed, t, upvotes0, body, "
                    "flags, mentions, step, phase FROM posts WHERE run_id = ? ORDER BY t, uid", [run_id]))]
                out["upvotes"] = [{"t": r3(t), "src": f"a{a}" if a is not None else None, "post_uid": uid,
                                   "post_id": pid} for t, a, uid, pid in cur.execute(
                    "SELECT b.t, b.agent_id, p.uid, b.post_id FROM board_events b LEFT JOIN posts p ON "
                    "p.run_id = b.run_id AND p.post_id = b.post_id AND p.kind = 'post' WHERE b.run_id = ? "
                    "AND b.kind = 'upvote' ORDER BY b.t", [run_id]).fetchall()]
            if fmt == "columns":
                keys = sorted({k for e in events for k in e})
                out["events"] = {k: [e.get(k) for e in events] for k in keys}
            else:
                out["events"] = events
            return out
        return etagged(req, meta, build)

    # ------------------------------------------------------------------ 5.6 state / 5.7 graph
    def state(req):
        run_id = req.path_params["run_id"]
        cur = reader()
        meta = run_meta(cur, run_id)
        t = qfloat(req, "t")
        if t is None:
            t = meta["t_end"] if meta["t_end"] is not None else 0.0
        include = set(multi(req, "include") or ["board", "agents", "metrics"])

        def build():
            out = {"t": t, "ts": (meta["t_origin"] or 0) + t, "run_id": run_id}
            ag = None
            if "board" in include:
                out["board"] = S.board_at(cur, run_id, t, meta["iface"])
            if "agents" in include:
                ag = out["agents"] = S.agents_at(cur, run_id, t, meta["t_origin"])
            if "metrics" in include:
                out["metrics"] = S.metrics_at(cur, run_id, t, ag, meta["t_origin"])
            if "graph" in include:
                out["graph"] = S.graph(cur, run_id, t, ["exposure", "reply", "mention", "dm", "upvote"], True)
            return out
        return etagged(req, meta, build)

    def graph(req):
        run_id = req.path_params["run_id"]
        cur = reader()
        meta = run_meta(cur, run_id)
        t = qfloat(req, "t")
        kinds = multi(req, "kinds") or ["exposure", "reply", "mention", "dm", "upvote"]
        seed = req.query_params.get("seed", "1") not in ("0", "false")
        return etagged(req, meta, lambda: S.graph(cur, run_id, t, kinds, seed))

    # ------------------------------------------------------------------ 5.8 agent steps
    def agent_steps(req):
        run_id = req.path_params["run_id"]
        try:
            aid = int(req.path_params["agent_id"])
        except ValueError:
            raise ApiError(400, "bad_param", "agent_id must be an integer")
        cur = reader()
        meta = run_meta(cur, run_id)
        if not cur.execute("SELECT 1 FROM agents WHERE run_id = ? AND agent_id = ?", [run_id, aid]).fetchone():
            raise ApiError(404, "unknown_agent", f"no agent {aid} in {run_id}")
        frm, to = qint(req, "from", None), qint(req, "to", None)
        out_max = qint(req, "output_max", 20000, 0, 20000)
        fields = set(multi(req, "fields") or [])
        with_model = req.query_params.get("model", "1") not in ("0", "false")

        def build():
            o = meta["t_origin"] or 0.0
            w, p = "run_id = ? AND agent_id = ?", [run_id, aid]
            if frm is not None:
                w += " AND step >= ?"
                p.append(frm)
            if to is not None:
                w += " AND step <= ?"
                p.append(to)
            steps = dicts(cur.execute(
                f"SELECT step, t, t_end, dur_s, phase_before, phase, is_solve, idle, command, exit, "
                f"left(output, {out_max}) AS output, output_len, submissions, is_board, board_via, cat, persistence, "
                f"call, tool_idx, line_off, seq, tok_prompt, tok_completion, tok_reasoning FROM steps WHERE {w} ORDER BY seq", p))
            calls = [s["call"] for s in steps if s["call"] is not None]
            if frm is None and to is None:
                crange = ""
                cparams = []
            else:
                lo_c = min(calls) if calls else 0
                hi_c = max(calls) if calls else -1
                if steps:  # include orphan calls between the first and last returned step
                    lo_c = min(lo_c, cur.execute("SELECT coalesce(max(call), 0) FROM steps WHERE run_id = ? AND "
                                                 "agent_id = ? AND seq < ? AND call IS NOT NULL",
                                                 [run_id, aid, steps[0]["seq"]]).fetchone()[0] + 1)
                crange = " AND call BETWEEN ? AND ?"
                cparams = [lo_c, hi_c]
            mcs = {m["call"]: m for m in dicts(cur.execute(
                "SELECT call, t, latency_s, phase, content, reasoning, n_tool_calls, n_steps, prompt_tokens, "
                "completion_tokens, reasoning_tokens, cached_tokens, total_tokens, usage FROM model_calls "
                "WHERE run_id = ? AND agent_id = ?" + crange + " ORDER BY seq", [run_id, aid] + cparams))}
            tcs = defaultdict(list)
            for tc in dicts(cur.execute("SELECT call, idx, tool_call_id, name, arguments, args_valid, command, step "
                                        "FROM tool_calls WHERE run_id = ? AND agent_id = ?" + crange +
                                        " ORDER BY call, idx", [run_id, aid] + cparams)):
                tcs[tc["call"]].append({"idx": tc["idx"], "id": tc["tool_call_id"], "name": tc["name"],
                                        "arguments": tc["arguments"], "args_valid": tc["args_valid"],
                                        "command": tc["command"], "step": tc["step"]})
            boards = defaultdict(list)
            srange = ""
            sparams = []
            if steps and (frm is not None or to is not None):
                srange = " AND step BETWEEN ? AND ?"
                sparams = [steps[0]["step"], steps[-1]["step"]]
            for b in dicts(cur.execute(
                    "SELECT step, t, kind, method, path, status, target_kind, target, thread, author, body, post_id, "
                    "nick, to_nick, line, attribution FROM board_events WHERE run_id = ? AND agent_id = ? "
                    "AND step IS NOT NULL" + srange + " ORDER BY seq", [run_id, aid] + sparams)):
                st = b.pop("step")
                b["t"] = r3(b["t"])
                boards[st].append({k: v for k, v in b.items() if v is not None})
            relay = {s: {"conns": c, "bytes_up": u, "bytes_down": d} for s, c, u, d in cur.execute(
                "SELECT step, count(*) FILTER (WHERE ev = 'open'), coalesce(sum(n) FILTER (WHERE dir = 'up'), 0), "
                "coalesce(sum(n) FILTER (WHERE dir = 'down'), 0) FROM relay_events WHERE run_id = ? AND agent_id = ? "
                "AND step IS NOT NULL" + srange + " GROUP BY step", [run_id, aid] + sparams).fetchall()}
            out_steps = []
            for s in steps:
                m = mcs.get(s["call"]) if s["call"] is not None else None
                rec = {**s, "t": r3(s["t"]), "t_end": r3(s["t_end"]),
                       "board": boards.get(s["step"], []), "relay": relay.get(s["step"], {"conns": 0, "bytes_up": 0,
                                                                                         "bytes_down": 0})}
                if with_model and m is not None:
                    rec["model"] = {"call": m["call"], "t": r3(m["t"]), "latency_s": m["latency_s"], "phase": m["phase"],
                                    "content": m["content"], "reasoning": m["reasoning"],
                                    "tool_calls": tcs.get(m["call"], []),
                                    "usage": {"prompt_tokens": m["prompt_tokens"],
                                              "completion_tokens": m["completion_tokens"],
                                              "reasoning_tokens": m["reasoning_tokens"],
                                              "cached_tokens": m["cached_tokens"], "total_tokens": m["total_tokens"]}}
                rec.pop("seq", None)
                if fields:
                    rec = {k: v for k, v in rec.items() if k in fields or k == "step"}
                out_steps.append(rec)
            orphans = [{"call": m["call"], "t": r3(m["t"]), "phase": m["phase"], "content": m["content"],
                        "reasoning": m["reasoning"], "n_tool_calls": m["n_tool_calls"],
                        "tool_calls": tcs.get(m["call"], []),
                        "usage": {"prompt_tokens": m["prompt_tokens"], "completion_tokens": m["completion_tokens"],
                                  "reasoning_tokens": m["reasoning_tokens"]}}
                       for m in mcs.values() if not m["n_steps"]]
            retries = [{"after_t": r3(t), "attempt": a, "wait": wt, "detail": d} for t, a, wt, d in cur.execute(
                "SELECT t, attempt, wait, detail FROM agent_records WHERE run_id = ? AND agent_id = ? AND kind = 'api_retry' "
                "ORDER BY seq", [run_id, aid]).fetchall()]
            records = [{"kind": k, "t": r3(t), "detail": d} for k, t, d in cur.execute(
                "SELECT kind, t, detail FROM agent_records WHERE run_id = ? AND agent_id = ? AND kind <> 'api_retry' "
                "ORDER BY seq", [run_id, aid]).fetchall()]
            harness = _j(cur.execute("SELECT harness FROM runs WHERE run_id = ?", [run_id]).fetchone()[0], {}) or {}
            return {"run_id": run_id, "agent_id": aid, "output_chars": harness.get("output_chars", 3000),
                    "steps": out_steps, "orphan_calls": orphans, "retries": retries, "records": records}
        return etagged(req, meta, build)

    # ------------------------------------------------------------------ 5.9 cells
    def cells(req):
        cur = reader()
        group = multi(req, "group") or ["model", "iface", "pop"]
        mets = multi(req, "metrics") or M.ANALYZE_14
        try:
            for g in group:
                M.group_expr(g)
            for m in mets:
                M.metric_expr(m)
        except ValueError as e:
            raise ApiError(400, "bad_param", str(e))
        ci = req.query_params.get("ci", "bootstrap")
        if ci not in ("none", "bootstrap"):
            raise ApiError(400, "bad_param", "ci must be none|bootstrap")
        B = qint(req, "B", 1000, 10, 20000)
        points = req.query_params.get("points") in ("1", "true")
        where, params = build_filters(req, "agent", default_status="done", default_goal=DEFAULT_GOAL)
        out = M.cells(cur, where, params, group, mets, ci=ci, B=B, points=points)
        return JSON({"group": group, "metrics": mets, "ci": ci, "B": B, "cells": out})

    # ------------------------------------------------------------------ 5.10 series
    def series(req):
        cur = reader()
        metric = req.query_params.get("metric", "board_share_rolling5")
        if metric not in SERIES_METRICS:
            raise ApiError(400, "bad_param", f"metric must be one of {SERIES_METRICS}")
        x = req.query_params.get("x", "step")
        if x not in ("step", "t", "t_since_contact", "t_since_solve"):
            raise ApiError(400, "bad_param", "x must be step|t|t_since_contact|t_since_solve")
        bucket = qfloat(req, "bucket", 10.0)
        if bucket <= 0:
            raise ApiError(400, "bad_param", "bucket must be > 0")
        band = req.query_params.get("band") in ("1", "true")
        run = req.query_params.get("run")
        span = None
        if run:
            rm = run_meta(cur, run)
            if x == "t":  # per-run time series cover the whole run so finished agents' cumulative lines carry on
                span = (0.0, max(0.0, rm["t_end"] or 0.0))
            agents = [(run, a) for (a,) in cur.execute("SELECT agent_id FROM agents WHERE run_id = ? ORDER BY agent_id",
                                                       [run]).fetchall()]
            groups = {(run, a): {"agent_id": a} for _, a in agents}
            group_keys = None
        else:
            group_keys = multi(req, "group") or ["iface", "pop"]
            try:
                gex = [M.group_expr(g) for g in group_keys]
            except ValueError as e:
                raise ApiError(400, "bad_param", str(e))
            where, params = build_filters(req, "agent", default_status="done", default_goal=DEFAULT_GOAL)
            rows = cur.execute(f"SELECT run_id, agent_id, {', '.join(gex)} FROM mat_agent_metrics WHERE {where}",
                               params).fetchall()
            agents = [(r[0], r[1]) for r in rows]
            groups = {(r[0], r[1]): dict(zip(group_keys, r[2:])) for r in rows}
        if not run and x == "step" and metric in ("board_share_rolling5", "cum_board_steps", "cum_tokens"):
            gsel = ", ".join(f"{e} AS g{i}" for i, e in enumerate(gex))
            rows = cur.execute(
                f"WITH m AS (SELECT run_id, agent_id, {gsel} FROM mat_agent_metrics WHERE {where}) "
                f"SELECT {', '.join(f'm.g{i}' for i in range(len(gex)))}, s.step, median(s.{metric}), "
                f"quantile_cont(s.{metric}, 0.25), quantile_cont(s.{metric}, 0.75), count(*) "
                f"FROM v_series_step s JOIN m USING (run_id, agent_id) WHERE s.step IS NOT NULL "
                f"GROUP BY ALL ORDER BY ALL", params).fetchall()
            ng = len(gex)
            xs = sorted({r[ng] for r in rows})
            xi = {v: i for i, v in enumerate(xs)}
            ser: dict = {}
            for r in rows:
                gk = tuple(r[:ng])
                d = ser.get(gk)
                if d is None:
                    d = ser[gk] = {"y": [None] * len(xs), "lo": [None] * len(xs), "hi": [None] * len(xs)}
                k = xi[r[ng]]
                d["y"][k], d["lo"][k], d["hi"][k] = r[ng + 1], r[ng + 2], r[ng + 3]
            n_by = defaultdict(int)
            for key, g in groups.items():
                n_by[tuple(g.get(k) for k in group_keys)] += 1
            out = []
            for gk in sorted(ser, key=lambda k: tuple(str(v) for v in k)):
                s_ = {"key": dict(zip(group_keys, gk)), "label": " | ".join(str(v) for v in gk), "n": n_by.get(gk, 0),
                      "y": ser[gk]["y"]}
                if band:
                    s_["lo"], s_["hi"] = ser[gk]["lo"], ser[gk]["hi"]
                out.append(s_)
            return JSON({"metric": metric, "x": x, "xs": xs, "series": out})
        per_agent = agent_series(cur, agents, metric, x, bucket, span)
        if run:
            xs = sorted({xx for s in per_agent.values() for xx in s})
            out = []
            for key in agents:
                s = per_agent.get(key, {})
                out.append({"key": {"agent_id": key[1]}, "label": f"a{key[1]}", "y": [s.get(xx) for xx in xs]})
            return JSON({"metric": metric, "x": x, "xs": xs, "series": out})
        by_group = defaultdict(list)
        for key, g in groups.items():
            by_group[tuple(g.get(k) for k in group_keys)].append(key)
        xs = sorted({xx for s in per_agent.values() for xx in s})
        out = []
        for gk in sorted(by_group, key=lambda k: tuple(str(v) for v in k)):
            ys, lo, hi = [], [], []
            for xx in xs:
                vals = sorted(per_agent[k][xx] for k in by_group[gk] if k in per_agent and xx in per_agent[k]
                              and per_agent[k][xx] is not None)
                if not vals:
                    ys.append(None)
                    lo.append(None)
                    hi.append(None)
                    continue
                ys.append(statistics.median(vals))
                lo.append(_q(vals, 0.25))
                hi.append(_q(vals, 0.75))
            s = {"key": dict(zip(group_keys, gk)), "label": " | ".join(str(v) for v in gk), "n": len(by_group[gk]),
                 "y": ys}
            if band:
                s["lo"], s["hi"] = lo, hi
            out.append(s)
        return JSON({"metric": metric, "x": x, "xs": xs, "series": out})

    # ------------------------------------------------------------------ 5.11 / 5.12 live
    def live_recent(req):
        limit = qint(req, "limit", 200, 1, sse.RING)
        runs_f = set(multi(req, "run") or []) or None
        kinds = set(multi(req, "kinds") or []) or None
        return JSON({"next_id": bc.next_id, "items": bc.recent(limit, runs_f, kinds)})

    async def live(req):
        runs_f = set(multi(req, "run") or []) or None
        kinds = set(multi(req, "kinds") or []) or None
        last = req.headers.get("last-event-id") or req.query_params.get("last_event_id")
        gen = sse.stream(bc, last, runs_f, kinds, req.is_disconnected)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ 5.13 control
    def control(req):
        return JSON({"error": "not_implemented", "detail": "control API is disabled in v1 (read-only observatory)",
                     "planned": PLANNED_CONTROL}, status_code=501)

    def api_404(req):
        return JSON({"error": "not_found", "detail": f"no API route {req.url.path}"}, status_code=404)

    # ------------------------------------------------------------------ 5.14 static
    BUILD_HINT = ("<!doctype html><title>Swarm Observatory</title><body style='font-family:system-ui;padding:2em'>"
                  "<h1>Frontend not built.</h1><p>Run: <code>cd dash/web &amp;&amp; npm i &amp;&amp; npm run build</code></p>"
                  "<p>The API is up: <a href='/api/health'>/api/health</a></p></body>")

    def index(req):
        p = cfg.web_dist / "index.html"
        if not p.is_file():
            return HTMLResponse(BUILD_HINT, status_code=503)
        rel = req.path_params.get("path", "")
        if rel:
            root = cfg.web_dist.resolve()
            f = (root / rel).resolve()
            if ".." not in Path(rel).parts and f.is_relative_to(root) and f.is_file():
                return FileResponse(f)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    def wrap(fn):
        async def handler(req):
            try:
                if _is_coro(fn):
                    return await fn(req)
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(fn, req)
            except ApiError as e:
                return JSON({"error": e.code, "detail": e.detail}, status_code=e.status)
            except Exception as e:  # never leak a bare 500 page
                import logging
                logging.getLogger("obs.api").exception("handler failed: %s", req.url)
                return JSON({"error": "internal", "detail": repr(e)[:300]}, status_code=500)
        return handler

    routes = [
        Route("/api/health", wrap(health)),
        Route("/api/facets", wrap(facets)),
        Route("/api/runs", wrap(runs)),
        Route("/api/runs/{run_id}", wrap(run_detail)),
        Route("/api/runs/{run_id}/timeline", wrap(timeline)),
        Route("/api/runs/{run_id}/state", wrap(state)),
        Route("/api/runs/{run_id}/graph", wrap(graph)),
        Route("/api/runs/{run_id}/agents/{agent_id}/steps", wrap(agent_steps)),
        Route("/api/metrics/cells", wrap(cells)),
        Route("/api/metrics/series", wrap(series)),
        Route("/api/live/recent", wrap(live_recent)),
        Route("/api/live", wrap(live)),
        Route("/api/control", control, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/api/control/{path:path}", control, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/api/{path:path}", api_404, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
    if (cfg.web_dist / "assets").is_dir():
        routes.append(Mount("/assets", app=StaticFiles(directory=cfg.web_dist / "assets"), name="assets"))
    routes += [Route("/", index), Route("/{path:path}", index)]
    app = Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=1024)])
    app.state.broadcaster = bc
    app.state.db = db
    return app


def _is_coro(fn):
    import inspect
    return inspect.iscoroutinefunction(fn)


def _q(vals: list[float], q: float) -> float:
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    i = int(pos)
    frac = pos - i
    return vals[i] + (vals[min(i + 1, len(vals) - 1)] - vals[i]) * frac


def agent_series(cur, agents: list[tuple[str, int]], metric: str, x: str, bucket: float,
                 span: tuple[float, float] | None = None) -> dict:
    """Per-agent {x: y}. x=step indexes by step; x=t* buckets relative time (seconds) by `bucket`.
    span=(lo, hi): densify every agent over [lo, hi] (counts -> 0, cumulative -> carried forward, 0 before)."""
    if not agents:
        return {}
    run_ids = sorted({r for r, _ in agents})
    want = set(agents)
    steps = defaultdict(list)
    for r, a, step, t, is_board, tok, call in cur.execute(
            "SELECT run_id, agent_id, step, t, is_board, coalesce(tok_prompt, 0) + coalesce(tok_completion, 0), call "
            "FROM steps WHERE list_contains(?, run_id) ORDER BY run_id, agent_id, seq", [run_ids]).fetchall():
        if (r, a) in want:
            steps[(r, a)].append((step, t, bool(is_board), tok or 0.0, call))
    evs = defaultdict(list)  # (run, agent) -> [(t, step, kind)]
    if metric in ("cum_posts", "posts"):
        q = ("SELECT run_id, agent_id, t, step, 'post' FROM posts WHERE list_contains(?, run_id) AND NOT is_seed "
             "AND agent_id IS NOT NULL")
    elif metric == "requests":
        q = ("SELECT run_id, agent_id, t, step, 'req' FROM board_events WHERE list_contains(?, run_id) "
             "AND kind IN ('req', 'irc_cmd') AND agent_id IS NOT NULL")
    elif metric == "model_calls":
        q = "SELECT run_id, agent_id, t, call, 'model' FROM model_calls WHERE list_contains(?, run_id)"
    else:
        q = None
    if q:
        for r, a, t, st, k in cur.execute(q, [run_ids]).fetchall():
            if (r, a) in want:
                evs[(r, a)].append((t, st, k))
    shifts = {}
    if x in ("t_since_contact", "t_since_solve"):
        for r, a, fc, sv in cur.execute(
                "SELECT a.run_id, a.agent_id, (SELECT min(s.t) FROM steps s WHERE s.run_id = a.run_id "
                "AND s.agent_id = a.agent_id AND s.is_board), a.solve_ts - r.t_origin "
                "FROM agents a JOIN runs r USING (run_id) WHERE list_contains(?, a.run_id)", [run_ids]).fetchall():
            v = fc if x == "t_since_contact" else sv
            if v is not None:
                shifts[(r, a)] = v
    out = {}
    for key in agents:
        st = steps.get(key, [])
        if x == "step":
            s = {}
            cum_b = cum_tok = 0.0
            window = []
            posts_by_step = defaultdict(int)
            for t, sp, k in evs.get(key, []):
                if sp is not None:
                    posts_by_step[sp] += 1
            cum_p = 0
            calls_seen = set()
            for step, t, b, tok, call in st:
                if step is None:
                    continue
                cum_b += b
                cum_tok += tok
                window = (window + [b])[-5:]
                cum_p += posts_by_step.get(step, 0)
                y = {"board_share_rolling5": sum(window) / len(window), "cum_board_steps": cum_b, "cum_tokens": cum_tok,
                     "cum_posts": cum_p, "posts": posts_by_step.get(step, 0), "requests": posts_by_step.get(step, 0),
                     "board_steps": int(b)}.get(metric)
                if metric == "model_calls":
                    if call is not None:
                        calls_seen.add(call)
                    y = len(calls_seen)
                s[step] = y
            out[key] = s
            continue
        shift = 0.0
        if x != "t":
            if key not in shifts:
                continue
            shift = shifts[key]

        def b_of(t):
            return round(math.floor((t - shift) / bucket) * bucket, 3)
        s = {}
        if metric in ("board_share_rolling5", "cum_board_steps", "cum_tokens", "board_steps"):
            cum_b = cum_tok = 0.0
            window = []
            for step, t, b, tok, call in st:
                if t is None:
                    continue
                bb = b_of(t)
                cum_b += b
                cum_tok += tok
                window = (window + [b])[-5:]
                if metric == "board_steps":
                    s[bb] = s.get(bb, 0) + int(b)
                else:
                    s[bb] = {"board_share_rolling5": sum(window) / len(window), "cum_board_steps": cum_b,
                             "cum_tokens": cum_tok}[metric]
        else:
            cum = 0
            for t, _sp, _k in sorted((e for e in evs.get(key, []) if e[0] is not None), key=lambda e: e[0]):
                bb = b_of(t)
                cum += 1
                if metric == "cum_posts":
                    s[bb] = cum
                else:
                    s[bb] = s.get(bb, 0) + 1
        # densify over the agent's active span: counts -> 0, cumulative -> carry forward
        ts_ = [t for _s, t, *_ in st if t is not None]
        if ts_ or span:
            lo_b, hi_b = (b_of(min(ts_)), b_of(max(ts_))) if ts_ else (b_of(span[0]), b_of(span[1]))
            if s:
                lo_b, hi_b = min(lo_b, min(s)), max(hi_b, max(s))
            if span:
                lo_b, hi_b = min(lo_b, b_of(span[0])), max(hi_b, b_of(span[1]))
            n_b = int(round((hi_b - lo_b) / bucket))
            last = 0
            for i in range(n_b + 1):
                bb = round(lo_b + i * bucket, 3)
                if bb in s:
                    last = s[bb]
                elif metric in ("requests", "posts", "model_calls", "board_steps"):
                    s[bb] = 0
                else:
                    s[bb] = last
        out[key] = s
    return out
