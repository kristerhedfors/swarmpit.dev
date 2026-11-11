"""Offline tests for the training-data package builder (study/dataset.py).

Run from the lab/ directory:  python3 -m unittest study.test_dataset

Every fixture is SYNTHETIC: a temp registry, temp candidate files, temp run dirs and temp content sinks whose
only text is neutral placeholder words ("lorem ipsum ..."). Nothing here reads the real data/, runs/ or
lab/seeds/ trees (the pilot is a temp placeholder or disabled), and assertions never print row text.
"""
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from study import dataset as D
from study import landscape as L
from study import registry

WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore "
         "et dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi").split()
PLACEHOLDER = re.compile(r"lorem|ipsum|dolor|consectetur|eiusmod", re.IGNORECASE)
QWEN = {"model": "qwen-3.8-27b", "budget": 40}


def words(n, off=0):
    return " ".join(WORDS[(off + i) % len(WORDS)] for i in range(n))


def board(name, bodies, threads=1):
    return {"board_name": name, "threads": [
        {"slug": f"t{t}", "title": words(2, t), "posts": [{"author": f"a{i}", "body": b, "ago_min": 5 * i}
                                                          for i, b in enumerate(bodies)]}
        for t in range(threads)]}


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def assert_safe(tc, text: str):
    """Console output: no placeholder words (stand-ins for content), only JSON numbers/ids/enums."""
    tc.assertIsNone(PLACEHOLDER.search(text), "content leaked into console output")


class Tree:
    """A temp study tree: registry + candidates + runs (+ content sinks, trigger artifacts) + archive."""

    def __init__(self, root: Path):
        self.root = root
        self.cands = root / "candidates"
        self.registry = root / "registry.json"
        self.runs = root / "runs"
        self.runs.mkdir(parents=True)
        self.out = root / "dataset"
        self.rep = 7000

    def add_seed(self, cand, *, parent=None, generation=0, origin=None, model="qwen38"):
        rid = registry.record_id(cand)
        path = registry.candidate_path(rid, self.cands)
        registry._atomic_write(path, registry.canonical_json(cand))
        row = registry.make_row(cand, parent_id=parent, generation=generation, channel="forum", model=model,
                                chatter_path=str(path), created_ts=1.7e9 + generation, origin=origin)
        registry.upsert(row, self.registry)
        return rid

    def add_run(self, rid, *, iface="forum", goal="unsolvable", writes=(), codes=None, sink=True, n_agents=4):
        """writes = [(agent, ts, thread_seq)]; codes = {agent: ENUM code}."""
        self.rep += 1
        name = f"{iface}-multi{n_agents}-r{self.rep}" + ("-unsolvable" if goal == "unsolvable" else "") + f"-{rid}"
        d = self.runs / name
        params = {"candidate_id": rid, "seed_path": str(registry.candidate_path(rid, self.cands))}
        if goal != "solvable":
            params["goal"] = goal
        _write(d / "run.json", {"run_id": name, "iface": iface, "n_agents": n_agents, "rep": self.rep,
                                "harness": dict(QWEN), "started": 1000.0, "params": params})
        for a in range(n_agents):
            _jsonl(d / f"a{a}" / "steps.jsonl", [{"kind": "step", "step": s, "t0": 1000.0 + 10 * s,
                                                  "t1": 1005.0 + 10 * s} for s in range(1, 30)])
        ev = [{"ts": 1001.0, "agent": None, "kind": "board_start"}]
        ev += [{"ts": ts, "agent": a, "kind": "post", "channel": iface, "post_id": 100 + i, "thread_seq": tseq,
                "body_len_chars": 11} for i, (a, ts, tseq) in enumerate(writes)]
        ev += [{"ts": 1012.0, "agent": a, "kind": "req", "method": "GET"} for a in range(n_agents)]
        _jsonl(d / "board_events.jsonl", ev)
        if sink:
            rows = [{"post_id": 100 + i, "author": f"agent{a}", "body": words(4, i), "channel": iface,
                     "thread_seq": tseq} for i, (a, _ts, tseq) in enumerate(writes)]
            rows.append({"post_id": 9999, "author": "x", "body": words(3), "channel": iface, "thread_seq": 1})
            _jsonl(d / "board_events.content.chatter.json", rows)
        if codes:
            recs = [{"run_id": name, "agent_id": a, "codes": [{"code": c, "confidence": 0.8}], "parser_version": 3,
                     "model_slug": "qwen38"} for a, c in codes.items()]
            _write(d / "instigation" / "triggers.json", {
                "run_id": name, "parser_version": 3, "model_slug": "qwen38", "peer_wrote_first": {},
                "records": recs, "panel": {"slugs": ["qwen38"], "votes": {},
                                           "agreement": {str(a): 1.0 for a in codes}}})
        return name

    def build(self, out=None, **kw):
        args = dict(runs_dir=self.runs, registry_path=self.registry, candidates_dir=self.cands,
                    archive_paths={}, pilot_path=False, now=1.76e9, git_sha="0" * 40)
        args.update(kw)
        return D.build(out or self.out, **args)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = D.main(list(argv), out=out)
        return rc, out.getvalue(), err.getvalue()


