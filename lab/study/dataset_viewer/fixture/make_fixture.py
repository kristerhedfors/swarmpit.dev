#!/usr/bin/env python3
"""Build the PLACEHOLDER "swarmpit-instigation" package used to develop and test the viewer.

The package is produced by the REAL builder (study/dataset.py build) from a synthetic study tree
written into a temp dir: a registry, candidate boards with lineage (mutations up to generation 3 and
near-duplicate copies), and candidate-seeded run dirs (run.json, per-agent steps, structural board
events, a quarantined content sink and trigger artifacts) across goals, models and channels. Every
string is neutral lorem-ipsum filler from a fixed word list and a seeded RNG; no real board content,
chatter or model output is ever read. So the viewer is always tested against exactly what the
builder writes.

    python3 lab/study/dataset_viewer/fixture/make_fixture.py --out DIR [--root ROOT]

Writes the version dir's files (seeds/evaluations/responses/pairs/sft.jsonl, schema.json,
manifest.json, DATASHEET.md) flat into DIR. With --root, also keeps the builder's whole output dir
(version dir, `latest` pointers, tarball) at ROOT, so `dataset.py serve --out ROOT` can serve it.
Deterministic. Prints counts only. Never point either path at the real data/ tree.
"""
import argparse
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

_LAB = Path(__file__).resolve().parents[3]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut "
         "labore et dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris "
         "nisi aliquip ex ea commodo consequat duis aute irure in reprehenderit voluptate velit esse "
         "cillum fugiat nulla pariatur excepteur sint occaecat cupidatat non proident sunt culpa qui "
         "officia deserunt mollit anim id est laborum").split()
AUTHORS = ("placeholder_a", "placeholder_b", "placeholder_c", "placeholder_d", "placeholder_e")
GOALS = ("solvable", "unsolvable")
MODELS = {"qwen38": {"model": "qwen-3.8-27b"},
          "mistral24": {"provider": "berget", "model": "mistralai/Mistral-Small-3.2-24B-Instruct-2506"},
          "glm53f": {"provider": "berget", "model": "zai-org/GLM-5.3-Flash"},
          "gemma31": {"provider": "berget", "model": "google/gemma-4-31B-it"}}
CHANNELS = ("forum", "js", "html", "guestbook")
CODES = ("SAW_PEER_POSTS", "SAW_SEED_ONLY", "PEER_WROTE_FIRST", "EMPTY_BOARD", "DIRECT_QUESTION",
         "ADDRESSED_BY_NAME", "CALL_TO_ACTION", "TASK_HINT", "REPLY_TO_SELF", "UNPROMPTED")
N_ROOTS = 40
N_AGENTS = 4
FIXED_NOW = 1.7603e9


def lorem(rng, lo, hi):
    s = " ".join(rng.choice(WORDS) for _ in range(rng.randint(lo, hi)))
    return s[:1].upper() + s[1:] + "."


def make_board(rng):
    return {"board_name": lorem(rng, 1, 3).rstrip("."), "threads": [
        {"slug": f"t{t}-{rng.choice(WORDS)}", "title": lorem(rng, 2, 6),
         "posts": [{"author": rng.choice(AUTHORS), "body": lorem(rng, 4, 60), "ago_min": rng.randint(0, 600)}
                   for _ in range(rng.randint(1, 4))]}
        for t in range(rng.randint(1, 3))]}


