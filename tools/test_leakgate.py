#!/usr/bin/env python3
"""Unit tests for tools/leakgate.py and the events.py / store.py content split.

Run: python3 tools/test_leakgate.py   (or python3 -m unittest tools/test_leakgate.py)

The leak-gate tests are hermetic: the "chatter corpus" is neutral placeholder text written into a temp
dir at run time, a shingle is lifted from it programmatically and planted into a scanned file, and the
tests assert the verdict carries numbers only -- never the matched text. No real chatter is touched or
printed.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import leakgate  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# events.py / store.py have no package imports, so load them straight from the lab tree.
events_mod = _load(REPO / "lab" / "board" / "events.py", "_lab_board_events")
store_mod = _load(REPO / "lab" / "board" / "store.py", "_lab_board_store")

# A distinctive neutral body + author authored here (not chatter) so we can assert they never reach
# the structural log.
BODY = "neutralplaceholderbody alpha bravo charlie delta echo foxtrot golf hotel india"
AUTHOR = "NeutralAuthorHandle"
STRUCTURAL_KEYS = {"ts", "agent", "kind", "channel", "post_id", "thread_seq",
                   "body_len_chars", "body_len_bytes", "method", "path", "status", "bytes", "ports"}
FORBIDDEN_KEYS = {"body", "author", "thread", "title", "slug"}


class BoardEventsSplitTest(unittest.TestCase):
    def _seed(self):
        return {"board_name": "bench", "threads": [
            {"slug": "roll-call", "title": "Roll call", "posts": [
                {"author": "seeded", "body": "seedbody", "ago_min": 10}]}]}

    def test_structural_log_has_no_content(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.jsonl"
            ev = events_mod.EventLog(str(log))
            store = store_mod.Store(self._seed(), ev)
            post = store.add_post(0, "roll-call", AUTHOR, BODY, "forum")
            store.upvote(0, post, "forum")
            ev.log_event(None, "board_start", channel="forum", ports=[1])
            ev.close()

            text = log.read_text(encoding="utf-8")
            # the confirmed-leak strings must be absent from the structural log entirely
            self.assertNotIn(BODY, text)
            self.assertNotIn(AUTHOR, text)
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
            self.assertTrue(records)
            posts = [r for r in records if r["kind"] in ("post", "dm", "upvote")]
            self.assertTrue(posts)
            for r in posts:
                self.assertFalse(set(r) & FORBIDDEN_KEYS, msg="write event carries content keys")
                self.assertFalse(set(r) - STRUCTURAL_KEYS, msg="write event carries unexpected keys")
            # the post event carries the opaque structural fields and correct lengths
            pe = next(r for r in posts if r["kind"] == "post")
            self.assertEqual(pe["post_id"], post["id"])
            self.assertIsInstance(pe["thread_seq"], int)
            self.assertEqual(pe["body_len_chars"], len(BODY))
            self.assertEqual(pe["body_len_bytes"], len(BODY.encode("utf-8")))

    def test_content_lands_only_in_quarantined_sink(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.jsonl"
            ev = events_mod.EventLog(str(log))
            store = store_mod.Store(self._seed(), ev)
            post = store.add_post(0, "roll-call", AUTHOR, BODY, "forum")
            ev.close()

            sink = Path(events_mod.content_sink_path(str(log)))
            self.assertTrue(sink.name.endswith(".chatter.json"))
            self.assertTrue(sink.is_file())
            # parse structurally; assert the body is present without surfacing it
            by_id = {}
            for line in sink.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    by_id[rec["post_id"]] = rec
            self.assertIn(post["id"], by_id)
            self.assertEqual(len(by_id[post["id"]]["body"]), len(BODY))
            self.assertEqual(by_id[post["id"]]["author"], AUTHOR)

    def test_parse_load_content_sink_roundtrip(self):
        parse = _load(REPO / "dash" / "server" / "obs" / "parse.py", "_obs_parse")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.jsonl"
            ev = events_mod.EventLog(str(log))
            store = store_mod.Store(self._seed(), ev)
            post = store.add_post(0, "roll-call", AUTHOR, BODY, "forum")
            ev.close()
            idx = parse.load_content_sink(events_mod.content_sink_path(str(log)))
            self.assertIn(post["id"], idx)
            self.assertEqual(idx[post["id"]]["author"], AUTHOR)
            self.assertEqual(parse.load_content_sink(str(Path(d) / "missing.chatter.json")), {})


class LeakGateTest(unittest.TestCase):
    CORPUS_SENTENCE = ("the quick brown fox jumps over the lazy dog near the "
                       "supercalifragilistic riverbank at dawn each morning")

    def _tree(self, d: Path):
        corpus = d / "corpus"
        scan = d / "scan"
        corpus.mkdir()
        scan.mkdir()
        (corpus / "seed.chatter.json").write_text(self.CORPUS_SENTENCE, encoding="utf-8")
        return corpus, scan

    def test_planted_leak_fails_with_numbers_only(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            corpus, scan = self._tree(d)
            tokens = leakgate._normalize((corpus / "seed.chatter.json").read_text(encoding="utf-8"))
            shingle = " ".join(tokens[:leakgate.SHINGLE_N])
            (scan / "scoreboard.json").write_text(f"prefix {shingle} suffix", encoding="utf-8")

            verdict, code = leakgate.run_gate(d, [corpus / "seed.chatter.json"], [scan / "scoreboard.json"])
            self.assertEqual(code, 1)
            self.assertFalse(verdict["ok"])
            self.assertEqual(verdict["error_code"], leakgate.E_LEAK)
            self.assertGreater(verdict["leak_hits"], 0)
            self.assertEqual(len(verdict["by_file"]), 1)
            self.assertEqual(verdict["by_file"][0]["path"], "scan/scoreboard.json")
            self.assertIsInstance(verdict["by_file"][0]["first_offset"], int)
            # the verdict must never echo the matched text
            blob = json.dumps(verdict)
            for tok in tokens[:leakgate.SHINGLE_N]:
                self.assertNotIn(tok, blob)

    def test_clean_numbers_only_file_passes(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            corpus, scan = self._tree(d)
            (scan / "scoreboard.json").write_text(
                json.dumps({"record_id": "ab12", "instigation_score": 0.5, "I": 0.4, "n_posts": 7}),
                encoding="utf-8")
            verdict, code = leakgate.run_gate(d, [corpus / "seed.chatter.json"], [scan / "scoreboard.json"])
            self.assertEqual(code, 0)
            self.assertTrue(verdict["ok"])
            self.assertIsNone(verdict["error_code"])
            self.assertEqual(verdict["leak_hits"], 0)
            self.assertEqual(verdict["by_file"], [])

    def test_literal_rule_catches_host_path(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            corpus, scan = self._tree(d)
            (scan / "scoreboard.json").write_text("see /Users/someone/secret for details", encoding="utf-8")
            verdict, code = leakgate.run_gate(d, [corpus / "seed.chatter.json"], [scan / "scoreboard.json"])
            self.assertEqual(code, 1)
            self.assertEqual(verdict["error_code"], leakgate.E_LEAK)

    def test_default_mode_empty_corpus_fails_closed(self):
        # Default-glob mode over a root with no chatter sources (e.g. wrong root / off-cwd invocation)
        # must NOT pass open: a zero-shingle corpus would otherwise match nothing and green-light any
        # scanned artifact. Expect a non-ok E_CORPUS verdict, numbers only.
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            scan_dir = d / "site" / "public" / "data" / "instigation"
            scan_dir.mkdir(parents=True)
            (scan_dir / "scoreboard.json").write_text(
                json.dumps({"record_id": "ab12", "I": 0.4}), encoding="utf-8")
            verdict, code = leakgate.run_gate(d)  # corpus_paths/scan_paths default to the globs
            self.assertEqual(code, 1)
            self.assertFalse(verdict["ok"])
            self.assertEqual(verdict["error_code"], leakgate.E_CORPUS)
            self.assertEqual(verdict["corpus_shingles"], 0)
            self.assertEqual(verdict["leak_hits"], 0)

    def test_hermetic_empty_corpus_is_not_forced_closed(self):
        # Explicit (hermetic) corpus mode is the caller's responsibility: an empty explicit corpus is
        # left alone (no E_CORPUS), so a clean scan still passes. This isolates the guard to the
        # default-glob path that scoreboard.py / export.py drive with --root.
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            _, scan = self._tree(d)
            (scan / "scoreboard.json").write_text(json.dumps({"I": 0.4}), encoding="utf-8")
            verdict, code = leakgate.run_gate(d, [], [scan / "scoreboard.json"])
            self.assertEqual(code, 0)
            self.assertTrue(verdict["ok"])
            self.assertIsNone(verdict["error_code"])

    def test_cli_emits_numbers_only_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            corpus, scan = self._tree(d)
            tokens = leakgate._normalize((corpus / "seed.chatter.json").read_text(encoding="utf-8"))
            shingle = " ".join(tokens[:leakgate.SHINGLE_N])
            (scan / "scoreboard.json").write_text(shingle, encoding="utf-8")
            res = subprocess.run([sys.executable, str(TOOLS / "leakgate.py"),
                                  "--corpus", str(corpus), "--scan", str(scan)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            out = res.stdout.strip()
            verdict = json.loads(out)  # stdout is exactly one JSON object
            self.assertFalse(verdict["ok"])
            for tok in tokens[:leakgate.SHINGLE_N]:
                self.assertNotIn(tok, out)


PKG_FILES = ("seeds.jsonl", "evaluations.jsonl", "responses.jsonl", "pairs.jsonl", "sft.jsonl",
             "schema.json", "manifest.json", "DATASHEET.md")
LOREM = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
         "incididunt placeholderwordlongish ut labore et dolore magna aliqua")


def _write_placeholder_package(base: Path, version: str = "2026-01-02-0123abcd") -> Path:
    """A neutral-placeholder copy of the section 9 package layout (no real content)."""
    vdir = base / "data" / "synthetic-agent-chatter" / "dataset" / version
    vdir.mkdir(parents=True)
    row = {"seed_id": "0123456789abcdef", "content": {"board_name": "lorem", "threads": [
        {"slug": "lorem", "title": "lorem", "posts": [{"author": "lorem", "body": LOREM, "ago_min": 1}]}]},
        "text": LOREM}
    for name in PKG_FILES:
        if name.endswith(".jsonl"):
            body = json.dumps(row) + "\n"
        elif name.endswith(".json"):
            body = json.dumps({"schema": "instigation.dataset.v1", "name": "swarmpit-instigation"})
        else:
            body = "# lorem\n"
        (vdir / name).write_text(body, encoding="utf-8")
    (vdir.parent / "latest").write_text(version + "\n", encoding="utf-8")
    return vdir


class DatasetGuardTest(unittest.TestCase):
    """Section 9: the instigation dataset package is corpus for the leak gate, can never be tracked in git,
    and can never pass the public-site gate. Placeholder fixtures only."""

    def test_never_tracked_rules(self):
        for rel in ("data/synthetic-agent-chatter/dataset/2026-01-02-0123abcd/seeds.jsonl",
                    "data/synthetic-agent-chatter/dataset/latest",
                    "elsewhere/pairs.jsonl", "elsewhere/DATASHEET.md", "x/y.chatter.json",
                    "x/swarmpit-instigation-2026.tar.gz", "x/2026-01-02-0123abcd/manifest.json",
                    "x/2026-01-02-0123abcd.tar.gz", "runs/r1/steps.jsonl"):
            self.assertTrue(leakgate.never_tracked(rel), msg=rel)
        for rel in ("lab/study/dataset.py", "lab/study/dataset_viewer/viewer.js", "tools/leakgate.py",
                    "site/public/data/instigation/scoreboard.json", "dash/server/tests/fixtures/runs/a.json"):
            self.assertFalse(leakgate.never_tracked(rel), msg=rel)

    def test_package_is_in_default_corpus(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            vdir = _write_placeholder_package(d)
            found = {p.resolve() for p in leakgate._iter_paths(d, leakgate.CORPUS_GLOBS)}
            for name in PKG_FILES:
                self.assertIn((vdir / name).resolve(), found, msg=name)
            self.assertIn((vdir.parent / "latest").resolve(), found)

    def test_dataset_text_in_published_artifact_is_a_leak(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            _write_placeholder_package(d)
            pub = d / "site" / "public" / "data" / "instigation"
            pub.mkdir(parents=True)
            shingle = " ".join(leakgate._normalize(LOREM)[:leakgate.SHINGLE_N])
            (pub / "scoreboard.json").write_text(json.dumps({"x": shingle}), encoding="utf-8")
            verdict, code = leakgate.run_gate(d)  # default globs: corpus includes the package
            self.assertEqual(code, 1)
            self.assertEqual(verdict["error_code"], leakgate.E_LEAK)
            blob = json.dumps(verdict)
            self.assertNotIn("lorem", blob)
            self.assertNotIn("placeholderwordlongish", blob)

    def test_package_metadata_is_not_corpus(self):
        """Structural fields of package rows (ids, run ids, enum keys, numbers) and the script-authored
        manifest/schema also appear in numbers-only published artifacts; they must not count as leaks.
        Content fields (content/text/body/completion) still do."""
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            vdir = d / "data" / "synthetic-agent-chatter" / "dataset" / "2026-01-02-0123abcd"
            vdir.mkdir(parents=True)
            meta = {"eval_id": "forum-multi4-r1001-unsolvable-0123456789abcdef", "seed_id": "0123456789abcdef",
                    "context": {"goal": "unsolvable", "model": "qwen38", "channel": "forum", "n_agents": 4},
                    "outcome": {"n_writers": 2, "write_fraction": 0.5, "instigation_score": 0.25},
                    "label": {"instigated": True, "pile_in": True}, "split": "train",
                    "triggers": {"primary": "SAW_PEER_POSTS", "per_agent": ["SAW_PEER_POSTS", None]}}
            (vdir / "evaluations.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
            (vdir / "seeds.jsonl").write_text(json.dumps(dict(meta, content={"board_name": "lorem", "threads": [
                {"slug": "s", "title": "t", "posts": [{"author": "a", "body": LOREM, "ago_min": 1}]}]},
                text=LOREM)) + "\n", encoding="utf-8")
            (vdir / "manifest.json").write_text(json.dumps({"schema": "instigation.dataset.v1",
                                                            "counts": {"files": {"seeds.jsonl": 1}}}), encoding="utf-8")
            pub = d / "site" / "public" / "data" / "instigation"
            pub.mkdir(parents=True)
            (pub / "explorer.json").write_text(json.dumps([meta, {"schema": "instigation.dataset.v1",
                                                                 "counts": {"files": {"seeds.jsonl": 1}}}]),
                                               encoding="utf-8")
            verdict, code = leakgate.run_gate(d)
            self.assertEqual((code, verdict["error_code"], verdict["leak_hits"]), (0, None, 0))
            shingle = " ".join(leakgate._normalize(LOREM)[:leakgate.SHINGLE_N])
            (pub / "scoreboard.json").write_text(json.dumps({"x": shingle}), encoding="utf-8")
            verdict, code = leakgate.run_gate(d)
            self.assertEqual((code, verdict["error_code"]), (1, leakgate.E_LEAK))
            self.assertEqual([f["path"] for f in verdict["by_file"]], ["site/public/data/instigation/scoreboard.json"])

    def test_split_ledger_is_metadata_only_when_well_formed(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd) / "data" / "synthetic-agent-chatter" / "dataset"
            base.mkdir(parents=True)
            led = base / "split_ledger.json"
            led.write_text(json.dumps({"schema": "instigation.split_ledger.v1",
                                       "roots": {"0123456789abcdef": {"split": "train", "since": "2026-01-02-0123abcd"}},
                                       "seeds": {"0123456789abcdef": {"split": "train", "since": "2026-01-02-0123abcd"}}}),
                           encoding="utf-8")
            corpus, n = leakgate.build_corpus([led])
            self.assertEqual(n, 0)
            led.write_text(json.dumps({"schema": "instigation.split_ledger.v1", "roots": {}, "seeds": {},
                                       "x": LOREM}), encoding="utf-8")
            corpus, n = leakgate.build_corpus([led])
            self.assertGreater(n, 0)   # not the ledger shape: hashed raw (fail closed)

    def test_unparseable_package_rows_fall_back_to_raw_text(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            vdir = d / "data" / "synthetic-agent-chatter" / "dataset" / "2026-01-02-0123abcd"
            vdir.mkdir(parents=True)
            (vdir / "responses.jsonl").write_text("not json " + LOREM + "\n", encoding="utf-8")
            corpus, n = leakgate.build_corpus([vdir / "responses.jsonl"])
            self.assertGreater(n, 0)

    def _git(self, d: Path, *args):
        return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                               *args], capture_output=True, text=True)

    def test_forced_add_of_package_fails_closed(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            if self._git(d, "init", "-q").returncode != 0:
                self.skipTest("git unavailable")
            (d / ".gitignore").write_text("data/synthetic-agent-chatter/\n", encoding="utf-8")
            vdir = _write_placeholder_package(d)
            # a renamed copy outside the ignored directory, staged normally
            (d / "copy").mkdir()
            (d / "copy" / "pairs.jsonl").write_text("{}\n", encoding="utf-8")
            self._git(d, "add", ".gitignore", "copy/pairs.jsonl")
            self._git(d, "add", "-f", str(vdir / "seeds.jsonl"))
            verdict, code = leakgate.run_gate(d)
            self.assertEqual(code, 1)
            self.assertEqual(verdict["error_code"], leakgate.E_TRACKED)
            paths = {r["path"] for r in verdict["by_file"]}
            self.assertEqual(paths, {"copy/pairs.jsonl",
                                     "data/synthetic-agent-chatter/dataset/2026-01-02-0123abcd/seeds.jsonl"})
            self.assertNotIn("lorem", json.dumps(verdict))
            # once unstaged, the guard no longer fires (the default corpus then holds the package)
            self._git(d, "rm", "-q", "--cached", "copy/pairs.jsonl",
                      "data/synthetic-agent-chatter/dataset/2026-01-02-0123abcd/seeds.jsonl")
            verdict, code = leakgate.run_gate(d)
            self.assertNotEqual(verdict["error_code"], leakgate.E_TRACKED)

    def test_check_public_hard_fails_on_package_copy(self):
        cp = _load(REPO / "site" / "tools" / "check_public.py", "_site_check_public")
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            vdir = _write_placeholder_package(d / "stage")
            public = d / "public"
            (public / "data").mkdir(parents=True)
            # 1) the package directory copied verbatim, 2) every file flattened and renamed to an innocuous
            # .json/.txt name, 3) a gzip archive under an innocuous name
            import shutil
            shutil.copytree(vdir, public / "data" / vdir.name)
            for i, name in enumerate(PKG_FILES):
                ext = ".txt" if name.endswith((".jsonl", ".md")) else ".json"
                shutil.copy(vdir / name, public / "data" / f"f{i}{ext}")
            (public / "data" / "pack.svg").write_bytes(b"\x1f\x8b\x08\x00" + bytes(64))
            probs = cp.check(public, deny=["placeholderdeny"], cue_validator=cp.stub_cue_validator)
            flagged = {p.split(":")[0] for p in probs if "E_DATASET_" in p}
            for name in PKG_FILES:
                self.assertIn(f"data/{vdir.name}/{name}", flagged, msg=name)
            for i, name in enumerate(PKG_FILES):
                if name == "DATASHEET.md":
                    continue  # a renamed neutral-prose datasheet is caught by the leak gate, not by shape
                ext = ".txt" if name.endswith((".jsonl", ".md")) else ".json"
                self.assertIn(f"data/f{i}{ext}", flagged, msg=name)
            self.assertIn("data/pack.svg", flagged)
            blob = "\n".join(probs)
            self.assertNotIn("lorem", blob)
            self.assertNotIn("placeholderwordlongish", blob)
            # the gate's data-only mode (used by export.py) fails the same way
            self.assertTrue(any("E_DATASET_" in p for p in cp.check(public, data_only=True,
                                                                     cue_validator=cp.stub_cue_validator)))


if __name__ == "__main__":
    unittest.main()
