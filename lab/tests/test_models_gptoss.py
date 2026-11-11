"""gptoss120 (a second, free Cerebras model, now RETIRED) and the study's unsolvable scoring episodes.

gptoss120 was retired on 2026-10-13 (it ignored the board: 2 writer episodes in 162). Its entry is
kept so existing runs and data still resolve; these tests pin that it stays fully loadable while
config.active_models() and the QD defaults leave it out.

No model call, VM or network: the orchestrator runs against a fake msb runner in a temp lab tree,
the harness against the scripted fake provider of test_harness, and hillclimb.run_episode against a
stubbed run_one. Every payload is a neutral placeholder written here.
"""
import contextlib
import io
import itertools
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import config  # noqa: E402
import orchestrate  # noqa: E402
import spend  # noqa: E402
from config import HARNESS_DEFAULTS, MODELS, provider_spec  # noqa: E402
from sandbox import Sandbox  # noqa: E402
from study import hillclimb  # noqa: E402
from tests.test_harness import ok, run_harness  # noqa: E402
from tests.test_sandbox import expected_sequence, lane_runner, make_fixture, split_polls  # noqa: E402

SLUG = "gptoss120"
CAND = "0123456789abcdef"


def cli_ns(**kw):
    base = dict(model=None, provider=None, host_port_base=config.HOST_PORT_BASE, tag="",
                goal=config.DEFAULT_GOAL, cap_eur=None, seed_path=None)
    return types.SimpleNamespace(**{**base, **kw})


class ConfigEntryTest(unittest.TestCase):
    def test_entry(self):
        m = MODELS[SLUG]
        self.assertEqual((m["provider"], m["model"], m["label"]), ("cerebras", "gpt-oss-120b", "GPT-OSS 120B"))
        self.assertEqual((m["price_in"], m["price_out"], m["json_object"]), (None, None, False))
        self.assertIn(SLUG, config.MODEL_ORDER)
        self.assertEqual(config.MODEL_ORDER[0], config.DEFAULT_SLUG)  # primary first, unchanged
        self.assertNotIn(SLUG, config.BERGET_BATCH)
        self.assertEqual(config.resolve_slug("gpt-oss-120b"), SLUG)
        self.assertEqual(config.slug_for("cerebras", "gpt-oss-120b"), SLUG)
        self.assertEqual(config.run_slug_of({"model": "gpt-oss-120b"}), SLUG)  # no provider: Cerebras
        self.assertEqual(config.price(SLUG), (None, None))
        self.assertEqual(spend.cost(SLUG, {"prompt": 10 ** 6, "completion": 10 ** 6, "reasoning": 10 ** 6}), 0.0)
        self.assertEqual(provider_spec(SLUG)["secret_env"], config.SECRET_ENV)

    def test_retired_but_kept(self):
        self.assertIs(MODELS[SLUG]["retired"], True)
        self.assertTrue(config.is_retired(SLUG))
        self.assertFalse(config.is_retired(config.DEFAULT_SLUG))
        self.assertFalse(config.is_retired("nope"))               # unknown slugs are not "retired"
        self.assertIn(SLUG, config.MODEL_ORDER)                    # old runs / landscape axis
        self.assertNotIn(SLUG, config.active_models())
        self.assertEqual(config.active_models(), tuple(s for s in config.MODEL_ORDER if s != SLUG))
        self.assertEqual(config.active_models()[0], config.DEFAULT_SLUG)
        self.assertTrue(all(not MODELS[s].get("retired") for s in config.active_models()))

    def test_default_slug_is_still_qwen38(self):
        self.assertEqual(config.DEFAULT_SLUG, "qwen38")
        self.assertEqual(config.MODELS[config.DEFAULT_SLUG]["model"], config.MODEL)
        self.assertEqual(config.run_slug_of({}), "qwen38")


