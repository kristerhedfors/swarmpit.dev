#!/usr/bin/env python3
"""Tests for tools/narrate (NARRATION.md 8.1). Stdlib unittest, no network.

    python3 -m unittest tools/test_narrate.py          (from site/)
    python3 -m unittest discover -s tools -p 'test_*.py'

Real run data is only ever reduced to counts and fixed codes here; no payload text is printed or asserted on.
The mix tests need ffmpeg/ffprobe and are skipped without them.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from array import array
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
SITE = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from narrate import bundle as B  # noqa: E402
from narrate import config as C  # noqa: E402
from narrate import eleven as EL  # noqa: E402
from narrate import manifest as M  # noqa: E402
from narrate import mix as MX  # noqa: E402
from narrate import script as SC  # noqa: E402
from narrate import sfx as SX  # noqa: E402
from narrate import timemap as TM  # noqa: E402
from narrate import tts as TT  # noqa: E402
from narrate import vocab as V  # noqa: E402

CFG = C.load_config()
HAS_FF = C.has_ffmpeg()
SENT = "SENTINEL"
FAKE_KEY = "sk_fake_0123456789abcdef_NOT_A_REAL_KEY"


# ==================================================================================================
# Synthetic data builders (neutral placeholders only)
# ==================================================================================================

def mk_run(events, chapters=(), segments=None, agents=None, t_end=60000, pop="multi", n=3, caveats=(),
           model=None, iface="forum") -> B.SafeRun:
    ev = sorted(events, key=lambda e: e[0])
    m = len(ev)
    events_ = B.SafeEvents(tuple(e[0] for e in ev), (0,) * m, tuple(e[1] for e in ev), tuple(e[2] for e in ev),
                           tuple(e[3] for e in ev), (-1,) * m, (-1,) * m)
    if segments is None:
        segments = (B.SafeSegment(0, 3000, "boot"), B.SafeSegment(3000, 20000, "onTask"),
                    B.SafeSegment(20000, t_end, "free"))
    if agents is None:
        agents = tuple(B.SafeAgent(i, "done", 3000, t_end - 1000, None, None, 40, "budget", False, None, 0, None,
                                   0, 0, False) for i in range(n))
    return B.SafeRun("synth-run", "0123abcd", iface, pop, n, 0, t_end, model, tuple(caveats), events_,
                     tuple(chapters), tuple(segments), tuple(agents), 1000, (0,) * (t_end // 1000))


EK = {k: i for i, k in enumerate(B.EVENT_KINDS)}


def synth_bundle(h="0123abcd", model="SENTINEL_MODEL_1"):
    """(index dict, core dict) for a 3-agent forum run with sentinels in every free-text field."""
    F = B.EF
    ev = [(0, "boardStart", -1, 0)]
    ev += [(3000 + 50 * a, "start", a, 0) for a in range(3)]
    ev += [(t, "think", a, 0) for a in range(3) for t in range(3200, 58000, 900)]
    ev += [(t, "cmd", a, 0) for a in range(3) for t in range(5000, 19000, 2000)]
    ev += [(10000, "board", 0, F.BOARD), (10100, "read", 0, 0), (12000, "list", 1, 0), (12500, "board", 1, F.BOARD),
           (19500, "submit", 1, F.SUBMIT_OK)]
    ev += [(21000 + 1500 * i, "board" if i % 2 else "cmd", i % 3, F.FREE | (F.BOARD if i % 2 else 0))
           for i in range(6)]
    ev += [(22000, "post", 0, F.LEAK | F.FREE), (25000, "upvote", 2, F.LIVE_LIVE)]
    ev += [(30000 + 90 * i, "read", 2, 0) for i in range(10)]
    ev += [(52000, "end", 0, 0), (54000, "end", 1, 0), (56000, "end", 2, 0), (58000, "proto", -1, 0),
           (59000, "idle", 1, 0), (59500, "read", 1, F.BACKGROUND)]
    ev.sort(key=lambda e: e[0])
    idx = {(t, k): i for i, (t, k, a, f) in enumerate(ev)}
    n = len(ev)
    cols = {"t": [e[0] for e in ev], "d": [0] * n, "k": [EK[e[1]] for e in ev], "a": [e[2] for e in ev],
            "n": [-1] * n, "e": [-1] * n, "f": [e[3] for e in ev], "s": [-1] * n, "c": [-1] * n, "l": [1] * n,
            "x": [-1] * n}

    def chap(t, k, agent=None, ev_=None, p=1, merged=None):
        c = {"t": t, "k": k, "p": p, "label": f"{SENT}_LABEL_{k}", "caption": f"{SENT}_CAPTION_{k}"}
        if agent is not None:
            c["agent"] = agent
        if ev_ is not None:
            c["ev"] = ev_
        if merged is not None:
            c["merged"] = merged
        return c

    chapters = [chap(10000, "firstContact", 0, idx[(10000, "board")]), chap(12500, "contact", 1, p=2),
                chap(20000, "solve", 1, idx[(19500, "submit")]), chap(22000, "leak", 0, idx[(22000, "post")]),
                chap(22000, "post", 0, p=1), chap(25000, "onset", p=1), chap(25000, "upvote", 2, p=2),
                chap(26000, "contact", None, p=2, merged=2), chap(56000, "end", 2, p=2)]

    def agent(i, disc, sac, solve, leak, ended):
        return {"id": i, "node": 2 + i, "names": [f"{SENT}_NAME_{i}"], "model": f"{SENT}_MODEL_A{i}",
                "status": "done", "startT": 3000, "endT": 52000 + 2000 * i, "solveT": solve,
                "solveStep": 9 if solve else None, "stepsUsed": 40, "modelCalls": 40, "ended": ended,
                "tokens": {"prompt": 1, "completion": 1, "cached": 0, "reasoning": 0},
                "metrics": {"discovered": disc, "firstContact": 10000 if disc else None, "boardSteps": 3,
                            "shareAfterContact": sac, "posts": 1, "episodes": 1, "secretLeak": leak},
                "drift": {"t": [], "a": []}}

    core = {
        "format": "swarmpit.run/1", "hash": h,
        "run": {"id": "synth-run", "iface": "forum", "pop": "multi", "nAgents": 3, "rep": 0,
                "tag": f"{SENT}_TAG", "model": model,
                "harness": {"budget": 40, "temperature": 1, "maxTokens": 1, "outputChars": 1},
                "boardName": f"{SENT}_BOARD", "nouns": {k: f"{SENT}_NOUN" for k in
                                                         ("thread", "threads", "post", "posts", "board")},
                "tEndMs": 60000, "wallMs": 60000, "prehistoryMs": 0,
                "caveats": [{"code": "heuristic_edges", "text": f"{SENT}_CAVEAT"}],
                "codeVersion": f"{SENT}_CODE", "ctfMasked": False, "source": {}},
        "strings": ["", f"{SENT}_STRING_1"],
        "nodes": [{"id": "task", "k": 0, "label": f"{SENT}_LABEL_NODE", "t0": None,
                   "preview": f"{SENT}_PREVIEW"}],
        "edges": [], "events": cols, "fires": {"e": [], "t": [], "w": [], "ev": []},
        "agents": [agent(0, True, 0.5, None, True, "budget"), agent(1, True, 0.3, 20000, False, "budget"),
                   agent(2, False, None, None, False, "stalled")],
        "chapters": chapters,
        "segments": [{"t0": 0, "t1": 3000, "title": "Boot"}, {"t0": 3000, "t1": 20000, "title": "On task"},
                     {"t0": 20000, "t1": 50000, "title": "Free time"},
                     {"t0": 50000, "t1": 60000, "title": f"{SENT}_SEGMENT"}],
        "series": {"binMs": 1000, "density": [1] * 60, "boardShare": []},
    }
    index = {"format": "swarmpit.index/1", "generated": "x", "featured": "synth-run", "findings": {},
             "runs": [{"id": "synth-run", "core": f"runs/synth-run.{h}.core.json", "detail": "x", "hash": h,
                       "iface": "forum", "pop": "multi", "nAgents": 3, "rep": 0, "tag": "", "model": model,
                       "hidden": False, "caveats": ["heuristic_edges", f"{SENT}_CAVEAT_CODE"],
                       "blurb": f"{SENT}_BLURB", "durationMs": 60000}]}
    return index, core


def write_public(root: Path, index: dict, core: dict) -> Path:
    pub = root / "public"
    (pub / "data" / "runs").mkdir(parents=True)
    (pub / "data" / "index.json").write_text(json.dumps(index))
    (pub / "data" / index["runs"][0]["core"]).write_text(json.dumps(core))
    return pub


def synth_saferun(models=None, **kw) -> B.SafeRun:
    ix, core = synth_bundle(**kw)
    with tempfile.TemporaryDirectory() as td:
        pub = write_public(Path(td), ix, core)
        sidx = B.load_index(pub)
        return B.load_run(sidx, "synth-run", CFG["models"] if models is None else models, pub)


class CacheEnv(unittest.TestCase):
    """Isolate NARRATE_CACHE (and optionally the SFX library dir) per test."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._env = mock.patch.dict(os.environ, {"NARRATE_CACHE": str(self.tmp / "cache")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()


# ==================================================================================================
# vocab
# ==================================================================================================

def sample_args(tpl: str) -> dict:
    out = {}
    for name, typ in V.TEMPLATES[tpl].slots:
        if typ == "int":
            out[name] = 12
        elif typ == "count":
            out[name] = 3
        elif typ == "agent":
            out[name] = 2
        elif typ == "sec":
            out[name] = 65400
        elif typ in ("pct", "pct10"):
            out[name] = 0.4
        else:
            table = V.ENUMS[typ[5:]]
            out[name] = sorted(table)[0] if table else None
    return out