def read_rows(vdir: Path, name: str):
    with open(vdir / name, encoding="utf-8") as f:
        return [json.loads(x) for x in f]


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.t = Tree(Path(self._td.name))
        t = self.t
        self.A = t.add_seed(board("pa", [words(30), words(12, 3)]), origin="scratch")
        self.A1 = t.add_seed(board("pa1", [words(20, 5)]), parent=self.A, generation=1, origin="mutation")
        self.A2 = t.add_seed(board("pa2", [words(8, 9)], threads=2), parent=self.A1, generation=2,
                             origin="mutation")
        self.B = t.add_seed(board("pb", [words(25, 11)]), origin="scratch")
        # near duplicate of A (one word of the long body differs): its own root, A's cluster and split
        self.Dn = t.add_seed(board("pa", [words(29) + " amet", words(12, 3)]), origin="scratch")
        self.P = t.add_seed(board("pp", [words(6, 2)]), origin="pilot", model="")
        self.r1 = t.add_run(self.A, writes=[(0, 1025.0, 1), (2, 1047.0, 2), (0, 1060.0, 1)],
                            codes={0: "TASK_HINT", 2: "SAW_PEER_POSTS"})
        self.r2 = t.add_run(self.A1, writes=[(1, 1031.0, 1)], codes={1: "CALL_TO_ACTION"})
        self.r3 = t.add_run(self.A2)                       # no writers
        self.r4 = t.add_run(self.B, writes=[(3, 1015.0, 1)], sink=False)  # writer, no artifact, no sink
        self.r5 = t.add_run(self.B, iface="js")
        self.r6 = t.add_run(self.A, goal="solvable", writes=[(1, 1020.0, 1)], codes={1: "DIRECT_QUESTION"})

    def tearDown(self):
        self._td.cleanup()


class HelperTests(unittest.TestCase):
    def test_wilson(self):
        self.assertIsNone(D.wilson(0, 0))
        lo, hi = D.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.236593, places=5)
        self.assertAlmostEqual(hi, 0.763407, places=5)
        self.assertEqual(D.wilson(0, 3)[0], 0.0)
        self.assertEqual(D.wilson(3, 3)[1], 1.0)

    def test_split_is_deterministic_and_roughly_80_10_10(self):
        got = [D.split_of(hashlib.sha256(str(i).encode()).hexdigest()[:16]) for i in range(2000)]
        self.assertEqual(got, [D.split_of(hashlib.sha256(str(i).encode()).hexdigest()[:16]) for i in range(2000)])
        frac = {s: got.count(s) / len(got) for s in D.SPLITS}
        self.assertAlmostEqual(frac["train"], 0.8, delta=0.04)
        self.assertAlmostEqual(frac["val"], 0.1, delta=0.03)

    def test_near_dup_clusters(self):
        texts = {"0" * 16: words(40), "1" * 16: words(39) + " elit", "2" * 16: " ".join(reversed(words(40))),
                 "3" * 16: words(40)}
        c = D.near_dup_clusters(texts)
        self.assertEqual(c["1" * 16], "0" * 16)
        self.assertEqual(c["3" * 16], "0" * 16)   # exact duplicate
        self.assertEqual(c["2" * 16], "2" * 16)

    def test_board_of_rejects_off_shape(self):
        self.assertIsNone(D.board_of({"board_name": "x", "threads": []}))
        self.assertIsNone(D.board_of({"board_name": "x", "threads": [{"slug": "s", "title": "t", "posts": [
            {"author": "a", "body": "b", "ago_min": -1}]}]}))
        b = board("x", ["y"])
        self.assertEqual(D.board_of(b), b)

    def test_validator(self):
        s = D._obj({"a": D._INT0, "b": {"type": ["string", "null"], "pattern": "^[a-z]+$"}})
        self.assertTrue(D.validate({"a": 1, "b": None}, s))
        self.assertTrue(D.validate({"a": 0, "b": "ab"}, s))
        self.assertFalse(D.validate({"a": True, "b": None}, s))
        self.assertFalse(D.validate({"a": -1, "b": None}, s))
        self.assertFalse(D.validate({"a": 1, "b": "A"}, s))
        self.assertFalse(D.validate({"a": 1, "b": None, "c": 1}, s))
        self.assertFalse(D.validate({"a": 1}, s))


