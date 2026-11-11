#!/usr/bin/env python3
"""runs/ -> public/data bundles for swarmpit.dev (SPEC section 4).

Pipeline: scratch obs DB (dash/server `obs ingest --rebuild`, reusing the CONTRACT derive verbatim) ->
per-run CoreBundle + DetailBundle (types in src/types.ts) -> redaction (tools/redact.py) -> parity
assertions -> versioned files -> public gate (tools/check_public.py).

    python3 tools/export.py [--runs DIR] [--out public/data] [--db .cache/obs-export.duckdb] [--no-ingest]
                            [--include-tagged] [--mask-ctf] [--featured RUN] [--synth] [--deny WORD ...]
                            [--force] [--run RUN ...]

Models are an experimental factor: cards, findings cells and pooled rows carry `model`, ordered primary model
first (lab/config.MODELS and MODEL_ORDER, read by path; SPEC 4.10).

The CTF goal is another factor (SPEC 4.11): runs.params.goal 'unsolvable' (run id segment `-unsolvable` after `r<rep>`) marks runs
whose flag can never be found. Those cores, cards and findings rows carry `goal: 'unsolvable'`; solvable runs
carry no goal key at all, so their bundles, hashes and index rows stay byte-identical to pre-goal exports.

Needs `duckdb`; when it is not importable the script re-executes itself with the dash/server venv python
(or `uv run --no-sync --project ../dash/server python`). runs/ is opened read-only ("rb"); msb, .env,
dash/ and lab/ are never written.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SITE = Path(__file__).resolve().parents[1]
REPO = SITE.parent
DASH_SERVER = REPO / "dash" / "server"
TOOLS = SITE / "tools"
sys.path.insert(0, str(TOOLS))

from redact import Redactor, default_denylist  # noqa: E402


def _default_runs() -> Path:
    if (REPO / "runs").is_dir():
        return REPO / "runs"
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out:
            p = Path(out)
            p = p if p.is_absolute() else (REPO / p).resolve()
            if (p.parent / "runs").is_dir():
                return p.parent / "runs"
    except (OSError, subprocess.SubprocessError):
        pass
    return REPO / "runs"


# ================================================================================================ tables
# Mirrors src/types.ts exactly (frozen order).
NODE_KINDS = ["task", "board", "agent", "thread", "post", "persona", "handle"]
EDGE_KINDS = ["contains", "authored", "works", "reads", "writes", "exposure", "reply", "mention", "dm", "upvote"]
EVENT_KINDS = ["boardStart", "start", "end", "crash", "retry", "think", "cmd", "board", "submit", "idle",
               "read", "list", "doc", "miss", "post", "upvote", "delete", "dm", "join", "connect", "disconnect", "proto"]
STEP_CATS = ["idle", "board", "submit", "persistence", "ctf", "recon", "other"]
CHAPTER_KINDS = ["boot", "firstContact", "contact", "solve", "phaseFree", "post", "leak", "onset", "upvote", "dm",
                 "persistence", "crash", "end"]
IMPORTANCE = ["leak", "onset", "solve", "firstContact", "dm", "post", "crash", "contact", "upvote", "persistence",
              "end", "phaseFree", "boot"]
POST_FLAGS = {"secret_leak", "self_id", "injection", "memory_note"}
EF = dict(BOARD=1, SOLVE=2, LEAK=4, PERSIST=8, FREE=16, BACKGROUND=32, HEURISTIC=64, SUBMIT_OK=128, ERROR=256,
          LIVE_LIVE=512)
NK = {k: i for i, k in enumerate(NODE_KINDS)}
EK = {k: i for i, k in enumerate(EDGE_KINDS)}
VK = {k: i for i, k in enumerate(EVENT_KINDS)}
SC = {k: i for i, k in enumerate(STEP_CATS)}
HIDDEN_BY_DEFAULT = {"think", "idle", "proto", "connect", "disconnect"}  # index density (SPEC 4.2)

INDEX_FORMAT, CORE_FORMAT, DETAIL_FORMAT = "swarmpit.index/1", "swarmpit.run/1", "swarmpit.detail/1"

TRUNC = {"l": 160, "label": 48, "preview": 240, "title": 120, "cmd": 600, "out_head": 700, "out_tail": 300,
         "reason": 900, "content": 400, "body": 1500, "text": 400}

NOUNS = {
    "forum": dict(thread="thread", threads="threads", post="post", posts="posts", board="forum"),
    "wiki": dict(thread="page", threads="pages", post="edit", posts="edits", board="wiki"),
    "cache": dict(thread="directory", threads="directories", post="artifact", posts="artifacts", board="artifact cache"),
    "irc": dict(thread="channel", threads="channels", post="message", posts="messages", board="IRC server"),
}
IFACE_ORDER = ["forum", "wiki", "cache", "irc"]
CAVEAT_TEXT = {
    "irc_inbound_broken": "IRC inbound was broken in this run: agent bytes never reached the board (pilot bug).",
    "tagged": "Calibration / smoke run, not part of the pilot cells.",
    "old_harness": "Recorded with an older harness version (voluntary_stop endings).",
    "final_missing": "Final copy-out missing; reconstructed from the live tap.",
    "heuristic_edges": "Reply and exposure edges are inferred; see edge tooltips.",
    "synthetic": "Synthetic stress-test data, not a real run.",
    "unsolvable": "Unsolvable goal: the flag can never be found, so no agent solves and there is no free time.",
}

# ================================================================================================ goals
# 'solvable' is the historical CTF and is implied when runs.params has no goal; it is never written to a bundle.
GOALS = ("solvable", "unsolvable")
DEFAULT_GOAL = "solvable"


def run_goal(params) -> str:
    """A run's goal from runs.params (dict or JSON text); absent or unknown reads as the solvable CTF."""
    p = jl(params, {})
    g = p.get("goal") if isinstance(p, dict) else None
    return g if g in GOALS else DEFAULT_GOAL


def goal_of(x: dict) -> str:
    """Goal of a card, core run meta or findings row (no key = solvable)."""
    return x.get("goal") or DEFAULT_GOAL


def goal_kw(goal: str) -> dict:
    """The `goal` key to splice into an output row: empty for the solvable default (byte parity)."""
    return {} if goal == DEFAULT_GOAL else {"goal": goal}

# ================================================================================================ models
# The Cerebras pilot model: rank 0 everywhere, the only auto-featured candidate, and the one model whose
# findings are cross-checked against runs/analysis.json (others against runs/analysis_models.json).
PRIMARY = "qwen-3.8-27b"
_MODELS: list[dict] | None = None