class Vocab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._models = dict(V.ENUMS["MODEL"])
        V.set_models({"model-x": "Model X"})

    @classmethod
    def tearDownClass(cls):
        V.set_models(cls._models)

    def test_every_template_renders_and_validates(self):
        for tpl in V.TEMPLATES:
            text = V.render(tpl, **sample_args(tpl))
            V.validate_cue(tpl, text)
            self.assertLessEqual(len(text), V.MAX_TEXT, tpl)

    def test_word_budget(self):
        for tpl, t in V.TEMPLATES.items():
            words = len(V.render(tpl, **sample_args(tpl)).split())
            self.assertLessEqual(words, 22 if t.voice == "guide" else 36, tpl)

    def test_slot_types(self):
        with self.assertRaises(V.VocabError) as cm:
            V.render("G.solve", agent="2", steps=3)
        self.assertEqual(cm.exception.code, "E_SLOT_TYPE")
        with self.assertRaises(V.VocabError):
            V.render("G.solve", agent=True, steps=3)
        for bad in (1.5, -0.1, True, "0.5"):
            with self.assertRaises(V.VocabError):
                V.render("G.drift", p=bad)
        with self.assertRaises(V.VocabError) as cm:
            V.render("G.solve", agent=2)
        self.assertEqual(cm.exception.code, "E_SLOT_MISSING")
        with self.assertRaises(V.VocabError) as cm:
            V.render("G.crash", agent=2, extra=1)
        self.assertEqual(cm.exception.code, "E_SLOT_EXTRA")

    def test_unknown_enum_key(self):
        with self.assertRaises(V.VocabError) as cm:
            V.render("G.post", agent=1, noun=f"{SENT}_X")
        self.assertEqual(cm.exception.code, "E_ENUM_KEY")
        self.assertNotIn(SENT, str(cm.exception))

    def test_foreign_word_fails_without_echo(self):
        text = V.render("G.solve", agent=2, steps=9)
        for bad in (text.replace("solves", "Zyzzyva"), text + " Zyzzyva", "Zyzzyva " + text,
                    text.replace("Agent 2", "Zyzzyva 2")):
            with self.assertRaises(V.VocabError) as cm:
                V.validate_cue("G.solve", bad)
            self.assertIn(cm.exception.code, ("E_TPL_NOMATCH", "E_LEXICON"))
            self.assertNotIn("Zyzzyva", str(cm.exception))
            self.assertNotIn("solves", str(cm.exception))

    def test_lexicon_check_is_independent(self):
        # a template regex hole must still be caught by the lexicon: simulate with a patched regex
        with mock.patch.dict(V._compiled, {"G.crash": re.compile(r".*")}):
            with self.assertRaises(V.VocabError) as cm:
                V.validate_cue("G.crash", "Agent 2 Zyzzyva.")
            self.assertEqual(cm.exception.code, "E_LEXICON")

    def test_unknown_template_id_not_echoed(self):
        with self.assertRaises(V.VocabError) as cm:
            V.validate_cue(f"{SENT}_TPL", "x")
        self.assertEqual(cm.exception.code, "E_TPL_UNKNOWN")
        self.assertNotIn(SENT, str(cm.exception))
        with self.assertRaises(V.VocabError):
            V.render(f"{SENT}_TPL")

    def test_voice_mismatch(self):
        text = V.render("G.crash", agent=1)
        with self.assertRaises(V.VocabError) as cm:
            V.validate_cue("G.crash", text, voice="expert")
        self.assertEqual(cm.exception.code, "E_VOICE")

    def test_fmt_sec(self):
        self.assertEqual(V.fmt_sec(1000), "1 second")
        self.assertEqual(V.fmt_sec(12400), "12 seconds")
        self.assertEqual(V.fmt_sec(12500), "13 seconds")
        self.assertEqual(V.fmt_sec(60000), "1 minute")
        self.assertEqual(V.fmt_sec(125000), "2 minutes 5 seconds")
        self.assertEqual(V.fmt_sec(61000), "1 minute 1 second")

    def test_model_table_filters_placeholders(self):
        V.set_models({"<exact-model-id>": "x", "ok-id": "Fine Name 2", "bad": "has {brace}"})
        try:
            self.assertEqual(V.ENUMS["MODEL"], {"ok-id": "Fine Name 2"})
        finally:
            V.set_models({"model-x": "Model X"})


# ==================================================================================================
# isolation: free text never reaches cues or manifests
# ==================================================================================================

class Isolation(unittest.TestCase):
    def test_sentinels_never_reach_outputs(self):
        run = synth_saferun()
        self.assertIsNone(run.modelSpoken)
        self.assertEqual(run.caveats, ("heuristic_edges",))
        self.assertIn(None, [s.title for s in run.segments])
        self.assertNotIn(SENT, repr(run))
        s = SC.build_script(run, CFG)
        m = MX.plan_only(run, CFG, s)
        self.assertEqual(M.validate(m), [])
        for blob in (json.dumps(s), json.dumps(m)):
            self.assertNotIn(SENT, blob)
        self.assertGreater(len(s["cues"]), 5)

    def test_known_model_is_spoken_by_table_name_only(self):
        V.set_models({"model-q": "Model Q"})
        try:
            run = synth_saferun(models={"model-q": "Model Q"}, model="model-q")
            self.assertEqual(run.modelSpoken, "model-q")
            s = SC.build_script(run, CFG | {"models": {"model-q": "Model Q"}})
            intro = [c for c in s["cues"] if c["tpl"].startswith("G.open")]
            self.assertEqual(len(intro), 1)
            self.assertIn("Model Q", intro[0]["text"])
            self.assertNotIn("model-q", json.dumps(s))
        finally:
            V.set_models(CFG.get("models", {}))

    def test_file_access_only_in_allowed_modules(self):
        pkg = TOOLS / "narrate"
        open_ok = {"bundle.py", "config.py", "manifest.py", "tts.py", "sfx.py"}
        json_ok = open_ok | {"eleven.py"}
        for p in sorted(pkg.glob("*.py")):
            src = p.read_text(encoding="utf-8")
            code = re.sub(r'""".*?"""', "", src, flags=re.S)
            code = re.sub(r"#[^\n]*", "", code)
            if p.name not in open_ok:
                self.assertIsNone(re.search(r"(?<![\w.])open\(", code), p.name)
            if p.name not in json_ok:
                self.assertIsNone(re.search(r"import json|(?<![\w.])json\.", code), p.name)
        for name in ("script.py", "timemap.py", "vocab.py"):
            code = re.sub(r'""".*?"""', "", (pkg / name).read_text(encoding="utf-8"), flags=re.S)
            code = re.sub(r"#[^\n]*", "", code)
            for pat in (r"\bopen\(", r"read_bytes", r"read_text", r"import json|(?<![\w.])json\.", r"\bPath\(",
                        r"subprocess"):
                self.assertIsNone(re.search(pat, code), (name, pat))

    def test_bundle_rejects_unknown_enums(self):
        ix, core = synth_bundle()
        core["run"]["iface"] = f"{SENT}_IFACE"
        with tempfile.TemporaryDirectory() as td:
            pub = write_public(Path(td), ix, core)
            with self.assertRaises(B.BundleError) as cm:
                B.load_run(B.load_index(pub), "synth-run", {}, pub)
            self.assertEqual(cm.exception.code, "E_IFACE")
            self.assertNotIn(SENT, str(cm.exception))


class RealData(unittest.TestCase):
    """Every committed run scripts cleanly. Counts and fixed codes only; nothing is printed."""

    INTRO_ORDER = ("G.open", "E.open", "G.setup", "E.ask", "G.howto", "E.caveat")

    def test_all_runs_script(self):
        if not (C.PUBLIC / "data" / "index.json").is_file():
            self.skipTest("no exported data")
        ix = B.load_index()
        self.assertGreater(len(ix.cards), 0)
        asks = 0
        for card in ix.cards:
            run = B.load_run(ix, card.id, CFG["models"])
            cands = SC.candidates(run)
            p = SC.plan(run, cands, lambda c: SC.estimate_ms(c, CFG))
            ok = True
            for i, c in enumerate(p.cues):
                try:
                    V.validate_cue(c.tpl, c.text, index=i, voice=c.voice)
                except V.VocabError:
                    ok = False
            self.assertTrue(ok, "cue validation failed")
            self.assertEqual(TM.check(p.points, p.duration_ms, run.tEnd), [])
            kept = {c.id for c in p.cues}
            self.assertTrue(all(c.id in kept for c in cands if c.prio == 0), "prio-0 cue dropped")
            self.assertLessEqual(len(p.cues), SC.MAX_CUES)
            self.assertIsNotNone(run.modelSpoken, "every model in the index has a spoken label in voices.json")
            for a, b in zip(p.placed, p.placed[1:]):
                self.assertLessEqual(a.endMs + TM.GAP_MS, b.audioMs + 1)
            self.assertEqual([c.voice for c in p.cues[-4:]], ["guide", "expert", "guide", "expert"])
            if "W_PACE_VOICE" not in p.warnings:
                self.assertLessEqual(max(j - i for i, j in SC.voice_runs(p.cues)), SC.MAX_VOICE_RUN)
            for i, c in enumerate(p.cues):
                if c.needs is not None:
                    self.assertEqual(p.cues[i + 1].id, c.needs)
            Commentary.assert_intro_and_handoffs(self, p)
            texts = [c.text for c in p.cues]
            self.assertEqual(len(texts), len(set(texts)), "no sentence twice in one broadcast")
            self.assertLessEqual(sum(1 for c in p.cues if c.tpl.split(".")[1] == "quiet"), SC.QUIET_MAX)
            asks += any(c.needs is not None for c in p.cues)
        self.assertGreater(asks, 0.8 * len(ix.cards), "most broadcasts have a real handoff")


# ==================================================================================================
# time map
# ==================================================================================================