class BuildTests(_Base):
    def test_package_layout_and_verify(self):
        res = self.t.build()
        self.assertTrue(res["ok"])
        self.assertTrue(res["new_version"])
        v = res["version"]
        self.assertRegex(v, r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}$")
        self.assertEqual(v[-8:], res["content_hash"][:8])
        vdir = self.t.out / v
        for f in D.PACKAGE_FILES:
            self.assertTrue((vdir / f).is_file(), f)
        self.assertTrue((self.t.out / f"{v}.tar.gz").is_file())
        self.assertEqual(D.latest_version(self.t.out), v)
        self.assertEqual(os.readlink(self.t.out / "latest"), v)
        self.assertEqual(D.package_path(self.t.out), vdir)
        ver = D.verify(self.t.out)
        self.assertTrue(ver["ok"], ver["errors"])
        self.assertEqual(ver["counts"]["seeds.jsonl"], 6)
        man = json.loads((vdir / "manifest.json").read_text())
        self.assertEqual(man["name"], "swarmpit-instigation")
        self.assertEqual(man["schema"], "instigation.dataset.v1")
        self.assertEqual(man["counts"]["files"], res["counts"])
        self.assertEqual(man["source"]["n_episodes"], 6)
        self.assertEqual(man["builder_git_sha"], "0" * 40)
        for f in D.PACKAGE_FILES:
            if f != "manifest.json":
                self.assertEqual(man["sha256"][f], hashlib.sha256((vdir / f).read_bytes()).hexdigest())
        # every row validates against the packaged schema
        schema = json.loads((vdir / "schema.json").read_text())
        for f in D.JSONL_FILES:
            for r in read_rows(vdir, f):
                self.assertTrue(D.validate(r, schema["files"][f]), f)
        # the tarball holds exactly the version dir
        import tarfile
        with tarfile.open(self.t.out / f"{v}.tar.gz") as tar:
            self.assertEqual(sorted(tar.getnames()), sorted(f"{v}/{f}" for f in D.PACKAGE_FILES))

    def test_datasheet_is_template_plus_numbers(self):
        v = self.t.build()["version"]
        text = (self.t.out / v / "DATASHEET.md").read_text()
        assert_safe(self, text)
        self.assertIn("Adversarial content -- for defensive research; do not publish", text)
        for head in ("Motivation", "Composition", "Collection process", "Labels and their limits", "Splits",
                     "Intended uses", "Safety", "License", "How to rebuild"):
            self.assertIn(f"## {head}", text)

    def test_seed_rows(self):
        v = self.t.build()["version"]
        seeds = {r["seed_id"]: r for r in read_rows(self.t.out / v, "seeds.jsonl")}
        self.assertEqual(set(seeds), {self.A, self.A1, self.A2, self.B, self.Dn, self.P})
        a = seeds[self.A]
        cand = json.loads(registry.candidate_path(self.A, self.t.cands).read_text())
        self.assertEqual(a["content"], cand)                       # exactly as stored
        self.assertEqual(registry.record_id(a["content"]), self.A)
        self.assertEqual(a["text"], D.render_text(cand))
        self.assertEqual(a["stats"]["seed_chars"], sum(len(p["body"]) for t in cand["threads"] for p in t["posts"]))
        self.assertEqual(a["stats"]["length_band"], L.length_band(a["stats"]["seed_chars"]))
        self.assertEqual(seeds[self.A2]["provenance"]["root_id"], self.A)
        self.assertEqual(seeds[self.A2]["provenance"]["parent_id"], self.A1)
        self.assertEqual(seeds[self.A2]["provenance"]["origin"], "mutation")
        self.assertEqual(seeds[self.P]["provenance"]["origin"], "pilot")
        self.assertIsNone(seeds[self.P]["provenance"]["gen_model"])
        o = a["outcomes"]
        self.assertEqual(o["n_evals"], 2)
        self.assertEqual(o["n_evals_with_writers"], 2)
        self.assertEqual(o["writer_rate"], 1.0)
        self.assertEqual(o["writer_rate_ci95"], D.wilson(2, 2))
        self.assertEqual(o["pile_in_rate"], 0.5)
        self.assertEqual(set(o["by_context"]), {"unsolvable|qwen38|forum|4|40", "solvable|qwen38|forum|4|40"})
        self.assertEqual(o["by_context"]["unsolvable|qwen38|forum|4|40"]["max_writers"], 2)
        self.assertEqual(o["by_context"]["unsolvable|qwen38|forum|4|40"]["mean_writers"], 2.0)
        self.assertEqual(a["triggers"], {"TASK_HINT": 1, "SAW_PEER_POSTS": 1, "DIRECT_QUESTION": 1})
        self.assertEqual(seeds[self.P]["outcomes"]["n_evals"], 0)
        self.assertIsNone(seeds[self.P]["outcomes"]["writer_rate"])
        # dedup: the near-duplicate joins A's cluster
        self.assertEqual(seeds[self.Dn]["dedup"]["near_dup_cluster"], seeds[self.A]["dedup"]["near_dup_cluster"])
        self.assertNotEqual(seeds[self.B]["dedup"]["near_dup_cluster"], seeds[self.A]["dedup"]["near_dup_cluster"])

    def test_evaluation_rows(self):
        v = self.t.build()["version"]
        ev = {r["eval_id"]: r for r in read_rows(self.t.out / v, "evaluations.jsonl")}
        self.assertEqual(set(ev), {self.r1, self.r2, self.r3, self.r4, self.r5, self.r6})
        e = ev[self.r1]
        self.assertEqual(e["context"], {"goal": "unsolvable", "model": "qwen38", "channel": "forum",
                                        "n_agents": 4, "budget": 40})
        self.assertEqual(e["outcome"]["n_writers"], 2)
        self.assertEqual(e["label"], {"instigated": True, "pile_in": True})
        self.assertEqual(e["triggers"]["per_agent"], ["TASK_HINT", None, "SAW_PEER_POSTS", None])
        self.assertEqual(e["triggers"]["panel_agreement"], 1.0)
        self.assertEqual(ev[self.r3]["label"], {"instigated": False, "pile_in": False})
        self.assertEqual(ev[self.r3]["triggers"]["primary"], "NO_WRITERS")
        self.assertEqual(ev[self.r4]["triggers"]["primary"], "UNRESOLVED")
        self.assertEqual(ev[self.r6]["context"]["goal"], "solvable")

    def test_responses_from_content_sink(self):
        v = self.t.build()["version"]
        rs = read_rows(self.t.out / v, "responses.jsonl")
        by_eval = {}
        for r in rs:
            by_eval.setdefault(r["eval_id"], []).append(r)
        self.assertEqual(set(by_eval), {self.r1, self.r2, self.r6})   # r4 wrote but has no sink
        r1 = by_eval[self.r1]
        self.assertEqual([(r["agent_id"], r["post_id"]) for r in r1], [(0, 100), (2, 101), (0, 102)])
        self.assertEqual([r["t_rel_s"] for r in r1], [25.0, 47.0, 60.0])
        self.assertEqual([r["trigger_code"] for r in r1], ["TASK_HINT", "SAW_PEER_POSTS", "TASK_HINT"])
        self.assertEqual([r["thread_ref"] for r in r1], [1, 2, 1])
        self.assertEqual([r["thread_ref_is_seed"] for r in r1], [True, False, True])  # A has one thread
        self.assertTrue(all(r["kind"] == "post" and r["seed_id"] == self.A for r in r1))
        self.assertTrue(all(r["body_chars"] == len(r["body"]) for r in rs))
        self.assertNotIn(9999, [r["post_id"] for r in rs])   # sink line without an agent write event
        man = json.loads((self.t.out / v / "manifest.json").read_text())
        self.assertEqual(man["source"]["runs_with_content_sink"], 5)
        self.assertEqual(man["source"]["runs_without_content_sink"], 1)

    def test_pairs_and_sft(self):
        v = self.t.build()["version"]
        seeds = {r["seed_id"]: r for r in read_rows(self.t.out / v, "seeds.jsonl")}
        pairs = read_rows(self.t.out / v, "pairs.jsonl")
        for p in pairs:
            self.assertEqual(seeds[p["chosen_seed_id"]]["split"], seeds[p["rejected_seed_id"]]["split"])
            self.assertEqual(p["split"], seeds[p["chosen_seed_id"]]["split"])
            self.assertEqual(p["chosen"]["n"], p["rejected"]["n"])            # equal-n cells only
            self.assertTrue(p["margin"]["n_writers"] >= D.PAIR_MIN_WRITER_MARGIN or (
                p["margin"]["n_writers"] == 0 and p["margin"]["instigation_score"] >= D.PAIR_MIN_SCORE_MARGIN))
            self.assertEqual(set(p["context"]), {"goal", "model", "channel", "n_agents", "budget"})
        ctx = "unsolvable|qwen38|forum"
        got = {(p["chosen_seed_id"], p["rejected_seed_id"]) for p in pairs
               if "|".join(p["context"][k] for k in ("goal", "model", "channel")) == ctx}
        # A (2 writers) > A1 (1) > A2 (0) -- one lineage, so one split
        self.assertEqual(got >= {(self.A, self.A1), (self.A, self.A2), (self.A1, self.A2)}, True)
        sft = read_rows(self.t.out / v, "sft.jsonl")
        self.assertEqual({(r["seed_id"], r["context"]["goal"], r["context"]["channel"]) for r in sft},
                         {(self.A, "unsolvable", "forum"), (self.A1, "unsolvable", "forum"),
                          (self.B, "unsolvable", "forum"), (self.A, "solvable", "forum")})
        for r in sft:
            self.assertEqual(json.loads(r["completion"]), seeds[r["seed_id"]]["content"])
            self.assertEqual(r["completion_text"], seeds[r["seed_id"]]["text"])
            assert_safe(self, r["prompt"])                 # script-authored from enums only
            self.assertIn(r["context"]["channel"], r["prompt"])
            self.assertIn(r["context"]["length_band"], r["prompt"])
            self.assertEqual((r["context"]["n_agents"], r["context"]["budget"]), (4, 40))
            self.assertEqual(r["near_dup_cluster"], seeds[r["seed_id"]]["dedup"]["near_dup_cluster"])
            self.assertGreater(r["weight"], 0)

    def test_split_by_lineage_root_and_near_dup(self):
        v = self.t.build()["version"]
        seeds = {r["seed_id"]: r for r in read_rows(self.t.out / v, "seeds.jsonl")}
        for s in seeds.values():
            self.assertEqual(s["split"], seeds[s["provenance"]["root_id"]]["split"])
        self.assertEqual(seeds[self.Dn]["split"], seeds[self.A]["split"])   # near-dup joins A's group
        for f in ("evaluations.jsonl", "responses.jsonl", "sft.jsonl"):
            for r in read_rows(self.t.out / v, f):
                self.assertEqual(r["split"], seeds[r["seed_id"]]["split"], f)

    def test_no_cross_split_pairs_when_roots_differ(self):
        forced = {self.A: "train", self.B: "test", self.P: "val"}
        with mock.patch.object(D, "split_of", lambda key: forced.get(key, "train")):
            v = self.t.build()["version"]
        seeds = {r["seed_id"]: r for r in read_rows(self.t.out / v, "seeds.jsonl")}
        self.assertEqual(seeds[self.B]["split"], "test")
        self.assertEqual(seeds[self.A2]["split"], "train")
        pairs = read_rows(self.t.out / v, "pairs.jsonl")
        self.assertTrue(pairs)
        for p in pairs:
            self.assertNotIn(self.B, (p["chosen_seed_id"], p["rejected_seed_id"]))  # B alone in test
        self.assertTrue(D.verify(self.t.out)["ok"])

    def test_pair_cap_is_deterministic(self):
        seeds = D.collect_seeds(self.t.registry, self.t.cands, False)
        eps = D.collect_episodes(self.t.runs)
        rows1, _ = D.assemble(seeds, eps, {}, max_pairs=1)
        rows2, _ = D.assemble(seeds, list(reversed(eps)), {}, max_pairs=1)
        self.assertEqual(rows1["pairs.jsonl"], rows2["pairs.jsonl"])
        per_ctx = {}
        for p in rows1["pairs.jsonl"]:
            k = json.dumps(p["context"], sort_keys=True)
            per_ctx[k] = per_ctx.get(k, 0) + 1
        self.assertTrue(all(n <= 1 for n in per_ctx.values()))

    def test_deterministic_rebuild(self):
        r1 = self.t.build(self.t.root / "o1")
        r2 = self.t.build(self.t.root / "o2")
        self.assertEqual(r1["version"], r2["version"])
        v = r1["version"]
        for f in D.PACKAGE_FILES:
            self.assertEqual((self.t.root / "o1" / v / f).read_bytes(), (self.t.root / "o2" / v / f).read_bytes(), f)
        self.assertEqual((self.t.root / "o1" / f"{v}.tar.gz").read_bytes(),
                         (self.t.root / "o2" / f"{v}.tar.gz").read_bytes())

    def test_idempotent_versioning(self):
        r1 = self.t.build()
        r2 = self.t.build(now=1.77e9, git_sha="1" * 40)   # another day, same content
        self.assertFalse(r2["new_version"])
        self.assertEqual(r2["version"], r1["version"])
        self.assertEqual(D._versions(self.t.out), [r1["version"]])
        # new content -> new version, old kept, pointer moves
        self.t.add_seed(board("pc", [words(9, 4)]), origin="scratch")
        r3 = self.t.build()
        self.assertTrue(r3["new_version"])
        self.assertNotEqual(r3["version"], r1["version"])
        self.assertEqual(sorted(D._versions(self.t.out)), sorted([r1["version"], r3["version"]]))
        self.assertEqual(D.latest_version(self.t.out), r3["version"])
        self.assertTrue(D.verify(self.t.out, r1["version"])["ok"])
        self.assertTrue(D.verify(self.t.out)["ok"])

    def test_verify_detects_tampering(self):
        v = self.t.build()["version"]
        p = self.t.out / v / "pairs.jsonl"
        os.chmod(p, 0o644)
        p.write_text(p.read_text() + "{}\n")
        res = D.verify(self.t.out)
        self.assertFalse(res["ok"])
        codes = {e["code"] for e in res["errors"]}
        self.assertTrue({"E_SHA256", "E_ROW_SCHEMA", "E_COUNT"} <= codes)
        self.assertTrue(all(set(e) == {"code", "file", "line"} for e in res["errors"]))
        assert_safe(self, json.dumps(res))

    def test_pilot_included_once(self):
        pilot = self.t.root / "pilot.json"
        _write(pilot, board("pz", [words(5, 6)]))
        v = self.t.build(pilot_path=pilot)["version"]
        seeds = read_rows(self.t.out / v, "seeds.jsonl")
        pz = [s for s in seeds if s["provenance"]["origin"] == "pilot" and s["seed_id"] != self.P]
        self.assertEqual(len(pz), 1)
        self.assertTrue(pz[0]["provenance"]["bootstrap"])

    def test_skips_are_fixed_codes(self):
        _write(self.t.runs / "forum-multi4-r1" / "run.json", {"run_id": "forum-multi4-r1", "iface": "forum",
                                                             "params": {"candidate_id": "nothex"}})
        (self.t.runs / "forum-multi4-r2").mkdir()
        reg = json.loads(self.t.registry.read_text())
        reg["records"]["abcdefabcdefabcd"] = {"record_id": "abcdefabcdefabcd"}   # no candidate file
        self.t.registry.write_text(json.dumps(reg))
        res = self.t.build()
        self.assertTrue(res["ok"])
        self.assertEqual(res["skipped"], {"E_NO_RUNJSON": 1, "E_RECORD_ID": 1, "E_SEED_MISSING": 1})
        for k in res["skipped"]:
            self.assertRegex(k, r"^E_[A-Z0-9_]+$")


