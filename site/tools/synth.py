#!/usr/bin/env python3
"""Deterministic synthetic stress bundle `_synth-500` (SPEC 4.1.8): 16 agents, ~500 nodes, ~5k events.

Used by `export.py --synth`; the card is hidden and carries the `synthetic` caveat (reach it via
#run=_synth-500&perf=1). Seeded PRNG only, so the output (and its hash) is stable across machines.
"""
from __future__ import annotations

import random

from export import (CAVEAT_TEXT, CORE_FORMAT, DETAIL_FORMAT, EF, EK, NK, NOUNS, SC, VK, Strings, TRUNC,
                    EVENT_KINDS, HIDDEN_BY_DEFAULT, trunc)

N_AGENTS = 16
T_END = 300_000
WORDS = ("vault seal passphrase branch dangling commit xor base64 reverse key socket roll call agent session "
         "notes leave memory flag verified recipe tips anyone else here confirmed shortcut fsck unseal").split()


def make_synth(meta: dict) -> tuple[dict, dict]:
    rng = random.Random(500)
    S = Strings()
    nodes: list[dict] = []
    nidx: dict[str, int] = {}

    def node(nid, k, label, t0, **kw):
        nidx[nid] = len(nodes)
        rec = {"id": nid, "k": NK[k], "label": trunc(label, TRUNC["label"]), "t0": t0}
        rec.update({a: b for a, b in kw.items() if b is not None})
        nodes.append(rec)
        return nidx[nid]

    def sentence(n):
        return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."

    node("task", "task", "CTF vault", None)
    node("board", "board", "agent-commons · forum", None)
    starts = [2000 + i * 173 for i in range(N_AGENTS)]
    for i in range(N_AGENTS):
        node(f"a{i}", "agent", f"a{i}", starts[i], agent=i, names=[f"synth-{i}"])

    seed_threads = [f"seed-topic-{j}" for j in range(8)]
    live_threads = [(f"live-topic-{j}", 20_000 + j * 15_000 + rng.randrange(5000)) for j in range(16)]
    for slug in seed_threads:
        node(f"th:{slug}", "thread", slug, None, title=slug.replace("-", " "))
    for slug, t0 in live_threads:
        node(f"th:{slug}", "thread", slug, t0, title=slug.replace("-", " "))
    thread_nodes = [nidx[f"th:{s}"] for s in seed_threads] + [nidx[f"th:{s}"] for s, _ in live_threads]
    thread_t0 = {nidx[f"th:{s}"]: None for s in seed_threads}
    thread_t0.update({nidx[f"th:{s}"]: t for s, t in live_threads})

    personas = [f"seed:persona_{j}" for j in range(30)]
    handles = [f"name:handle{j}" for j in range(20)]
    n_seed_posts, n_live_posts = 60, 340
    first_post = len(nodes)
    persona_base = first_post + n_seed_posts + n_live_posts
    handle_base = persona_base + len(personas)

    # seed posts
    seed_post_nodes = []
    bodies: dict[str, str] = {}
    for j in range(n_seed_posts):
        th = thread_nodes[j % 8]
        pa = j % len(personas)
        body = sentence(18 + j % 30)
        bodies[f"s{j + 1}"] = body
        seed_post_nodes.append(node(f"s{j + 1}", "post", f"#{j + 1} persona_{pa}", None, thread=th,
                                    author=persona_base + pa, seed=True, tSeed=-3_600_000 * 6 + j * 300_000,
                                    postId=j + 1, preview=trunc(body, TRUNC["preview"]), bodyLen=len(body),
                                    upvotes0=1 + j % 9))
    # live posts: times spread over the run, authors round-robin with jitter
    live_specs = []
    for j in range(n_live_posts):
        a = rng.randrange(N_AGENTS)
        th = rng.choice(thread_nodes)
        t0 = thread_t0[th]
        lo = max(starts[a] + 15_000, (t0 or 0) + 1)
        t = rng.randrange(lo, T_END - 2000)
        live_specs.append((t, a, th))
    live_specs.sort()
    live_post_nodes = []
    for j, (t, a, th) in enumerate(live_specs):
        pid = n_seed_posts + 1 + j
        body = sentence(10 + j % 40)
        flags = ["secret_leak"] if j % 37 == 5 else (["self_id"] if j % 11 == 0 else None)
        if flags and "secret_leak" in flags:
            body = "Flag verified: FLAG{0000synth0000} " + body
        bodies[f"p{pid}"] = body
        live_post_nodes.append(node(f"p{pid}", "post", f"#{pid} a{a}", t, thread=th, author=nidx[f"a{a}"], agent=a,
                                    postId=pid, preview=trunc(body, TRUNC["preview"]), bodyLen=len(body), flags=flags))
    for pn in personas:
        node(pn, "persona", pn.split(":")[1], None)
    for hn in handles:
        node(hn, "handle", hn.split(":")[1], None)
    assert nidx[personas[0]] == persona_base and nidx[handles[0]] == handle_base

    edges: list[dict] = []
    eidx: dict = {}
    efires: list[list] = []

    def edge(s, d, k, inferred=False):
        key = (s, d, EK[k])
        if key not in eidx:
            eidx[key] = len(edges)
            rec = {"s": s, "d": d, "k": EK[k]}
            if inferred:
                rec["inferred"] = True
            if nodes[s]["k"] == NK["agent"] and nodes[d]["k"] == NK["agent"] and s != d:
                rec["liveLive"] = True
            edges.append(rec)
            efires.append([])
        return eidx[key]

    events: list[dict] = []
    items: list = []

    def event(t, k, a, n=-1, e=-1, f=0, s=-1, c=-1, label="", x=None, d=0, rank=3, seq=0):
        ev = dict(t=int(t), d=int(d), k=VK[k], a=a, n=n, e=e, f=f, s=s, c=c, l=label, x=x,
                  lane=a if a >= 0 else -1, rank=rank, seq=seq)
        events.append(ev)
        return ev

    def fire(e, t, w=1.0, ev=None):
        efires[e].append((int(t), float(w), ev))

    bs = event(400, "boardStart", -1, 1, label="agent-commons opens (forum)", seq=0)
    for th in thread_nodes[:8]:
        fire(edge(1, th, "contains"), 0, 1, bs)
    for pn in seed_post_nodes:
        fire(edge(nodes[pn]["thread"], pn, "contains"), 0, 1, bs)
        fire(edge(nodes[pn]["author"], pn, "authored"), 0, 1, bs)

    solve_t: dict[int, int] = {}
    end_t: dict[int, int] = {}
    seqc = [0] * N_AGENTS
    drift = {i: ([], []) for i in range(N_AGENTS)}
    for i in range(N_AGENTS):
        an = nidx[f"a{i}"]
        event(starts[i], "start", i, an, label="start", rank=1, seq=0)
        t = starts[i] + 8000
        av = 0.0
        solve_step = 25 + rng.randrange(25)
        for st in range(1, 101):
            seqc[i] += 2
            free = st > solve_step
            lat = 200 + rng.randrange(900)
            event(t, "think", i, an, f=EF["FREE"] if free else 0, s=st, label=sentence(8), d=lat,
                  x={"t": "think", "reason": sentence(6), "content": "", "nTools": 1, "tok": [4000 + st * 90, 120, 40, 2048]},
                  rank=0, seq=seqc[i])
            t += lat
            board = rng.random() < (0.55 if free else 0.18)
            fl = EF["FREE"] if free else 0
            dur = 30 + rng.randrange(400)
            if st == solve_step:
                e = edge(an, 0, "works")
                ev = event(t, "submit", i, 0, e=e, f=fl | EF["SOLVE"] | EF["SUBMIT_OK"], s=st, c=SC["submit"],
                           label="submit 'FLAG{0000synth0000}'", d=dur, rank=1, seq=seqc[i] + 1,
                           x={"t": "step", "cmd": "submit 'FLAG{0000synth0000}'", "out": "correct", "outLen": 7, "exit": 0})
                fire(e, t, 1, ev)
                solve_t[i] = t + dur
            elif board:
                event(t, "board", i, 1, f=fl | EF["BOARD"] | EF["HEURISTIC"], s=st, c=SC["board"],
                      label="curl -s http://board.internal:8080/api/v1/threads", d=dur, rank=1, seq=seqc[i] + 1,
                      x={"t": "step", "cmd": "curl -s http://board.internal:8080/api/v1/threads", "out": "[...]",
                         "outLen": 5, "exit": 0, "via": "relay"})
                for r in range(1 + rng.randrange(3)):
                    th = rng.choice([x for x in thread_nodes if (thread_t0[x] or 0) <= t])
                    tr = t + 5 + r * 3
                    e = edge(an, th, "reads")
                    ev = event(tr, "read", i, th, e=e, f=fl, s=st, label=f"GET /api/v1/threads/{nodes[th]['label']}",
                               x={"t": "req", "method": "GET", "path": f"/api/v1/threads/{nodes[th]['label']}",
                                  "status": 200, "bytes": 900}, seq=seqc[i] * 10 + r)
                    fire(e, tr, 1, ev)
                    # exposure to the authors of a couple of posts in that thread
                    for pn in [p for p in seed_post_nodes + live_post_nodes if nodes[p]["thread"] == th
                               and (nodes[p]["t0"] or -1) < tr][:3]:
                        au = nodes[pn]["author"]
                        if au != an:
                            fire(edge(an, au, "exposure", inferred=True), tr, 1, ev)
            else:
                cat = rng.choice(["ctf", "ctf", "recon", "other"]) if not free else "idle"
                if cat == "idle":
                    event(t, "idle", i, -1, f=fl, s=st, c=SC["idle"], d=0, rank=1, seq=seqc[i] + 1)
                else:
                    e = edge(an, 0, "works")
                    ev = event(t, "cmd", i, 0, e=e, f=fl, s=st, c=SC[cat], label="cd ~/ctf/project && git log --all",
                               d=dur, rank=1, seq=seqc[i] + 1,
                               x={"t": "step", "cmd": "cd ~/ctf/project && git log --all", "out": sentence(8),
                                  "outLen": 120, "exit": 0})
                    fire(e, t, 1, ev)
            av = 0.65 * av + 0.35 * (1.0 if board else 0.0)
            drift[i][0].append(t)
            drift[i][1].append(round(av, 4))
            t += dur + 50
            if t > T_END - 3000:
                break
        end_t[i] = min(t, T_END - 1000)
        event(end_t[i], "end", i, an, label="end · budget", x={"t": "line", "text": "ended: budget"}, rank=1, seq=10_000)

    born = set(thread_nodes[:8])
    upvotes = 0
    for j, pn in enumerate(live_post_nodes):
        p = nodes[pn]
        a, t, th = p["agent"], p["t0"], p["thread"]
        an = nidx[f"a{a}"]
        f = EF["FREE"] if t >= solve_t.get(a, 10 ** 9) else 0
        if "flags" in p and "secret_leak" in p["flags"]:
            f |= EF["LEAK"]
        e = edge(an, th, "writes")
        ev = event(t, "post", a, pn, e=e, f=f, label=f'{nodes[th]["label"]}: "{p["preview"][:60]}"',
                   x={"t": "req", "method": "POST", "path": f"/api/v1/threads/{nodes[th]['label']}/posts", "status": 201,
                      "bytes": 33}, seq=50_000 + j)
        fire(e, t, 1, ev)
        fire(edge(an, pn, "authored"), t, 1, ev)
        fire(edge(th, pn, "contains"), t, 1, ev)
        if th not in born:
            born.add(th)
            fire(edge(1, th, "contains"), t, 1, ev)
        prev = [q for q in seed_post_nodes + live_post_nodes[:j] if nodes[q]["thread"] == th and nodes[q]["author"] != an]
        if prev:
            au = nodes[prev[-1]]["author"]
            ee = edge(an, au, "reply", inferred=True)
            fire(ee, t, 1, ev)
            if nodes[au]["k"] == NK["agent"]:
                ev["f"] |= EF["LIVE_LIVE"]
        if j % 9 == 0:
            fire(edge(an, handle_base + j % len(handles), "mention"), t, 1, ev)
        if j % 4 == 0 and upvotes < 100:
            target = seed_post_nodes[j % len(seed_post_nodes)]
            tu = min(T_END - 1500, t + 700)
            ue = edge(an, nodes[target]["author"], "upvote")
            uev = event(tu, "upvote", a, target, e=ue, f=f, label=f"▲ #{nodes[target]['postId']}", seq=60_000 + j)
            fire(ue, tu, 1, uev)
            upvotes += 1

    events.sort(key=lambda v: (v["t"], v["lane"], v["rank"], v["seq"]))
    for i, v in enumerate(events):
        v["i"] = i
    cols = {k: [] for k in "tdkanefsclx"}
    for v in events:
        for k in "tdkanefsc":
            cols[k].append(v[k])
        cols["l"].append(S(trunc(v["l"], TRUNC["l"])))
        if v["x"] is not None:
            cols["x"].append(len(items))
            items.append(v["x"])
        else:
            cols["x"].append(-1)
    fires = sorted(((e, t, w, ev["i"] if ev is not None else -1) for e, fl in enumerate(efires) for (t, w, ev) in fl),
                   key=lambda x: (x[0], x[1], x[3]))
    fire_cols = {"e": [x[0] for x in fires], "t": [x[1] for x in fires], "w": [x[2] for x in fires], "ev": [x[3] for x in fires]}

    agents = []
    for i in range(N_AGENTS):
        n_board = sum(1 for v in events if v["a"] == i and v["k"] == VK["board"])
        agents.append({
            "id": i, "node": nidx[f"a{i}"], "names": [f"synth-{i}"], "model": "synthetic", "status": "done",
            "startT": starts[i], "endT": end_t[i], "solveT": solve_t.get(i), "solveStep": None,
            "stepsUsed": len(drift[i][0]), "modelCalls": len(drift[i][0]), "ended": "budget",
            "tokens": {"prompt": 500_000, "completion": 12_000, "cached": 400_000, "reasoning": 3000},
            "metrics": {"discovered": n_board > 0, "firstContact": None, "boardSteps": n_board,
                        "shareAfterContact": None, "posts": sum(1 for p in live_post_nodes if nodes[p]["agent"] == i),
                        "episodes": 0, "secretLeak": False},
            "drift": {"t": drift[i][0], "a": drift[i][1]},
        })
    first_solve = min(solve_t.values())
    last_end = max(end_t.values())
    segments = [{"t0": 0, "t1": starts[0], "title": "Boot"}, {"t0": starts[0], "t1": first_solve, "title": "On task"},
                {"t0": first_solve, "t1": last_end, "title": "Free time"}, {"t0": last_end, "t1": T_END, "title": "Wind-down"}]
    chapters = [{"t": first_solve, "k": "solve", "p": 1, "label": "first solve", "caption": "The first synthetic agent solves."}]
    bin_ms = 2000
    nb = T_END // bin_ms
    density = [0] * nb
    for v in events:
        if EVENT_KINDS[v["k"]] not in HIDDEN_BY_DEFAULT:
            density[min(nb - 1, v["t"] // bin_ms)] += 1
    run = {
        "id": "_synth-500", "iface": "forum", "pop": "multi", "nAgents": N_AGENTS, "rep": 0, "tag": "synthetic",
        "model": "synthetic", "harness": {"budget": 100, "temperature": 0.7, "maxTokens": 2048, "outputChars": 3000},
        "boardName": "agent-commons", "nouns": NOUNS["forum"], "tEndMs": T_END, "wallMs": T_END,
        "prehistoryMs": min(n["tSeed"] for n in nodes if "tSeed" in n),
        "caveats": [{"code": "synthetic", "text": CAVEAT_TEXT["synthetic"]}], "codeVersion": "synthetic",
        "ctfMasked": False,
        "source": {"contract": "dash/CONTRACT.md v1", "obsSchema": meta["obsSchema"], "obsDerive": meta["obsDerive"],
                   "dataVersion": 0},
    }
    core = {"format": CORE_FORMAT, "run": run, "strings": S.list, "nodes": nodes, "edges": edges, "events": cols,
            "fires": fire_cols, "agents": agents, "chapters": chapters, "segments": segments,
            "series": {"binMs": bin_ms, "density": density, "boardShare": [[0] * nb for _ in range(N_AGENTS)]}}
    posts = {k: {"body": v, "leakSpans": [], "mentions": []} for k, v in bodies.items()}
    for k, v in posts.items():
        i = v["body"].find("FLAG{0000synth0000}")
        if i >= 0:
            v["leakSpans"] = [[i, i + len("FLAG{0000synth0000}")]]
    return core, {"format": DETAIL_FORMAT, "items": items, "posts": posts}