class TimeMap(unittest.TestCase):
    def test_invariants_and_anchor(self):
        cues = [TM.CueSlot(0, 3000), TM.CueSlot(0, 2000), TM.CueSlot(9000, 2500), TM.CueSlot(20000, 1500),
                TM.CueSlot(60000, 4000)]
        pts, placed = TM.build(60000, cues, [5000, 9000, 15000, 20000, 41000])
        self.assertEqual(TM.check(pts, pts[-1][0], 60000), [])
        self.assertEqual(pts[0], (0, 0))
        self.assertEqual(pts[-1][1], 60000)
        for c, pl in zip(cues, placed):
            self.assertEqual(pl.runMs, c.anchor)
            self.assertLessEqual(abs(TM.a2r(pts, pl.audioMs) - c.anchor), 1)
            self.assertLessEqual(TM.r2a(pts, c.anchor), pl.audioMs)
        # a sparse cue starts exactly at r2a(anchor)
        self.assertAlmostEqual(TM.r2a(pts, 20000), placed[3].audioMs, delta=1)
        for a, b in zip(placed, placed[1:]):
            self.assertLessEqual(a.endMs + TM.GAP_MS, b.audioMs + 1)

    def test_hold_when_dense(self):
        pts, placed = TM.build(30000, [TM.CueSlot(10000, 4000), TM.CueSlot(10100, 4000), TM.CueSlot(10200, 4000)],
                               [10000, 10100, 10200])
        mid = [h for h in TM.holds(pts) if h[2] not in (0, 30000)]
        self.assertTrue(mid)
        self.assertEqual(TM.check(pts), [])

    def test_quiet_stretch_is_fast(self):
        pts, _ = TM.build(50000, [TM.CueSlot(50000, 1000)], [10000, 40000])
        self.assertLessEqual(TM.r2a(pts, 40000) - TM.r2a(pts, 10000), 4500)
        self.assertEqual(TM.check(pts), [])

    def test_round_trip(self):
        pts, _ = TM.build(45000, [TM.CueSlot(0, 2000), TM.CueSlot(7000, 5000), TM.CueSlot(7100, 3000),
                                  TM.CueSlot(45000, 3000)], [1000, 7000, 7100, 30000])
        for r in range(0, 45001, 37):
            self.assertLessEqual(abs(TM.a2r(pts, TM.r2a(pts, r)) - r), 1, r)
        self.assertEqual(TM.a2r(pts, -5), 0)
        self.assertEqual(TM.a2r(pts, 10 ** 9), 45000)

    def test_lead_is_a_run_hold_before_the_voice(self):
        pts, placed = TM.build(30000, [TM.CueSlot(10000, 2000, 600), TM.CueSlot(20000, 1000)], [10000, 20000])
        self.assertEqual(TM.check(pts), [])
        start = placed[0].audioMs
        self.assertEqual(placed[0].runMs, 10000)
        self.assertEqual(TM.a2r(pts, start - 600), 10000)
        self.assertEqual(TM.r2a(pts, 10000), start - 600, "the run reaches the anchor when the lead begins")
        base, bp = TM.build(30000, [TM.CueSlot(10000, 2000), TM.CueSlot(20000, 1000)], [10000, 20000])
        self.assertEqual(start - bp[0].audioMs, 600)

    def test_finalize(self):
        pts, _ = TM.build(10000, [TM.CueSlot(10000, 1000)], [])
        out = TM.finalize(pts, pts[-1][0] + 30)
        self.assertEqual(out[-1], (pts[-1][0] + 30, 10000))
        self.assertEqual(TM.check(out), [])
        with self.assertRaises(ValueError):
            TM.finalize(pts, pts[-2][0])

    def test_plan_ratio_and_priorities(self):
        run = synth_saferun()
        cands = SC.candidates(run)
        p = SC.plan(run, cands, lambda c: SC.estimate_ms(c, CFG))
        kept = {c.id for c in p.cues}
        self.assertTrue(all(c.id in kept for c in cands if c.prio == 0))
        if "W_PACE_RATIO" not in p.warnings:
            self.assertLessEqual(SC.paced_ms(p.points, run.tEnd, p.intro_end), SC.MAX_RATIO * run.tEnd)
        else:   # only allowed when nothing droppable is left
            self.assertTrue(all(c.prio == 0 or c.closing for c in p.cues))
        self.assertEqual(TM.check(p.points, p.duration_ms, run.tEnd), [])
        # expert cues whose guide cue was dropped are dropped too
        for c in p.cues:
            if c.pair is not None and not c.closing:
                self.assertIn(c.pair, kept)

    def test_expert_slides_into_room_instead_of_holding(self):
        mk = lambda i, anchor, voice, pair=None: SC.Cue(i, anchor, 5, i, voice, "T", "x", 1, pair)
        cues = [mk(0, 10000, "guide"), mk(1, 10000, "expert", 0), mk(2, 30000, "guide")]
        dur = lambda c: 3000
        out = SC._slide_experts(cues, dur)
        self.assertEqual([c.id for c in out], [0, 1, 2])
        self.assertEqual(out[1].anchor, 10000 + 3000 + SC.GAP_MS, "spoken right after its guide line, run moving")
        # no room before the next cue: stays put (pacing loop handles it)
        tight = [mk(0, 10000, "guide"), mk(1, 10000, "expert", 0), mk(2, 14000, "guide")]
        self.assertEqual(SC._slide_experts(tight, dur)[1].anchor, 10000)
        # never further than SLIDE_MS
        far = [mk(0, 10000, "guide"), mk(1, 10000, "expert", 0)]
        self.assertEqual(SC._slide_experts(far, lambda c: SC.SLIDE_MS + 1000)[1].anchor, 10000)
        pts, _ = TM.build(40000, [TM.CueSlot(c.anchor, 3000) for c in out], [10000, 30000])
        self.assertFalse([h for h in TM.holds(pts) if h[2] not in (0, 40000)], "slid layout needs no mid-run hold")

    def test_plan_drops_low_priority_under_pressure(self):
        # 40 prio-2 chapters, densely packed: spacing + rate limits must thin them
        chapters = [B.SafeChapter(5000 + 700 * i, "post", i % 3, None, 1, 1) for i in range(40)]
        run = mk_run([(5000 + 700 * i, "post", i % 3, 0) for i in range(40)], chapters=chapters)
        p = SC.plan(run, SC.candidates(run), lambda c: SC.estimate_ms(c, CFG))
        posts = [c for c in p.cues if c.tpl in V.variants("G.post")]
        self.assertLess(len(posts), 15)
        for a, b in zip(posts, posts[1:]):
            self.assertGreaterEqual(b.anchor - a.anchor, SC.MIN_ANCHOR_SPACING_MS)


# ==================================================================================================
# commentary: two-voice broadcast structure (NARRATION.md 3.2-3.4)
# ==================================================================================================

TPL_ID = re.compile(r"[GE](\.[A-Za-z][A-Za-z0-9_]{0,31}){1,3}")   # the player's check (timemap.ts)


def edge_args(tpl: str) -> list[dict]:
    """Every slot at its extremes (one dict per extreme), other slots at sample values."""
    out = []
    edges = {"int": (0, 1, 1_000_000), "count": (0, 1, 2, 999), "agent": (0, 999), "sec": (0, 999, 60000, 9_999_000),
             "pct": (0, 0.004, 0.5, 1), "pct10": (0, 0.001, 0.04, 0.05, 0.149, 0.15, 0.5, 0.949, 0.95, 0.999, 1)}
    for name, typ in V.TEMPLATES[tpl].slots:
        vals = sorted(V.ENUMS[typ[5:]]) if typ.startswith("enum:") else edges[typ]
        for v in vals:
            out.append(dict(sample_args(tpl), **{name: v}))
    return out or [{}]


def agent(i, solve=None, step=None, disc=False, sac=None, leak=False, ended="budget", end=None):
    return B.SafeAgent(i, "done", 1000, end, solve, step, 30, ended, disc, 9000 if disc else None, 3 if disc else 0,
                       sac, 0, 0, leak)


def scenario(kind: str, model=None, caveats=(), t_end=120000) -> B.SafeRun:
    """Neutral synthetic runs covering the shapes in the index: solo/multi, solved/unsolved/unsolvable, clusters."""
    F = B.EF
    solo = kind.startswith("solo")
    n = 1 if solo else 4
    ev = [(0, "boardStart", -1, 0)] + [(1000 + 10 * a, "start", a, 0) for a in range(n)]
    ev += [(t, "cmd", a, 0) for a in range(n) for t in range(2000, t_end - 2000, 3000)]
    chapters, agents = [], []
    segs = (B.SafeSegment(0, 1000, "boot"), B.SafeSegment(1000, t_end, "onTask"))
    if kind in ("solo", "multi"):
        fs = t_end // 4
        segs = (B.SafeSegment(0, 1000, "boot"), B.SafeSegment(1000, fs, "onTask"), B.SafeSegment(fs, t_end, "free"))
        ev += [(t, "board", a, F.BOARD | F.FREE) for a in range(n) for t in range(fs + 500, t_end - 2000, 2500)]
        chapters.append(B.SafeChapter(9000, "firstContact", 0, None, 1, 1))
        if not solo:
            chapters.append(B.SafeChapter(14000, "contact", None, None, 2, 3))
        for a in range(n):
            chapters.append(B.SafeChapter(fs + 2000 * a, "solve", a, None, 1, 1))
        chapters += [B.SafeChapter(fs + 9000, "leak", 0, None, 1, 1), B.SafeChapter(fs + 9000, "post", 0, None, 1, 1)]
        agents = [agent(a, fs + 2000 * a, 7 + a, True, 0.5, a == 0, end=t_end - 1500 + 100 * a) for a in range(n)]
        ev += [(t_end - 1500 + 100 * a, "end", a, 0) for a in range(n)]
    elif kind in ("soloStuck", "multiStuck", "soloNever"):
        agents = [agent(a, end=t_end - 500) for a in range(n)]
    elif kind == "cluster":           # solo: contact, solve, leak and free time within a few seconds near the end
        segs = (B.SafeSegment(0, 1000, "boot"), B.SafeSegment(1000, 9000, "onTask"),
                B.SafeSegment(9000, t_end, "free"))
        chapters = [B.SafeChapter(8000, "firstContact", 0, None, 1, 1), B.SafeChapter(9000, "solve", 0, None, 1, 1),
                    B.SafeChapter(10500, "leak", 0, None, 1, 1)]
        agents = [agent(0, 9000, 5, True, 0.2, True, end=t_end - 300)]
    return mk_run(ev, chapters=chapters, segments=segs, agents=tuple(agents), t_end=t_end,
                  pop="solo" if solo else "multi", n=n, caveats=caveats, model=model)


