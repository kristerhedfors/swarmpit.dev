"""Server-side reconstruction of board / agent / graph state at relative time t (CONTRACT 5.6, 5.7)."""
from __future__ import annotations

import json
from collections import defaultdict

NEW_WINDOW_S = 30.0
HEAD = 400


def _j(v, d):
    if v is None:
        return d
    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return d


def _dicts(res):
    cols = [c[0] for c in res.description]
    return [dict(zip(cols, r)) for r in res.fetchall()]


def board_at(cur, run_id: str, t: float, iface: str | None) -> dict:
    posts = _dicts(cur.execute(
        "SELECT uid, post_id, kind, author, author_node, agent_id, is_seed, thread, t, upvotes0, body, flags "
        "FROM posts WHERE run_id = ? AND (t IS NULL OR t <= ?) ORDER BY t NULLS FIRST, uid", [run_id, t]))
    ups = dict(cur.execute(
        "SELECT post_id, count(*) FROM board_events WHERE run_id = ? AND kind = 'upvote' AND t <= ? GROUP BY post_id",
        [run_id, t]).fetchall())
    threads = _dicts(cur.execute(
        "SELECT thread, title, is_seed, created_t, created_by FROM threads WHERE run_id = ? "
        "AND (created_t IS NULL OR created_t <= ?) ORDER BY is_seed DESC, created_t", [run_id, t]))
    by_thread = defaultdict(list)
    dms = []
    for p in posts:
        q = {"uid": p["uid"], "post_id": p["post_id"], "author": p["author"], "author_node": p["author_node"],
             "is_seed": p["is_seed"], "t": p["t"], "upvotes": (p["upvotes0"] or 0) + ups.get(p["post_id"], 0),
             "body": p["body"], "flags": _j(p["flags"], []), "new": p["t"] is not None and p["t"] > t - NEW_WINDOW_S}
        if p["kind"] == "dm":
            q["to"] = p["thread"]
            dms.append(q)
        else:
            by_thread[p["thread"]].append(q)
    out_threads = []
    known = set()
    for th in threads:
        known.add(th["thread"])
        out_threads.append({**th, "posts": by_thread.get(th["thread"], [])})
    for slug, ps in by_thread.items():
        if slug not in known:
            out_threads.append({"thread": slug, "title": slug, "is_seed": False, "created_t": ps[0]["t"],
                                "created_by": ps[0]["author_node"], "posts": ps})
    return {"iface": iface, "n_posts": sum(1 for p in posts if p["kind"] == "post"), "threads": out_threads,
            "dms": dms}


