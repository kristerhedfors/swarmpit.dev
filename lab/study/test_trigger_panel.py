"""Offline tests for the cross-model trigger panel (scale-up contract P).

No network and no real model call: every panel member is an injected call_model returning neutral
placeholder replies written by THIS test (fixed enum codes, "lorem" filler). No board or chatter content
is read, printed, or asserted.

    cd lab && python3 -m unittest study.test_trigger_panel
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from study import landscape, modelio, triggers as tg
from study.test_instigation import TRIPWIRE, make_fixture


def reply(code, conf=0.9):
    return json.dumps({"codes": [{"code": code, "confidence": conf}]})


def member(by_agent):
    """A call_model answering per agent id (parsed from the user content's leading ``agent_id=N`` token).
    A value that is an Exception instance is raised instead."""
    def call(sp, uc, slug):
        aid = int(uc.split("agent_id=", 1)[1].split()[0])
        v = by_agent[aid]
        if isinstance(v, BaseException):
            raise v
        return v
    return call


class PanelBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = make_fixture(Path(tmp.name))
        self.inst = self.run_dir / "instigation"

    def load(self, name="triggers.json"):
        return json.loads((self.inst / name).read_text())

    def rec(self, art, aid):
        return next(r for r in art["records"] if r["agent_id"] == aid)


class Consensus(unittest.TestCase):
    S = ["qwen38", "mistral24", "glm53f"]

    def test_majority(self):
        v = {"qwen38": ("TASK_HINT", 0.4), "mistral24": ("NONE", 0.9), "glm53f": ("TASK_HINT", 0.8)}
        code, conf, first = tg.consensus(v, self.S)
        self.assertEqual((code, first), ("TASK_HINT", "qwen38"))
        self.assertAlmostEqual(conf, 0.6)

    def test_tie_goes_to_earliest_slug(self):
        v = {"qwen38": None, "mistral24": ("NONE", 0.2), "glm53f": ("EMPTY_BOARD", 0.9)}
        self.assertEqual(tg.consensus(v, self.S)[0], "NONE")
        v = {"qwen38": ("UNPROMPTED", 0.1), "mistral24": ("NONE", 0.2), "glm53f": ("EMPTY_BOARD", 0.9)}
        self.assertEqual(tg.consensus(v, self.S)[0], "UNPROMPTED")
        self.assertEqual(tg.consensus(v, list(reversed(self.S)))[0], "EMPTY_BOARD")

    def test_abstentions_do_not_vote_and_all_abstain_is_none(self):
        v = {"qwen38": None, "mistral24": None, "glm53f": ("NONE", 0.5)}
        self.assertEqual(tg.consensus(v, self.S)[0], "NONE")
        self.assertIsNone(tg.consensus({s: None for s in self.S}, self.S))

    def test_top_code_rule(self):
        self.assertEqual(tg._top_code({"codes": [{"code": "NONE", "confidence": 0.3},
                                                 {"code": "TASK_HINT", "confidence": 0.7}]}),
                         ("TASK_HINT", 0.7))
        # equal confidence -> earlier ENUM position (landscape._best_code rule)
        self.assertEqual(tg._top_code({"codes": [{"code": "NONE", "confidence": 0.5},
                                                 {"code": "EMPTY_BOARD", "confidence": 0.5}]})[0],
                         "EMPTY_BOARD")
        self.assertIsNone(tg._top_code({"ok": False, "error_code": "E_PARSE"}))
        self.assertIsNone(tg._top_code({"codes": [{"code": "LOREM", "confidence": 1}]}))

    def test_slug_validation(self):
        self.assertEqual(tg._panel_slugs("qwen38, mistral24,qwen38"), ["qwen38", "mistral24"])
        for bad in ("", "qwen38,../x", ["lorem"], []):
            with self.assertRaises(ValueError) as cm:
                tg._panel_slugs(bad)
            self.assertEqual(str(cm.exception), "E_PANEL_SLUG")


class RunPanel(PanelBase):
    def test_artifacts_votes_and_consensus(self):
        calls = {
            "qwen38": member({0: reply("EMPTY_BOARD", 0.8), 1: reply("TASK_HINT", 0.6)}),
            "mistral24": member({0: reply("EMPTY_BOARD", 0.6), 1: reply("NONE", 0.9)}),
            "glm53f": member({0: reply("TASK_HINT", 1.0), 1: reply("NONE", 0.5)}),
        }
        slugs = ["qwen38", "mistral24", "glm53f"]
        art = tg.run_panel(self.run_dir, None, slugs, call_model=calls)
        self.assertEqual(art, self.load())
        for s in slugs:  # per-member artifacts in today's single-model shape + quarantined raw dirs
            m = self.load(f"triggers.{s}.json")
            self.assertEqual(m["model_slug"], s)
            self.assertEqual({r["agent_id"] for r in m["records"]}, {0, 1})
            self.assertTrue((self.inst / "raw" / s / "a0.txt").is_file())
        self.assertEqual(set(art), {"run_id", "parser_version", "model_slug", "peer_wrote_first",
                                    "records", "panel"})
        r0, r1 = self.rec(art, 0), self.rec(art, 1)
        self.assertEqual(r0["codes"][0]["code"], "EMPTY_BOARD")
        self.assertAlmostEqual(r0["codes"][0]["confidence"], 0.7)
        self.assertEqual(r0["model_slug"], "qwen38")
        self.assertEqual(r1["codes"][0]["code"], "NONE")
        self.assertEqual(r1["model_slug"], "mistral24")
        self.assertEqual(art["panel"]["slugs"], slugs)
        self.assertEqual(art["panel"]["votes"]["1"], {"qwen38": "TASK_HINT", "mistral24": "NONE",
                                                      "glm53f": "NONE"})
        self.assertAlmostEqual(art["panel"]["agreement"]["0"], 2 / 3)
        # landscape reads the consensus artifact unchanged
        from study import instigation as ig
        self.assertIn(landscape.primary_trigger(art, ig.compute(self.run_dir)), landscape.TRIGGER_AXIS)

    def test_model_error_for_one_slug_abstains(self):
        calls = {"qwen38": member({0: modelio.ModelError("E_HTTP"), 1: modelio.ModelError("E_HTTP")}),
                 "mistral24": member({0: reply("CALL_TO_ACTION"), 1: reply("NONE")})}
        art = tg.run_panel(self.run_dir, None, ["qwen38", "mistral24"], call_model=calls)
        self.assertEqual(self.rec(art, 0)["codes"][0]["code"], "CALL_TO_ACTION")
        self.assertEqual(art["panel"]["votes"]["0"], {"qwen38": None, "mistral24": "CALL_TO_ACTION"})
        self.assertEqual(art["panel"]["agreement"]["0"], 0.5)
        m = self.load("triggers.qwen38.json")
        self.assertEqual({r["error_code"] for r in m["records"]}, {"E_HTTP"})

    def test_model_error_code_is_bounded(self):
        calls = {"qwen38": member({0: modelio.ModelError("lorem " + TRIPWIRE), 1: reply("NONE")})}
        tg.run_panel(self.run_dir, None, ["qwen38"], call_model=calls)
        disk = (self.inst / "triggers.qwen38.json").read_text()
        self.assertNotIn(TRIPWIRE, disk)
        self.assertIn("E_MODEL", disk)

    def test_all_abstain_is_panel_error(self):
        calls = {"qwen38": member({0: "lorem ipsum", 1: reply("NONE")}),
                 "mistral24": member({0: modelio.ModelError("E_TRANSPORT"), 1: reply("NONE")})}
        art = tg.run_panel(self.run_dir, None, ["qwen38", "mistral24"], call_model=calls)
        r0 = self.rec(art, 0)
        self.assertEqual(r0["error_code"], tg.PANEL_ERROR)
        self.assertNotIn("codes", r0)
        self.assertEqual(art["panel"]["votes"]["0"], {"qwen38": None, "mistral24": None})
        self.assertEqual(art["panel"]["agreement"]["0"], 0.0)
        self.assertEqual(self.rec(art, 1)["codes"][0]["code"], "NONE")

    def test_non_model_error_propagates(self):
        calls = {"qwen38": member({0: RuntimeError("E_X"), 1: reply("NONE")})}
        with self.assertRaises(RuntimeError):
            tg.run_panel(self.run_dir, None, ["qwen38"], call_model=calls)

    def test_single_callable_receives_slug(self):
        seen = []

        def call(sp, uc, slug):
            seen.append(slug)
            return reply("UNPROMPTED")

        art = tg.run_panel(self.run_dir, [0], ["qwen38", "glm53f"], call_model=call)
        self.assertEqual(sorted(set(seen)), ["glm53f", "qwen38"])
        self.assertEqual([r["agent_id"] for r in art["records"]], [0])

    def test_no_model_text_leaks(self):
        poison = ('lorem ' + TRIPWIRE + ' {"codes": [{"code": "NONE", "confidence": 0.4}], '
                  '"model_slug": "' + TRIPWIRE + '"}')
        calls = {"qwen38": member({0: poison, 1: poison}),
                 "mistral24": member({0: '{"codes": ["' + TRIPWIRE + '"]}', 1: poison})}
        art = tg.run_panel(self.run_dir, None, ["qwen38", "mistral24"], call_model=calls)
        for name in ("triggers.json", "triggers.qwen38.json", "triggers.mistral24.json"):
            self.assertNotIn(TRIPWIRE, (self.inst / name).read_text())
        self.assertNotIn(TRIPWIRE, json.dumps(art))

    def test_default_panel(self):
        """qwen38 alone since gptoss120 was retired; a one-member panel still writes the panel block."""
        self.assertEqual(tg.DEFAULT_PANEL, ("qwen38",))
        self.assertTrue(all(not config.MODELS[s].get("retired") for s in tg.DEFAULT_PANEL))
        art = tg.run_panel(self.run_dir, None, call_model=lambda sp, uc, slug: reply("NONE"))
        self.assertEqual(art["panel"]["slugs"], ["qwen38"])
        self.assertEqual(art["model_slug"], "qwen38")
        self.assertTrue((self.inst / "triggers.qwen38.json").is_file())
        self.assertFalse((self.inst / "triggers.gptoss120.json").exists())
        self.assertTrue(set(art["panel"]["agreement"].values()) <= {0.0, 1.0})

    def test_retired_slug_still_runs_when_named(self):
        """run_panel itself keeps retired slugs usable (re-classifying old runs); qd.py gates new runs."""
        self.assertTrue(config.MODELS["gptoss120"].get("retired"))
        art = tg.run_panel(self.run_dir, None, ["qwen38", "gptoss120"],
                           call_model=lambda sp, uc, slug: reply("NONE"))
        self.assertEqual(art["panel"]["slugs"], ["qwen38", "gptoss120"])
        self.assertTrue((self.inst / "triggers.gptoss120.json").is_file())


class Cli(PanelBase):
    def test_panel_flag(self):
        with mock.patch.object(tg, "_default_call_model", lambda sp, uc, slug: reply("NONE")), \
                mock.patch("sys.stdout") as out:
            rc = tg.main([str(self.run_dir), "--panel", "qwen38,mistral24"])
        self.assertEqual(rc, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["n_records"], 2)
        self.assertIn("panel", self.load())

    def test_bad_panel_slug(self):
        with mock.patch("sys.stdout") as out:
            rc = tg.main([str(self.run_dir), "--panel", "lorem"])
        self.assertEqual(rc, 2)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["error_code"], "E_PANEL_SLUG")


if __name__ == "__main__":
    unittest.main()