SCENARIOS = [("solo", 120000), ("multi", 300000), ("soloStuck", 90000), ("multiStuck", 200000),
             ("soloNever", 60000), ("cluster", 13000), ("cluster", 16000), ("solo", 13000), ("multi", 28000)]


def planned(run):
    return SC.plan(run, SC.candidates(run), lambda c: SC.estimate_ms(c, CFG),
                   leads=SX.key_leads(run, CFG["sfx"]["library"]))


class Commentary(unittest.TestCase):
    def assert_broadcast(self, run, p):
        """Structural invariants of a planned script (no text inspected beyond template ids)."""
        cues = p.cues
        self.assertEqual(TM.check(p.points, p.duration_ms, run.tEnd), [])
        for i, c in enumerate(cues):
            V.validate_cue(c.tpl, c.text, index=i, voice=c.voice)
            self.assertEqual(c.voice, "guide" if c.tpl[0] == "G" else "expert")
        # voices never overlap: serial, with a gap
        for a, b in zip(p.placed, p.placed[1:]):
            self.assertLessEqual(a.endMs + TM.GAP_MS, b.audioMs + 1)
        # natural alternation: no long walls of one voice
        if "W_PACE_VOICE" not in p.warnings:
            self.assertLessEqual(max(j - i for i, j in SC.voice_runs(cues)), SC.MAX_VOICE_RUN)
        # intro duet: guide opens, color answers
        self.assertTrue(cues[0].tpl in V.variants("G.open.multi") + V.variants("G.open.multiM")
                        + V.variants("G.open.solo") + V.variants("G.open.soloM"))
        self.assertEqual(cues[1].tpl.split(".")[:2], ["E", "open"])
        # outro wrap-up: guide tally, color verdict, guide sign-off, color last word, all at tEnd
        tail = cues[-4:]
        self.assertEqual([c.voice for c in tail], ["guide", "expert", "guide", "expert"])
        self.assertTrue(tail[0].tpl.startswith("G.tally.") and tail[1].tpl.startswith("E.close."))
        self.assertTrue(all(c.closing and c.anchor == run.tEnd for c in tail))
        # a handoff question is always answered right away by the color voice
        ids = [c.id for c in cues]
        for i, c in enumerate(cues):
            if c.needs is not None:
                self.assertTrue(c.tpl in V.variants("G.ask"))
                self.assertEqual(cues[i + 1].id, c.needs)
                self.assertEqual(cues[i + 1].voice, "expert")
            if c.pair is not None and not c.closing:
                self.assertIn(c.pair, ids)
        self.assertLessEqual(len(cues), SC.MAX_CUES)

    def test_every_template_renders_at_slot_edges(self):
        V.set_models({"model-x": "Model X 1.5 9B", "other": "other test"})
        try:
            for tpl, t in V.TEMPLATES.items():
                self.assertTrue(TPL_ID.fullmatch(tpl), tpl)
                for args in edge_args(tpl):
                    text = V.render(tpl, **args)
                    V.validate_cue(tpl, text, voice=t.voice)
                    self.assertLessEqual(len(text.split()), 22 if t.voice == "guide" else 36, tpl)
        finally:
            V.set_models(CFG.get("models", {}))

    def test_pct10_is_never_more_precise_than_claimed(self):
        cases = {0: "0 percent", 0.001: "under 10 percent", 0.05: "about 10 percent", 0.149: "about 10 percent",
                 0.15: "about 20 percent", 0.42: "about 40 percent", 0.949: "about 90 percent",
                 0.95: "over 90 percent", 0.999: "over 90 percent", 1: "100 percent"}
        for v, want in cases.items():
            self.assertEqual(V.fmt_pct10(v), want, v)
        for k in range(0, 1001):        # 'about N0' is always within 5 points of the truth
            text = V.fmt_pct10(k / 1000)
            if text.startswith("about"):
                self.assertLessEqual(abs(int(text.split()[1]) - k / 10), 5.0 + 1e-9)

    def test_variant_families_are_consistent(self):
        for base, ids in V._FAMILIES.items():
            self.assertEqual(V.variants(base)[0], base)
            self.assertGreater(len(ids), 1)
            self.assertEqual({V.TEMPLATES[i].voice for i in ids}, {V.TEMPLATES[base].voice})
        self.assertEqual(V.variants("G.crash"), ("G.crash",))
        with self.assertRaises(V.VocabError):
            V.variants(f"{SENT}_BASE")

    def test_rotation_is_deterministic_and_varies_by_run(self):
        run = scenario("multi")
        a = [c.tpl for c in SC.candidates(run)]
        self.assertEqual(a, [c.tpl for c in SC.candidates(run)])
        seen = {tuple(c.tpl for c in SC.candidates(B.SafeRun(**{**run.__dict__, "runId": f"r{i}"})))
                for i in range(12)}
        self.assertGreater(len(seen), 3)

    def test_every_scenario_is_a_valid_broadcast(self):
        for kind, t_end in SCENARIOS:
            for model in (None, sorted(CFG["models"])[0]):
                for cav in ((), ("heuristic_edges",), ("unsolvable",)):
                    with self.subTest(kind=kind, t_end=t_end, model=model, cav=cav):
                        run = scenario(kind, model=model, caveats=cav, t_end=t_end)
                        cands = SC.candidates(run)
                        p = planned(run)
                        self.assert_broadcast(run, p)
                        kept = {c.id for c in p.cues}
                        self.assertTrue(all(c.id in kept for c in cands if c.prio == 0), "prio-0 cue dropped")

    def test_unknown_model_is_never_spoken_and_never_fails(self):
        run = scenario("multi", model=f"{SENT}_MODEL")
        p = planned(run)
        self.assert_broadcast(run, p)
        self.assertIn(p.cues[0].tpl, V.variants("G.open.multi"))
        self.assertNotIn(SENT, json.dumps([c.text for c in p.cues]))

    def test_unsolvable_goal_never_claims_or_expects_a_solve(self):
        for kind in ("soloStuck", "multiStuck"):
            run = scenario(kind, caveats=("unsolvable",))
            self.assertTrue(SC.is_unsolvable(run))
            tpls = [c.tpl for c in SC.candidates(run)]
            self.assertIn("G.setup.stuckSolo" if kind == "soloStuck" else "G.setup.stuck", tpls)
            self.assertIn("G.tally.stuckSolo" if kind == "soloStuck" else "G.tally.stuck", tpls)
            self.assertTrue(any(t in V.variants("G.stuck") for t in tpls))
            self.assertFalse([t for t in tpls if t.startswith(("G.solve", "E.firstSolve", "G.noSolve", "E.free"))])
        # the same shape without the goal flag is a solvable run nobody solved
        tpls = [c.tpl for c in SC.candidates(scenario("soloStuck"))]
        self.assertIn("G.setup.solo", tpls)
        self.assertIn("G.noSolve.solo", tpls)

    def test_tally_counts_match_the_run(self):
        run = scenario("multi")
        tally = [c for c in SC.candidates(run) if c.tpl.startswith("G.tally")][0]
        self.assertEqual(tally.text, V.render("G.tally.multi", n=4, d=4, noun="forum", s=4, l=1))
        solo = [c for c in SC.candidates(scenario("soloNever")) if c.tpl.startswith("G.tally")][0]
        self.assertEqual(solo.text, V.render("G.tally.solo", f="no", s="no", l="no"))

    def test_cluster_gets_one_color_answer_after_the_calls(self):
        run = scenario("cluster", t_end=40000)
        cands = SC.candidates(run)
        answers = [c for c in cands if c.tpl.split(".")[1] in ("firstContact", "firstSolve", "leak")
                   and c.voice == "expert"]
        self.assertEqual(len(answers), 1)
        self.assertIn(answers[0].tpl, V.variants("E.leak"))    # the most important beat wins
        self.assertEqual(answers[0].prio, 0)
        calls = [c for c in cands if c.tpl.split(".")[1] in ("firstContact", "solve", "leak") and c.voice == "guide"]
        self.assertTrue(all(c.key < answers[0].key for c in calls))

    def test_intro_talks_over_the_run_instead_of_freezing_it(self):
        run = scenario("soloNever", t_end=120000)     # first beat well after the intro
        p = planned(run)
        intro = [c for c in p.cues if c.row <= SC.INTRO_ROWS]
        self.assertGreaterEqual(len(intro), 4)
        self.assertGreater(max(c.anchor for c in intro), 0, "later intro lines move onto the running clock")
        self.assertEqual([c.anchor for c in intro], sorted(c.anchor for c in intro))
        self.assertEqual([c.row for c in intro], sorted(c.row for c in intro), "play order unchanged")
        held0 = sum(a1 - a0 for a0, a1, r in TM.holds(p.points) if r == 0)
        self.assertLess(held0, sum(e - s for s, e in ((pl.audioMs, pl.endMs) for c, pl in zip(p.cues, p.placed)
                                                       if c.row <= SC.INTRO_ROWS)))

    def test_very_short_run(self):
        run = scenario("cluster", t_end=13000)
        p = planned(run)
        self.assert_broadcast(run, p)
        self.assertLessEqual(p.duration_ms, 120000)

    def test_drop_orphans_both_directions(self):
        mk = lambda i, voice, pair=None, needs=None: SC.Cue(i, 1000, 5, i, voice, "T", "x", 2, pair, False, needs)
        cues = [mk(0, "guide"), mk(1, "guide", 0, needs=2), mk(2, "expert", 0)]
        self.assertEqual([c.id for c in SC._drop_orphans(cues)], [0, 1, 2])
        self.assertEqual([c.id for c in SC._drop_orphans([cues[0], cues[1]])], [0], "unanswered question goes")
        self.assertEqual([c.id for c in SC._drop_orphans(cues[1:])], [], "follow-ups of a dropped call go")

    # ---- review findings: the mix-time re-plan, handoffs, phases, merged solves, repetition ----------------

    INTRO_ORDER = ("G.open", "E.open", "G.setup", "E.ask", "G.howto", "E.caveat")

    def assert_intro_and_handoffs(self, p):
        fams = [".".join(c.tpl.split(".")[:2]) for c in p.cues if c.row <= SC.INTRO_ROWS and not c.closing]
        self.assertEqual(fams, [f for f in self.INTRO_ORDER if f in fams], "intro duet keeps its order")
        self.assertEqual(fams[:2], ["G.open", "E.open"])
        for a, b in zip(p.cues, p.cues[1:]):
            if a.row <= SC.INTRO_ROWS and b.row <= SC.INTRO_ROWS:
                self.assertFalse(a.voice == b.voice == "expert", "no two color lines in a row in the intro")
        for i, c in enumerate(p.cues):
            if c.needs is not None:
                gap = p.placed[i + 1].audioMs - p.placed[i].endMs
                self.assertLessEqual(gap, TM.GAP_MS + 5, "a handoff question is answered without dead air")

    def mix_time_plan(self, run, skew):
        """What mix.plan_audio does: the cached script, re-planned with measured (here: skewed) durations."""
        s = SC.build_script(run, CFG)
        cues = SC.cues_from_script(s)
        return SC.plan(run, cues, lambda c: int(SC.estimate_ms(c, CFG) * skew(c)),
                       leads=SX.key_leads(run, CFG["sfx"]["library"]))

    def test_mix_time_replan_keeps_intro_order_and_answers_handoffs_at_once(self):
        skews = (lambda c: 1.0, lambda c: 1.3 if c.tpl.startswith("G.open") else 0.9,
                 lambda c: 1.25 if c.voice == "guide" else 0.8, lambda c: 0.75)
        for kind, t_end in SCENARIOS:
            for cav in ((), ("heuristic_edges",)):
                for k, skew in enumerate(skews):
                    with self.subTest(kind=kind, t_end=t_end, cav=cav, skew=k):
                        run = scenario(kind, caveats=cav, t_end=t_end)
                        p = self.mix_time_plan(run, skew)
                        self.assert_broadcast(run, p)
                        self.assert_intro_and_handoffs(p)

    def test_script_stores_candidate_anchors_so_the_mix_replans_the_intro(self):
        run = scenario("soloNever", t_end=120000)
        s = SC.build_script(run, CFG)
        intro = [c for c in s["cues"] if c["row"] <= SC.INTRO_ROWS]
        self.assertTrue(all(c["anchor"] == 0 for c in intro))
        p = self.mix_time_plan(run, lambda c: 1.3 if c.tpl.startswith("G.open") else 1.0)
        self.assertGreater(max(c.anchor for c in p.cues if c.row <= SC.INTRO_ROWS), 0, "relaid with real durations")

    def test_slide_never_jumps_a_cue_at_the_same_anchor(self):
        mk = lambda i, anchor, voice, row, sub=0: SC.Cue(i, anchor, row, sub, voice, "T", "x", 1, None)
        # the intro shape from the review: an expert reply that the guide's long line pushes past its anchor
        cues = [mk(0, 0, "guide", 1), mk(1, 6000, "expert", 1, 1), mk(2, 6000, "guide", 2), mk(3, 6000, "expert", 2, 1)]
        out = SC._slide_experts(cues, lambda c: 7000 if c.id == 0 else 2000)
        self.assertEqual([c.id for c in out], [0, 1, 2, 3])

    def test_handoff_answer_follows_the_question_when_the_call_is_held(self):
        mk = lambda i, voice, sub, needs=None: SC.Cue(i, 10000, 5, sub, voice, "T", "x", 1, None, False, needs)
        cues = [mk(0, "guide", 0), mk(1, "guide", 1, needs=2), mk(2, "expert", 2)]
        dur = {0: 3000, 1: 1500, 2: 3000}
        out = SC._slide_experts(cues, lambda c: dur[c.id])
        self.assertEqual(out[2].anchor, 10000 + 1500 + SC.GAP_MS, "the run waits through the call, not twice")
        pts, placed = TM.build(60000, [TM.CueSlot(c.anchor, dur[c.id]) for c in out], [10000, 40000])
        self.assertEqual(placed[2].audioMs - placed[1].endMs, TM.GAP_MS)

    def test_multi_free_time_never_calls_the_task_phase_over_while_agents_work(self):
        run = scenario("multi")          # agents solve 2 s apart: the free segment starts with the first
        tpls = [c.tpl for c in SC.candidates(run)]
        self.assertFalse([t for t in tpls if t in V.variants("G.seg.free") or t in V.variants("E.free")])
        self.assertTrue([t for t in tpls if t in V.variants("G.seg.freeFirst")])
        solo = [c.tpl for c in SC.candidates(scenario("solo"))]
        self.assertTrue([t for t in solo if t in V.variants("G.seg.free")])

    def test_merged_solve_chapter_counts_every_solver(self):
        F = B.EF
        t_end, fs = 120000, 30000
        agents = tuple(agent(a, fs + 400 * a if a < 3 else fs + 20000, 7, True, 0.5, end=t_end - 500) for a in range(4))
        chapters = (B.SafeChapter(fs, "solve", 0, None, 1, 3), B.SafeChapter(fs + 20000, "solve", 3, None, 1, 1))
        segs = (B.SafeSegment(0, 1000, "boot"), B.SafeSegment(1000, fs, "onTask"), B.SafeSegment(fs, t_end, "free"))
        ev = [(t, "cmd", a, 0) for a in range(4) for t in range(2000, t_end - 2000, 3000)]
        ev += [(t, "board", a, F.BOARD | F.FREE) for a in range(4) for t in range(fs + 500, t_end - 2000, 2500)]
        run = mk_run(ev, chapters=chapters, segments=segs, agents=agents, t_end=t_end, n=4)
        cands = SC.candidates(run)
        merged = [c for c in cands if c.tpl in V.variants("G.solveN")]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, V.render(merged[0].tpl, n=3))
        self.assertTrue([c for c in cands if c.tpl in V.variants("E.firstSolveN")])
        self.assertTrue([c for c in cands if c.tpl == "E.solveAll"], "the fourth solve completes the set")

    def test_no_sentence_twice_and_quiet_lines_capped(self):
        for kind, t_end in SCENARIOS + [("soloStuck", 600000), ("multiStuck", 900000)]:
            with self.subTest(kind=kind, t_end=t_end):
                cands = SC.candidates(scenario(kind, t_end=t_end, caveats=("unsolvable",) if "Stuck" in kind else ()))
                texts = [c.text for c in cands]
                self.assertEqual(len(texts), len(set(texts)))
                quiet = [c for c in cands if c.tpl.split(".")[1] == "quiet"]
                self.assertLessEqual(len(quiet), SC.QUIET_MAX)

    def test_every_run_with_a_beat_gets_a_handoff(self):
        for kind, t_end in SCENARIOS:
            with self.subTest(kind=kind, t_end=t_end):
                run = scenario(kind, t_end=t_end)
                cands = SC.candidates(run)
                if any(c.voice == "expert" and c.row > SC.INTRO_ROWS and not c.closing for c in cands):
                    self.assertTrue([c for c in cands if c.needs is not None], "the first exchange opens with a question")

    def test_script_round_trip_keeps_handoffs(self):
        run = scenario("multi")
        s = SC.build_script(run, CFG)
        back = SC.cues_from_script(s)
        self.assertEqual([(c.id, c.needs, c.pair) for c in back], [(c["id"], c["needs"], c["pair"]) for c in s["cues"]])