def agents_at(cur, run_id: str, t: float, t_origin: float | None) -> list[dict]:
    agents = _dicts(cur.execute(
        "SELECT agent_id, status, start_ts, end_ts, solve_ts, display_names FROM agents WHERE run_id = ? "
        "ORDER BY agent_id", [run_id]))
    last_steps = {r["agent_id"]: r for r in _dicts(cur.execute(
        "SELECT * EXCLUDE (rn) FROM (SELECT agent_id, step, t, t_end, command, exit, cat, is_board, phase, call, "
        "left(output, ?) AS output_head, row_number() OVER (PARTITION BY agent_id ORDER BY seq DESC) rn "
        "FROM steps WHERE run_id = ? AND t <= ?) WHERE rn = 1", [HEAD, run_id, t]))}
    last_models = {r["agent_id"]: r for r in _dicts(cur.execute(
        "SELECT * EXCLUDE (rn) FROM (SELECT agent_id, call, t, left(reasoning, ?) AS reasoning_head, "
        "left(content, ?) AS content_head, row_number() OVER (PARTITION BY agent_id ORDER BY seq DESC) rn "
        "FROM model_calls WHERE run_id = ? AND t <= ?) WHERE rn = 1", [HEAD, HEAD, run_id, t]))}
    cum_steps = {r["agent_id"]: r for r in _dicts(cur.execute(
        "WITH s AS (SELECT agent_id, seq, is_board, (is_board AND NOT coalesce(lag(is_board) OVER "
        "(PARTITION BY agent_id ORDER BY seq), FALSE)) AS ep FROM steps WHERE run_id = ? AND t <= ?) "
        "SELECT agent_id, count(*) AS steps, count(*) FILTER (WHERE is_board) AS board_steps, "
        "count(*) FILTER (WHERE ep) AS episodes FROM s GROUP BY agent_id", [run_id, t]))}
    roll = {r[0]: r[1] for r in cur.execute(
        "SELECT agent_id, avg(is_board::INT) FROM (SELECT agent_id, is_board, row_number() OVER "
        "(PARTITION BY agent_id ORDER BY seq DESC) rn FROM steps WHERE run_id = ? AND t <= ?) WHERE rn <= 5 "
        "GROUP BY agent_id", [run_id, t]).fetchall()}
    toks = {r["agent_id"]: r for r in _dicts(cur.execute(
        "SELECT agent_id, sum(prompt_tokens) AS tok_prompt, sum(completion_tokens) AS tok_completion, "
        "sum(reasoning_tokens) AS tok_reasoning FROM model_calls WHERE run_id = ? AND t <= ? GROUP BY agent_id",
        [run_id, t]))}
    posts = dict(cur.execute("SELECT agent_id, count(*) FROM posts WHERE run_id = ? AND NOT is_seed AND t <= ? "
                             "GROUP BY agent_id", [run_id, t]).fetchall())
    reqs = dict(cur.execute("SELECT agent_id, count(*) FROM board_events WHERE run_id = ? AND kind IN ('req','irc_cmd') "
                            "AND t <= ? GROUP BY agent_id", [run_id, t]).fetchall())
    out = []
    o = t_origin or 0.0
    for a in agents:
        aid = a["agent_id"]
        st_t = None if a["start_ts"] is None else a["start_ts"] - o
        en_t = None if a["end_ts"] is None else a["end_ts"] - o
        ls = last_steps.get(aid)
        if st_t is None and ls is None:
            status = "not_started"
        elif st_t is not None and t < st_t:
            status = "not_started"
        elif en_t is not None and t >= en_t:
            status = a["status"] if a["status"] in ("crashed", "timed_out") else "done"
        elif a["status"] in ("crashed", "timed_out") and en_t is None and ls is not None:
            status = "running"
        else:
            status = "running"
        solved_t = None if a["solve_ts"] is None else a["solve_ts"] - o
        phase = (ls or {}).get("phase") or "task"
        if solved_t is not None and t >= solved_t:
            phase = "free"
        cs = cum_steps.get(aid, {})
        tk = toks.get(aid, {})
        out.append({
            "agent_id": aid, "status": status, "phase": phase if status != "not_started" else None,
            "step": (ls or {}).get("step"), "call": (last_models.get(aid) or {}).get("call"),
            "names": _j(a["display_names"], []),
            "last_step": ls and {k: ls[k] for k in ("step", "t", "t_end", "command", "exit", "cat", "is_board",
                                                    "output_head")},
            "last_model": last_models.get(aid) and {k: last_models[aid][k] for k in
                                                     ("call", "t", "reasoning_head", "content_head")},
            "cum": {"steps": cs.get("steps", 0), "board_steps": cs.get("board_steps", 0),
                    "episodes": cs.get("episodes", 0), "posts": posts.get(aid, 0), "requests": reqs.get(aid, 0),
                    "tok_prompt": tk.get("tok_prompt") or 0, "tok_completion": tk.get("tok_completion") or 0,
                    "tok_reasoning": tk.get("tok_reasoning") or 0},
            "board_share_rolling5": roll.get(aid),
        })
    return out