def _lab_config():
    """lab/config.py loaded by path (lab/ is not on sys.path and is never written)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_swarmpit_lab_config", REPO / "lab" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def model_registry() -> list[dict]:
    """[{id, slug, label, provider}] in lab MODEL_ORDER, from lab/config.MODELS (the single source of model
    metadata). A lab tree without MODELS yields only the primary model."""
    global _MODELS
    if _MODELS is None:
        cfg = _lab_config()
        M = getattr(cfg, "MODELS", None)
        if not M:
            _MODELS = [{"id": PRIMARY, "slug": "qwen38", "label": PRIMARY, "provider": "cerebras"}]
        else:
            order = list(getattr(cfg, "MODEL_ORDER", None) or M)
            _MODELS = [{"id": M[s]["model"], "slug": s, "label": M[s].get("label") or M[s]["model"],
                        "provider": M[s].get("provider") or ""} for s in order if s in M]
    return _MODELS


def model_rank(mid: str | None) -> tuple[int, str]:
    """Sort key: the primary model first, then lab MODEL_ORDER, then unknown ids alphabetically."""
    mid = mid or PRIMARY
    if mid == PRIMARY:
        return 0, mid
    ids = [m["id"] for m in model_registry() if m["id"] != PRIMARY]
    return (1 + ids.index(mid) if mid in ids else 1 + len(ids)), mid


def model_meta(mid: str) -> dict:
    m = next((x for x in model_registry() if x["id"] == mid), None)
    if m:
        return m
    return {"id": mid, "slug": re.sub(r"[^a-z0-9]", "", mid.split("/")[-1].lower())[:12] or "model", "label": mid,
            "provider": ""}


# ================================================================================================ helpers
def ms(t) -> int | None:
    return None if t is None else int(round(float(t) * 1000))


def ctf_flags(ctf: dict) -> list[str]:
    """Flag-like CTF values: `flag` first, then any other `*flag*` field (an unsolvable run's never-placed expected
    flag or sealed plaintext, whatever lab names it), in key order."""
    ks = ["flag"] + sorted(k for k in ctf if k != "flag" and "flag" in k.lower())
    return list(dict.fromkeys(v for k in ks if isinstance(v := ctf.get(k), str) and v))


def ctf_keys(ctf: dict) -> list[str]:
    """Hex seal keys: `key_hex` (served by vaultd) first, then any other `*key_hex` field (an unsolvable run's
    host-only `seal_key_hex`, never placed in the VM)."""
    ks = ["key_hex"] + sorted(k for k in ctf if k != "key_hex" and k.lower().endswith("key_hex"))
    return list(dict.fromkeys(v for k in ks if isinstance(v := ctf.get(k), str) and v))


def ctf_seals(ctf: dict) -> list[str]:
    """The synthetic vault seal: base64(reverse(flag XOR key)) (lab/agent/setup_vm.py) plus the non-reversed
    variant, for every (flag-like value, hex key) pair. It is derivable from the flag and key, which are shown
    deliberately, so it is allow-listed (or masked with --mask-ctf) instead of being eaten inconsistently by the
    entropy rule. A solvable run has one pair (flag, key_hex), giving the historical list; an unsolvable run's
    vault is sealed under seal_key_hex, so those pairs cover it whichever plaintext lab sealed."""
    out = []
    for fl in ctf_flags(ctf):
        for kh in ctf_keys(ctf):
            try:
                k = bytes.fromhex(kh)
            except ValueError:
                continue
            if not k:
                continue
            x = bytes(c ^ k[i % len(k)] for i, c in enumerate(fl.encode()))
            for b in (x[::-1], x):
                e = base64.b64encode(b).decode()
                out += [e, e.rstrip("=")]
    return list(dict.fromkeys(out))


def ctf_allow(ctf: dict) -> list[str]:
    base = [v for v in (ctf.get("flag"), ctf.get("passphrase"), ctf.get("key_hex"), ctf.get("vault_id")) if v]
    extra = [v for v in ctf_flags(ctf) + ctf_keys(ctf) if v not in base]
    return base + extra + ctf_seals(ctf)


def trunc(s: str | None, n: int) -> str:
    """Truncate to n chars (incl. the ellipsis), preferring a whitespace boundary in the last 24 chars so a
    cut never leaves a fragment such as '/home/ag' or 'x@board.inte' that reads as a different token."""
    s = s or ""
    if len(s) <= n:
        return s
    cut = max(0, n - 1)
    ws = max(s.rfind(" ", 0, cut), s.rfind("\n", 0, cut), s.rfind("\t", 0, cut))
    if ws >= cut - 24 and ws > 0:
        cut = ws
    return s[:cut] + "…"


def one_line(s: str | None) -> str:
    s = (s or "").strip()
    return re.sub(r"\s*\n\s*", " ⏎ ", s)


def head_tail(s: str | None) -> str:
    s = s or ""
    h, t = TRUNC["out_head"], TRUNC["out_tail"]
    if len(s) <= h + t + 24:
        return s
    a = h
    nl = max(s.rfind("\n", 0, h), s.rfind(" ", 0, h))
    if nl >= h - 80 and nl > 0:
        a = nl
    b = len(s) - t
    nl = min([x for x in (s.find("\n", b), s.find(" ", b)) if x >= 0] or [b])
    if nl <= b + 80:
        b = nl + 1
    return s[:a] + f"\n…[{b - a} chars]…\n" + s[b:]


def mmss(t_ms: int | None) -> str:
    if t_ms is None:
        return "?"
    s = max(0, t_ms) / 1000
    return f"{int(s // 60)}:{int(s % 60):02d}"


def jdump(o) -> str:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def jl(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class Strings:
    def __init__(self):
        self.list = [""]
        self.idx = {"": 0}

    def __call__(self, s: str) -> int:
        s = s or ""
        i = self.idx.get(s)
        if i is None:
            i = self.idx[s] = len(self.list)
            self.list.append(s)
        return i


def redact_tree(o, R: Redactor):
    """Walk the final object tree so that no string field can bypass redaction (SPEC 4.8)."""
    if isinstance(o, str):
        return R.redact(o)
    if isinstance(o, list):
        return [redact_tree(v, R) for v in o]
    if isinstance(o, dict):
        return {(R.redact(k) if isinstance(k, str) else k): redact_tree(v, R) for k, v in o.items()}
    return o


def leak_spans(body: str, needles: list[str]) -> list[list[int]]:
    spans = []
    for n in needles:
        if not n:
            continue
        i = body.find(n)
        while i >= 0:
            spans.append([i, i + len(n)])
            i = body.find(n, i + len(n))
    spans.sort()
    merged: list[list[int]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    return merged


class ExportError(Exception):
    pass


# ================================================================================================ per-run build
class RunBuilder:
    def __init__(self, con, rid: str, R: Redactor, runs_dir: Path, mask_ctf: bool, meta: dict):
        self.con, self.rid, self.runs_dir, self.mask_ctf, self.meta = con, rid, runs_dir, mask_ctf, meta
        self.R = R
        self.errors: list[str] = []

    # ------------------------------------------------------------------------------------ small utils
    def red(self, s: str | None) -> str:
        return self.R.redact(s or "")

    def node(self, nid: str, k: str, label: str, t0, **kw) -> int:
        i = len(self.nodes)
        rec = {"id": nid, "k": NK[k], "label": trunc(self.red(label), TRUNC["label"]), "t0": t0}
        rec.update({a: b for a, b in kw.items() if b is not None})
        self.nodes.append(rec)
        self.nidx[nid] = i
        return i

    def edge(self, s: int, d: int, k: str, inferred=False, ev_text: str | None = None) -> int:
        key = (s, d, EK[k])
        e = self.eidx.get(key)
        if e is None:
            e = self.eidx[key] = len(self.edges)
            rec = {"s": s, "d": d, "k": EK[k]}
            if inferred:
                rec["inferred"] = True
            if ev_text:
                rec["ev"] = self.S(trunc(self.red(ev_text), TRUNC["l"]))
            if self.nodes[s]["k"] == NK["agent"] and self.nodes[d]["k"] == NK["agent"] and s != d:
                rec["liveLive"] = True
            self.edges.append(rec)
            self.efires.append([])
        return e

    def fire(self, e: int, t: int, w: float = 1.0, ev=None):
        self.efires[e].append((int(t), float(w), ev))

    def event(self, t, k, a=-1, n=-1, e=-1, f=0, s=-1, c=-1, label="", x=None, d=0, lane=None, rank=3, seq=0,
              src=None) -> dict:
        ev = dict(t=int(t), d=max(0, int(d or 0)), k=VK[k], a=-1 if a is None else int(a), n=n, e=e, f=f,
                  s=-1 if s is None else int(s), c=c, l=label, x=x,
                  lane=(-1 if a is None or a < 0 else a) if lane is None else lane, rank=rank, seq=seq, src=src)
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------------------------------ build
    def build(self):
        con, rid = self.con, self.rid
        self.S = Strings()
        self.nodes: list[dict] = []
        self.nidx: dict[str, int] = {}
        self.edges: list[dict] = []
        self.eidx: dict[tuple, int] = {}
        self.efires: list[list] = []
        self.events: list[dict] = []

        r = rows(con, "SELECT * FROM runs WHERE run_id = ?", [rid])[0]
        self.run = r
        harness = jl(r["harness"], {})
        params = jl(r["params"], {})
        goal = run_goal(params)
        ctf = jl(r["ctf"], {}) or {}
        self.ctf_values = ctf_allow(ctf)
        iface = r["iface"]
        nouns = NOUNS.get(iface, NOUNS["forum"])
        t_origin = r["t_origin"]
        tEnd = ms(r["t_end"]) or 0
        seed = rows(con, "SELECT board_name FROM seeds WHERE seed_hash = ?", [r["seed_hash"]])
        board_name = (seed[0]["board_name"] if seed else None) or "agent-commons"

        agents = rows(con, "SELECT * FROM agents WHERE run_id = ? ORDER BY agent_id", [rid])
        mets = {m["agent_id"]: m for m in rows(con, "SELECT * FROM mat_agent_metrics WHERE run_id = ?", [rid])}
        runm = rows(con, "SELECT * FROM mat_run_metrics WHERE run_id = ?", [rid])
        runm = runm[0] if runm else {}
        steps = rows(con, "SELECT * FROM steps WHERE run_id = ? ORDER BY agent_id, seq", [rid])
        calls = rows(con, "SELECT * FROM model_calls WHERE run_id = ? ORDER BY agent_id, seq", [rid])
        recs = rows(con, "SELECT * FROM agent_records WHERE run_id = ? ORDER BY agent_id, seq", [rid])
        bevs = rows(con, "SELECT * FROM board_events WHERE run_id = ? ORDER BY seq", [rid])
        posts = rows(con, "SELECT * FROM posts WHERE run_id = ? ORDER BY is_seed DESC, t, uid", [rid])
        threads = rows(con, "SELECT * FROM threads WHERE run_id = ?", [rid])
        inter = rows(con, "SELECT * FROM interactions WHERE run_id = ? ORDER BY t, edge_uid", [rid])
        n_agents = max(int(r["n_agents"] or 0), len(agents))
        self.n_agents = n_agents
        amap = {a["agent_id"]: a for a in agents}

        # ------------------------------------------------------------------ nodes: task, board, agents
        self.node("task", "task", "CTF vault", None)
        self.node("board", "board", f"{board_name} · {nouns['board']}", None)
        for i in range(n_agents):
            a = amap.get(i, {})
            st = ms(a["start_ts"] - t_origin) if a.get("start_ts") is not None else None
            names = [self.red(x) for x in jl(a.get("display_names"), [])]
            self.node(f"a{i}", "agent", f"a{i}", st, agent=i, names=names)

        # ------------------------------------------------------------------ threads
        th: dict[str, dict] = {}
        for t in threads:
            th[t["thread"]] = dict(slug=t["thread"], title=t["title"] or t["thread"], seed=bool(t["is_seed"]),
                                   t0=None if t["is_seed"] else ms(t["created_t"]))
        def ensure_thread(slug, t_ms, title=None):
            if slug not in th:
                th[slug] = dict(slug=slug, title=title or slug, seed=False, t0=t_ms)
            elif not th[slug]["seed"] and th[slug]["t0"] is not None and t_ms is not None and t_ms < th[slug]["t0"]:
                th[slug]["t0"] = t_ms
        for b in bevs:
            tk, tg = b["target_kind"], b["target"]
            if b["kind"] == "req" and tk == "thread" and tg:
                ensure_thread(tg, ms(b["t"]))
            elif b["kind"] == "req" and tk == "listing" and tg and iface == "cache":
                ensure_thread(tg, ms(b["t"]))
        for p in posts:
            if p["kind"] == "dm":
                ensure_thread(f"dm:{p['thread']}", ms(p["t"]), title=f"DM {p['thread']}")
            elif p["thread"]:
                ensure_thread(p["thread"], ms(p["t"]))
        def th_key(x):
            return (0 if x["seed"] else 1, x["t0"] if x["t0"] is not None else -1, x["slug"])
        for x in sorted(th.values(), key=th_key):
            slug = x["slug"]
            label = ("#" + slug) if iface == "irc" and not slug.startswith("dm:") else slug
            self.node(f"th:{slug}", "thread", label, None if x["seed"] else x["t0"],
                      title=trunc(self.red(x["title"]), TRUNC["title"]))

        # ------------------------------------------------------------------ posts
        self.post_body: dict[str, str] = {}
        self.post_mentions: dict[str, list] = {}
        seeds = [p for p in posts if p["is_seed"]]
        lives = [p for p in posts if not p["is_seed"]]
        seeds.sort(key=lambda p: (p["t"], p["post_id"] or 0))
        lives.sort(key=lambda p: (p["t"], p["uid"]))
        personas: list[str] = []
        for p in seeds:
            if p["author_node"] and p["author_node"] not in personas:
                personas.append(p["author_node"])
        handles: list[str] = []
        for it in inter:
            for nd in (it["src"], it["dst"]):
                if nd and nd.startswith("seed:") and nd not in personas:
                    personas.append(nd)
                if nd and nd.startswith("name:") and nd not in handles:
                    handles.append(nd)
        handles.sort()
        # node indices of personas/handles are only known after posts; compute ahead
        n_threads_end = len(self.nodes)
        n_posts = len(seeds) + len(lives)
        persona_idx = {pn: n_threads_end + n_posts + j for j, pn in enumerate(personas)}
        handle_idx = {hn: n_threads_end + n_posts + len(personas) + j for j, hn in enumerate(handles)}

        def author_index(an):
            if an is None:
                return None
            if an in self.nidx:
                return self.nidx[an]
            return persona_idx.get(an, handle_idx.get(an))

        for p in seeds + lives:
            body = self.red(p["body"] or "")
            uid = p["uid"]
            self.post_body[uid] = body
            self.post_mentions[uid] = [self.red(m) for m in jl(p["mentions"], [])]
            thread_slug = f"dm:{p['thread']}" if p["kind"] == "dm" else p["thread"]
            agent = p["agent_id"]
            who = f"a{agent}" if agent is not None else (p["author"] or "?")
            if p["kind"] == "dm":
                label = f"dm {who}→{p['thread']}"
            else:
                label = f"#{p['post_id']} {who}"
            flags = [f for f in jl(p["flags"], []) if f in POST_FLAGS]
            kw = dict(thread=self.nidx.get(f"th:{thread_slug}"), author=author_index(p["author_node"]),
                      agent=agent, postId=p["post_id"], preview=trunc(one_line(body), TRUNC["preview"]),
                      bodyLen=int(p["body_len"] or len(p["body"] or "")))
            if flags:
                kw["flags"] = flags
            if p["is_seed"]:
                kw.update(seed=True, tSeed=ms(p["t"]))
                if p["upvotes0"]:
                    kw["upvotes0"] = int(p["upvotes0"])
                self.node(uid, "post", label, None, **kw)
            else:
                self.node(uid, "post", label, ms(p["t"]), **kw)
        for pn in personas:
            self.node(pn, "persona", pn.split(":", 1)[1], None)
        for hn in handles:
            self.node(hn, "handle", hn.split(":", 1)[1], None)
        assert all(self.nidx[k] == v for k, v in persona_idx.items())

        # ------------------------------------------------------------------ deletes
        for b in bevs:
            if b["kind"] == "delete" and b["post_id"] is not None:
                for uid in (f"p{b['post_id']}", f"s{b['post_id']}"):
                    if uid in self.nidx:
                        self.nodes[self.nidx[uid]]["t1"] = ms(b["t"])
                        break

        # ------------------------------------------------------------------ phase lookups
        step_phase = {(s["agent_id"], s["step"]): s["phase_before"] for s in steps}
        solve_ms = {a["agent_id"]: ms(a["solve_ts"] - t_origin) for a in agents if a.get("solve_ts") is not None}

        def phase_at(agent, step, t_ms):
            if agent is None:
                return "task"
            if step is not None and (agent, step) in step_phase:
                return step_phase[(agent, step)]
            sv = solve_ms.get(agent)
            return "free" if sv is not None and t_ms >= sv else "task"

        # ------------------------------------------------------------------ events: steps.jsonl records
        for rc in recs:
            kind = rc["kind"]
            a = rc["agent_id"]
            data = jl(rc["data"], {})
            t = ms(rc["t"]) if rc["t"] is not None else 0
            an = self.nidx.get(f"a{a}", -1)
            if kind == "start":
                self.event(t, "start", a, an, label="start", rank=1, seq=rc["seq"], src="rec")
            elif kind == "end":
                txt = (f"ended: {data.get('ended') or rc['detail'] or 'end'} · solved {bool(data.get('solved'))}"
                       f" · {data.get('steps_used', '?')} steps · {data.get('model_calls', '?')} model calls")
                self.event(t, "end", a, an, label=f"end · {data.get('ended') or rc['detail'] or ''}".strip(" ·"),
                           x={"t": "line", "text": trunc(self.red(txt), TRUNC["text"])}, rank=1, seq=rc["seq"], src="rec")
            elif kind == "crash":
                txt = self.red(rc["detail"] or data.get("error") or "crash")
                self.event(t, "crash", a, an, label=trunc(one_line("crash: " + txt), TRUNC["l"]),
                           x={"t": "line", "text": trunc(txt, TRUNC["text"])}, rank=1, seq=rc["seq"], src="rec")
            elif kind == "api_retry":
                txt = self.red(f"API retry attempt {rc['attempt']}, waited {rc['wait']} s"
                               + (f": {rc['detail']}" if rc["detail"] else ""))
                self.event(t, "retry", a, an, label=trunc(one_line(txt), TRUNC["l"]),
                           x={"t": "line", "text": trunc(txt, TRUNC["text"])}, rank=1, seq=rc["seq"], src="rec")

        for mc in calls:
            a = mc["agent_id"]
            reason = self.red((mc["reasoning"] or "").strip())
            content = self.red((mc["content"] or "").strip())
            lab = one_line(reason or content)
            tok = [int(mc["prompt_tokens"] or 0), int(mc["completion_tokens"] or 0), int(mc["reasoning_tokens"] or 0),
                   int(mc["cached_tokens"] or 0)]
            self.event(ms(mc["t"]), "think", a, self.nidx.get(f"a{a}", -1),
                       f=EF["FREE"] if mc["phase"] == "free" else 0, s=mc["call"],
                       label=trunc(lab, TRUNC["l"]), d=ms(mc["latency_s"] or 0),
                       x={"t": "think", "reason": trunc(reason, TRUNC["reason"]), "content": trunc(content, TRUNC["content"]),
                          "nTools": int(mc["n_tool_calls"] or 0), "tok": tok},
                       rank=0, seq=mc["seq"], src="model")

        self.step_ev: dict[tuple, dict] = {}
        for st in steps:
            a = st["agent_id"]
            t = ms(st["t"])
            d = ms((st["t_end"] or st["t"]) - st["t"]) if st["t_end"] is not None else 0
            f = EF["FREE"] if st["phase_before"] == "free" else 0
            if st["is_solve"]:
                f |= EF["SOLVE"]
            if st["idle"]:
                ev = self.event(t, "idle", a, -1, f=f, s=st["step"], c=SC["idle"], label="", d=d, rank=1, seq=st["seq"],
                                src="step")
                self.step_ev[(a, st["step"])] = ev
                continue
            cmd = self.red(st["command"] or "")
            if st["exit"] is not None and int(st["exit"]) != 0:
                f |= EF["ERROR"]
            if st["persistence"]:
                f |= EF["PERSIST"]
            subs = [self.red(s.get("value") if isinstance(s, dict) else str(s)) for s in jl(st["submissions"], [])]
            det = {"t": "step", "cmd": trunc(cmd, TRUNC["cmd"]), "out": head_tail(self.red(st["output"] or "")),
                   "outLen": int(st["output_len"] or len(st["output"] or "")),
                   "exit": None if st["exit"] is None else int(st["exit"])}
            if st["board_via"] in ("relay", "taint"):
                det["via"] = st["board_via"]
            if subs:
                det["submit"] = subs
            cat = st["cat"] or "other"
            if st["is_board"]:
                f |= EF["BOARD"]
                if st["board_via"] in ("relay", "taint"):
                    f |= EF["HEURISTIC"]
                ev = self.event(t, "board", a, 1, f=f, s=st["step"], c=SC["board"], label=trunc(one_line(cmd), TRUNC["l"]),
                                x=det, d=d, rank=1, seq=st["seq"], src="step")
            else:
                k = "submit" if cat == "submit" else "cmd"
                if k == "submit" and st["submit_correct"]:
                    f |= EF["SUBMIT_OK"]
                e = self.edge(self.nidx[f"a{a}"], 0, "works")
                ev = self.event(t, k, a, 0, e=e, f=f, s=st["step"], c=SC.get(cat, SC["other"]),
                                label=trunc(one_line(cmd), TRUNC["l"]), x=det, d=d, rank=1, seq=st["seq"], src="step")
                self.fire(e, t, 1, ev)
            self.step_ev[(a, st["step"])] = ev

        # ------------------------------------------------------------------ events: board
        # fold write reqs into the post/upvote/delete they accompany; fold PRIVMSG/NOTICE into the irc post/dm
        self.board_line_ev: dict[int, dict] = {}
        folded_lines: set[int] = set()
        accomp_req: dict[int, dict] = {}
        accomp_irc: dict[int, dict] = {}
        by_agent: dict = {}
        for b in bevs:
            by_agent.setdefault(b["agent_id"], []).append(b)
        for a, lst in by_agent.items():
            used: set[int] = set()
            for j, b in enumerate(lst):
                if b["kind"] in ("post", "upvote", "delete", "dm"):
                    for nb in lst[j + 1:]:
                        if nb["kind"] == "req" and nb["target_kind"] == "write" and nb["line_off"] not in used:
                            used.add(nb["line_off"])
                            accomp_req[b["line_off"]] = nb
                            break
                        if nb["kind"] in ("post", "upvote", "delete", "dm"):
                            break
                    if iface == "irc" and b["kind"] in ("post", "dm"):
                        for pb in reversed(lst[:j]):
                            if (pb["kind"] == "irc_cmd" and (pb["cmd"] or "").upper() in ("PRIVMSG", "NOTICE")
                                    and pb["line_off"] not in used):
                                used.add(pb["line_off"])
                                accomp_irc[b["line_off"]] = pb
                                break
        for b in bevs:
            if b["kind"] == "req" and b["target_kind"] == "write" and (b["status"] or 0) < 400:
                folded_lines.add(b["line_off"])
        for pb in accomp_irc.values():
            folded_lines.add(pb["line_off"])
        self.folded = len(folded_lines)

        born_thread: set[int] = set()
        board_start_ev = None
        board_sourced = 0
        for b in bevs:
            kind = b["kind"]
            a = b["agent_id"]
            t = ms(b["t"]) if b["t"] is not None else 0
            seq = b["seq"]
            lo = b["line_off"]
            if lo in folded_lines and kind in ("req", "irc_cmd"):
                continue
            board_sourced += 1
            an = self.nidx.get(f"a{a}") if a is not None else None
            f = 0
            if b["attribution"] == "background":
                f |= EF["BACKGROUND"]
            if a is not None and phase_at(a, b["step"], t) == "free":
                f |= EF["FREE"]
            if kind == "board_start":
                ev = self.event(t, "boardStart", None, 1, f=0, label=f"{board_name} opens ({nouns['board']})",
                                lane=-1, seq=seq, src="board")
                board_start_ev = ev
            elif kind == "req":
                status = int(b["status"] or 0)
                tk, tg = b["target_kind"], b["target"]
                path = self.red(b["path"] or "")
                det = {"t": "req", "method": b["method"] or "", "path": trunc(path, TRUNC["cmd"]), "status": status,
                       "bytes": int(b["bytes"] or 0)}
                lab = f"{b['method'] or ''} {path}".strip()
                if status >= 400 or tk in (None, "none", "write"):
                    if status >= 400:
                        f |= EF["ERROR"]
                    ev = self.event(t, "miss", a, 1, f=f, s=b["step"], label=trunc(f"{lab} {status}", TRUNC["l"]),
                                    x=det, seq=seq, src="board")
                else:
                    if tk == "thread":
                        k, n = "read", self.nidx.get(f"th:{tg}", 1)
                    elif tk == "post":
                        k, n = "read", self.nidx.get(tg, 1)
                    elif tk == "listing":
                        k, n = "list", (self.nidx.get(f"th:{tg}", 1) if (iface == "cache" and tg) else 1)
                    else:
                        k, n = "doc", 1
                    e = -1
                    if an is not None:
                        e = self.edge(an, n, "reads")
                    ev = self.event(t, k, a, n, e=e, f=f, s=b["step"], label=trunc(lab, TRUNC["l"]), x=det, seq=seq,
                                    src="board")
                    if e >= 0:
                        self.fire(e, t, 1, ev)
            elif kind in ("post", "dm"):
                uid = f"p{b['post_id']}" if kind == "post" else f"d{lo}"
                pn = self.nidx.get(uid, -1)
                prow = next((p for p in lives if p["uid"] == uid), None)
                flags = jl(prow["flags"], []) if prow else []
                if "secret_leak" in flags:
                    f |= EF["LEAK"]
                slug = (f"dm:{b['to_nick']}" if kind == "dm" else b["thread"]) or ""
                tn = self.nidx.get(f"th:{slug}", -1)
                body = self.post_body.get(uid, self.red(b["body"] or ""))
                tl = self.nodes[tn]["label"] if tn >= 0 else slug
                if kind == "post":
                    lab = f'{tl}: "{one_line(body)}"'
                else:
                    lab = f'DM {b["nick"] or ""} → {b["to_nick"] or ""}: "{one_line(body)}"'
                x = None
                if lo in accomp_req:
                    q = accomp_req[lo]
                    x = {"t": "req", "method": q["method"] or "", "path": trunc(self.red(q["path"] or ""), TRUNC["cmd"]),
                         "status": int(q["status"] or 0), "bytes": int(q["bytes"] or 0)}
                elif lo in accomp_irc:
                    x = {"t": "line", "text": trunc(self.red(accomp_irc[lo]["line"] or ""), TRUNC["text"])}
                ev = self.event(t, kind, a, pn, f=f, s=b["step"], label=trunc(lab, TRUNC["l"]), x=x, seq=seq, src="board")
                if an is not None and tn >= 0:
                    e = self.edge(an, tn, "writes")
                    self.fire(e, t, 1, ev)
                    if kind == "post":
                        ev["e"] = e
                if an is not None and pn >= 0:
                    self.fire(self.edge(an, pn, "authored"), t, 1, ev)
                if tn >= 0 and pn >= 0:
                    self.fire(self.edge(tn, pn, "contains"), t, 1, ev)
                if tn >= 0 and tn not in born_thread and self.nodes[tn]["t0"] is not None:
                    born_thread.add(tn)
                    self.fire(self.edge(1, tn, "contains"), t, 1, ev)
            elif kind in ("upvote", "delete"):
                pid = b["post_id"]
                uid = f"p{pid}" if f"p{pid}" in self.nidx else f"s{pid}"
                pn = self.nidx.get(uid, -1)
                if kind == "upvote":
                    lab = f"▲ #{pid} {b['author'] or ''}".strip()
                else:
                    lab = f"delete #{pid}"
                x = None
                if lo in accomp_req:
                    q = accomp_req[lo]
                    x = {"t": "req", "method": q["method"] or "", "path": trunc(self.red(q["path"] or ""), TRUNC["cmd"]),
                         "status": int(q["status"] or 0), "bytes": int(q["bytes"] or 0)}
                ev = self.event(t, kind, a, pn, f=f, s=b["step"], label=trunc(self.red(lab), TRUNC["l"]), x=x, seq=seq,
                                src="board")
                if kind == "delete" and an is not None and pn >= 0:
                    tn = self.nodes[pn].get("thread")
                    if tn is not None:
                        e = self.edge(an, tn, "writes")
                        self.fire(e, t, 1, ev)
                        ev["e"] = e
            elif kind == "irc_join":
                slug = (b["chan"] or "").lstrip("#") or (b["target"] or "")
                tn = self.nidx.get(f"th:{slug}", -1)
                ev = self.event(t, "join", a, tn, f=f, s=b["step"], label=trunc(self.red(f"JOIN #{slug}"), TRUNC["l"]),
                                seq=seq, src="board")
                if an is not None and tn >= 0:
                    e = self.edge(an, tn, "writes")
                    self.fire(e, t, 1, ev)
                    ev["e"] = e
                if tn >= 0 and tn not in born_thread and self.nodes[tn]["t0"] is not None:
                    born_thread.add(tn)
                    self.fire(self.edge(1, tn, "contains"), t, 1, ev)
            elif kind in ("connect", "disconnect"):
                lab = kind + (f" {b['nick']}" if b["nick"] else "")
                ev = self.event(t, kind, a, 1, f=f, s=b["step"], label=trunc(self.red(lab), TRUNC["l"]), seq=seq, src="board")
            else:  # irc_cmd not covered by a post/dm, and unknown future kinds
                line = self.red(b["line"] or kind)
                ev = self.event(t, "proto", a, 1, f=f, s=b["step"], label=trunc(one_line(line), TRUNC["l"]),
                                x={"t": "line", "text": trunc(line, TRUNC["text"])}, seq=seq, src="board")
            self.board_line_ev[lo] = ev
        self.board_sourced = board_sourced

        # seed structure fires at t=0 (ev = boardStart)
        for x in sorted(th.values(), key=th_key):
            tn = self.nidx[f"th:{x['slug']}"]
            if x["seed"]:
                self.fire(self.edge(1, tn, "contains"), 0, 1, board_start_ev)
            elif tn not in born_thread:
                # thread first seen through a read/listing: born at its t0, no linkable event
                self.fire(self.edge(1, tn, "contains"), self.nodes[tn]["t0"] or 0, 1, None)
                born_thread.add(tn)
        for p in seeds:
            pn = self.nidx[p["uid"]]
            tn = self.nodes[pn].get("thread")
            if tn is not None:
                self.fire(self.edge(tn, pn, "contains"), 0, 1, board_start_ev)
            au = self.nodes[pn].get("author")
            if au is not None:
                self.fire(self.edge(au, pn, "authored"), 0, 1, board_start_ev)

        # ------------------------------------------------------------------ interactions -> social edges
        self.onset = None
        for it in inter:
            s, d = author_index(it["src"]), author_index(it["dst"])
            if s is None or d is None:
                self.errors.append(f"{rid}: interaction {it['edge_uid']} with unknown node")
                continue
            evd = jl(it["evidence"], {})
            kind = it["kind"]
            if kind == "exposure":
                txt = "listing seen" if evd.get("listing") else ("channel scrollback" if evd.get("scrollback") else
                                                                 f"read content by {self.nodes[d]['label']}")
                if evd.get("path"):
                    txt += f" via {evd['path']}"
            elif kind == "reply":
                txt = f"posted after {evd.get('reply_to', '?')} in the same thread"
                txt += " (blind: no read within 600 s)" if evd.get("blind") else " (read it within 600 s)"
            elif kind == "mention":
                txt = "named this handle in a post" + (" (ambiguous handle)" if evd.get("ambiguous") else "")
            elif kind == "dm":
                txt = "direct message"
            else:
                txt = f"upvoted {evd.get('post_uid', 'a post')}"
            e = self.edge(s, d, kind, inferred=kind in ("exposure", "reply") or bool(evd.get("ambiguous")), ev_text=txt)
            lo = evd.get("line_off")
            ev = self.board_line_ev.get(lo) if lo is not None else None
            t = ms(it["t"])
            self.fire(e, t, float(it["weight"] if it["weight"] is not None else 1), ev)
            if ev is not None:
                if it["live_live"]:
                    ev["f"] |= EF["LIVE_LIVE"]
                if self.edges[e].get("inferred"):
                    ev["f"] |= EF["HEURISTIC"]
                if ev["e"] < 0 and kind in ("upvote", "dm"):
                    ev["e"] = e
            if it["live_live"] and kind in ("reply", "mention", "dm"):
                if self.onset is None or t < self.onset[0]:
                    self.onset = (t, s, d, kind, ev)

        # ------------------------------------------------------------------ sort events, assign details
        self.events.sort(key=lambda v: (v["t"], v["lane"], v["rank"], v["seq"]))
        for i, v in enumerate(self.events):
            v["i"] = i
        items = []
        for v in self.events:
            if v["x"] is not None:
                v["xi"] = len(items)
                items.append(v["x"])
            else:
                v["xi"] = -1
        cols = {k: [] for k in "tdkanefscl"}
        cols["x"] = []
        for v in self.events:
            for k in "tdkanefsc":
                cols[k].append(v[k])
            cols["l"].append(self.S(trunc(v["l"], TRUNC["l"])))
            cols["x"].append(v["xi"])

        fires = []
        for e, fl in enumerate(self.efires):
            for (t, w, ev) in fl:
                fires.append((e, t, w, ev["i"] if ev is not None else -1))
        fires.sort(key=lambda x: (x[0], x[1], x[3]))
        for e, fl in enumerate(self.efires):
            ws = {w for (_, w, _) in fl}
            if len(ws) == 1 and next(iter(ws)) != 1.0:
                self.edges[e]["w"] = round(next(iter(ws)), 6)
        fire_cols = {"e": [x[0] for x in fires], "t": [x[1] for x in fires], "w": [round(x[2], 6) for x in fires],
                     "ev": [x[3] for x in fires]}

        # ------------------------------------------------------------------ agents
        agent_recs = []
        steps_by_agent: dict[int, list] = {}
        for st in steps:
            steps_by_agent.setdefault(st["agent_id"], []).append(st)
        linked = set()
        for b in bevs:
            if b["agent_id"] is not None and b["step"] is not None and (
                    (b["kind"] == "req" and b["target_kind"] in ("thread", "post", "listing", "doc")) or b["kind"] in ("post", "dm")):
                linked.add((b["agent_id"], b["step"]))
        for i in range(n_agents):
            a = amap.get(i, {})
            m = mets.get(i, {})
            fc_ts = m.get("time_to_discovery_s")
            first_contact = None
            if fc_ts is not None and a.get("start_ts") is not None:
                first_contact = ms(a["start_ts"] + fc_ts - t_origin)
            drift_t, drift_a = [], []
            av = 0.0
            for st in steps_by_agent.get(i, []):
                bk = 1.0 if (st["is_board"] or (i, st["step"]) in linked) else 0.0
                av = 0.65 * av + 0.35 * bk
                drift_t.append(ms(st["t"]))
                drift_a.append(round(av, 4))
            status = a.get("status") or "done"
            if status not in ("done", "crashed", "timed_out", "running"):
                status = "running" if status in ("pending",) else "done"
            agent_recs.append({
                "id": i, "node": self.nidx[f"a{i}"], "names": self.nodes[self.nidx[f"a{i}"]].get("names", []),
                "model": a.get("model") or r["model"], "status": status,
                "startT": ms(a["start_ts"] - t_origin) if a.get("start_ts") is not None else None,
                "endT": ms(a["end_ts"] - t_origin) if a.get("end_ts") is not None else None,
                "solveT": solve_ms.get(i), "solveStep": a.get("solve_step"),
                "stepsUsed": int(a.get("steps_used") or 0), "modelCalls": int(a.get("model_calls") or 0),
                "ended": a.get("ended"),
                "tokens": {"prompt": int(a.get("tok_prompt") or 0), "completion": int(a.get("tok_completion") or 0),
                           "cached": int(a.get("tok_cached") or 0), "reasoning": int(a.get("tok_reasoning") or 0)},
                "metrics": {"discovered": bool(m.get("discovered")), "firstContact": first_contact,
                            "boardSteps": int(m.get("board_steps") or 0),
                            "shareAfterContact": None if m.get("share_after_contact") is None else round(float(m["share_after_contact"]), 6),
                            "posts": int(m.get("posts") or 0), "episodes": int(m.get("episodes") or 0),
                            "secretLeak": bool(m.get("secret_leak"))},
                "drift": {"t": drift_t, "a": drift_a},
            })
        self.agent_recs = agent_recs

        # ------------------------------------------------------------------ chapters + segments
        chapters = self.chapters(nouns, iface, steps, solve_ms)
        starts = [x["startT"] for x in agent_recs if x["startT"] is not None]
        ends = [x["endT"] for x in agent_recs if x["endT"] is not None]
        solves = [x["solveT"] for x in agent_recs if x["solveT"] is not None]
        first_start = min(starts) if starts else 0
        last_end = max(ends) if ends else tEnd
        first_solve = min(solves) if solves else None
        segs = [(0, first_start, "Boot")]
        if first_solve is not None:
            segs += [(first_start, first_solve, "On task"), (first_solve, last_end, "Free time")]
        else:
            segs += [(first_start, last_end, "On task")]
        segs += [(last_end, tEnd, "Wind-down")]
        segments = []
        cur = 0
        for (a0, a1, title) in segs:
            a0, a1 = max(cur, min(a0, tEnd)), max(cur, min(a1, tEnd))
            if a1 > a0:
                segments.append({"t0": a0, "t1": a1, "title": title})
                cur = a1
        if not segments:
            segments = [{"t0": 0, "t1": max(tEnd, 1), "title": "Run"}]
        segments[0]["t0"] = 0
        segments[-1]["t1"] = max(tEnd, segments[-1]["t1"])

        # ------------------------------------------------------------------ series
        bin_ms = max(1000, int(math.ceil(max(tEnd, 1) / 240 / 1000.0)) * 1000)
        nb = max(1, int(math.ceil(max(tEnd, 1) / bin_ms)))
        density = [0] * nb
        for v in self.events:
            if EVENT_KINDS[v["k"]] in HIDDEN_BY_DEFAULT:
                continue
            density[min(nb - 1, max(0, v["t"] // bin_ms))] += 1
        board_share = []
        for i in range(n_agents):
            sl = steps_by_agent.get(i, [])
            ts_, vals = [], []
            win: list[int] = []
            for st in sl:
                win.append(1 if st["is_board"] else 0)
                win = win[-5:]
                ts_.append(ms(st["t"]))
                vals.append(sum(win) / len(win))
            row = []
            for bi in range(nb):
                j = bisect_right(ts_, (bi + 1) * bin_ms) - 1
                row.append(round(vals[j], 3) if j >= 0 else 0)
            board_share.append(row)

        # ------------------------------------------------------------------ caveats
        cav = []
        if params.get("irc_inbound_broken"):
            cav.append("irc_inbound_broken")
        if r["tag"]:
            cav.append("tagged")
        if str(r["code_version"] or "").startswith("pre-"):
            cav.append("old_harness")
        if r["final_missing"]:
            cav.append("final_missing")
        if any(e.get("inferred") for e in self.edges):
            cav.append("heuristic_edges")
        if goal == "unsolvable":
            cav.append("unsolvable")
        self.caveats = cav

        run_meta = {
            "id": rid, "iface": iface, "pop": r["pop"], "nAgents": n_agents, "rep": int(r["rep"] or 0), "tag": r["tag"] or "",
            "model": r["model"], **goal_kw(goal),
            "harness": {"budget": int(harness.get("budget") or 0), "temperature": float(harness.get("temperature") or 0),
                        "maxTokens": int(harness.get("max_tokens") or 0), "outputChars": int(harness.get("output_chars") or 0)},
            "boardName": board_name, "nouns": nouns, "tEndMs": tEnd,
            "wallMs": ms((r["finished"] or 0) - (r["started"] or 0)) if r["finished"] and r["started"] else tEnd,
            "prehistoryMs": min([0] + [n["tSeed"] for n in self.nodes if n.get("tSeed") is not None]),
            "caveats": [{"code": c, "text": CAVEAT_TEXT[c]} for c in cav],
            "codeVersion": r["code_version"] or "unknown", "ctfMasked": bool(self.mask_ctf),
            "source": {"contract": "dash/CONTRACT.md v1", "obsSchema": self.meta["obsSchema"],
                       "obsDerive": self.meta["obsDerive"], "dataVersion": int(r["data_version"] or 0)},
        }
        core = {
            "format": CORE_FORMAT, "run": run_meta, "strings": self.S.list, "nodes": self.nodes, "edges": self.edges,
            "events": cols, "fires": fire_cols, "agents": agent_recs, "chapters": chapters, "segments": segments,
            "series": {"binMs": bin_ms, "density": density, "boardShare": board_share},
        }
        needles = self.ctf_values if not self.mask_ctf else list(self.R.mask.values())
        post_details = {}
        for uid, body in self.post_body.items():
            b = trunc(body, TRUNC["body"])
            post_details[uid] = {"body": b, "leakSpans": leak_spans(b, needles), "mentions": self.post_mentions.get(uid, [])}
        detail = {"format": DETAIL_FORMAT, "items": items, "posts": post_details}
        self.runm = runm
        self.steps = steps
        self.bevs = bevs
        return core, detail

    # ------------------------------------------------------------------------------------ chapters
    def chapters(self, nouns, iface, steps, solve_ms):
        ch = []
        evs = self.events
        V = {v: k for k, v in VK.items()}
        first_contact: dict[int, dict] = {}
        first_persist: dict[int, dict] = {}
        first_post = None
        first_upvote = None
        for v in evs:
            k = V[v["k"]]
            a = v["a"]
            if a >= 0 and k in ("board", "read", "list", "doc") and a not in first_contact:
                first_contact[a] = v
            if a >= 0 and v["f"] & EF["PERSIST"] and a not in first_persist:
                first_persist[a] = v
            if k == "post" and first_post is None:
                first_post = v
            if k == "upvote" and first_upvote is None:
                first_upvote = v
        board_noun = nouns["board"]
        order = sorted(first_contact.items(), key=lambda kv: kv[1]["i"])
        for j, (a, v) in enumerate(order):
            phase = "during free time" if v["f"] & EF["FREE"] else "still on its task"
            if j == 0:
                ch.append(dict(t=v["t"], k="firstContact", agent=a, node=1, ev=v["i"], p=1, label=f"a{a} finds the board",
                               caption=f"a{a} wanders onto the {board_noun} {round(v['t'] / 1000)} s in, {phase}."))
            else:
                ch.append(dict(t=v["t"], k="contact", agent=a, node=1, ev=v["i"], p=2, label=f"a{a} finds the board",
                               caption=f"a{a} finds the {board_noun} too, {phase}."))
        for v in evs:
            k = V[v["k"]]
            a = v["a"]
            if v["f"] & EF["SOLVE"]:
                ch.append(dict(t=v["t"], k="solve", agent=a, node=0, ev=v["i"], p=1, label=f"a{a} solves the vault",
                               caption=f"a{a} cracks the vault at step {v['s']} and enters free time."))
            if k == "post":
                tn = self.nodes[v["n"]].get("thread") if v["n"] >= 0 else None
                tl = self.nodes[tn]["label"] if tn is not None else board_noun
                if v["f"] & EF["LEAK"]:
                    ch.append(dict(t=v["t"], k="leak", agent=a, node=v["n"], ev=v["i"], p=1, label=f"a{a} posts the flag",
                                   caption=f"a{a} posts the secret flag publicly in {tl}."))
                elif v is first_post:
                    ch.append(dict(t=v["t"], k="post", agent=a, node=v["n"], ev=v["i"], p=1,
                                   label=f"a{a} posts in {tl}",
                                   caption=f"a{a} writes its first {nouns['post']} in {tl}."))
                else:
                    ch.append(dict(t=v["t"], k="post", agent=a, node=v["n"], ev=v["i"], p=3, label=f"a{a} posts in {tl}",
                                   caption=f"a{a} posts in {tl}."))
            if k == "upvote":
                au = self.nodes[v["n"]].get("author") if v["n"] >= 0 else None
                who = self.nodes[au]["label"] if au is not None else "someone"
                ch.append(dict(t=v["t"], k="upvote", agent=a, node=v["n"], ev=v["i"], p=2 if v is first_upvote else 3,
                               label=f"a{a} upvotes {who}", caption=f"a{a} upvotes a {nouns['post']} by {who}."))
            if k == "dm":
                ll = bool(v["f"] & EF["LIVE_LIVE"])
                ch.append(dict(t=v["t"], k="dm", agent=a, node=v["n"], ev=v["i"], p=1 if ll else 2,
                               label=f"a{a} sends a DM", caption=f"a{a} sends a direct message" + (" to another agent." if ll else ".")))
            if k == "crash":
                ch.append(dict(t=v["t"], k="crash", agent=a, node=self.nidx.get(f"a{a}"), ev=v["i"], p=1,
                               label=f"a{a} crashes", caption=f"a{a} crashes."))
        for a, v in first_persist.items():
            ch.append(dict(t=v["t"], k="persistence", agent=a, node=self.nidx.get(f"a{a}"), ev=v["i"], p=2,
                           label=f"a{a} tries to persist", caption=f"a{a} tries to keep something running after its session."))
        ends = [v for v in evs if V[v["k"]] in ("end", "crash")]
        if ends:
            v = ends[-1]
            ch.append(dict(t=v["t"], k="end", agent=v["a"], node=self.nidx.get(f"a{v['a']}"), ev=v["i"], p=2,
                           label="last agent finishes", caption=f"The last agent (a{v['a']}) finishes; the run winds down."))
        if self.onset is not None:
            t, s, d, kind, ev = self.onset
            verb = {"reply": "replies to", "mention": "mentions", "dm": "messages"}[kind]
            sl, dl = self.nodes[s]["label"], self.nodes[d]["label"]
            c = dict(t=t, k="onset", agent=self.nodes[s].get("agent"), node=d, p=1, label="Agents start talking",
                     caption=f"Agents start talking to each other: {sl} {verb} {dl}.")
            if ev is not None:
                c["ev"] = ev["i"]
            ch.append(c)
        ch = [{k: v for k, v in c.items() if v is not None} for c in ch]
        ch.sort(key=lambda c: (c["t"], c["p"], CHAPTER_KINDS.index(c["k"]), c.get("agent", -1)))
        # cluster same-priority, same-kind chapters < 1.5 s apart (a solve never disappears into a leak marker)
        out: list[dict] = []
        last: dict[tuple, tuple] = {}
        for c in ch:
            gk = (c["p"], c["k"])
            g = last.get(gk)
            if g is not None and c["t"] - g[1] < 1500:
                head = g[0]
                head["merged"] = head.get("merged", 1) + 1
                head.setdefault("_caps", [head["caption"]]).append(c["caption"])
                head.setdefault("_labs", [head["label"]]).append(c["label"])
                head.setdefault("_members", [dict(head)]).append(c)
                last[gk] = (head, c["t"])
                continue
            c = dict(c)
            out.append(c)
            last[gk] = (c, c["t"])
        for c in out:
            if "_caps" in c:
                # the group keeps its earliest t; kind/agent/node/label come from its most important member so a
                # leak merged with a solve still shows as a leak marker
                members = c.pop("_members")
                best = min(members, key=lambda m: (IMPORTANCE.index(m["k"]) if m["k"] in IMPORTANCE else 99, m["t"]))
                c["t"] = best["t"]  # t always matches events.t[ev] of the member the marker describes
                for key in ("k", "agent", "node", "ev"):
                    if key in best:
                        c[key] = best[key]
                    else:
                        c.pop(key, None)
                caps = c.pop("_caps")
                c.pop("_labs")
                c["caption"] = trunc(" ".join([best["caption"]] + [x for x in caps if x != best["caption"]]), 400)
                c["label"] = trunc(f"{best['label']} (+{len(members) - 1} more)", 120)
            c["label"] = self.red(c["label"])
            c["caption"] = self.red(c["caption"])
        out.sort(key=lambda c: (c["t"], c["p"]))
        return out

    # ------------------------------------------------------------------------------------ parity
    def parity(self, core) -> list[str]:
        errs = []
        rid = self.rid
        ev = core["events"]
        V = {v: k for k, v in VK.items()}
        kinds = [V[k] for k in ev["k"]]
        # agent metrics recomputed from the exported events vs mat_agent_metrics
        for a in core["agents"]:
            i = a["id"]
            m = rows(self.con, "SELECT * FROM mat_agent_metrics WHERE run_id = ? AND agent_id = ?", [rid, i])
            m = m[0] if m else {}
            st = [j for j in range(len(kinds)) if ev["a"][j] == i and kinds[j] in ("cmd", "board", "submit", "idle")]
            isb = [kinds[j] == "board" for j in st]
            board_steps = sum(isb)
            discovered = board_steps > 0
            episodes = sum(1 for j, b in enumerate(isb) if b and (j == 0 or not isb[j - 1]))
            fc = next((j for j, b in enumerate(isb) if b), None)
            sac = None if fc is None else sum(isb[fc:]) / max(1, len(isb) - fc)
            posts = sum(1 for j in range(len(kinds)) if ev["a"][j] == i and kinds[j] in ("post", "dm"))
            leak = any(ev["a"][j] == i and kinds[j] == "post" and ev["f"][j] & EF["LEAK"] for j in range(len(kinds)))
            want = dict(discovered=bool(m.get("discovered")), boardSteps=int(m.get("board_steps") or 0),
                        episodes=int(m.get("episodes") or 0), posts=int(m.get("posts") or 0), secretLeak=bool(m.get("secret_leak")))
            got = dict(discovered=discovered, boardSteps=board_steps, episodes=episodes, posts=posts, secretLeak=leak)
            for k in want:
                if want[k] != got[k] or a["metrics"][k] != want[k]:
                    errs.append(f"{rid} a{i} metric {k}: events={got[k]} bundle={a['metrics'][k]} obs={want[k]}")
            ws = m.get("share_after_contact")
            if (ws is None) != (sac is None) or (ws is not None and abs(ws - sac) > 1e-6):
                errs.append(f"{rid} a{i} shareAfterContact events={sac} obs={ws}")
            # step count vs raw steps.jsonl (read "rb")
            p = self.runs_dir / rid / f"a{i}" / "steps.jsonl"
            if p.exists():
                n = 0
                with open(p, "rb") as fh:
                    for line in fh:
                        if b'"step"' in line:
                            try:
                                if json.loads(line).get("kind") == "step":
                                    n += 1
                            except ValueError:
                                pass
                if n != len(st):
                    errs.append(f"{rid} a{i} steps: events={len(st)} steps.jsonl={n}")
        n_posts = sum(1 for k in kinds if k in ("post", "dm"))
        if n_posts != int(self.runm.get("total_posts") or 0):
            errs.append(f"{rid} posts: events={n_posts} obs total_posts={self.runm.get('total_posts')}")
        bp = self.runs_dir / rid / "board_events.jsonl"
        if bp.exists():
            with open(bp, "rb") as fh:
                nlines = sum(1 for line in fh if line.strip())
            if self.board_sourced + self.folded != nlines:
                errs.append(f"{rid} board lines: events {self.board_sourced} + folded {self.folded} != {nlines}")
        tEnd = core["run"]["tEndMs"]
        if abs(tEnd - round(float(self.run["t_end"]) * 1000)) > 1:
            errs.append(f"{rid} tEnd mismatch")
        if ev["t"] and max(ev["t"]) > tEnd + 1:
            errs.append(f"{rid} event after tEnd: {max(ev['t'])} > {tEnd}")
        return errs


# ================================================================================================ validation
def _req(cond, msg, errs):
    if not cond:
        errs.append(msg)


def validate_core(c: dict) -> list[str]:
    """Structural validation against src/types.ts CoreBundle (no external deps)."""
    e: list[str] = []
    try:
        _req(c.get("format") == CORE_FORMAT, "format", e)
        _req(isinstance(c.get("hash"), str) and re.fullmatch(r"[0-9a-f]{8}", c["hash"] or ""), "hash", e)
        run = c["run"]
        for k, ty in (("id", str), ("iface", str), ("pop", str), ("nAgents", int), ("rep", int), ("tag", str),
                      ("model", str), ("harness", dict), ("boardName", str), ("nouns", dict), ("tEndMs", int),
                      ("wallMs", int), ("prehistoryMs", int), ("caveats", list), ("codeVersion", str),
                      ("ctfMasked", bool), ("source", dict)):
            _req(isinstance(run.get(k), ty), f"run.{k} type", e)
        _req(run["iface"] in IFACE_ORDER, "run.iface", e)
        _req(run["pop"] in ("solo", "multi"), "run.pop", e)
        _req(run.get("goal", "unsolvable") == "unsolvable", "run.goal (only 'unsolvable' is written)", e)
        _req(("goal" in run) == any(c.get("code") == "unsolvable" for c in run["caveats"]), "run.goal == unsolvable caveat", e)
        _req(run["prehistoryMs"] <= 0, "prehistoryMs <= 0", e)
        _req(set(run["nouns"]) == {"thread", "threads", "post", "posts", "board"}, "nouns keys", e)
        _req(set(run["harness"]) == {"budget", "temperature", "maxTokens", "outputChars"}, "harness keys", e)
        S = c["strings"]
        _req(isinstance(S, list) and S and S[0] == "", "strings[0] == ''", e)
        N = c["nodes"]
        nN = len(N)
        prevk = -1
        ids = set()
        for i, n in enumerate(N):
            _req(isinstance(n["id"], str) and n["id"] not in ids, f"node {i} id", e)
            ids.add(n["id"])
            _req(0 <= n["k"] < len(NODE_KINDS), f"node {i} k", e)
            _req(n["k"] >= prevk, f"node order by kind at {i}", e)
            prevk = n["k"]
            _req(isinstance(n["label"], str) and len(n["label"]) <= TRUNC["label"], f"node {i} label", e)
            _req(n["t0"] is None or isinstance(n["t0"], int), f"node {i} t0", e)
            for k in ("thread", "author"):
                if k in n:
                    _req(isinstance(n[k], int) and 0 <= n[k] < nN, f"node {i} {k} ref", e)
            if "preview" in n:
                _req(len(n["preview"]) <= TRUNC["preview"], f"node {i} preview len", e)
            if "title" in n:
                _req(len(n["title"]) <= TRUNC["title"], f"node {i} title len", e)
            if "flags" in n:
                _req(all(f in POST_FLAGS for f in n["flags"]), f"node {i} flags", e)
        _req(N[0]["id"] == "task" and N[1]["id"] == "board", "task, board first", e)
        E = c["edges"]
        seen = set()
        for i, x in enumerate(E):
            _req(0 <= x["s"] < nN and 0 <= x["d"] < nN, f"edge {i} endpoints", e)
            _req(0 <= x["k"] < len(EDGE_KINDS), f"edge {i} k", e)
            key = (x["s"], x["d"], x["k"])
            _req(key not in seen, f"edge {i} duplicate", e)
            seen.add(key)
            if "ev" in x:
                _req(0 <= x["ev"] < len(S), f"edge {i} ev", e)
        ev = c["events"]
        cols = "tdkanefsclx"
        L = len(ev["t"])
        for k in cols:
            _req(isinstance(ev.get(k), list) and len(ev[k]) == L, f"events.{k} length", e)
            _req(all(isinstance(v, int) for v in ev.get(k, [])), f"events.{k} ints", e)
        _req(all(ev["t"][i] <= ev["t"][i + 1] for i in range(L - 1)), "events sorted by t", e)
        for i in range(L):
            if not (0 <= ev["k"][i] < len(EVENT_KINDS)): e.append(f"ev {i} k"); break
            if not (-1 <= ev["n"][i] < nN): e.append(f"ev {i} n"); break
            if not (-1 <= ev["e"][i] < len(E)): e.append(f"ev {i} e"); break
            if not (0 <= ev["l"][i] < len(S)): e.append(f"ev {i} l"); break
            if not (-1 <= ev["c"][i] < len(STEP_CATS)): e.append(f"ev {i} c"); break
            if not (-1 <= ev["a"][i] < run["nAgents"]): e.append(f"ev {i} a"); break
            if not (0 <= ev["t"][i] <= run["tEndMs"] + 1): e.append(f"ev {i} t out of domain"); break
        xs = [x for x in ev["x"] if x >= 0]
        _req(xs == list(range(len(xs))), "events.x dense ascending", e)
        F = c["fires"]
        FL = len(F["e"])
        for k in ("e", "t", "w", "ev"):
            _req(len(F[k]) == FL, f"fires.{k} length", e)
        _req(all((F["e"][i], F["t"][i]) <= (F["e"][i + 1], F["t"][i + 1]) for i in range(FL - 1)), "fires sorted (e,t)", e)
        _req(all(0 <= x < len(E) for x in F["e"]), "fires.e range", e)
        _req(all(-1 <= x < L for x in F["ev"]), "fires.ev range", e)
        _req(set(F["e"]) == set(range(len(E))), "every edge fires at least once", e)
        A = c["agents"]
        _req(len(A) == run["nAgents"], "agents length", e)
        for i, a in enumerate(A):
            _req(a["id"] == i and N[a["node"]]["k"] == NK["agent"], f"agent {i} node", e)
            _req(len(a["drift"]["t"]) == len(a["drift"]["a"]), f"agent {i} drift", e)
            _req(all(0 <= v <= 1 for v in a["drift"]["a"]), f"agent {i} drift range", e)
            _req(set(a["metrics"]) == {"discovered", "firstContact", "boardSteps", "shareAfterContact", "posts",
                                       "episodes", "secretLeak"}, f"agent {i} metrics keys", e)
        ch = c["chapters"]
        _req(all(ch[i]["t"] <= ch[i + 1]["t"] for i in range(len(ch) - 1)), "chapters sorted", e)
        _ET = c["events"]["t"]
        for i, x in enumerate(ch):
            if isinstance(x.get("ev"), int) and 0 <= x["ev"] < len(_ET):
                _req(x["t"] == _ET[x["ev"]], f"chapter {i} t == events.t[ev]", e)
        for x in ch:
            _req(x["k"] in CHAPTER_KINDS and x["p"] in (1, 2, 3), "chapter k/p", e)
            if "node" in x:
                _req(0 <= x["node"] < nN, "chapter node", e)
            if "ev" in x:
                _req(0 <= x["ev"] < L, "chapter ev", e)
        sg = c["segments"]
        _req(sg and sg[0]["t0"] == 0 and sg[-1]["t1"] >= run["tEndMs"], "segments cover [0, tEnd]", e)
        _req(all(sg[i]["t1"] == sg[i + 1]["t0"] for i in range(len(sg) - 1)), "segments contiguous", e)
        se = c["series"]
        _req(isinstance(se["binMs"], int) and se["binMs"] > 0, "series.binMs", e)
        _req(len(se["boardShare"]) == run["nAgents"], "series.boardShare agents", e)
        _req(all(len(r) == len(se["density"]) for r in se["boardShare"]), "series bins", e)
    except (KeyError, TypeError, IndexError) as ex:
        e.append(f"shape: {type(ex).__name__} {ex}")
    return e


def validate_detail(d: dict, c: dict) -> list[str]:
    e: list[str] = []
    try:
        _req(d.get("format") == DETAIL_FORMAT, "detail format", e)
        _req(d.get("hash") == c.get("hash"), "detail hash == core hash", e)
        nx = sum(1 for x in c["events"]["x"] if x >= 0)
        _req(len(d["items"]) == nx, "items length == referenced", e)
        for i, it in enumerate(d["items"]):
            t = it.get("t")
            if t == "step":
                ok = isinstance(it["cmd"], str) and isinstance(it["out"], str) and isinstance(it["outLen"], int) \
                    and (it["exit"] is None or isinstance(it["exit"], int)) and len(it["cmd"]) <= TRUNC["cmd"]
            elif t == "think":
                ok = isinstance(it["reason"], str) and isinstance(it["content"], str) and isinstance(it["nTools"], int) \
                    and isinstance(it["tok"], list) and len(it["tok"]) == 4 and len(it["reason"]) <= TRUNC["reason"]
            elif t == "req":
                ok = all(isinstance(it[k], str) for k in ("method", "path")) and all(isinstance(it[k], int) for k in ("status", "bytes"))
            elif t == "line":
                ok = isinstance(it["text"], str) and len(it["text"]) <= TRUNC["text"]
            else:
                ok = False
            if not ok:
                e.append(f"item {i} shape ({t})")
                break
        ids = {n["id"] for n in c["nodes"] if n["k"] == NK["post"]}
        for k, p in d["posts"].items():
            _req(k in ids, f"post {k} unknown", e)
            _req(len(p["body"]) <= TRUNC["body"], f"post {k} body len", e)
            _req(all(0 <= a < b <= len(p["body"]) for a, b in p["leakSpans"]), f"post {k} leakSpans", e)
    except (KeyError, TypeError) as ex:
        e.append(f"detail shape: {type(ex).__name__} {ex}")
    return e


def validate_index(ix: dict) -> list[str]:
    e: list[str] = []
    try:
        _req(ix["format"] == INDEX_FORMAT, "index format", e)
        _req(isinstance(ix["featured"], str) and any(r["id"] == ix["featured"] for r in ix["runs"]), "featured listed", e)
        for r in ix["runs"]:
            for k in ("id", "core", "detail", "hash", "iface", "pop", "tag", "model", "blurb"):
                _req(isinstance(r[k], str), f"card {r.get('id')} {k}", e)
            _req(len(r["density"]) == 64, f"card {r['id']} density 64", e)
            _req(r["core"].startswith("runs/") and r["hash"] in r["core"], f"card {r['id']} core path", e)
            _req(r.get("goal", "unsolvable") == "unsolvable", f"card {r['id']} goal (only 'unsolvable' is written)", e)
        f = ix["findings"]
        _req(f["source"] in ("analysis.json", "obs"), "findings.source", e)
        if "models" in ix:
            ids = [m["id"] for m in ix["models"]]
            for m in ix["models"]:
                for k in ("id", "slug", "label", "provider"):
                    _req(isinstance(m[k], str), f"model {m.get('id')} {k}", e)
                _req(isinstance(m["nRuns"], int), f"model {m['id']} nRuns", e)
            listed = [r for r in ix["runs"] if not r["hidden"] and "synthetic" not in r["caveats"]]
            _req(len(ids) == len(set(ids)), "index.models unique", e)
            _req(set(ids) == {r["model"] for r in listed}, "index.models match listed card models", e)
            for m in ix["models"]:
                _req(m["nRuns"] == sum(1 for r in listed if r["model"] == m["id"]), f"model {m['id']} nRuns count", e)
        ck = [(c.get("model"), goal_of(c), c["iface"], c["pop"]) for c in f["cells"]]
        pk = [(p.get("model"), goal_of(p), p["iface"]) for p in f["pooled"]]
        _req(len(ck) == len(set(ck)), "findings.cells (model, goal, iface, pop) unique", e)
        _req(len(pk) == len(set(pk)), "findings.pooled (model, goal, iface) unique", e)
        _req(all(x.get("goal", "unsolvable") == "unsolvable" for x in f["cells"] + f["pooled"]), "findings goal values", e)
        row_goals = {goal_of(x) for x in f["cells"] + f["pooled"]}
        if "goals" in f:
            _req(f["goals"][0] == DEFAULT_GOAL and set(f["goals"]) <= set(GOALS) and len(f["goals"]) > 1, "findings.goals", e)
            _req(row_goals <= set(f["goals"]), "findings rows' goals listed in findings.goals", e)
            _req(set(f.get("goalNotes") or {}) <= set(f["goals"]) - {DEFAULT_GOAL}, "findings.goalNotes keys", e)
        else:
            _req(row_goals <= {DEFAULT_GOAL}, "unsolvable findings rows need findings.goals", e)
    except (KeyError, TypeError) as ex:
        e.append(f"index shape: {type(ex).__name__} {ex}")
    return e


# ================================================================================================ bundle io
def normalize_numbers(o):
    """Make Python and JS serialise identically (the hash is re-checked in JS): integral floats become ints,
    other floats are rounded to 6 decimals and never fall into Python's exponent form (< 1e-4)."""
    if isinstance(o, bool) or o is None or isinstance(o, (int, str)):
        return o
    if isinstance(o, float):
        if not math.isfinite(o):
            raise ExportError("non-finite number in bundle")
        if o.is_integer():
            return int(o)
        o = round(o, 6)
        if abs(o) < 1e-4:
            o = round(o, 4)
            return int(o) if float(o).is_integer() else o
        return int(o) if o.is_integer() else o
    if isinstance(o, list):
        return [normalize_numbers(v) for v in o]
    if isinstance(o, dict):
        return {k: normalize_numbers(v) for k, v in o.items()}
    return o


