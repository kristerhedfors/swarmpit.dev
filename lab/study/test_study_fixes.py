"""Offline tests for the four validation fixes (PROTOCOL.md sections 3, 4 & 6).

No network and no real model call: the model transport is a fake and orchestrate's
VM/env entry points are stubbed. Every assertion is structural -- run ids, record
ids, flags, counts and the literal placeholder strings THIS test supplies. No board
or chatter content is read, printed, or asserted.

    cd lab && python3 -m unittest study.test_study_fixes
"""
import contextlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

import orchestrate
from study import hillclimb, modelio, registry, triggers


def _load_leakgate():
    """Import tools/leakgate.py by path (it lives at the repo root, outside the package)."""
    path = registry.REPO_DIR / "tools" / "leakgate.py"
    spec = importlib.util.spec_from_file_location("leakgate_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTransport:
    """Duck-types modelio.Transport.chat; echoes a fixed string, never the network."""

    def __init__(self, content="{\"codes\": []}"):
        self.content = content
        self.calls = 0
        self.last_messages = None
        self.last_kw = None

    def chat(self, messages, **kw):
        self.calls += 1
        self.last_messages = messages
        self.last_kw = kw
        return {"choices": [{"message": {"content": self.content}}]}


class ModelioCompleteTests(unittest.TestCase):
    """bug #3: modelio.complete() exists and returns the assistant text (the triggers model path)."""

    def test_complete_is_callable(self):
        self.assertTrue(callable(getattr(modelio, "complete", None)))

    def test_complete_returns_message_content_via_injected_transport(self):
        tp = FakeTransport(content="RESPONSE-TEXT")
        out = modelio.complete("qwen38", "SYS", "USER", transport=tp)
        self.assertEqual(out, "RESPONSE-TEXT")
        self.assertEqual(tp.calls, 1)
        roles = [m["role"] for m in tp.last_messages]
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(tp.last_messages[0]["content"], "SYS")
        self.assertEqual(tp.last_messages[1]["content"], "USER")

    def test_complete_empty_response_is_empty_string(self):
        self.assertEqual(modelio.complete("qwen38", "s", "u", transport=FakeTransport(content=None)), "")


class TriggersDefaultModelPathTests(unittest.TestCase):
    """bug #3: the NON-injected call_model path reaches modelio.complete without a missing attribute."""

    def test_default_call_model_uses_modelio_complete(self):
        tp = FakeTransport(content="DEFAULT-PATH-OK")
        orig = modelio.make_transport
        modelio.make_transport = lambda *a, **k: tp  # no network / no key needed
        try:
            out = triggers._default_call_model("SYS", "USER", "qwen38")
        finally:
            modelio.make_transport = orig
        self.assertEqual(out, "DEFAULT-PATH-OK")
        self.assertEqual(tp.calls, 1)

    def test_parse_agent_runs_through_default_path_shape(self):
        # parse_agent drives call_model and validates; use an injected call_model returning strict JSON.
        def call_model(system_prompt, user_content, model_slug):
            return '{"codes": [{"code": "UNPROMPTED", "confidence": 0.5}], "model_slug": "qwen38"}'

        rec = triggers.validate_response(call_model("", "", "qwen38"), "forum-solo-r0", 0, "qwen38")
        self.assertEqual(rec["agent_id"], 0)
        self.assertEqual([c["code"] for c in rec["codes"]], ["UNPROMPTED"])
        self.assertNotIn("error_code", rec)


class CandidateTagTests(unittest.TestCase):
    """bug #1: a candidate's record id is folded into its scoring-episode run id / run dir."""

    A = "a" * 16
    B = "b" * 16

    def _path(self, rid):
        return f"data/synthetic-agent-chatter/candidates/{rid}.chatter.json"

    def test_tag_is_distinct_per_candidate_and_empty_for_non_candidate(self):
        ta = hillclimb.candidate_tag(self._path(self.A))
        tb = hillclimb.candidate_tag(self._path(self.B))
        self.assertEqual(ta, f"-{self.A}")
        self.assertNotEqual(ta, tb)
        self.assertEqual(hillclimb.candidate_tag("lab/seeds/seed.json"), "")
        self.assertEqual(hillclimb.candidate_tag(None), "")

    def test_tag_never_contains_the_goal_marker(self):
        # the record id is hex, so the folded tag can never read as the -unsolvable goal segment.
        self.assertNotIn(orchestrate.GOAL_MARK, hillclimb.candidate_tag(self._path(self.A)).lower())

    def test_run_ids_are_distinct_and_parse_back_to_the_cell(self):
        ctx = orchestrate.default_ctx()
        ra = orchestrate.run_id_for("forum", 4, 0, hillclimb.candidate_tag(self._path(self.A)), ctx)
        rb = orchestrate.run_id_for("forum", 4, 0, hillclimb.candidate_tag(self._path(self.B)), ctx)
        self.assertNotEqual(ra, rb)
        self.assertTrue(ra.startswith("forum-multi4-r0-"))
        # the folded id leaves the goal readable (solvable) and survives vm_run_id unchanged
        import config
        self.assertEqual(config.id_goal(ra), "solvable")
        self.assertEqual(orchestrate.vm_run_id(ra), ra)


class RunEpisodeUniquenessTests(unittest.TestCase):
    """bug #1 end-to-end: run_episode derives its dir from the candidate tag and passes it to run_one."""

    def setUp(self):
        self._orig = (orchestrate.load_env, orchestrate.port_lane, orchestrate.run_one)
        self.calls = []
        orchestrate.load_env = lambda *a, **k: None

        @contextlib.contextmanager
        def fake_lane(base, n):
            yield

        orchestrate.port_lane = fake_lane
        orchestrate.run_one = lambda iface, n, rep, harness, tag="", ctx=None, **k: \
            self.calls.append((iface, n, rep, tag, ctx.candidate_id if ctx else None))

    def tearDown(self):
        orchestrate.load_env, orchestrate.port_lane, orchestrate.run_one = self._orig

    def _path(self, rid):
        return f"data/synthetic-agent-chatter/candidates/{rid}.chatter.json"

    def test_two_candidates_same_cell_get_distinct_fresh_run_dirs(self):
        a, b = "c" * 16, "d" * 16
        da = hillclimb.run_episode(self._path(a), iface="forum", n_agents=4, rep=0, budget=12)
        db = hillclimb.run_episode(self._path(b), iface="forum", n_agents=4, rep=0, budget=12)
        self.assertNotEqual(da, db)  # distinct run dirs despite the same (iface, n_agents, rep)
        self.assertTrue(da.name.endswith(a) and db.name.endswith(b))
        # run_one received the candidate tag and the ctx carried the matching candidate id
        self.assertEqual(self.calls[0][3], f"-{a}")
        self.assertEqual(self.calls[0][4], a)
        self.assertEqual(self.calls[1][3], f"-{b}")


class LeakgateOpaqueIdExemptionTests(unittest.TestCase):
    """bug #1 (leak gate): opaque hex record_id/run_id tokens are declared Claude-safe (PROTOCOL.md
    1/5/7) and must NOT be treated as leak signals, while body shingles and genuine distinctive long
    tokens must still be caught. All fixtures here are synthetic placeholders -- no chatter is read.
    """

    HEX16 = "deadbeefcafe1234"            # pure 16-hex: a record_id-shaped opaque id -> exempt
    LONGWORD = "supercalifragilistic"     # 20-char non-hex distinctive token -> still a leak feature

    def _gate(self, lg, corpus_text, scan_text):
        with tempfile.TemporaryDirectory() as td:
            c = Path(td) / "corpus.txt"
            s = Path(td) / "scan.json"
            c.write_text(corpus_text)
            s.write_text(scan_text)
            verdict, code = lg.run_gate(Path(td), corpus_paths=[c], scan_paths=[s])
            return verdict, code

    def test_hex_id_is_not_hashed_as_a_long_token(self):
        lg = _load_leakgate()
        self.assertTrue(lg._HEX_ID.match(self.HEX16))
        # feature stream of a lone hex id yields no long-token feature (shingle needs >= 8 words)
        self.assertEqual(list(lg._features([self.HEX16])), [])

    def test_shared_hex_id_does_not_count_as_a_leak(self):
        lg = _load_leakgate()
        corpus = f"the quick brown fox jumps over lazy dogs {self.HEX16} end here now"
        scan = f'{{"record_id": "{self.HEX16}", "run_id": "forum-multi4-r0-{self.HEX16}"}}'
        verdict, code = self._gate(lg, corpus, scan)
        self.assertEqual(verdict["leak_hits"], 0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(code, 0)

    def test_genuine_long_token_still_leaks(self):
        lg = _load_leakgate()
        corpus = f"alpha beta gamma delta {self.LONGWORD} epsilon zeta eta theta"
        scan = f'{{"note": "{self.LONGWORD}"}}'
        verdict, code = self._gate(lg, corpus, scan)
        self.assertGreaterEqual(verdict["leak_hits"], 1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["error_code"], lg.E_LEAK)

    def test_body_shingle_still_leaks(self):
        lg = _load_leakgate()
        corpus = "one two three four five six seven eight nine ten"
        scan = "one two three four five six seven eight"  # an 8-word shingle from the corpus
        verdict, _ = self._gate(lg, corpus, scan)
        self.assertGreaterEqual(verdict["leak_hits"], 1)
        self.assertFalse(verdict["ok"])


if __name__ == "__main__":
    unittest.main()