class ContextTest(unittest.TestCase):
    def test_ctx_is_unledgered_non_default_on_any_lane(self):
        for base in (config.HOST_PORT_BASE, 19100, 19400):
            ctx = orchestrate.ctx_from_args(cli_ns(model=SLUG, host_port_base=base))
            self.assertEqual((ctx.slug, ctx.provider["name"], ctx.ledger), (SLUG, "cerebras", None))
            self.assertFalse(ctx.is_default)
            self.assertEqual(ctx.run_slug, SLUG)
            self.assertEqual(ctx.sandbox_kwargs()["inference_host"], config.INFERENCE_HOST)
        ctx = orchestrate.ctx_from_args(cli_ns(model="gpt-oss-120b", provider="cerebras"))
        self.assertEqual(ctx.slug, SLUG)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            orchestrate.ctx_from_args(cli_ns(model=SLUG, provider="berget"))

    def test_harness_names_the_model(self):
        ctx = orchestrate.ctx_from_args(cli_ns(model=SLUG, host_port_base=19100))
        h = orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS))
        self.assertEqual((h["model"], h["provider"]), ("gpt-oss-120b", "cerebras"))
        self.assertEqual(config.run_slug_of(h), SLUG)
        # the default model's harness dict stays byte-identical
        self.assertEqual(orchestrate.harness_for(orchestrate.default_ctx(), dict(HARNESS_DEFAULTS)), HARNESS_DEFAULTS)

    def test_run_and_vm_ids(self):
        rid = orchestrate.make_run_id("forum", 4, 3, f"-{CAND}", SLUG, "unsolvable")
        self.assertEqual(rid, f"forum-multi4-r3-unsolvable--{SLUG}-{CAND}")
        self.assertEqual(config.id_goal(rid), "unsolvable")
        self.assertEqual(config.vm_run_id(rid), f"forum-multi4-r3--{SLUG}-{CAND}")


class LaneSequenceTest(unittest.TestCase):
    def test_cerebras_lane_sequence_and_cfg(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lab, runs = td / "lab", td / "runs"
            make_fixture(lab)
            runs.mkdir()
            ctx = orchestrate.RunContext(SLUG, MODELS[SLUG], provider_spec(SLUG), 19100)
            recorded = []
            sb = Sandbox(msb="msb", runner=lane_runner(recorded), **ctx.sandbox_kwargs())
            fake_time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda *a, **k: None)
            harness = orchestrate.harness_for(ctx, dict(HARNESS_DEFAULTS))
            with mock.patch.multiple(orchestrate, LAB=lab, LOCK_DIR=td, VENDOR=lab / "vendor",
                                     RUNS=runs, time=fake_time), contextlib.redirect_stdout(io.StringIO()):
                orchestrate.run_one("forum", 2, 0, harness, sb=sb, ctx=ctx)
            self.assertEqual(split_polls(recorded), split_polls(expected_sequence(
                lab, runs, "forum", 2, slug=SLUG, base=19100, fatal_check=True)))
            out = runs / f"forum-multi2-r0--{SLUG}"
            cfg = json.loads((out / "cfg-a0.json").read_text())
            self.assertNotIn("provider", cfg)  # the harness defaults to the Cerebras host/path/key
            self.assertEqual(cfg["harness"]["model"], "gpt-oss-120b")
            meta = json.loads((out / "run.json").read_text())
            self.assertEqual(config.run_slug_of(meta["harness"]), SLUG)


class ReasoningReplyTest(unittest.TestCase):
    """A reasoning model's reply: separate `reasoning` field, empty/null content beside tool calls."""

    def test_reasoning_field_logged_not_echoed_and_tool_call_runs(self):
        call = {"id": "c-1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo a"})}}
        w = run_harness([ok({"content": None, "reasoning": "lorem", "tool_calls": [call]}, finish="tool_calls",
                            usage={"prompt_tokens": 5, "completion_tokens": 9,
                                   "completion_tokens_details": {"reasoning_tokens": 7}}),
                         ok({"content": "", "reasoning": "ipsum"}), ok(), ok()],
                        harness={"model": "gpt-oss-120b", "budget": 2})
        self.assertEqual(w.commands, [["bash", "-c", "echo a"]])
        recs = [s for s in w.steps if s["kind"] == "model"]
        self.assertEqual([r["reasoning"] for r in recs[:2]], ["lorem", "ipsum"])
        for _, _, body, _ in w.requests:
            req = json.loads(body)
            self.assertEqual(req["model"], "gpt-oss-120b")
            for m in req["messages"]:
                self.assertNotIn("reasoning", m)
            assistants = [m for m in req["messages"] if m["role"] == "assistant"]
            self.assertTrue(all(isinstance(m["content"], str) for m in assistants))
        self.assertEqual(w.conns[0][0], config.INFERENCE_HOST)
        # reasoning tokens are counted for the ledger's usage parser (unpriced here, so 0 EUR)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "steps.jsonl"
            p.write_text("\n".join(json.dumps(s) for s in w.steps))
            self.assertEqual(spend._steps_usage(p)["reasoning"], 7)