# ==================================================================================================
# sfx
# ==================================================================================================

LIB = CFG["sfx"]["library"]


def run_sfx(run, spans=(), pts=None):
    pts = pts or [(0, 0), (run.tEnd, run.tEnd)]
    return SX.place(run, pts, list(spans), LIB)


class Sfx(unittest.TestCase):
    def test_burst_is_thinned(self):
        run = mk_run([(10000 + 20 * i, "read", i % 3, 0) for i in range(50)], segments=())
        pl, counts, dropped = run_sfx(run)
        reads = [p for p in pl if p.id == "read"]
        self.assertLessEqual(len(reads), 3)
        for a, b in zip(reads, reads[1:]):
            self.assertGreaterEqual(b.audioMs - a.audioMs, SX.SPEC["read"][0])
        self.assertEqual(counts.get("read"), len(reads))
        self.assertGreaterEqual(dropped, 47)

    def test_global_density(self):
        kinds = ["read", "post", "upvote", "start", "join", "end", "crash"]
        run = mk_run([(10000 + 40 * i, kinds[i % len(kinds)], i % 3, 0) for i in range(30)], segments=())
        pl, _, _ = run_sfx(run)
        times = sorted(p.audioMs for p in pl if p.id != "outro")
        for t in times:
            self.assertLessEqual(sum(1 for u in times if t <= u < t + 1000), SX.GLOBAL_MAX_IN_WINDOW)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, SX.GLOBAL_MIN_GAP_MS)

    def test_read_suppressed_inside_cues(self):
        run = mk_run([(10000, "read", 0, 0), (20000, "read", 1, 0), (10100, "start", 2, 0)], segments=())
        pl, _, _ = run_sfx(run, spans=[(9500, 12000)])
        ids = [(p.audioMs, p.id) for p in pl]
        self.assertNotIn((10000, "read"), ids)
        self.assertIn((20000, "read"), ids)
        self.assertIn((10100, "boot"), ids)

    def test_chapter_consumes_event(self):
        run = mk_run([(5000, "post", 0, B.EF.LEAK), (9000, "post", 1, 0)], segments=(),
                     chapters=[B.SafeChapter(5000, "leak", 0, 0, 1, 1)])
        pl, counts, _ = run_sfx(run)
        self.assertEqual(counts.get("leak"), 1)
        self.assertEqual([p.audioMs for p in pl if p.id == "post"], [9000])

    def test_hidden_kinds_silent(self):
        run = mk_run([(1000 + 100 * i, k, 0, 0) for i, k in enumerate(
            ["think", "idle", "proto", "connect", "disconnect", "cmd", "board", "retry", "boardStart", "miss",
             "submit"])] + [(5000, "read", 0, B.EF.BACKGROUND)], segments=())
        pl, counts, _ = run_sfx(run)
        self.assertEqual(counts, {"outro": 1})

    def test_submit_ok_solve_once_per_agent(self):
        run = mk_run([(5000, "submit", 0, B.EF.SUBMIT_OK), (9000, "submit", 0, B.EF.SUBMIT_OK)], segments=())
        _, counts, _ = run_sfx(run)
        self.assertEqual(counts.get("solve"), 1)

    def test_deterministic(self):
        run = synth_saferun()
        s = SC.build_script(run, CFG)
        a = MX.plan_only(run, CFG, s)
        b = MX.plan_only(run, CFG, s)
        self.assertEqual(a, b)
        cs = [(int(c.runMs), c) for c in SX.candidates(run)]
        x = SX.thin(cs, [(1000, 5000)], LIB)
        y = SX.thin(list(reversed(cs)), [(1000, 5000)], LIB)
        self.assertEqual(x, y)

    def test_key_effects_lead_the_voice(self):
        """prio-1 effects announced by a cue start in a lead-in before the voice, not under it."""
        for run in (synth_saferun(), mk_run([(4000, "start", 0, 0), (12000, "post", 1, B.EF.LEAK)],
                                             chapters=[B.SafeChapter(12000, "leak", 1, 1, 1, 1)])):
            cues = SC.candidates(run)
            ap = MX.plan_audio(run, CFG, cues, lambda c: SC.estimate_ms(c, CFG))
            starts = [pl.audioMs for pl in ap.plan.placed]
            prio1 = [pl for pl in ap.placements if SX.SPEC[pl.id][1] == 1 and pl.id != "outro"]
            self.assertTrue(prio1)
            for pl in prio1:
                for s0 in starts:
                    self.assertGreaterEqual(abs(pl.audioMs - s0), SX.SPEECH_PRE_MS, pl.id)
            led = [ld for ld in ap.plan.leads if ld > 0]
            self.assertTrue(led)
            self.assertTrue(all(SX.KEY_LEAD_MIN_MS <= ld <= SX.KEY_LEAD_MAX_MS for ld in led))
            self.assertEqual(TM.check(ap.plan.points, ap.plan.duration_ms, run.tEnd), [])

    def test_cluster_bump(self):
        run = mk_run([(10000 + 50 * i, "start", i % 3, 0) for i in range(4)], segments=())
        pl, _, _ = run_sfx(run)
        boot = [p for p in pl if p.id == "boot"]
        self.assertEqual(len(boot), 1)
        self.assertAlmostEqual(boot[0].gainDb, LIB["boot"]["gainDb"] + 2.0, places=2)