def mutate(rng, parent, near_dup):
    b = json.loads(json.dumps(parent))
    posts = [p for t in b["threads"] for p in t["posts"]]
    if near_dup:  # one word appended to the longest body: Jaccard stays high
        max(posts, key=lambda p: len(p["body"]))["body"] += " " + rng.choice(WORDS)
    else:
        rng.choice(posts)["body"] = lorem(rng, 4, 60)
        if rng.random() < 0.4:
            b["threads"][0]["posts"].append({"author": rng.choice(AUTHORS), "body": lorem(rng, 3, 20),
                                             "ago_min": rng.randint(0, 120)})
    return b


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_tree(root: Path, seed: int = 20261013) -> dict:
    from study import registry
    rng = random.Random(seed)
    cands, reg, runs = root / "candidates", root / "registry.json", root / "runs"
    runs.mkdir(parents=True)
    seeds = []   # (rid, generation)

    def add(board, parent=None, gen=0, origin="scratch"):
        rid = registry.record_id(board)
        path = registry.candidate_path(rid, cands)
        registry._atomic_write(path, registry.canonical_json(board))
        registry.upsert(registry.make_row(board, parent_id=parent, generation=gen, channel="forum",
                                          model="qwen38", chatter_path=str(path),
                                          created_ts=1.76e9 + 60 * len(seeds), origin=origin), reg)
        seeds.append((rid, gen))
        return rid, board

    for _ in range(N_ROOTS):
        rid, b = add(make_board(rng))
        if rng.random() < 0.25:   # a near-duplicate of this root under its own lineage
            add(mutate(rng, b, near_dup=True))
        cur, cb = rid, b
        for gen in range(1, rng.choice((1, 1, 2, 3, 4))):
            cur, cb = add(mutate(rng, cb, near_dup=False), parent=cur, gen=gen, origin="mutation")

    rep = 1000
    for rid, _gen in seeds:
        quality = rng.random()
        for _ in range(rng.randint(1, 4)):
            rep += 1
            goal, slug, ch = rng.choice(GOALS), rng.choice(sorted(MODELS)), rng.choice(CHANNELS)
            name = f"{ch}-multi{N_AGENTS}-r{rep}" + ("-unsolvable" if goal == "unsolvable" else "") + f"-{rid}"
            d = runs / name
            params = {"candidate_id": rid, "seed_path": str(registry.candidate_path(rid, cands))}
            if goal == "unsolvable":
                params["goal"] = goal
            (d).mkdir(parents=True)
            (d / "run.json").write_text(json.dumps({
                "run_id": name, "iface": ch, "n_agents": N_AGENTS, "rep": rep, "started": 1000.0,
                "harness": dict(MODELS[slug], budget=40), "params": params}))
            for a in range(N_AGENTS):
                _jsonl(d / f"a{a}" / "steps.jsonl", [{"kind": "step", "step": s, "t0": 1000.0 + 10 * s,
                                                      "t1": 1005.0 + 10 * s} for s in range(1, 30)])
            writers = [a for a in range(N_AGENTS) if rng.random() < quality * 0.6]
            writes = []
            for a in writers:
                for _ in range(rng.randint(1, 3)):
                    writes.append((a, 1010.0 + rng.randint(0, 250), rng.randint(1, 3),
                                   "dm" if rng.random() < 0.15 else "post"))
            writes.sort(key=lambda w: (w[1], w[0]))
            ev = [{"ts": 1001.0, "agent": None, "kind": "board_start"}]
            ev += [{"ts": ts, "agent": a, "kind": k, "channel": ch, "post_id": 100 + i, "thread_seq": tseq,
                    "body_len_chars": 20} for i, (a, ts, tseq, k) in enumerate(writes)]
            ev += [{"ts": 1012.0, "agent": a, "kind": "req", "method": "GET"} for a in range(N_AGENTS)]
            _jsonl(d / "board_events.jsonl", ev)
            _jsonl(d / "board_events.content.chatter.json",
                   [{"post_id": 100 + i, "author": f"agent{a}", "body": lorem(rng, 3, 40), "channel": ch,
                     "thread_seq": tseq} for i, (a, _ts, tseq, _k) in enumerate(writes)])
            if writers:
                recs = [{"run_id": name, "agent_id": a, "codes": [{"code": rng.choice(CODES), "confidence": 0.8}],
                         "parser_version": 3, "model_slug": "qwen38"} for a in writers]
                (d / "instigation").mkdir()
                (d / "instigation" / "triggers.json").write_text(json.dumps({
                    "run_id": name, "parser_version": 3, "model_slug": "qwen38", "peer_wrote_first": {},
                    "records": recs, "panel": {"slugs": ["qwen38"], "votes": {},
                                               "agreement": {str(a): 1.0 for a in writers}}}))
    return {"registry_path": reg, "candidates_dir": cands, "runs_dir": runs}


def build(out: Path, root: Path | None = None) -> dict:
    from study import dataset as D
    with tempfile.TemporaryDirectory(prefix="dsfixture-") as td:
        tree = make_tree(Path(td) / "tree")
        if root is not None and root.exists():
            shutil.rmtree(root)
        dest = root if root is not None else Path(td) / "dataset"
        res = D.build(dest, archive_paths={}, pilot_path=False, now=FIXED_NOW, git_sha="0" * 40, **tree)
        if not res.get("ok"):
            raise RuntimeError("E_BUILD")
        vdir = dest / res["version"]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        for f in D.PACKAGE_FILES:
            shutil.copyfile(vdir / f, out / f)
    return {"ok": True, "version": res["version"], "counts": res.get("counts", {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    try:
        print(json.dumps(build(Path(a.out), Path(a.root) if a.root else None), sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 -- fixed code only
        print(json.dumps({"ok": False, "error": "E_FIXTURE"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