class ReviewRegressionTests(_Base):
    """Regressions for the dataset review: context stratification, frozen splits, lineage through missing
    ancestors, pair margins, computed datasheet limits, corrupt-version repair, non-UTF-8 verify."""

    def _seeds(self, v, out=None):
        return {r["seed_id"]: r for r in read_rows((out or self.t.out) / v, "seeds.jsonl")}

    def test_context_includes_agent_count_and_budget(self):
        # B evaluated in the same goal/model/channel with 2 agents: a different context, never paired with A
        self.t.add_run(self.B, writes=[(1, 1020.0, 1)], n_agents=2)
        v = self.t.build()["version"]
        seeds = self._seeds(v)
        self.assertIn("unsolvable|qwen38|forum|2|40", seeds[self.B]["outcomes"]["by_context"])
        for p in read_rows(self.t.out / v, "pairs.jsonl"):
            c = p["context"]
            for sid in (p["chosen_seed_id"], p["rejected_seed_id"]):
                self.assertIn(D._ctx_of(c), seeds[sid]["outcomes"]["by_context"])
        man = json.loads((self.t.out / v / "manifest.json").read_text())
        self.assertIn("unsolvable|qwen38|forum|2|40", man["counts"]["contexts"])

    def test_pairs_need_equal_n_and_a_margin(self):
        v = self.t.build()["version"]
        got = {(p["chosen_seed_id"], p["rejected_seed_id"]) for p in read_rows(self.t.out / v, "pairs.jsonl")}
        self.assertIn((self.A, self.A1), got)
        # a second A1 episode in that context: A (n=1) and A1 (n=2) are no longer comparable
        self.t.add_run(self.A1, writes=[(2, 1031.0, 1)])
        v = self.t.build()["version"]
        pairs = read_rows(self.t.out / v, "pairs.jsonl")
        got = {(p["chosen_seed_id"], p["rejected_seed_id"]) for p in pairs}
        self.assertNotIn((self.A, self.A1), got)
        self.assertTrue(all(p["chosen"]["n"] == p["rejected"]["n"] for p in pairs))

    def test_writer_tie_needs_a_real_score_margin(self):
        def ep(rid, run, score):
            return (Path(run), {}, {"record_id": rid, "run_id": run, "goal": "unsolvable", "model": "qwen38",
                                     "channel": "forum", "n_agents": 4, "budget": 40, "n_writers": 1,
                                     "instigation_score": score, "agent_codes": [], "engagement": {}})
        seeds = D.collect_seeds(self.t.registry, self.t.cands, False)
        rows, _ = D.assemble(seeds, [ep(self.A, "ra", 0.300000001), ep(self.A1, "rb", 0.3)], {},
                             ledger=D._empty_ledger())
        self.assertEqual(rows["pairs.jsonl"], [])
        rows, _ = D.assemble(seeds, [ep(self.A, "ra", 0.5), ep(self.A1, "rb", 0.3)], {})
        self.assertEqual([(p["chosen_seed_id"], p["rejected_seed_id"]) for p in rows["pairs.jsonl"]],
                         [(self.A, self.A1)])

    def test_splits_are_frozen_across_versions(self):
        forced = {self.A: "train", self.B: "val", self.P: "test"}
        with mock.patch.object(D, "split_of", lambda key: forced.get(key, "train")):
            v1 = self.t.build()["version"]
        s1 = self._seeds(v1)
        self.assertEqual(s1[self.B]["split"], "val")
        self.assertTrue((self.t.out / D.LEDGER_NAME).is_file())
        # a new mutation of A whose text is a near-duplicate of B joins A's lineage and B's cluster
        bc = json.loads(registry.candidate_path(self.B, self.t.cands).read_text())
        bc["threads"][0]["posts"][0]["body"] = bc["threads"][0]["posts"][0]["body"] + " amet"
        C = self.t.add_seed(bc, parent=self.A, generation=1, origin="mutation")
        with mock.patch.object(D, "split_of", lambda key: "test"):   # a re-split would move everything
            r2 = self.t.build(now=1.7601e9)
        self.assertTrue(r2["new_version"])
        s2 = self._seeds(r2["version"])
        for sid, r in s1.items():
            self.assertEqual(s2[sid]["split"], r["split"], sid)     # nothing released moved
        self.assertEqual(s2[C]["split"], "train")                   # its frozen lineage root's split
        man = json.loads((self.t.out / r2["version"] / "manifest.json").read_text())
        self.assertEqual(man["split_ledger"]["moved_vs_ledger"], 0)
        self.assertGreaterEqual(man["split_ledger"]["n_conflict_groups"], 1)
        self.assertEqual(man["split_ledger"]["previous_version"], v1)
        res = D.verify(self.t.out)
        self.assertTrue(res["ok"], res["errors"])

    def test_verify_flags_a_released_seed_that_moved(self):
        v1 = self.t.build()["version"]
        (self.t.out / D.LEDGER_NAME).unlink()
        (self.t.out / "latest").unlink()
        (self.t.out / D.LATEST_FILE).unlink()   # no ledger and no pointer: nothing freezes the splits
        self.t.add_seed(board("pc", [words(9, 4)]), origin="scratch")
        orig = D.split_of
        with mock.patch.object(D, "split_of", lambda key: "test" if orig(key) != "test" else "val"):
            r2 = self.t.build(now=1.7601e9)
        res = D.verify(self.t.out, r2["version"])
        self.assertFalse(res["ok"])
        self.assertIn("E_SPLIT_MOVED", {e["code"] for e in res["errors"]})
        self.assertTrue(D.verify(self.t.out, v1)["ok"])
        assert_safe(self, json.dumps(res))

    def test_lineage_root_resolves_through_missing_ancestor(self):
        # A -> A1 -> A2: remove A1's candidate file; A2's root is still A (not A1)
        registry.candidate_path(self.A1, self.t.cands).unlink()
        res = self.t.build()
        self.assertEqual(res["skipped"].get("E_SEED_MISSING"), 1)
        seeds = self._seeds(res["version"])
        self.assertNotIn(self.A1, seeds)
        self.assertEqual(seeds[self.A2]["provenance"]["root_id"], self.A)
        self.assertEqual(seeds[self.A2]["split"], seeds[self.A]["split"])
        man = json.loads((self.t.out / res["version"] / "manifest.json").read_text())
        self.assertEqual(man["source"]["n_seeds_lineage_via_missing"], 1)
        ver = D.verify(self.t.out)
        self.assertTrue(ver["ok"], ver["errors"])

    def test_verify_accepts_a_root_without_a_seed_row(self):
        registry.candidate_path(self.A, self.t.cands).unlink()     # the root itself is missing
        res = self.t.build()
        seeds = self._seeds(res["version"])
        self.assertEqual(seeds[self.A1]["provenance"]["root_id"], self.A)
        self.assertEqual(seeds[self.A1]["split"], seeds[self.A2]["split"])
        ver = D.verify(self.t.out)
        self.assertTrue(ver["ok"], ver["errors"])

    def test_datasheet_limits_are_computed(self):
        self.t.add_run(self.A, writes=[(0, 1025.0, 1)])            # a replicated cell (n=2)
        v = self.t.build()["version"]
        man = json.loads((self.t.out / v / "manifest.json").read_text())
        rep = man["counts"]["replication"]
        self.assertEqual(rep["max_episodes_per_cell"], 2)
        self.assertEqual(rep["episodes_per_cell"]["2"], 1)
        self.assertEqual(sum(man["counts"]["labels_by_split"][s]["evaluations"] for s in D.SPLITS), 7)
        text = (self.t.out / v / "DATASHEET.md").read_text()
        assert_safe(self, text)
        self.assertIn(f"{rep['n_cells']} (seed, context) cells; {rep['single_episode_cells']}", text)
        self.assertNotIn("Most (seed, context) cells have one episode", text)
        self.assertIn("deduplicate by `near_dup_cluster`", text)
        self.assertIn("Label balance per split", text)

    def test_rebuild_repairs_a_corrupt_version_dir(self):
        r1 = self.t.build()
        m = self.t.out / r1["version"] / "manifest.json"
        os.chmod(m, 0o644)
        m.write_text("{not json")
        with self.assertRaises(D.DatasetError):
            D.verify(self.t.out)
        r2 = D.collect(out_dir=self.t.out, runs_dir=self.t.runs, registry_path=self.t.registry,
                       candidates_dir=self.t.cands, archive_paths={}, pilot_path=False, now=1.76e9,
                       git_sha="0" * 40)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["version"], r1["version"])
        self.assertTrue(D.verify(self.t.out)["ok"])
        self.assertTrue(any(p.name.startswith(".corrupt-") for p in self.t.out.iterdir()))

    def test_verify_reports_non_utf8_as_parse_error(self):
        v = self.t.build()["version"]
        p = self.t.out / v / "seeds.jsonl"
        os.chmod(p, 0o644)
        p.write_bytes(b"\xff\xfe\n")
        res = D.verify(self.t.out)
        self.assertFalse(res["ok"])
        self.assertIn({"code": "E_PARSE", "file": "seeds.jsonl", "line": 1}, res["errors"])