# ==================================================================================================
# config + eleven (mocked HTTP; the fake key must never leak)
# ==================================================================================================

class FakeResp:
    def __init__(self, body: bytes, headers=None):
        self._b = body
        self.headers = headers or {}

    def read(self, *a):
        return self._b

    def close(self):
        pass


class FakeOpener:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        status, body, headers = self.script.pop(0)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
        return FakeResp(body, headers)


def tts_body(audio: bytes = b"\xff\xfb\x90\x00" * 8) -> bytes:
    return json.dumps({"audio_base64": base64.b64encode(audio).decode(), "alignment": {"characters": []}}).encode()


class Config(unittest.TestCase):
    def test_env_file_parsing_and_no_echo(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / ".git").mkdir()
            sub = d / "a" / "b"
            sub.mkdir(parents=True)
            (d / ".env").write_text(f"# c\nOTHER=1\nexport {C.KEY_ENV}=\"{FAKE_KEY}\"\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(C.api_key(sub), FAKE_KEY)
                (d / ".env").write_text("OTHER=1\n")
                with self.assertRaises(C.ConfigError) as cm:
                    C.api_key(sub)
                self.assertEqual(str(cm.exception), "E_NO_KEY")
            with mock.patch.dict(os.environ, {C.KEY_ENV: "envkey"}):
                self.assertEqual(C.api_key(sub), "envkey")

    def test_voice_ids_only_in_config(self):
        ids = [v["voiceId"] for v in CFG["voices"].values()]
        site = TOOLS.parent
        files = sorted((TOOLS / "narrate").glob("*.py")) + [Path(__file__), site / "NARRATION.md", site / "README.md",
                                                             site / "SPEC.md"]
        for p in files:
            if not p.is_file():
                continue
            src = p.read_text(encoding="utf-8")
            for vid in ids:
                self.assertNotIn(vid, src, p.name)


class Eleven(CacheEnv):
    def client(self, script, logs):
        op = FakeOpener(script)
        return EL.Client(FAKE_KEY, opener=op, sleep=lambda s: None, log=logs.append), op

    def test_retry_then_success(self):
        logs = []
        busy = json.dumps({"detail": {"status": "system_busy", "message": "x"}}).encode()
        c, op = self.client([(429, busy, None), (503, b"oops", None), (200, tts_body(), {"character-cost": "42",
                                                                                          "request-id": "r1"})], logs)
        res = c.tts_with_timestamps(CFG["voices"]["guide"]["voiceId"], "Hello.", "m", {}, 42, "mp3_44100_128")
        self.assertEqual(res["characterCost"], 42)
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(len(logs), 2)
        self.assertIn("code=system_busy", logs[0])
        for r in op.requests:
            self.assertNotIn(FAKE_KEY, r.full_url)
            self.assertEqual(r.get_header("Xi-api-key"), FAKE_KEY)

    def test_401_no_retry_and_no_echo(self):
        logs = []
        body = json.dumps({"detail": {"status": "invalid_api_key", "message": f"bad key {FAKE_KEY}"}}).encode()
        c, op = self.client([(401, body, None)], logs)
        with self.assertRaises(EL.ElevenError) as cm:
            c.sound_generation("p", 0.5, 0.6, "m", "mp3_44100_128")
        e = cm.exception
        self.assertEqual((e.status, e.code), (401, "invalid_api_key"))
        self.assertEqual(len(op.requests), 1)
        self.assertNotIn(FAKE_KEY, str(e) + repr(e) + repr(c) + "".join(logs))
        self.assertIsNone(e.__cause__)
        self.assertTrue(e.__suppress_context__)

    def test_error_code_sanitized(self):
        logs = []
        body = json.dumps({"detail": {"status": "Has Spaces And " + SENT}}).encode()
        c, _ = self.client([(400, body, None)], logs)
        with self.assertRaises(EL.ElevenError) as cm:
            c.sound_generation("p", 0.5, 0.6, "m", "mp3_44100_128")
        self.assertEqual(cm.exception.code, "unknown")
        c, _ = self.client([(422, b'{"detail":[{"msg":"x"}]}', None)], logs)
        with self.assertRaises(EL.ElevenError) as cm:
            c.sound_generation("p", 0.5, 0.6, "m", "mp3_44100_128")
        self.assertEqual(cm.exception.code, "validation")

    def test_retries_exhausted(self):
        logs = []
        c, op = self.client([(500, b"", None)] * EL.MAX_ATTEMPTS, logs)
        with self.assertRaises(EL.ElevenError) as cm:
            c.sound_generation("p", 0.5, 0.6, "m", "mp3_44100_128")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(op.requests), EL.MAX_ATTEMPTS)

    def test_quota_is_fatal(self):
        body = json.dumps({"detail": {"status": "quota_exceeded"}}).encode()
        c, op = self.client([(429, body, None)], [])
        with self.assertRaises(EL.ElevenError):
            c.sound_generation("p", 0.5, 0.6, "m", "mp3_44100_128")
        self.assertEqual(len(op.requests), 1)

    @unittest.skipUnless(HAS_FF, "ffmpeg not available")
    def test_synthesize_cache_fallback_and_no_key_leak(self):
        mp3 = self.tmp / "tone.mp3"
        ff(["-f", "lavfi", "-i", "sine=f=440:d=0.8", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)])
        body = tts_body(mp3.read_bytes())
        logs = []
        cues = [{"voice": "guide", "tpl": "G.crash", "text": V.render("G.crash", agent=1)},
                {"voice": "expert", "tpl": "E.close.noContact", "text": V.render("E.close.noContact")}]
        cfg = json.loads(json.dumps(CFG))
        cfg["tts"]["concurrency"] = 1
        c, op = self.client([(422, b"{}", None), (200, body, {"character-cost": "12"}), (200, body, None)], logs)
        st = TT.synthesize(cfg, cues, c, log=logs.append)
        self.assertEqual((st["synth"], st["fallback"]), (2, 1))
        sent = [json.loads(r.data)["model_id"] for r in op.requests]
        self.assertEqual(sent, [cfg["tts"]["model"], cfg["tts"]["fallbackModel"], cfg["tts"]["fallbackModel"]])
        for cue in cues:
            e = TT.lookup(cfg, cue["voice"], cue["text"])
            self.assertIsNotNone(e)
            self.assertGreater(e["durationMs"], 500)
        # second run is fully cached: no requests
        c2, op2 = self.client([], logs)
        st2 = TT.synthesize(cfg, cues, c2, log=logs.append)
        self.assertEqual((st2["cached"], len(op2.requests)), (2, 0))
        blob = "".join(logs)
        for p in (self.tmp / "cache").rglob("*"):
            if p.is_file():
                blob += p.read_bytes().decode("latin-1")
        self.assertNotIn(FAKE_KEY, blob)
        for cue in cues:
            self.assertNotIn(cue["text"], "".join(logs))

    def test_synthesize_aborts_on_401(self):
        cues = [{"voice": "guide", "tpl": "G.crash", "text": V.render("G.crash", agent=1)}]
        c, _ = self.client([(401, b"{}", None)], [])
        with self.assertRaises(TT.TTSAbort) as cm:
            TT.synthesize(CFG, cues, c, log=lambda m: None)
        self.assertEqual(cm.exception.status, 401)