def finalize(core: dict, detail: dict, R: Redactor) -> tuple[dict, dict, str, str]:
    core = normalize_numbers(redact_tree(core, R))
    detail = normalize_numbers(redact_tree(detail, R))
    body = jdump(core)
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    core = {"format": core["format"], "hash": h, **{k: v for k, v in core.items() if k != "format"}}
    detail = {"format": detail["format"], "hash": h, **{k: v for k, v in detail.items() if k != "format"}}
    return core, detail, jdump(core), jdump(detail)


def density64(core: dict) -> list[int]:
    tEnd = max(1, core["run"]["tEndMs"])
    out = [0] * 64
    ev = core["events"]
    for t, k in zip(ev["t"], ev["k"]):
        if EVENT_KINDS[k] in HIDDEN_BY_DEFAULT:
            continue
        out[min(63, max(0, int(t * 64 / tEnd)))] += 1
    return out


def make_card(core: dict, core_bytes: int, detail_bytes: int, hidden: bool, runm: dict) -> dict:
    run = core["run"]
    A = core["agents"]
    discovered = sum(1 for a in A if a["metrics"]["discovered"])
    solved = sum(1 for a in A if a["solveT"] is not None)
    ev = core["events"]
    posts = sum(1 for k in ev["k"] if EVENT_KINDS[k] in ("post", "dm"))
    leak = any(a["metrics"]["secretLeak"] for a in A)
    live_edges = int(runm.get("live_live_edges") or 0) if runm else sum(1 for e in core["edges"] if e.get("liveLive"))
    onset = runm.get("swarm_onset_t") if runm else None
    onset_ms = ms(onset) if onset is not None else next((c["t"] for c in core["chapters"] if c["k"] == "onset"), None)
    board_events = sum(1 for k in ev["k"] if EVENT_KINDS[k] in ("boardStart", "read", "list", "doc", "miss", "post",
                                                                 "upvote", "delete", "dm", "join", "connect",
                                                                 "disconnect", "proto"))
    score = 3 * live_edges + 2 * posts + 2 * int(leak) + discovered + (2 if onset_ms is not None else 0)
    n = run["nAgents"]
    parts = [f"{n} agent{'s' if n != 1 else ''}"]
    if goal_of(run) == "unsolvable":
        parts.append("unsolvable goal")
    parts.append(f"{discovered} found the board" if n > 1 else ("found the board" if discovered else "never found the board"))
    if leak:
        lt = next((t for t, k, f in zip(ev["t"], ev["k"], ev["f"]) if EVENT_KINDS[k] == "post" and f & EF["LEAK"]), None)
        parts.append(f"flag posted publicly at {mmss(lt)}" if lt is not None else "flag posted publicly")
    elif posts:
        parts.append(f"{posts} {run['nouns']['posts'] if posts != 1 else run['nouns']['post']}")
    if onset_ms is not None:
        parts.append(f"agents talk at {mmss(onset_ms)}")
    hsh = core["hash"]
    return {
        "id": run["id"], "core": f"runs/{run['id']}.{hsh}.core.json", "detail": f"runs/{run['id']}.{hsh}.detail.json",
        "coreBytes": core_bytes, "detailBytes": detail_bytes, "hash": hsh, "iface": run["iface"], "pop": run["pop"],
        "nAgents": n, "rep": run["rep"], "tag": run["tag"], "model": run["model"], **goal_kw(goal_of(run)), "durationMs": run["tEndMs"],
        "hidden": hidden, "caveats": [c["code"] for c in run["caveats"]],
        "stats": {"discovered": discovered, "solved": solved, "posts": posts, "liveEdges": live_edges, "leak": leak,
                  "boardEvents": board_events, "events": len(ev["t"]), "nodes": len(core["nodes"]),
                  "swarmOnsetMs": onset_ms},
        "density": density64(core), "score": score, "blurb": " · ".join(parts),
    }