def metrics_at(cur, run_id: str, t: float, agents: list[dict] | None, t_origin: float | None) -> dict:
    o = t_origin or 0.0
    r = cur.execute(
        "SELECT (SELECT count(*) FROM agents WHERE run_id = $1 AND start_ts - $3 <= $2), "
        "(SELECT count(DISTINCT agent_id) FROM steps WHERE run_id = $1 AND is_board AND t <= $2), "
        "(SELECT count(*) FROM agents WHERE run_id = $1 AND solve_ts - $3 <= $2), "
        "(SELECT count(*) FROM posts WHERE run_id = $1 AND NOT is_seed AND t <= $2), "
        "(SELECT count(*) FROM board_events WHERE run_id = $1 AND kind IN ('req','irc_cmd') AND t <= $2), "
        "(SELECT count(*) FROM interactions WHERE run_id = $1 AND live_live AND t <= $2), "
        "(SELECT min(t) FROM interactions WHERE run_id = $1 AND live_live AND kind IN ('reply','mention','dm') "
        " AND t <= $2)", [run_id, t, o]).fetchone()
    return {"agents_started": r[0], "agents_discovered": r[1], "agents_solved": r[2], "posts": r[3],
            "requests": r[4], "live_live_edges": r[5], "swarm_onset_t": r[6]}


def graph(cur, run_id: str, t: float | None, kinds: list[str], seed: bool) -> dict:
    params = [run_id, kinds]
    tq = ""
    if t is not None:
        tq = " AND (t IS NULL OR t <= ?)"
        params.append(t)
    rows = cur.execute(
        "SELECT src, dst, kind, count(*) AS n, sum(weight) AS w, min(t) AS first_t, max(t) AS last_t, "
        "list(t ORDER BY t) AS ts "
        f"FROM interactions WHERE run_id = ? AND list_contains(?, kind){tq} GROUP BY ALL ORDER BY first_t",
        params).fetchall()
    edges = []
    for src, dst, kind, n, w, ft, lt, ts in rows:
        if not seed and (dst.startswith("seed:") or src.startswith("seed:")):
            continue
        edges.append({"src": src, "dst": dst, "kind": kind, "n": n, "w": w, "first_t": ft, "last_t": lt,
                      "ts": [round(x, 3) if x is not None else None for x in ts]})
    agents = _dicts(cur.execute(
        "SELECT a.agent_id, a.display_names, a.solve_ts - r.t_origin AS solved_t FROM agents a JOIN runs r "
        "USING (run_id) WHERE run_id = ? ORDER BY agent_id", [run_id]))
    pq = "SELECT author_node, count(*) FROM posts WHERE run_id = ?" + (" AND (t IS NULL OR t <= ?)" if t is not None else "") \
        + " GROUP BY author_node"
    post_counts = dict(cur.execute(pq, [run_id] + ([t] if t is not None else [])).fetchall())
    in_w, out_w = defaultdict(float), defaultdict(float)
    for e in edges:
        out_w[e["src"]] += e["w"] or 0
        in_w[e["dst"]] += e["w"] or 0
    nodes = []
    ids = set()
    for a in agents:
        nid = f"a{a['agent_id']}"
        ids.add(nid)
        nodes.append({"id": nid, "kind": "agent", "label": nid, "agent_id": a["agent_id"],
                      "names": _j(a["display_names"], []), "posts": post_counts.get(nid, 0), "in_w": in_w[nid],
                      "out_w": out_w[nid], "solved_t": a["solved_t"]})
    for e in edges:
        for nid in (e["src"], e["dst"]):
            if nid in ids:
                continue
            ids.add(nid)
            kind = "seed" if nid.startswith("seed:") else ("agent" if nid.startswith("a") else "name")
            nodes.append({"id": nid, "kind": kind, "label": nid.split(":", 1)[-1], "posts": post_counts.get(nid, 0),
                          "in_w": in_w[nid], "out_w": out_w[nid]})
    pairs = {(e["src"], e["dst"]) for e in edges if e["src"].startswith("a") and e["dst"].startswith("a")
             and e["src"] != e["dst"]}
    n = len(agents)
    density = len(pairs) / (n * (n - 1)) if n > 1 else 0.0
    recip = (sum(1 for (s, d) in pairs if (d, s) in pairs) / len(pairs)) if pairs else 0.0
    return {"t": t, "nodes": nodes, "edges": edges,
            "summary": {"live_live_edges": sum(e["n"] for e in edges if (e["src"], e["dst"]) in pairs),
                        "density": round(density, 4), "reciprocity": round(recip, 4)}}