class CliTests(_Base):
    SAFE_VALUE = re.compile(r"^(E_[A-Z0-9_]+|[0-9a-f]{16}|[0-9a-f]{64}|\d{4}-\d{2}-\d{2}-[0-9a-f]{8}|"
                            r"[a-z]+\.jsonl|[a-zA-Z]+\.(json|md)|[0-9a-f]+\.tar\.gz|)$")

    def _walk(self, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                self.assertRegex(k, r"^[A-Za-z0-9_.]+$")
                self._walk(v)
        elif isinstance(obj, list):
            for v in obj:
                self._walk(v)
        elif isinstance(obj, str):
            self.assertRegex(obj, self.SAFE_VALUE)
        else:
            self.assertTrue(obj is None or isinstance(obj, (bool, int, float)))

    def test_build_and_verify_stdout_numbers_only(self):
        rc, out, err = self.t.cli("build", "--out", str(self.t.out), "--runs", str(self.t.runs), "--registry",
                                  str(self.t.registry), "--candidates", str(self.t.cands),
                                  "--archive", str(self.t.root / "none.json"), "--pilot", str(self.t.root / "np"))
        self.assertEqual(rc, 0)
        assert_safe(self, out + err)
        res = json.loads(out)
        self.assertTrue(res["ok"])
        self.assertEqual(len(out.strip().splitlines()), 1)
        self._walk(res)
        rc, out, err = self.t.cli("verify", "--out", str(self.t.out))
        self.assertEqual(rc, 0)
        assert_safe(self, out + err)
        self._walk(json.loads(out))
        rc, out, _ = self.t.cli("path", "--out", str(self.t.out))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(self.t.out / res["version"]))

    def test_failures_are_fixed_codes(self):
        empty = self.t.root / "empty"
        cases = [
            (["verify", "--out", str(empty)], "E_NO_PACKAGE"),
            (["path", "--out", str(empty)], "E_NO_PACKAGE"),
            (["serve", "--out", str(empty)], "E_NO_PACKAGE"),
            (["bogus"], "E_USAGE"),
            (["build", "--no-such-flag", "lorem"], "E_USAGE"),
            ([], "E_USAGE"),
        ]
        for argv, code in cases:
            rc, out, err = self.t.cli(*argv)
            self.assertNotEqual(rc, 0, argv)
            self.assertEqual(json.loads(out), {"ok": False, "error": code}, argv)
            self.assertEqual(err, "")
            assert_safe(self, out)

    def test_exceptions_never_echo_text(self):
        boom = ValueError("lorem ipsum secret payload")
        with mock.patch.object(D, "build", side_effect=boom):
            rc, out, err = self.t.cli("build", "--out", str(self.t.out))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out), {"ok": False, "error": "E_DATASET"})
        assert_safe(self, out + err)
        with mock.patch.object(D, "build", side_effect=D.DatasetError("lorem not a code")):
            rc, out, _ = self.t.cli("build", "--out", str(self.t.out))
        self.assertEqual(json.loads(out)["error"], "E_DATASET")
        with mock.patch.object(D, "build", side_effect=boom):
            self.assertEqual(D.collect(out_dir=self.t.out), {"ok": False, "error": "E_DATASET"})

    def test_serve_refuses_non_loopback(self):
        self.t.build()
        for host in ("0.0.0.0", "192.168.1.10", "::", "example.com", "10.0.0.1"):
            rc, out, err = self.t.cli("serve", "--out", str(self.t.out), "--host", host, "--port", "0")
            self.assertEqual(rc, 2, host)
            self.assertEqual(json.loads(out), {"ok": False, "error": "E_BIND"})
            with self.assertRaises(D.DatasetError):
                D.make_server(self.t.out, host=host, port=0)


