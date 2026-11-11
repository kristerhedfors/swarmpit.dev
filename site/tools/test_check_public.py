"""Unit tests for the public gate's narration checks (NARRATION.md section 10). Stdlib unittest, no network.

Fixtures are tiny synthetic trees with neutral placeholder text and fake MPEG frames. Problem strings are
asserted on fixed codes only.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_public as C  # noqa: E402


class NarrationGate(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def run_gate(self, mutate=None, validator=C.stub_cue_validator, **kw):
        man = C.write_narration_fixture(self.root, mutate=mutate)
        warns: list[str] = []
        probs = C.check(self.root, deny=["placeholderdeny"], warnings=warns, cue_validator=validator, **kw)
        return man, probs, warns

    def codes(self, probs):
        return " | ".join(probs)

    def test_selftest(self):
        self.assertEqual(C.selftest(), 0)

    def test_good_fixture_passes(self):
        _, probs, warns = self.run_gate()
        self.assertEqual(probs, [])
        self.assertEqual(warns, [])

    def test_manifest_violations_have_fixed_codes(self):
        cases = {
            "E_MAP_MONOTONIC": lambda m: m["timeMap"].__setitem__(2, [1500, 6000]),
            "E_MAP_SLOPE": lambda m: m["timeMap"].__setitem__(1, [100, 5000]),
            "E_MAP_ENDS": lambda m: m["timeMap"].__setitem__(-1, [9000, 5000]),
            "E_CUE_OVERLAP": lambda m: m["cues"][0].__setitem__("endMs", 8500),
            "E_CUE_ANCHOR": lambda m: m["cues"][1].__setitem__("runMs", 10),
            "E_VOICE": lambda m: m["cues"][0].__setitem__("voice", "narrator"),
            "E_TPL_UNKNOWN": lambda m: m["cues"][0].__setitem__("tpl", "G.nope"),
            "E_TPL_NOMATCH": lambda m: m["cues"][0].__setitem__("text", "Agent 3 posts a secret."),
            "E_CUE_KEYS": lambda m: m["cues"][0].__setitem__("label", "x"),
            "E_KEYS": lambda m: m.__setitem__("blurb", "x"),
            "E_SCHEMA": lambda m: m.__setitem__("schema", "swarmpit.narration/0"),
            "E_AUDIO_URL": lambda m: m.__setitem__("audio", "https://example.test/x.mp3"),
            "E_SFX_COUNTS": lambda m: m["sfx"]["counts"].__setitem__("nope", 1),
            "E_TEXT_LEN": lambda m: m["cues"][0].__setitem__("text", ""),
        }
        for code, mut in cases.items():
            with self.subTest(code=code):
                self.tearDown(); self.setUp()
                _, probs, _ = self.run_gate(mut)
                self.assertTrue(any(code in p for p in probs), f"{code}: {self.codes(probs)}")
                for p in probs:   # never echo cue text
                    self.assertNotIn("secret", p)
                    self.assertNotIn("posts", p)

    def test_missing_validator_fails_closed(self):
        _, probs, _ = self.run_gate(validator=None)
        self.assertTrue(any("E_VALIDATOR_UNAVAILABLE" in p for p in probs))

    def test_validator_exception_message_is_not_echoed(self):
        def leaky(tpl, text, index=None, voice=None):
            raise ValueError("LEAKY_MESSAGE " + text)
        _, probs, _ = self.run_gate(validator=leaky)
        self.assertTrue(any("E_INVALID" in p for p in probs))
        self.assertFalse(any("LEAKY" in p for p in probs))

    def test_denylisted_word_in_cue(self):
        # passes the stub template? no; but the denylist check runs independently of template validation
        _, probs, _ = self.run_gate(lambda m: m["cues"][0].__setitem__("text", "Agent 3 posts placeholderdeny."))
        self.assertTrue(any("E_DENYLIST cue=0" in p for p in probs))

    def test_audio_bytes_and_missing_mp3(self):
        _, probs, _ = self.run_gate(lambda m: m.__setitem__("audioBytes", 1))
        self.assertTrue(any("E_AUDIO_BYTES" in p for p in probs))
        self.tearDown(); self.setUp()
        man, _, _ = self.run_gate()
        (self.root / "audio" / "narration" / man["audio"].rsplit("/", 1)[1]).unlink()
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_AUDIO_MISSING" in p for p in probs))

    def _mp3(self, data: bytes, name: str | None = None) -> Path:
        h8 = hashlib.sha256(data).hexdigest()[:8]
        p = self.root / "audio" / "narration" / (name or f"r9.{h8}.mp3")
        p.write_bytes(data)
        return p

    def test_mp3_checks(self):
        self.run_gate()
        self._mp3(b"\xff\xfb\x90\x64" + bytes(64))
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_ORPHAN" in p for p in probs))
        self.tearDown(); self.setUp(); self.run_gate()
        self._mp3(b"ID3\x04" + bytes(64))
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_TAGS" in p for p in probs))
        self.tearDown(); self.setUp(); self.run_gate()
        self._mp3(b"\x00\x00" + bytes(64))
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_SYNC" in p for p in probs))
        self.tearDown(); self.setUp(); self.run_gate()
        self._mp3(b"\xff\xfb\x90\x64" + bytes(8), name="r9.00000000.mp3")
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_HASH" in p for p in probs))
        self.tearDown(); self.setUp(); self.run_gate()
        self._mp3(b"\xff\xfb\x90\x64" + b"/Users/" + bytes(8))
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_LITERAL" in p for p in probs))
        self.tearDown(); self.setUp(); self.run_gate()
        self._mp3(b"\xff\xfb\x90\x64" + b"sk_" + b"0123456789abcdef" * 3 + bytes(8))
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_MP3_SECRET" in p for p in probs))

    def test_mp3_outside_audio_dir_and_unknown_binary(self):
        self.run_gate()
        (self.root / "data" / "x.mp3").write_bytes(b"\xff\xfb\x90\x64")
        (self.root / "data" / "y.png").write_bytes(b"\x89PNG")
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any(p.startswith("data/x.mp3: mp3 outside") for p in probs))
        self.assertTrue(any(p.startswith("data/y.png: unexpected file type") for p in probs))

    def test_audio_budgets_match_pipeline(self):
        """The gate and tools/narrate/manifest.py enforce the same narration audio limits (NARRATION.md 6)."""
        sys.path.insert(0, str(Path(C.__file__).resolve().parent))
        from narrate import manifest as NM
        self.assertEqual(C.BUDGET["mp3"], NM.MAX_MP3_BYTES)
        self.assertEqual(C.BUDGET["audio"], NM.MAX_AUDIO_TOTAL)
        self.assertEqual((C.BUDGET["mp3"], C.BUDGET["audio"]), (2 * 1024 * 1024, 120 * 1024 * 1024))

    def test_mp3_budget(self):
        self.run_gate()
        old = C.BUDGET["mp3"]
        try:
            C.BUDGET["mp3"] = 100
            probs = C.check(self.root, cue_validator=C.stub_cue_validator)
            self.assertTrue(any("mp3 " in p and "> 100" in p for p in probs))
            # enforced even with --no-budget (budgets=False only relaxes the JS/CSS limits)
            self.assertTrue(any("> 100" in p for p in C.check(self.root, cue_validator=C.stub_cue_validator, budgets=False)))
            C.BUDGET["mp3"] = old
            old_a = C.BUDGET["audio"]
            try:
                C.BUDGET["audio"] = 100
                probs = C.check(self.root, cue_validator=C.stub_cue_validator, budgets=False)
                self.assertTrue(any(p.startswith("audio/**") for p in probs))
            finally:
                C.BUDGET["audio"] = old_a
        finally:
            C.BUDGET["mp3"] = old

    def test_index_consistency(self):
        self.run_gate()
        ip = self.root / "data" / "narration" / "index.json"
        ip.write_text(json.dumps({"schema": C.NARR_INDEX_SCHEMA, "runs": {}}), encoding="utf-8")
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_NOT_INDEXED" in p for p in probs))
        ip.write_text(json.dumps({"schema": C.NARR_INDEX_SCHEMA, "runs": {"r1": {"hash": "x", "manifest": "../r1.json", "durationMs": 1}}}),
                      encoding="utf-8")
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_INDEX_ENTRY" in p for p in probs))
        ip.unlink()
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("missing narration index" in p for p in probs))

    def test_unknown_run_fails_and_stale_hash_warns(self):
        self.run_gate()
        (self.root / "data" / "index.json").write_text(json.dumps({"runs": [{"id": "r1", "hash": "fedcba98"}]}), encoding="utf-8")
        warns: list[str] = []
        probs = C.check(self.root, warnings=warns, cue_validator=C.stub_cue_validator)
        self.assertEqual(probs, [])
        self.assertTrue(any("stale narration" in w for w in warns))
        (self.root / "data" / "index.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
        probs = C.check(self.root, cue_validator=C.stub_cue_validator)
        self.assertTrue(any("E_UNKNOWN_RUN" in p for p in probs))

    def test_data_only_skips_audio(self):
        man, _, _ = self.run_gate()
        (self.root / "audio" / "narration" / man["audio"].rsplit("/", 1)[1]).unlink()
        self.assertEqual(C.check(self.root, data_only=True, cue_validator=C.stub_cue_validator), [])

    def test_real_vocab_rejects_placeholder_templates(self):
        v = C.load_cue_validator()
        if v is None:
            self.skipTest("tools/narrate/vocab.py not importable")
        _, probs, _ = self.run_gate(validator=v)
        self.assertTrue(any("E_TPL_UNKNOWN cue=0" in p for p in probs))

    def test_a2r_matches_player(self):
        tm = [[0, 0], [2000, 0], [5000, 3000], [6000, 3000], [7000, 9000], [9000, 9000]]
        self.assertEqual(C.a2r(tm, 1000), 0)
        self.assertEqual(C.a2r(tm, 3500), 1500)
        self.assertEqual(C.a2r(tm, 6500), 6000)
        self.assertEqual(C.a2r(tm, 99999), 9000)


if __name__ == "__main__":
    unittest.main()
