"""Offline tests for the landscape bootstrap (bootstrap.py) and registry provenance fields.

No network, no real seed: the pilot is a tmp file of neutral placeholder text ("lorem"),
and the generator model is a fake transport returning placeholder seeds. Assertions are
on the content-free contract: metas carry ids / numbers / enums only, registry rows gain
bootstrap / origin with sensible defaults, and old rows without them still load.

    cd lab && python3 -m unittest study.test_bootstrap
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from study import bootstrap, generate, modelio, registry

_PLACEHOLDER_PILOT = {
    "board_name": "placeholder-board",
    "threads": [
        {"slug": "t-a", "title": "Title A",
         "posts": [{"author": "u1", "body": "lorem", "ago_min": 30},
                   {"author": "u2", "body": "lorem ipsum", "ago_min": 3}]},
    ],
}

_META_KEYS = {"ok", "record_id", "length_chars", "n_posts", "bootstrap", "origin",
              "channel", "generation", "parent_id"}
_ERR_KEYS = {"ok", "error", "channel", "index", "origin"}


class FakeTransport:
    """Returns a distinct placeholder seed per call (so each variant gets its own record id)."""

    def __init__(self, slug="qwen38", echo=None, fail=False):
        self.slug = slug
        self.calls = 0
        self._echo = echo
        self._fail = fail

    def chat(self, messages, **kw):
        self.calls += 1
        if self._fail:
            raise modelio.ModelError("E_HTTP")
        seed = self._echo if self._echo is not None else {
            "board_name": f"placeholder-{self.calls}",
            "threads": [{"slug": "s", "title": "T",
                         "posts": [{"author": "a", "body": "lorem " * self.calls, "ago_min": 1}]}]}
        return {"choices": [{"message": {"content": json.dumps(seed)}}]}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.rp = self.td / "registry.json"
        self.cd = self.td / "candidates"
        self.pilot = self.td / "pilot.json"
        self.pilot.write_text(json.dumps(_PLACEHOLDER_PILOT))

    def tearDown(self):
        self._td.cleanup()


class RegistryProvenanceTest(_Tmp):
    def test_make_row_defaults(self):
        r = registry.make_row(_PLACEHOLDER_PILOT)
        self.assertIs(r["bootstrap"], False)
        self.assertEqual(r["origin"], "scratch")
        r = registry.make_row(_PLACEHOLDER_PILOT, parent_id="0" * 16)
        self.assertEqual(r["origin"], "mutation")
        r = registry.make_row(_PLACEHOLDER_PILOT, bootstrap=True, origin="pilot")
        self.assertEqual((r["bootstrap"], r["origin"]), (True, "pilot"))
        self.assertEqual(set(r), set(registry.ROW_FIELDS))
        with self.assertRaises(ValueError):
            registry.make_row(_PLACEHOLDER_PILOT, origin="bogus")

    def test_old_rows_load_with_defaults(self):
        old = {"record_id": "a" * 16, "parent_id": None, "generation": 0, "channel": "forum"}
        old_child = {"record_id": "b" * 16, "parent_id": "a" * 16, "generation": 1, "channel": "forum"}
        self.rp.write_text(json.dumps({"records": {old["record_id"]: old,
                                                   old_child["record_id"]: old_child}}))
        rows = registry.load(self.rp)
        self.assertEqual(rows["a" * 16]["origin"], "scratch")
        self.assertEqual(rows["b" * 16]["origin"], "mutation")
        self.assertIs(rows["a" * 16]["bootstrap"], False)
        self.assertEqual(set(rows["a" * 16]), set(registry.ROW_FIELDS))

    def test_annotate(self):
        row = registry.upsert(registry.make_row(_PLACEHOLDER_PILOT), self.rp)
        got = registry.annotate(row["record_id"], self.rp, bootstrap=True, origin="pilot")
        self.assertEqual((got["bootstrap"], got["origin"]), (True, "pilot"))
        self.assertEqual(registry.annotate("f" * 16, self.rp, origin="pilot"), {})
        with self.assertRaises(ValueError):
            registry.annotate(row["record_id"], self.rp, length_chars=1)
        with self.assertRaises(ValueError):
            registry.annotate(row["record_id"], self.rp, origin="bogus")

    def test_generate_persist_defaults(self):
        m = generate.generate_candidate(transport=FakeTransport(), candidates_dir=self.cd,
                                        registry_path=self.rp)
        row = registry.get(m["record_id"], self.rp)
        self.assertEqual((row["bootstrap"], row["origin"]), (False, "scratch"))


class NormalizePilotTest(unittest.TestCase):
    def test_structural_mapping(self):
        odd = {"board_name": "b", "extra": 1,
               "threads": [{"posts": [{"body": "lorem"}, {"author": "x", "body": "lorem", "ago_min": "z"}]},
                           {"slug": "empty", "title": "E", "posts": []}]}
        c = bootstrap.normalize_pilot(odd)
        self.assertEqual(len(c["threads"]), 1)
        t = c["threads"][0]
        self.assertEqual(t["slug"], "thread-0")
        self.assertEqual(t["title"], "thread-0")
        self.assertEqual([p["ago_min"] for p in t["posts"]], [0, 0])
        self.assertEqual(t["posts"][0]["author"], "anon")
        self.assertNotIn("extra", c)

    def test_bad_shapes(self):
        for bad in (None, [], {"threads": []}, {"board_name": "b", "threads": []},
                    {"board_name": "b", "threads": [{"posts": [{"body": 3}]}]}):
            with self.assertRaises(bootstrap.BootstrapError) as cm:
                bootstrap.normalize_pilot(bad)
            self.assertEqual(cm.exception.args[0], "E_SCHEMA")


class BootstrapPilotTest(_Tmp):
    def test_pilot_only(self):
        metas = bootstrap.bootstrap_pilot(self.pilot, ("forum", "irc"), registry_path=self.rp,
                                          candidates_dir=self.cd)
        self.assertEqual(len(metas), 2)
        self.assertEqual({m["channel"] for m in metas}, {"forum", "irc"})
        self.assertEqual(len({m["record_id"] for m in metas}), 1)
        for m in metas:
            self.assertEqual(set(m), _META_KEYS)
            self.assertEqual((m["bootstrap"], m["origin"], m["generation"]), (True, "pilot", 0))
            self.assertIsNone(m["parent_id"])
        rid = metas[0]["record_id"]
        self.assertTrue(registry.candidate_path(rid, self.cd).exists())
        row = registry.get(rid, self.rp)
        self.assertEqual((row["bootstrap"], row["origin"], row["channel"]), (True, "pilot", "forum"))
        # rerun is idempotent on the record id
        again = bootstrap.bootstrap_pilot(self.pilot, ("forum",), registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual(again[0]["record_id"], rid)
        self.assertEqual(len(registry.load(self.rp)), 1)

    def test_pilot_with_variants(self):
        tp = FakeTransport()
        metas = bootstrap.bootstrap_pilot(self.pilot, ("forum", "wiki"), 3, transport=tp,
                                          registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual(tp.calls, 3)
        self.assertEqual(len(metas), 5)
        self.assertTrue(all(m["ok"] for m in metas))
        pid = metas[0]["record_id"]
        variants = metas[2:]
        self.assertEqual([m["channel"] for m in variants], ["forum", "wiki", "forum"])
        for m in variants:
            self.assertEqual((m["bootstrap"], m["origin"], m["generation"], m["parent_id"]),
                             (True, "pilot", 0, pid))
            row = registry.get(m["record_id"], self.rp)
            self.assertEqual((row["bootstrap"], row["origin"], row["parent_id"]), (True, "pilot", pid))
        self.assertEqual(len(registry.load(self.rp)), 4)

    def test_variant_echo_is_duplicate(self):
        tp = FakeTransport(echo=_PLACEHOLDER_PILOT)
        metas = bootstrap.bootstrap_pilot(self.pilot, ("forum",), 1, transport=tp,
                                          registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual(metas[-1]["error"], "E_DUPLICATE")
        row = registry.get(metas[0]["record_id"], self.rp)
        self.assertEqual((row["origin"], row["parent_id"], row["bootstrap"]), ("pilot", None, True))

    def test_errors_are_fixed_codes(self):
        missing = bootstrap.bootstrap_pilot(self.td / "nope.json", registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual(missing, [{"ok": False, "error": "E_PILOT_READ", "channel": "", "index": 0,
                                    "origin": "pilot"}])
        bad = self.td / "bad.json"
        bad.write_text("{not json lorem")
        self.assertEqual(bootstrap.bootstrap_pilot(bad, registry_path=self.rp)[0]["error"], "E_PILOT_READ")
        self.assertEqual(bootstrap.bootstrap_pilot(self.pilot, ("nochan",), registry_path=self.rp)[0]["error"],
                         "E_CHANNEL")
        metas = bootstrap.bootstrap_pilot(self.pilot, ("forum",), 2, transport=FakeTransport(fail=True),
                                          registry_path=self.rp, candidates_dir=self.cd)
        self.assertTrue(metas[0]["ok"])
        self.assertEqual([m["error"] for m in metas[1:]], ["E_MODEL", "E_MODEL"])
        for m in metas[1:]:
            self.assertEqual(set(m), _ERR_KEYS)


class BootstrapScratchTest(_Tmp):
    def test_scratch(self):
        tp = FakeTransport()
        metas = bootstrap.bootstrap_scratch(("forum", "paste"), 2, transport=tp,
                                            registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual(tp.calls, 4)
        self.assertEqual([m["channel"] for m in metas], ["forum", "forum", "paste", "paste"])
        for m in metas:
            self.assertEqual(set(m), _META_KEYS)
            self.assertEqual((m["bootstrap"], m["origin"], m["generation"]), (False, "scratch", 0))
            row = registry.get(m["record_id"], self.rp)
            self.assertEqual((row["origin"], row["channel"]), ("scratch", m["channel"]))

    def test_scratch_errors(self):
        self.assertEqual(bootstrap.bootstrap_scratch(("x",), 1, registry_path=self.rp)[0]["error"], "E_CHANNEL")
        metas = bootstrap.bootstrap_scratch(("forum",), 2, transport=FakeTransport(fail=True),
                                            registry_path=self.rp, candidates_dir=self.cd)
        self.assertEqual([m["error"] for m in metas], ["E_MODEL", "E_MODEL"])


class CliTest(_Tmp):
    def test_pilot_cli_numbers_only(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap.main(["pilot", "--pilot", str(self.pilot), "--channels", "forum,kv",
                                 "--registry", str(self.rp), "--candidates-dir", str(self.cd)])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("lorem", out)
        self.assertNotIn("placeholder", out)
        doc = json.loads(out)
        self.assertEqual((doc["ok"], doc["n_ok"], doc["n_err"]), (True, 2, 0))
        for m in doc["metas"]:
            self.assertEqual(set(m), _META_KEYS)

    def test_cli_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap.main(["pilot", "--pilot", str(self.td / "missing.json"),
                                 "--registry", str(self.rp), "--candidates-dir", str(self.cd)])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(buf.getvalue())["metas"][0]["error"], "E_PILOT_READ")


class EnvFileTests(unittest.TestCase):
    """orchestrate.load_env: repo .env first, else the main checkout's (worktree case); values never
    leave os.environ. Uses a placeholder variable name/value in a temp dir only."""

    def test_repo_env_then_worktree_fallback(self):
        import os
        import subprocess
        from unittest import mock

        import orchestrate
        var = "QD_TEST_PLACEHOLDER_KEY"
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main"
            main.mkdir()
            git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(git + ["init", "-q"], check=True)
            (main / "f").write_text("x")
            subprocess.run(git + ["add", "f"], check=True)
            subprocess.run(git + ["commit", "-qm", "x"], check=True)
            wt = Path(td) / "wt"
            subprocess.run(git + ["worktree", "add", "-q", str(wt)], check=True)
            (main / ".env").write_text(f"{var}=lorem\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(var, None)
                with mock.patch.object(orchestrate, "REPO", wt):
                    self.assertEqual(orchestrate.env_file().resolve(), (main / ".env").resolve())
                    orchestrate.load_env(var)
                    self.assertEqual(os.environ.get(var), "lorem")
                os.environ.pop(var, None)
                (wt / ".env").write_text(f"{var}=ipsum\n")
                with mock.patch.object(orchestrate, "REPO", wt):
                    orchestrate.load_env(var)
                    self.assertEqual(os.environ.get(var), "ipsum")
                os.environ.pop(var, None)
                with mock.patch.object(orchestrate, "REPO", Path(td) / "nowhere"):
                    self.assertIsNone(orchestrate.env_file())
                    with self.assertRaises(AssertionError):
                        orchestrate.load_env(var)


if __name__ == "__main__":
    unittest.main()