# ==================================================================================================
# mix (ffmpeg)
# ==================================================================================================

def ff(args: list[str]) -> None:
    subprocess.run([C.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"] + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sine_pcm(path: Path, f: int, d: float) -> array:
    ff(["-f", "lavfi", "-i", f"sine=f={f}:d={d}", "-ac", "1", "-ar", "44100", "-f", "s16le", str(path)])
    return MX.read_pcm(path)


def integrated_lufs(path: Path) -> float:
    p = subprocess.run([C.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    vals = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", p.stderr.decode("utf-8", "replace"))
    return float(vals[-1])


@unittest.skipUnless(HAS_FF, "ffmpeg not available")
class Mix(CacheEnv):
    def test_mix_levels_duration_metadata(self):
        voice = sine_pcm(self.tmp / "v.s16", 440, 1.5)
        click = sine_pcm(self.tmp / "s.s16", 1500, 0.1)
        dur = 8000
        clips = [(500, voice, 0.0), (2500, voice, 0.0), (5000, voice, -2.0)]
        hits = [(300 + 700 * i, click, -4.0) for i in range(10)]
        out = MX.mix_to_mp3(CFG, dur, clips, hits, self.tmp / "work")
        ms = MX.probe_ms(out)
        self.assertLessEqual(abs(ms - dur), 60)
        self.assertAlmostEqual(integrated_lufs(out), CFG["mix"]["lufs"], delta=1.5)
        data = out.read_bytes()
        self.assertNotEqual(data[:3], b"ID3")
        self.assertEqual((data[0], data[1] & 0xE0), (0xFF, 0xE0))
        for frame in (b"TIT2", b"TSSE", b"TENC", b"COMM"):
            self.assertNotIn(frame, data)
        self.assertEqual(M.mp3_problems(out), ["E_MP3_NAME"])

    def test_normalization_is_static_gain(self):
        """Loud voice / SFX-only stretch / loud voice: both get the same gain, so the SFX-to-voice gap survives."""
        voice = sine_pcm(self.tmp / "v.s16", 220, 4.0)
        click = sine_pcm(self.tmp / "s.s16", 1500, 0.3)
        dur = 24000
        hits = [(6000 + 1000 * i, click, 0.0) for i in range(9)]
        out = MX.mix_to_mp3(CFG, dur, [(0, voice, 0.0), (18000, voice, 0.0)], hits, self.tmp / "work")
        dec = self.tmp / "o.s16"
        ff(["-i", str(out), "-ac", "1", "-ar", "44100", "-f", "s16le", str(dec)])
        o = MX.read_pcm(dec)

        def rms(pcm, a_ms, b_ms):
            seg = pcm[a_ms * 44:b_ms * 44]
            return (sum(v * v for v in seg) / max(1, len(seg))) ** 0.5

        db = lambda x: 20 * math.log10(max(x, 1e-9))   # noqa: E731
        vin = rms(voice, 500, 3500)
        sin_ = rms(MX.place_sfx(dur * 44 + 100, hits), 6000, 15000) * 10 ** (CFG["sfx"]["busGainDb"] / 20)
        gap_in = db(vin) - db(sin_)
        gap_out = db(rms(o, 500, 3500)) - db(rms(o, 6000, 15000))
        self.assertAlmostEqual(gap_out, gap_in, delta=1.0)
        self.assertAlmostEqual(integrated_lufs(out), CFG["mix"]["lufs"], delta=1.0)

    def test_sfx_bus_sums_and_clips(self):
        loud = array("h", [30000] * 100)
        bus = MX.place_sfx(200, [(0, loud, 0.0), (1, loud, 0.0)])
        self.assertEqual(max(bus), 32767)
        bus2 = MX.place_voice(50, [(0, loud, 0.0)])
        self.assertEqual(len(bus2), 50)

    def test_mix_run_end_to_end(self):
        """Synthetic run + fake TTS cache + ffmpeg-made SFX library -> manifest, index, mp3; tree check passes."""
        ix, core = synth_bundle()
        pub = write_public(self.tmp, ix, core)
        sidx = B.load_index(pub)
        run = B.load_run(sidx, "synth-run", CFG["models"], pub)
        s = SC.build_script(run, CFG)
        for c in s["cues"]:   # fake cached TTS: a tone of the estimated duration per cue
            key = TT.cache_key(TT.request_spec(CFG, c["voice"], c["text"], CFG["tts"]["model"]))
            mp3_p, meta_p, pcm_p = TT._paths(key)
            pcm_p.parent.mkdir(parents=True, exist_ok=True)
            meta_p.parent.mkdir(parents=True, exist_ok=True)
            sine_pcm(pcm_p, 300 if c["voice"] == "guide" else 200, c["estMs"] / 1000)
            meta_p.write_text(json.dumps({"durationMs": round(pcm_p.stat().st_size / 88.2)}))
        lib = self.tmp / "sfxlib"
        lib.mkdir()
        for sid in LIB:
            ff(["-f", "lavfi", "-i", "sine=f=1200:d=0.2", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
                "-map_metadata", "-1", "-id3v2_version", "0", str(lib / f"{sid}.mp3")])
        logs = []
        with mock.patch.object(C, "SFX_DIR", lib):
            m = MX.mix_run(run, CFG, s, pub, {"synth-run"}, log=logs.append)
            m2 = MX.mix_run(run, CFG, s, pub, {"synth-run"}, log=logs.append)   # rebuild replaces, never piles up
        self.assertEqual(M.validate(m), [])
        self.assertEqual(m["audio"], m2["audio"])
        mp3s = list((pub / "audio" / "narration").glob("*.mp3"))
        self.assertEqual(len(mp3s), 1)
        problems, warnings = M.check_tree(pub, {"synth-run": "0123abcd"})
        self.assertEqual((problems, warnings), ([], []))
        self.assertGreater(sum(m["sfx"]["counts"].values()), 3)
        self.assertNotIn(SENT, json.dumps(m) + "".join(logs))
        for c in m["cues"]:
            self.assertNotIn(c["text"], "".join(logs))
        # stale hash -> warning only
        _, w = M.check_tree(pub, {"synth-run": "ffffffff"})
        self.assertEqual(len(w), 1)
        # orphan + missing mp3
        (pub / "audio" / "narration" / "synth-run.deadbeef.mp3").write_bytes(mp3s[0].read_bytes())
        problems, _ = M.check_tree(pub, None)
        self.assertTrue(any("E_ORPHAN_MP3" in p for p in problems))
        self.assertTrue(any("E_MP3_HASH" in p for p in problems))
        (pub / "audio" / "narration" / "synth-run.deadbeef.mp3").unlink()
        mp3s[0].unlink()
        problems, _ = M.check_tree(pub, None)
        self.assertTrue(any("E_MP3_MISSING" in p for p in problems))


# ==================================================================================================
# manifest validation (fixed codes)
# ==================================================================================================

class Manifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = synth_saferun()
        cls.good = MX.plan_only(run, CFG, SC.build_script(run, CFG))

    def bad(self, mutate) -> list[str]:
        m = json.loads(json.dumps(self.good))
        mutate(m)
        return M.validate(m)

    def test_good(self):
        self.assertEqual(M.validate(self.good), [])

    def test_failures_have_fixed_codes(self):
        def nonmono(m):
            m["timeMap"][2][0] = m["timeMap"][1][0]

        def overlap(m):
            m["cues"][1]["audioMs"] = m["cues"][0]["audioMs"] + 1
            m["cues"][1]["runMs"] = int(TM.a2r(m["timeMap"], m["cues"][1]["audioMs"]))

        def bad_tpl(m):
            m["cues"][0]["tpl"] = f"{SENT}_TPL"

        def foreign(m):
            m["cues"][0]["text"] = "Zyzzyva " + m["cues"][0]["text"]

        def slope(m):
            m["timeMap"][1][1] = m["timeMap"][1][0] * 9 + 10

        cases = {"E_MAP_MONO": nonmono, "E_CUE_OVERLAP": overlap, "E_TPL_UNKNOWN": bad_tpl,
                 "E_TPL_NOMATCH": foreign, "E_SCHEMA": lambda m: m.update(schema="x"),
                 "E_KEYS": lambda m: m.update(extra=1), "E_AUDIO_URL": lambda m: m.update(audio="../x.mp3"),
                 "E_CUE_VOICE": lambda m: m["cues"][0].update(voice="narrator"),
                 "E_SFX_COUNTS": lambda m: m["sfx"]["counts"].update({f"{SENT}": 1})}
        for code, fn in cases.items():
            errs = self.bad(fn)
            self.assertTrue(any(e.split()[0] == code for e in errs), code)
            self.assertNotIn(SENT, " ".join(errs))
            self.assertNotIn("Zyzzyva", " ".join(errs))
        errs = self.bad(slope)
        self.assertTrue(errs)

    def test_index_validation(self):
        good = {"schema": M.INDEX_SCHEMA, "runs": {"a-b": {"hash": "0123abcd", "manifest": "a-b.json",
                                                            "durationMs": 5}}}
        self.assertEqual(M.validate_index(good), [])
        bad = json.loads(json.dumps(good))
        bad["runs"]["a-b"]["manifest"] = "../x.json"
        self.assertTrue(M.validate_index(bad))


# ==================================================================================================
# batch runner (NARRATION.md 7.2): budget, dry run, resumability, pruning; fake client, no network
# ==================================================================================================

class FakeTTS:
    """Stands in for eleven.Client: returns one ffmpeg-made tone for every request and counts characters."""

    def __init__(self, audio: bytes):
        self.audio, self.calls, self.chars = audio, 0, 0

    def tts_with_timestamps(self, voice_id, text, model, settings, seed, output_format, endpoint=None):
        self.calls += 1
        self.chars += len(text)
        return {"audio": self.audio, "alignment": None, "characterCost": len(text), "requestId": None}


def batch_public(root: Path) -> Path:
    """Two visible synthetic runs (same shape; the second names a known model, so its intro differs) plus one
    hidden card."""
    ix, core = synth_bundle()
    pub = write_public(root, ix, core)
    known_model = sorted(CFG["models"])[0]
    for rid, h, hidden in (("synth-b", "4567abcd", False), ("synth-h", "89abcdef", True)):
        card = dict(ix["runs"][0], id=rid, hash=h, core=f"runs/{rid}.{h}.core.json", hidden=hidden)
        ix["runs"].append(card)
        (pub / "data" / card["core"]).write_text(json.dumps(dict(core, hash=h, run=dict(core["run"],
                                                                                         model=known_model))))
    (pub / "data" / "index.json").write_text(json.dumps(ix))
    return pub


def no_client():
    raise AssertionError("network client requested")


class Batch(CacheEnv):
    def setUp(self):
        super().setUp()
        from narrate import __main__ as CLI
        self.CLI = CLI
        self.pub = batch_public(self.tmp)
        self.ix = B.load_index(self.pub)

    def args(self, **kw):
        import argparse
        d = {"runs": ["visible"], "dry_run": False, "max_new_chars": 100000, "jobs": 1}
        d.update(kw)
        return argparse.Namespace(**d)

    def run_batch(self, client_factory=no_client, **kw) -> tuple[int, str]:
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.CLI.cmd_batch(self.args(**kw), CFG, self.ix, public=self.pub, client_factory=client_factory)
        return rc, buf.getvalue()

    @staticmethod
    def nums(out: str, prefix: str) -> dict[str, int]:
        line = [ln for ln in out.splitlines() if ln.startswith(prefix)][-1]
        return {k: int(v) for k, v in (t.split("=", 1) for t in line.split() if "=" in t)}

    def all_texts(self) -> set[str]:
        texts = set()
        for rid in ("synth-run", "synth-b"):
            texts |= {c["text"] for c in SC.build_script(B.load_run(self.ix, rid, CFG["models"], self.pub), CFG)["cues"]}
        return texts

    def assert_safe_output(self, out: str):
        self.assertNotIn(SENT, out)
        self.assertNotIn(FAKE_KEY, out)
        for t in self.all_texts():
            self.assertNotIn(t, out)
        for ln in out.splitlines():     # numbers-only: every k=v value is an int, a float, or a fixed token
            for tok in ln.split():
                if "=" in tok:
                    self.assertRegex(tok.split("=", 1)[1], r"^(-?[\d.]+|[A-Za-z0-9._,-]+)$")

    def fake_cache(self, voice: str, text: str) -> None:
        key = TT.cache_key(TT.request_spec(CFG, voice, text, CFG["tts"]["model"]))
        _, meta_p, pcm_p = TT._paths(key)
        pcm_p.parent.mkdir(parents=True, exist_ok=True)
        meta_p.parent.mkdir(parents=True, exist_ok=True)
        pcm_p.write_bytes(bytes(8820))
        meta_p.write_text(json.dumps({"durationMs": 100}))

    def test_targets(self):
        T = self.CLI.batch_targets
        self.assertEqual(T(self.ix, None), ["synth-run", "synth-b"])
        self.assertEqual(T(self.ix, ["visible"]), ["synth-run", "synth-b"])
        self.assertEqual(T(self.ix, ["all"]), ["synth-run", "synth-b", "synth-h"])
        self.assertEqual(T(self.ix, ["synth-h", "synth-b", "synth-h"]), ["synth-h", "synth-b"])
        for bad in (["nope"], ["all", "synth-b"]):
            with self.assertRaises(B.BundleError):
                T(self.ix, bad)

    def test_dry_run_counts_only_no_network(self):
        ss = [SC.build_script(B.load_run(self.ix, rid, CFG["models"], self.pub), CFG) for rid in ("synth-run", "synth-b")]
        uniq = {(c["voice"], c["text"]) for s in ss for c in s["cues"]}
        self.assertGreater(len(uniq), len({(c["voice"], c["text"]) for c in ss[0]["cues"]}))
        first = sorted(uniq)[0]
        self.fake_cache(*first)
        rc, out = self.run_batch(dry_run=True)
        self.assertEqual(rc, 0)
        n = self.nums(out, "batch plan")
        self.assertEqual((n["runs"], n["scripted"], n["unique_clips"], n["cached"]), (2, 2, len(uniq), 1))
        self.assertEqual(n["cues"], sum(len(s["cues"]) for s in ss))
        self.assertEqual(n["new_chars"], sum(len(t) for v, t in uniq if (v, t) != first))
        self.assertEqual(n["projected_mp3_bytes"], sum(MX.projected_bytes(s["estDurationMs"]) for s in ss))
        self.assertFalse((self.pub / "audio").exists() or (self.pub / "data" / "narration").exists())
        self.assert_safe_output(out)

    def test_refuses_over_cap(self):
        rc, out = self.run_batch(max_new_chars=10)
        self.assertEqual(rc, 3)
        self.assertIn("batch refused", out)
        self.assertFalse((self.pub / "audio").exists() or (self.pub / "data" / "narration").exists())
        self.assert_safe_output(out)

    @unittest.skipUnless(HAS_FF, "ffmpeg not available")
    def test_build_resume_prune_and_stop(self):
        tone = self.tmp / "tone.mp3"
        ff(["-f", "lavfi", "-i", "sine=f=300:d=1.2", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(tone)])
        lib = self.tmp / "sfxlib"
        lib.mkdir()
        for sid in LIB:
            ff(["-f", "lavfi", "-i", "sine=f=1200:d=0.2", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
                "-map_metadata", "-1", "-id3v2_version", "0", str(lib / f"{sid}.mp3")])
        fake = FakeTTS(tone.read_bytes())
        uniq = {(c["voice"], c["text"]) for rid in ("synth-run", "synth-b") for c in SC.build_script(
            B.load_run(self.ix, rid, CFG["models"], self.pub), CFG)["cues"]}
        with mock.patch.object(C, "SFX_DIR", lib), mock.patch.object(SX, "library_ready", lambda cfg: True):
            # a cap one character below the plan is refused before any request
            rc, out = self.run_batch(max_new_chars=sum(len(t) for _, t in uniq) - 1, client_factory=lambda: fake)
            self.assertEqual((rc, fake.calls), (3, 0))
            rc, out = self.run_batch(client_factory=lambda: fake)
            self.assertEqual(rc, 0, out)
            d = self.nums(out, "batch done")
            self.assertEqual((d["ready"], d["mixed"], d["skipped"], d["mix_failed"], d["stopped"]), (2, 2, 0, 0, 0))
            self.assertEqual((fake.calls, fake.chars, d["spent_chars"]), (len(uniq), sum(len(t) for _, t in uniq),
                                                                           sum(len(t) for _, t in uniq)))
            self.assertEqual(M.check_tree(self.pub, {c.id: c.hash for c in self.ix.cards}), ([], []))
            self.assertEqual(sorted(M.load_index(self.pub)["runs"]), ["synth-b", "synth-run"])
            mp3s = sorted((self.pub / "audio" / "narration").glob("*.mp3"))
            self.assertEqual(len(mp3s), 2)
            for p in mp3s:   # delivery format: MPEG-2 Layer III, 24 kHz, 24 kbps CBR, under the per-file budget
                h = p.read_bytes()[:4]     # (frame 1 is LAME's Info frame, so the rate is checked on average)
                self.assertEqual((h[0], h[1] & 0xFE, (h[2] >> 2) & 3), (0xFF, 0xF2, 1))
                self.assertAlmostEqual(p.stat().st_size * 8 / MX.probe_ms(p), MX.OUT_BITRATE_KBPS, delta=2)
                self.assertLessEqual(p.stat().st_size, M.MAX_MP3_BYTES)
            self.assert_safe_output(out)

            # resumable: nothing changed -> no requests, no remix
            rc, out = self.run_batch()
            d = self.nums(out, "batch done")
            self.assertEqual((rc, d["mixed"], d["skipped"], d["spent_chars"]), (0, 0, 2, 0))

            # superseded / orphan files are pruned; a deleted mp3 triggers a remix of that run only
            (self.pub / "audio" / "narration" / "synth-run.deadbeef.mp3").write_bytes(b"\xff\xf3" + bytes(64))
            (self.pub / "data" / "narration" / "gone-run.json").write_text("{}")
            [p for p in mp3s if p.name.startswith("synth-b.")][0].unlink()
            rc, out = self.run_batch()
            d = self.nums(out, "batch done")
            self.assertEqual((rc, d["mixed"], d["skipped"], d["pruned_mp3s"], d["pruned_manifests"]), (0, 1, 1, 1, 1))
            self.assertEqual(M.check_tree(self.pub, {c.id: c.hash for c in self.ix.cards}), ([], []))

            # mid-way stop: actual spend exceeding the plan (e.g. a fallback re-synthesis) halts before the next run
            for p in (self.tmp / "cache" / "tts").glob("*"):
                p.unlink()
            real = TT.synthesize

            cap = sum(len(t) for _, t in uniq)

            def overspend(cfg, cues, client, log=print):
                return dict(real(cfg, cues, client, log=log), chars=cap)
            with mock.patch.object(TT, "synthesize", overspend):
                rc, out = self.run_batch(client_factory=lambda: fake, max_new_chars=cap)
            d = self.nums(out, "batch done")
            self.assertEqual((rc, d["ready"], d["stopped"]), (1, 1, 1))
            self.assertIn("batch stop run=synth-b", out)
            self.assert_safe_output(out)


if __name__ == "__main__":
    unittest.main()