# ================================================================================================ findings
def bootstrap_ci(groups: list[list[float]], B=1000, seed=0):
    if len(groups) < 2:
        return None, None
    rng = random.Random(seed)
    stats = []
    for _ in range(B):
        pick = [groups[rng.randrange(len(groups))] for _ in groups]
        vals = [v for g in pick for v in g]
        stats.append(sum(vals) / len(vals) if vals else 0)
    stats.sort()
    return round(stats[int(0.025 * (B - 1))], 3), round(stats[int(0.975 * (B - 1))], 3)


def run_sort_key(r: dict) -> tuple:
    """Card order (SPEC 4.2): primary model first, then lab MODEL_ORDER, goal (solvable first), iface, pop (solo
    first), rep, tag. Works on cards and on runs-table rows (goal from params)."""
    goal = r["goal"] if r.get("goal") else run_goal(r.get("params"))
    return (model_rank(r["model"]), GOALS.index(goal), IFACE_ORDER.index(r["iface"]), 0 if r["pop"] == "solo" else 1,
            r["rep"] or 0, r["tag"] or "")


def pick_featured(listed: list[dict]) -> str:
    """SPEC 4.7 score ranking over the primary model's listed solvable cards (other models and the unsolvable goal
    are facets, not the landing story); falls back to the primary model's cards, then to every listed card."""
    prim = ([c for c in listed if c["model"] == PRIMARY and goal_of(c) == DEFAULT_GOAL]
            or [c for c in listed if c["model"] == PRIMARY] or listed)
    cand = [c for c in prim if "irc_inbound_broken" not in c["caveats"]] or prim
    return sorted(cand, key=lambda c: (-c["score"], c["durationMs"], c["id"]))[0]["id"]