class UnsolvableEpisodeTest(unittest.TestCase):
    def test_episode_ids_unique_over_candidate_channel_model_goal_rep(self):
        ids = set()
        combos = list(itertools.product((CAND, "fedcba9876543210"), ("forum", "irc", "kv"),
                                        ("qwen38", SLUG, "mistral24"), config.GOALS, (0, 1)))
        for cand, ch, slug, goal, rep in combos:
            seed = Path("/nonexistent/candidates") / f"{cand}.chatter.json"
            rid = hillclimb.episode_run_id(seed, iface=ch, n_agents=4, rep=rep, slug=slug, goal=goal)
            self.assertEqual(config.id_goal(rid), goal)
            self.assertTrue(rid.endswith(f"-{cand}"))
            self.assertNotIn("unsolvable", config.vm_run_id(rid))
            ids.add(rid)
        self.assertEqual(len(ids), len(combos))

    def test_study_goal_default_is_unsolvable(self):
        self.assertEqual(hillclimb.STUDY_GOAL, "unsolvable")
        seed = Path("/nonexistent/candidates") / f"{CAND}.chatter.json"
        self.assertIn("-unsolvable", hillclimb.episode_run_id(seed, iface="forum", n_agents=4, rep=0))

    def _episode(self, **kw):
        calls, envs = [], []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            seed = td / "candidates" / f"{CAND}.chatter.json"  # never opened: only its name is read
            with mock.patch.multiple(orchestrate, RUNS=td / "runs", LOCK_DIR=td,
                                     load_env=lambda *a: envs.append(a),
                                     run_one=lambda *a, **k: calls.append((a, k))):
                path = hillclimb.run_episode(seed, iface="irc", n_agents=4, rep=7, budget=40, **kw)
            return path.relative_to(td / "runs"), calls, envs

    def test_run_episode_passes_goal_through_ctx(self):
        for slug, base in (("qwen38", 19100), (SLUG, 19200)):
            path, [(a, k)], envs = self._episode(slug=slug, host_port_base=base)
            ctx = k["ctx"]
            seg = "" if slug == "qwen38" else f"--{slug}"
            self.assertEqual(str(path), f"irc-multi4-r7-unsolvable{seg}-{CAND}")
            self.assertEqual((ctx.goal, ctx.candidate_id, ctx.slug, ctx.host_port_base),
                             ("unsolvable", CAND, slug, base))
            self.assertEqual(k["tag"], f"-{CAND}")
            self.assertEqual(a[:3], ("irc", 4, 7))
            self.assertIs(a[3]["stall_exit"], False)  # agents keep trying for the whole budget
            self.assertEqual(a[3]["budget"], 40)
            self.assertEqual(envs, [(config.SECRET_ENV,)])
            self.assertEqual(orchestrate.run_id_for("irc", 4, 7, k["tag"], ctx), str(path))

    def test_run_episode_solvable_keeps_the_old_shape(self):
        path, [(a, k)], _ = self._episode(goal="solvable")
        self.assertEqual(str(path), f"irc-multi4-r7-{CAND}")
        self.assertEqual(k["ctx"].goal, "solvable")
        self.assertNotIn("stall_exit", a[3])

    def test_cli_goal_flag(self):
        seen = []
        with mock.patch.object(hillclimb, "step", lambda scores, **kw: seen.append(kw["goal"]) or {}), \
                mock.patch.object(hillclimb.scoreboard, "build_and_write", lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()):
            hillclimb.main(["--generations", "1"])
            hillclimb.main(["--generations", "1", "--goal", "solvable"])
        self.assertEqual(seen, ["unsolvable", "solvable"])


if __name__ == "__main__":
    unittest.main()