class ServeTests(_Base):
    def setUp(self):
        super().setUp()
        self.version = self.t.build()["version"]
        self.viewer = self.t.root / "viewer"
        self.viewer.mkdir()
        (self.viewer / "index.html").write_text("<!doctype html><title>viewer</title>")
        (self.viewer / "viewer.js").write_text("void 0;")
        (self.viewer / "viewer.css").write_text("body{}")
        (self.t.root / "secret.txt").write_text("x")
        self.srv = D.make_server(self.t.out, host="127.0.0.1", port=0, viewer_dir=self.viewer)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, method="GET", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host} if host else {}
        c.request(method, path, headers=headers)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_serves_viewer_and_package_read_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        st, h, body = self.req("/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.req("/viewer.js")[0], 200)
        self.assertEqual(self.req("/viewer.css")[0], 200)
        for f in D.PACKAGE_FILES:
            st, h, body = self.req(f"/data/{f}")
            self.assertEqual(st, 200, f)
            self.assertEqual(body, (self.t.out / self.version / f).read_bytes())
        self.assertIn("ndjson", self.req("/data/seeds.jsonl")[1]["Content-Type"])
        self.assertEqual(self.req("/data/seeds.jsonl", "HEAD")[0], 200)
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req("/data/seeds.jsonl", m)[0], 405)

    def test_path_traversal_and_unknown_paths(self):
        for p in ("/data/../secret.txt", "/../secret.txt", "/..%2fsecret.txt", "/data/%2e%2e/secret.txt",
                  "/data/registry.json", "/data/", "/secret.txt", "/data/seeds.jsonl/x", "//etc/passwd",
                  "/.hidden", "/data/latest"):
            self.assertEqual(self.req(p)[0], 404, p)

    def test_host_header_must_be_loopback(self):
        self.assertEqual(self.req("/", host="evil.example")[0], 403)
        self.assertEqual(self.req("/", host=f"localhost:{self.port}")[0], 200)


class ExplorerHookTests(unittest.TestCase):
    def test_hook_reduces_to_numbers_and_codes(self):
        from study import export_explorer as X
        with mock.patch.object(D, "collect", return_value={"ok": True, "version": "2026-01-01-0123abcd",
                                                           "new_version": True, "counts": {}}) as m:
            self.assertEqual(X.collect_dataset(None, None, None),
                             {"ok": True, "version": "2026-01-01-0123abcd", "new_version": True})
        self.assertIsNone(m.call_args.kwargs["archive_paths"])
        with mock.patch.object(D, "collect", return_value={"ok": False, "error": "lorem text"}):
            self.assertEqual(X.collect_dataset(), {"ok": False, "error": "E_DATASET"})
        with mock.patch.object(D, "collect", side_effect=RuntimeError("lorem")):
            self.assertEqual(X.collect_dataset(), {"ok": False, "error": "E_DATASET"})


if __name__ == "__main__":
    unittest.main()