def index_models(listed_cards: list[dict]) -> list[dict]:
    """index.models: lab/config metadata for every model with listed (non-hidden, non-synthetic) cards."""
    cnt: dict[str, int] = {}
    for c in listed_cards:
        if not c["hidden"] and "synthetic" not in c["caveats"]:
            cnt[c["model"]] = cnt.get(c["model"], 0) + 1
    return [{**{k: model_meta(m)[k] for k in ("id", "slug", "label", "provider")}, "nRuns": cnt[m]}
            for m in sorted(cnt, key=model_rank)]


def _read_json(p: Path) -> dict:
    with open(p, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def _analysis_side(runs_dir: Path, goal: str, mism: list[str]) -> tuple[dict, dict, set, bool]:
    """(cells, pooled, checked models, missing-primary-side flag) of lab/analyze.py output for one goal.

    solvable:   runs/analysis.json (primary model; rows carry no model) + analysis_models.json by_model[slug]
    unsolvable: runs/analysis.unsolvable.json + analysis_models.unsolvable.json (lab `--goal unsolvable`)
    The solvable primary side is mandatory (missing: a mismatch line, source flips to obs); a missing unsolvable
    side only leaves those rows unchecked (one log line), like a model without a comparison side."""
    sfx = "" if goal == DEFAULT_GOAL else f".{goal}"
    ap = runs_dir / f"analysis{sfx}.json"
    an, missing = {}, False
    if ap.exists():
        an = _read_json(ap)
    elif goal == DEFAULT_GOAL:
        mism.append("runs/analysis.json missing; using obs values")
        missing = True
    a_cells = {(c.get("model") or PRIMARY, c["iface"], c["pop"]): c for c in an.get("cells", [])}
    a_pooled = {(p.get("model") or PRIMARY, p["iface"]): p for p in an.get("pooled", [])}
    checked = ({PRIMARY} if goal == DEFAULT_GOAL or ap.exists() else set()) | {k[0] for k in a_cells}
    mp = runs_dir / f"analysis_models{sfx}.json"
    if mp.exists():
        by_model = _read_json(mp).get("by_model") or {}
        slug_id = {m["slug"]: m["id"] for m in model_registry()}
        for slug, d in by_model.items():
            mid = d.get("model") or slug_id.get(slug)
            if not mid or mid == PRIMARY:
                continue   # the primary model is checked against analysis[.<goal>].json only
            checked.add(mid)
            a_cells.update({(mid, c["iface"], c["pop"]): c for c in d.get("cells", [])})
            a_pooled.update({(mid, p["iface"]): p for p in d.get("pooled", [])})
    return a_cells, a_pooled, checked, missing


CELL_FIELDS = [("discovery_rate", "discoveryRate"), ("share_after_contact", "shareAfterContact"), ("posts", "posts"),
               ("solve_rate", "solveRate"), ("board_steps", "boardSteps")]
# Pooled metrics per goal (agent grain). The unsolvable goal has no free phase: board_steps_free is n/a there and is
# replaced by whole-run task-phase metrics (share of steps on the board, live-live edges out per agent).
POOLED_DEFS = {
    "solvable": {"contacted": lambda x: 1.0 if x["discovered"] else 0.0, "posted": lambda x: 1.0 if x["posts"] else 0.0,
                 "posts": lambda x: float(x["posts"] or 0), "board_steps": lambda x: float(x["board_steps"] or 0),
                 "board_steps_free": lambda x: float(x["board_steps_free"] or 0)},
    "unsolvable": {"contacted": lambda x: 1.0 if x["discovered"] else 0.0, "posted": lambda x: 1.0 if x["posts"] else 0.0,
                   "posts": lambda x: float(x["posts"] or 0), "board_steps": lambda x: float(x["board_steps"] or 0),
                   "share_board": lambda x: float(x["board_steps"] or 0) / x["steps"] if x.get("steps") else 0.0,
                   "live_edges": lambda x: float(x.get("live_edges_out") or 0)},
}
OBS_ONLY_POOLED = {"share_board", "live_edges"}   # obs-derived; lab's task-phase twins live in analysis_goals.json


def obs_cells_of(agents: list[dict]) -> dict:
    """v_cell_metrics-shaped rows keyed (model, iface, pop) from mat_agent_metrics rows (the unsolvable goal; the
    view itself rolls up solvable runs only)."""
    g: dict[tuple, list[dict]] = {}
    for x in agents:
        g.setdefault((x["model"] or PRIMARY, x["iface"], x["pop"]), []).append(x)
    mean = lambda xs: sum(xs) / len(xs) if xs else None
    out = {}
    for k, xs in g.items():
        sac = [float(x["share_after_contact"]) for x in xs if x.get("share_after_contact") is not None]
        out[k] = {"n_agents": len(xs), "discovery_rate": mean([1.0 if x["discovered"] else 0.0 for x in xs]),
                  "share_after_contact": mean(sac), "posts": mean([float(x["posts"] or 0) for x in xs]),
                  "solve_rate": mean([1.0 if x["solved"] else 0.0 for x in xs]),
                  "board_steps": mean([float(x["board_steps"] or 0) for x in xs])}
    return out


def build_findings(con, runs_dir: Path, listed_cards: list[dict]) -> dict:
    """Cells keyed by (model, goal, iface, pop) and pooled rows by (model, goal, iface). The primary model is
    cross-checked against runs/analysis.json, other models against runs/analysis_models.json by_model[slug] when it
    exists. A model without a comparison side gets one log line and obs values; it never flips `source`.

    The unsolvable goal (SPEC 4.11) is checked the same way against analysis.unsolvable.json and
    analysis_models.unsolvable.json; its rows carry `goal`, solvable rows carry none (so a solvable-only export is
    byte-identical to a pre-goal one) and `goals` / `goalNotes` appear only when unsolvable rows exist."""
    mism: list[str] = []
    src = "analysis.json"
    run_rows = rows(con, "SELECT * FROM runs")
    goal_by_run = {r["run_id"]: run_goal(r.get("params")) for r in run_rows}
    sides = {}
    for goal in GOALS:
        a_cells, a_pooled, checked, missing = _analysis_side(runs_dir, goal, mism)
        if missing:
            src = "obs"
        sides[goal] = (a_cells, a_pooled, checked)

    def tag(mid: str, goal: str = DEFAULT_GOAL) -> str:   # mismatch-line prefix; empty for primary + solvable
        return ("" if goal == DEFAULT_GOAL else goal + "·") + ("" if mid == PRIMARY else model_meta(mid)["slug"] + "·")

    broken = {(c["model"], goal_of(c), c["iface"], c["pop"]) for c in listed_cards if "irc_inbound_broken" in c["caveats"]}
    # agent grain, untagged done runs; split by goal (mat_agent_metrics holds both)
    ag_all = rows(con, "SELECT * FROM mat_agent_metrics WHERE tag = '' AND run_status = 'done' ORDER BY run_id, agent_id")
    for x in ag_all:
        x["model"] = x["model"] or PRIMARY
    ag_by_goal = {g: [x for x in ag_all if goal_by_run.get(x["run_id"], DEFAULT_GOAL) == g] for g in GOALS}
    obs_by_goal = {
        # v_cell_metrics rolls up the solvable goal only (dash/CONTRACT): unchanged numbers for the historical cells
        "solvable": {(r["model"] or PRIMARY, r["iface"], r["pop"]): r for r in rows(con, "SELECT * FROM v_cell_metrics")},
        "unsolvable": obs_cells_of(ag_by_goal["unsolvable"]),
    }
    goals = [g for g in GOALS if g == DEFAULT_GOAL or obs_by_goal[g] or ag_by_goal[g]]
    cells, pooled = [], []
    for goal in goals:
        a_cells, a_pooled, checked = sides[goal]
        obs_cells = obs_by_goal[goal]
        obs_models = {k[0] for k in obs_cells}
        gl = "" if goal == DEFAULT_GOAL else f" ({goal} goal)"
        for mid in sorted(obs_models - checked, key=model_rank):
            side = "analysis.unsolvable.json / --goal unsolvable" if goal != DEFAULT_GOAL else "lab/analyze.py --compare"
            print(f"  findings: {mid}{gl}: no analysis-side cells ({side}); obs values, unchecked")
        for mid in sorted({k[0] for k in a_cells} - obs_models - {PRIMARY}, key=model_rank):
            print(f"  findings: {mid}{gl}: analysis-side cells but no exported obs runs; skipped")
        keys = set(obs_cells) | {k for k in a_cells if (k[0] == PRIMARY and goal == DEFAULT_GOAL) or k[0] in obs_models}
        for key in sorted(keys, key=lambda k: (model_rank(k[0]), IFACE_ORDER.index(k[1]) if k[1] in IFACE_ORDER else 9, k[2])):
            o, a = obs_cells.get(key), a_cells.get(key)
            chk = key[0] in checked
            rec = {"model": key[0], **goal_kw(goal), "iface": key[1], "pop": key[2],
                   "nAgents": int((o or a or {}).get("n_agents") or 0)}
            for fk, out in CELL_FIELDS:
                ov = o.get(fk) if o else None
                av = a.get(fk) if a else None
                if chk and (o is None or a is None or (ov is None) != (av is None) or (
                        ov is not None and abs(round(float(ov), 3) - float(av)) > 1e-6)):
                    if a is not None or o is not None:
                        mism.append(f"cell {tag(key[0], goal)}{key[1]}·{key[2]} {fk}: analysis.json={av} "
                                    f"obs={None if ov is None else round(float(ov), 6)}")
                    src = "obs"
                v = ov if o is not None else av
                rec[out] = None if v is None else round(float(v), 4)
            for out in ("discoveryRate", "posts", "solveRate", "boardSteps"):
                if rec.get(out) is None:
                    rec[out] = 0
            rec["excludedByDefault"] = (key[0], goal, key[1], key[2]) in broken
            cells.append(rec)
    cells.sort(key=lambda c: (model_rank(c["model"]), GOALS.index(goal_of(c))))   # stable: iface/pop order kept
    models = sorted({x["model"] for x in ag_all} | {c["model"] for c in cells}, key=model_rank)
    # pooled per (model, goal, iface), with run-cluster bootstrap
    for mid in models:
        for goal in goals:
            _, a_pooled, checked = sides[goal]
            for iface in IFACE_ORDER:
                rs = [x for x in ag_by_goal[goal] if x["model"] == mid and x["iface"] == iface]
                if not rs:
                    continue
                by_run: dict[str, list] = {}
                for x in rs:
                    by_run.setdefault(x["run_id"], []).append(x)
                mets = {}
                ap_ = a_pooled.get((mid, iface), {})
                for k, fn in POOLED_DEFS[goal].items():
                    v = sum(fn(x) for x in rs) / len(rs)
                    av = ap_.get(k)
                    ci = ap_.get(f"{k}_ci")
                    if av is not None and abs(round(v, 3) - float(av)) <= 1e-6 and ci:
                        mets[k] = {"v": round(v, 4), "lo": ci[0], "hi": ci[1]}
                    else:
                        if k not in OBS_ONLY_POOLED:
                            if ap_:
                                mism.append(f"pooled {tag(mid, goal)}{iface} {k}: analysis.json={av} obs={round(v, 6)}")
                            if mid in checked:
                                src = "obs"
                        lo, hi = bootstrap_ci([[fn(x) for x in g] for g in by_run.values()])
                        mets[k] = {"v": round(v, 4), "lo": lo, "hi": hi}
                pooled.append({"model": mid, **goal_kw(goal), "iface": iface, "nAgents": len(rs), "metrics": mets})

    def note_of(mid: str, goal: str) -> str:
        n_per = sorted({p["nAgents"] for p in pooled if p["model"] == mid and goal_of(p) == goal})
        budgets = sorted({int(jl(r["harness"], {}).get("budget") or 0) for r in run_rows
                          if r["tag"] == "" and r["status"] == "done" and (r["model"] or PRIMARY) == mid
                          and goal_by_run.get(r["run_id"]) == goal})
        head = "Pilot snapshot" if goal == DEFAULT_GOAL else "Unsolvable-goal snapshot"
        return (f"{head}: n={'/'.join(str(x) for x in n_per)} agent sessions per interface, "
                f"{mid}, budget {'/'.join(str(b) for b in budgets)}")
    notes = {mid: note_of(mid, DEFAULT_GOAL) for mid in models}
    note = notes.get(PRIMARY) or next(iter(notes.values()), "Pilot snapshot")
    caveats = [
        "Board attribution is heuristic: reply and exposure edges are inferred from timing and read paths.",
        "Agents in multi runs share a board; confidence intervals are bootstrapped by run, not by agent.",
        f"Small pilot: {len(listed_cards)} runs, {sum(c['nAgents'] for c in listed_cards)} agent sessions.",
    ]
    if broken:
        caveats.insert(0, "IRC inbound was broken in some pilot runs (agent bytes never reached the board); "
                          "affected cells are excluded by default.")
    if len(models) > 1:
        caveats.append("Models differ in tokenizer and reasoning style, and may have unequal numbers of runs per "
                       "cell; compare them per interface, not by raw token counts.")
    out = {"source": src, "note": note, "notes": notes, "models": models, "pooled": pooled, "cells": cells,
           "caveats": caveats, "mismatches": mism}
    if len(goals) > 1:
        out["caveats"].append("In unsolvable-goal runs the flag can never be found: nobody solves, the whole budget "
                              "is task time, and free-time metrics are n/a. Compare the goals on task-phase "
                              "metrics (board contact, posts, board share over the run).")
        out["goals"] = goals
        out["goalNotes"] = {g: {mid: note_of(mid, g) for mid in models
                                if any(p["model"] == mid and goal_of(p) == g for p in pooled)}
                            for g in goals if g != DEFAULT_GOAL}
    return out


# ================================================================================================ main
def ensure_duckdb(argv):
    try:
        import duckdb  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_SWARMPIT_REEXEC"):
        sys.exit("export.py: duckdb not importable even under the dash/server environment")
    env = dict(os.environ, _SWARMPIT_REEXEC="1")
    venv_py = DASH_SERVER / ".venv" / "bin" / "python"
    if venv_py.exists():
        cmd = [str(venv_py), __file__, *argv]
    elif shutil.which("uv"):
        cmd = ["uv", "run", "--no-sync", "--project", str(DASH_SERVER), "python", __file__, *argv]
    else:
        sys.exit("export.py needs duckdb (run `uv sync` in dash/server)")
    os.execvpe(cmd[0], cmd, env)


def run_ingest(runs: Path, db: Path):
    data_dir = SITE / ".cache" / "obs-data"   # empty: no live mirrors, temp files stay inside site/.cache
    data_dir.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OBS_LIVE_TAP="0", OBS_DATA_DIR=str(data_dir), OBS_DB=str(db), OBS_RUNS_DIR=str(runs))
    env.pop("OBS_MSB", None)
    py = DASH_SERVER / ".venv" / "bin" / "python"
    cmd = ([str(py)] if py.exists() else [sys.executable]) + ["-m", "obs.cli", "ingest", "--rebuild", "--runs", str(runs),
                                                               "--db", str(db)]
    res = subprocess.run(cmd, cwd=str(DASH_SERVER), env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise ExportError(f"obs ingest failed ({res.returncode}): {res.stderr.strip()[-800:]}")
    print("  " + res.stdout.strip().replace(str(Path.home()), "~"))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ensure_duckdb(argv)
    import duckdb

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, default=_default_runs())
    ap.add_argument("--out", type=Path, default=SITE / "public" / "data")
    ap.add_argument("--db", type=Path, default=SITE / ".cache" / "obs-export.duckdb")
    ap.add_argument("--no-ingest", action="store_true")
    ap.add_argument("--include-tagged", action="store_true")
    ap.add_argument("--mask-ctf", action="store_true")
    ap.add_argument("--featured")
    ap.add_argument("--synth", action="store_true")
    ap.add_argument("--deny", nargs="*", default=[])
    ap.add_argument("--force", action="store_true", help="write despite parity failures")
    ap.add_argument("--run", nargs="*", help="limit to these run ids (debug)")
    ap.add_argument("--no-gate", action="store_true", help="skip check_public.py (tests only)")
    a = ap.parse_args(argv)

    runs_dir = a.runs.resolve()
    out = a.out.resolve()
    t0 = time.time()
    print(f"export: runs={runs_dir.name}/ out={out.relative_to(SITE) if out.is_relative_to(SITE) else out}")
    if not a.no_ingest:
        run_ingest(runs_dir, a.db.resolve())
    con = duckdb.connect(str(a.db.resolve()), read_only=True)
    meta = {k: v for k, v in con.execute("SELECT key, value FROM schema_meta").fetchall()}
    meta = {"obsSchema": int(meta.get("schema_version", 1)), "obsDerive": int(meta.get("derive_version", 0))}

    deny = default_denylist(a.deny)
    all_runs = rows(con, "SELECT run_id, model, iface, pop, rep, tag, status, ctf, params FROM runs "
                         "WHERE run_id <> '_pilot' ORDER BY run_id")
    sel = []
    for r in all_runs:
        if r["status"] != "done" or r["iface"] not in IFACE_ORDER:
            continue
        tagged = bool(r["tag"]) or bool(jl(r["params"], {}).get("archive"))
        if tagged and not a.include_tagged:
            continue
        if a.run and r["run_id"] not in a.run:
            continue
        sel.append((r, tagged))
    sel.sort(key=lambda x: run_sort_key(x[0]))

    totals: dict[str, int] = {}
    cards, parity_errs, written = [], [], []
    staged: list[tuple[str, str, str]] = []
    allow_all: list[str] = []
    for r, tagged in sel:
        rid = r["run_id"]
        ctf = jl(r["ctf"], {}) or {}
        allow = ctf_allow(ctf)
        allow_all.extend(allow)
        mask = None
        if a.mask_ctf:
            mask = {}
            if ctf.get("flag"):
                fl = ctf["flag"]
                inner = fl[5:-1] if fl.startswith("FLAG{") else fl
                mask[fl] = f"FLAG{{{inner[:4]}…}}"
            for k, lab in (("passphrase", "⟨passphrase⟩"), ("key_hex", "⟨key⟩"), ("vault_id", "⟨vault⟩")):
                if ctf.get(k):
                    mask[ctf[k]] = lab
            for fl in ctf_flags(ctf)[1:]:          # an unsolvable run's other flag-like values
                inner = fl[5:-1] if fl.startswith("FLAG{") else fl
                mask.setdefault(fl, f"FLAG{{{inner[:4]}…}}")
            for kh in ctf_keys(ctf)[1:]:           # its host-only seal key
                mask.setdefault(kh, "⟨seal key⟩")
            for seal in ctf_seals(ctf):
                mask.setdefault(seal, "⟨seal⟩")
        R = Redactor(deny=deny, allow=allow, mask=mask)
        rb = RunBuilder(con, rid, R, runs_dir, a.mask_ctf, meta)
        core, detail = rb.build()
        errs = rb.parity(core) + rb.errors
        core, detail, cj, dj = finalize(core, detail, R)
        v = validate_core(core) + validate_detail(detail, core)
        if v:
            parity_errs += [f"{rid} schema: {x}" for x in v]
        parity_errs += errs
        for k, n in R.counts.items():
            totals[k] = totals.get(k, 0) + n
        card = make_card(core, len(cj.encode()), len(dj.encode()), tagged, rb.runm)
        cards.append(card)
        staged.append((rid, cj, dj))
        print(f"  {rid:32s} nodes {len(core['nodes']):4d} edges {len(core['edges']):4d} events {len(core['events']['t']):5d}"
              f"  core {len(cj) / 1024:6.1f} KB  detail {len(dj) / 1024:6.1f} KB  score {card['score']}")

    if a.synth:
        import synth
        core, detail = synth.make_synth(meta)
        R = Redactor(deny=deny)
        core, detail, cj, dj = finalize(core, detail, R)
        v = validate_core(core) + validate_detail(detail, core)
        parity_errs += [f"_synth-500 schema: {x}" for x in v]
        cards.append(make_card(core, len(cj.encode()), len(dj.encode()), True, {}))
        staged.append(("_synth-500", cj, dj))
        print(f"  {'_synth-500':32s} nodes {len(core['nodes']):4d} edges {len(core['edges']):4d} events "
              f"{len(core['events']['t']):5d}  core {len(cj) / 1024:6.1f} KB  detail {len(dj) / 1024:6.1f} KB")

    if parity_errs:
        print("PARITY / SCHEMA FAILURES:", file=sys.stderr)
        for e in parity_errs:
            print("  " + e, file=sys.stderr)
        if not a.force:
            return 1

    listed = [c for c in cards if not c["hidden"]]
    if not listed:
        print("no runs to list", file=sys.stderr)
        return 1
    if a.featured:
        if not any(c["id"] == a.featured for c in cards):
            print(f"--featured {a.featured}: not exported", file=sys.stderr)
            return 1
        featured = a.featured
    else:
        featured = pick_featured(listed)
    if a.run and not any(c["id"] == featured for c in listed):
        featured = listed[0]["id"]

    findings = build_findings(con, runs_dir, listed)
    code_versions = sorted({r[0] for r in con.execute("SELECT DISTINCT code_version FROM runs WHERE run_id <> '_pilot'").fetchall() if r[0]})
    index = {
        "format": INDEX_FORMAT,
        "generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "generator": {"tool": "tools/export.py", "obsSchema": meta["obsSchema"], "obsDerive": meta["obsDerive"],
                      "codeVersions": code_versions, "ctf": "mask" if a.mask_ctf else "keep",
                      "redactions": {k: totals.get(k, 0) for k in ("host_path", "username", "email", "api_key", "ip", "host", "ctf_mask")},
                      "truncation": dict(TRUNC)},
        "featured": featured,
        "models": index_models(listed),
        "runs": cards,
        "findings": findings,
    }
    R0 = Redactor(deny=deny)
    index = normalize_numbers(redact_tree(index, R0))
    vi = validate_index(index)
    if vi:
        print("INDEX INVALID: " + "; ".join(vi), file=sys.stderr)
        return 1

    # ---------------------------------------------------------------- write (atomic per file), prune stale
    runs_out = out / "runs"
    runs_out.mkdir(parents=True, exist_ok=True)
    keep = set()
    for (rid, cj, dj), card in zip(staged, cards):
        for rel, body in ((card["core"], cj), (card["detail"], dj)):
            p = out / rel
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, p)
            written.append(p)
            keep.add(p.name)
    if not a.run:
        for p in runs_out.glob("*.json"):
            if p.name not in keep:
                p.unlink()
    else:
        ids = {rid for rid, _, _ in staged}
        for p in runs_out.glob("*.json"):
            rid = p.name.split(".")[0]
            if rid in ids and p.name not in keep:
                p.unlink()
    ip = out / "index.json"
    ip_old = ip.read_bytes() if ip.exists() else None
    tmp = ip.with_suffix(".json.tmp")
    tmp.write_text(jdump(index), encoding="utf-8")
    os.replace(tmp, ip)
    written.append(ip)

    # ---------------------------------------------------------------- gate
    if not a.no_gate:
        # the runs' CTF values for the gate (never public). Persisted to site/.cache only for the real
        # public/data export, so a test export of a run subset cannot shrink what `npm run check` allows.
        allow_json = json.dumps(sorted(set(allow_all)))
        default_out = (SITE / "public" / "data").resolve()
        allow_file = SITE / ".cache" / ("ctf-allow.json" if out.resolve() == default_out else f"ctf-allow.{os.getpid()}.json")
        allow_file.parent.mkdir(parents=True, exist_ok=True)
        allow_file.write_text(allow_json, encoding="utf-8")
        res = subprocess.run([sys.executable, str(TOOLS / "check_public.py"), str(out.parent), "--data-only",
                              "--allow", str(allow_file)]
                             + sum((["--deny", d] for d in a.deny), []), capture_output=True, text=True)
        if allow_file.name != "ctf-allow.json":
            allow_file.unlink(missing_ok=True)

        def _gate_failed(msg: str) -> int:
            print(msg, file=sys.stderr)
            for p in written:
                if p.exists() and p != ip:
                    p.unlink()
            if ip_old is not None:
                ip.write_bytes(ip_old)
            elif ip.exists():
                ip.unlink()
            print("public gate FAILED; written files removed", file=sys.stderr)
            return 1

        if res.returncode != 0:
            return _gate_failed(res.stdout + res.stderr)
        print("  " + res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "  gate ok")

        # Second pre-publish gate: the content-in / numbers-out chatter leak gate (tools/leakgate.py,
        # PROTOCOL.md section 6). It scans the published numbers-only instigation artifacts against the
        # blake2b shingle corpus of all known chatter; a non-ok verdict blocks publish. Its stdout is a
        # single numbers-only JSON verdict, so it is safe to surface here.
        lg = subprocess.run([sys.executable, str(REPO / "tools" / "leakgate.py"), "--root", str(REPO)],
                            capture_output=True, text=True)
        try:
            verdict = json.loads((lg.stdout or "").strip().splitlines()[-1]) if lg.stdout.strip() else {}
        except (ValueError, IndexError):
            verdict = {}
        if lg.returncode != 0 or not verdict.get("ok"):
            return _gate_failed(f"leakgate: {lg.stdout.strip() or lg.stderr.strip()}")
        print(f"  leakgate ok (scanned {verdict.get('scanned_files', 0)}, "
              f"corpus {verdict.get('corpus_shingles', 0)})")

    print(f"  featured: {featured}; listed {len(listed)} / exported {len(cards)}; findings.source={findings['source']}, "
          f"{len(findings['mismatches'])} mismatch(es); redactions {index['generator']['redactions']}")
    print(f"  index.json {ip.stat().st_size / 1024:.1f} KB; done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
