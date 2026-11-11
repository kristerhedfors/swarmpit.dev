"""Offline tests for the MAP-Elites driver (lab/study/qd.py).

No VM, no network, no real model call, no real spend ledger: episodes, metrics, the trigger panel,
the generator and the ledger are injected fakes over temp registry / archive / state paths.
Candidates are neutral placeholder seeds ("lorem") created by the fakes. Every assertion is
structural -- record ids, cell keys, counts, ports, flags, EUR numbers and fixed error codes.

    cd lab && python3 -m unittest study.test_qd
"""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from study import generate, landscape, qd, registry

_ALLOWED_LINE_KEYS = {"gen", "record_id", "channel", "model", "probe", "cell_key", "instigation_score",
                      "n_writers", "engagement_score", "inserted", "trigger_error", "error", "origin",
                      "parent_id", "event"}


def _placeholder(n_words=3, salt=0):
    return {"board_name": f"b{salt}", "threads": [{"slug": "t", "title": "lorem", "posts": [
        {"author": "a", "body": " ".join(["lorem"] * n_words), "ago_min": 1}]}]}


def _metric(n_writers, seed_chars=150, score=0.25, I=None):
    agents = [{"agent_id": i, "wrote": i < n_writers,
               "time_to_first_write_s": (3.0 + i) if i < n_writers else None} for i in range(4)]
    m = {"run_id": "forum-multi4-r1", "n_writers": n_writers, "seed_chars": seed_chars,
         "instigation_score": score if n_writers else 0.0, "agents": agents, "engagement": {},
         "engagement_score": 0.5}
    if I is not None:
        m["I"] = I
    return m


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.paths = dict(registry_path=self.tmp / "registry.json", candidates_dir=self.tmp / "candidates",
                          archive_path=self.tmp / "archive.json", state_path=self.tmp / "qd_state.json",
                          db=self.tmp / "obs.duckdb")
        self.deps = qd.dry_run_deps(self.tmp)
        self.out = io.StringIO()

    def tearDown(self):
        self._td.cleanup()

    def make(self, **kw):
        args = dict(channels=["forum", "js"], models=["qwen38"], budget=40, n_agents=4,
                    workers={"cerebras": 2, "berget": 1}, trigger_panel=["qwen38"],
                    host_port_base=21000, deps=self.deps, out=self.out, **self.paths)
        args.update(kw)
        return qd.QD(**args)

    def lines(self):
        return [json.loads(x) for x in self.out.getvalue().splitlines() if x.strip()]

    def seed(self, n=2, pool=True, origin="scratch"):
        """Register n placeholder candidates as bootstrap rows (and, by default, in the driver's
        managed pool, as bootstrap() does); returns their ids."""
        rids = []
        for i in range(n):
            cand = _placeholder(3 + i, i)
            path = registry.candidate_path(registry.record_id(cand), self.paths["candidates_dir"])
            registry._atomic_write(path, registry.canonical_json(cand))
            row = registry.make_row(cand, channel="forum", model="dry", chatter_path=str(path))
            row.update({"origin": origin, "bootstrap": origin == "pilot"})
            registry.upsert(row, self.paths["registry_path"])
            rids.append(row["record_id"])
        if pool:
            st = qd.load_state(self.paths["state_path"])
            st["pool"] += [r for r in rids if r not in st["pool"]]
            qd.save_state(st, self.paths["state_path"])
        return rids


class ConfigTests(_Base):
    def test_budget_floor_enforced(self):
        with self.assertRaises(qd.QDError) as cm:
            self.make(budget=39)
        self.assertEqual(cm.exception.args[0], "E_BUDGET")

    def test_goal_constants_mirror_config(self):
        import config
        self.assertEqual(set(qd.GOALS), set(config.GOALS))
        self.assertEqual(qd.LEGACY_GOAL, config.DEFAULT_GOAL)
        self.assertEqual(qd.DEFAULT_GOAL, "unsolvable")
        self.assertEqual(qd.DEFAULT_MODELS, config.active_models())
        self.assertEqual(qd.DEFAULT_PANEL, ("qwen38",))
        for m in qd.DEFAULT_MODELS + qd.DEFAULT_PANEL:
            self.assertIn(m, config.MODELS)
            self.assertFalse(config.is_retired(m))
        for m in qd.DEFAULT_PANEL:
            self.assertIsNone(config.MODELS[m]["price_in"])  # the classifier panel is free models only
        self.assertEqual(qd.DEFAULT_MODELS[0], config.DEFAULT_SLUG)

    def test_gptoss_retired_but_kept(self):
        import config
        self.assertTrue(config.is_retired("gptoss120"))
        self.assertIn("gptoss120", config.MODELS)
        self.assertIn("gptoss120", config.MODEL_ORDER)        # existing data still references it
        self.assertNotIn("gptoss120", config.active_models())
        self.assertNotIn("gptoss120", qd.DEFAULT_MODELS + qd.DEFAULT_PANEL)
        self.assertIs(qd._model_spec("gptoss120")["retired"], True)
        self.assertIs(qd._model_spec("qwen38")["retired"], False)

    def test_retired_model_refused_unless_allowed(self):
        for kw in ({"models": ["gptoss120"]}, {"models": ["qwen38", "gptoss120"]},
                   {"trigger_panel": ["qwen38", "gptoss120"]}, {"gen_slug": "gptoss120"}):
            with self.assertRaises(qd.QDError) as cm:
                self.make(**kw)
            self.assertEqual(cm.exception.args[0], "E_RETIRED")
            self.make(allow_retired=True, **kw)   # an explicit override still works

    def test_retired_flag_from_injected_model_spec(self):
        """A model_spec without a "retired" key (older fakes) counts as active."""
        base = self.deps.model_spec
        self.deps.model_spec = lambda s: {k: v for k, v in base(s).items() if k != "retired"}
        self.make(models=["qwen38", "gptoss120"])
        self.deps.model_spec = lambda s: {**base(s), "retired": s == "qwen38"}
        with self.assertRaises(qd.QDError) as cm:
            self.make()
        self.assertEqual(cm.exception.args[0], "E_RETIRED")

    def test_bad_goal_model_panel_rejected(self):
        for kw, code in (({"goal": "nope"}, "E_GOAL"), ({"models": ["nope"]}, "E_MODEL"),
                         ({"models": []}, "E_MODEL"), ({"trigger_panel": ["nope"]}, "E_PANEL_SLUG"),
                         ({"trigger_panel": ["mistral24"]}, "E_PANEL_PRICED")):
            with self.assertRaises(qd.QDError) as cm:
                self.make(**kw)
            self.assertEqual(cm.exception.args[0], code)

    def test_unsolvable_refused_when_episode_cannot_take_goal(self):
        """Never silently run a solvable episode for an unsolvable study."""
        self.deps.run_episode = lambda seed_path, *, iface, n_agents, rep, budget, **_k: None
        self.make()  # **kwargs accepts goal
        self.deps.run_episode = lambda seed_path, *, iface, n_agents, rep, budget, slug=None: None
        with self.assertRaises(qd.QDError) as cm:
            self.make()
        self.assertEqual(cm.exception.args[0], "E_GOAL")
        self.make(goal="solvable")

    def test_default_model_single_lane_enforced(self):
        """A pinned slug's provider pool must be 1 worker on that lane: refuse up front."""
        self.deps.single_lane = {"qwen38": 19000}
        for kw in ({"workers": {"cerebras": 2}, "host_port_base": 19000},
                   {"workers": {"cerebras": 1}, "host_port_base": 19100}):
            with self.assertRaises(qd.QDError) as cm:
                self.make(**kw)
            self.assertEqual(cm.exception.args[0], "E_LANES")
        self.make(workers={"cerebras": 1}, host_port_base=19000)
        self.make(workers={"cerebras": 3}, host_port_base=21000, models=["gptoss120"], allow_retired=True)

    def test_candidate_seed_runs_on_any_lane(self):
        """QD episodes are candidate-seeded, so the default slug may use extra lanes; a non-candidate
        seed (e.g. the pilot) is still pinned to the default lane by orchestrate.ctx_from_args."""
        from study import hillclimb
        import config
        self.assertEqual(qd._single_lane(), {})
        seed = self.tmp / "candidates" / ("0" * 16 + ".chatter.json")
        ctx = hillclimb._ctx(seed, config.DEFAULT_SLUG, config.HOST_PORT_BASE + qd.LANE_STRIDE, None, False)
        self.assertFalse(ctx.is_default)
        with self.assertRaises(SystemExit):
            hillclimb._ctx(self.tmp / "seed.json", config.DEFAULT_SLUG,
                           config.HOST_PORT_BASE + qd.LANE_STRIDE, None, False)
        self.make(workers={"cerebras": 3}, host_port_base=19000)

    def test_unknown_channel_rejected(self):
        for kw in ({"channels": ["nope"]}, {"probe_channels": ["nope"]}, {"channels": []}):
            with self.assertRaises(qd.QDError) as cm:
                self.make(**kw)
            self.assertEqual(cm.exception.args[0], "E_CHANNEL")

    def test_goal_archive_paths_are_separate(self):
        base = self.tmp / "landscape_archive.json"
        self.assertEqual(qd.goal_archive_path("solvable", base), base)
        self.assertEqual(qd.goal_archive_path("unsolvable", base).name, "landscape_archive.unsolvable.json")

    def test_cli_budget_and_dry_run(self):
        out = io.StringIO()
        self.assertEqual(qd.main(["--dry-run", "--budget", "10"], out=out), 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "E_BUDGET")
        out = io.StringIO()
        # default grid (no --channels): forum + the code-comment channels js/html; irc is not in it
        rc = qd.main(["--dry-run", "--generations", "1", "--batch", "2",
                      "--workers-cerebras", "2", "--workers-berget", "1", "--bootstrap",
                      "--scratch-per-channel", "1", "--pilot-variants", "1"], out=out)
        self.assertEqual(rc, 0)
        summ = json.loads(out.getvalue().splitlines()[-1])
        for k in ("ok", "goal", "models", "coverage", "coverage_cells", "coverage_by_model", "mean_elite_score",
                  "max_n_writers", "max_n_writers_by_channel", "promoted_channels", "n_distinct_scores",
                  "trigger_code_counts", "n_unresolved", "n_evaluated", "spent_eur_by_model",
                  "panel_agreement_mean", "providers_stopped"):
            self.assertIn(k, summ)
        self.assertEqual(summ["goal"], "unsolvable")
        self.assertEqual(summ["grid_channels"][:3], ["forum", "js", "html"])
        self.assertNotIn("irc", summ["grid_channels"])
        self.assertTrue({"js", "html"} <= set(summ["max_n_writers_by_channel"]))
        # the dry run exercises several models, one of them priced against a fake ledger
        import config
        self.assertEqual(summ["models"], list(config.active_models()))
        self.assertNotIn("gptoss120", summ["models"])
        self.assertIn("mistral24", summ["spent_eur_by_model"])
        self.assertGreater(summ["spent_eur_by_model"]["mistral24"], 0.0)
        self.assertLessEqual(sum(summ["spent_eur_by_model"].values()), qd.DRY_SPEND_CAP_EUR + 1e-9)
        self.assertGreater(summ["n_evaluated"], 0)
        for code in summ["trigger_code_counts"]:
            self.assertIn(code, landscape.TRIGGER_AXIS)
        for m, n in summ["coverage_by_model"].items():
            self.assertIn(m, summ["models"])
            self.assertIsInstance(n, int)
        self.assertEqual(summ["coverage"], sum(summ["coverage_by_model"].values()))
        self.assertLessEqual(summ["coverage_cells"], summ["coverage"])
        self.assertTrue(set(summ["promoted_channels"]) <= set(qd.DEFAULT_PROBES))
        # dry-run publish runs the real builders + strict landscape validator into its temp dir
        self.assertIs(summ["publish"]["ok"], True)

    def test_default_grid_and_probe_channels(self):
        a = qd._parse(["--dry-run"])
        self.assertEqual(qd._csv(a.channels), ["forum", "js", "html"])
        self.assertEqual(qd._csv(a.probe_channels), ["guestbook", "paste", "kv"])
        self.assertEqual(qd.DEFAULT_CHANNELS, ("forum", "js", "html"))

    def test_irc_still_accepted_explicitly(self):
        # dropped from the default grid only: an explicit --channels irc (old studies) still validates
        q = self.make(channels=["forum", "irc"])
        self.assertEqual(q.grid, ["forum", "irc"])
        with self.assertRaises(qd.QDError):
            self.make(channels=["forum", "email"])

    def test_code_comment_channels_evaluate_into_their_own_cells(self):
        self.seed(1)
        q = self.make(channels=["js", "html"])
        q.evaluate(q.pending(), 0)
        chans = {line["channel"] for line in self.lines() if "cell_key" in line}
        self.assertEqual(chans, {"js", "html"})
        for line in self.lines():
            if "cell_key" in line:
                self.assertEqual(landscape.split_cell_key(line["cell_key"])[1], line["channel"])

    def test_cli_refuses_priced_model_without_spend_cap(self):
        out = io.StringIO()
        rc = qd.main(["--dry-run", "--models", "qwen38,mistral24", "--spend-cap-eur", "0",
                      "--generations", "0"], out=out)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "E_SPEND_CAP")

    def test_cli_refuses_retired_model_and_panel(self):
        for argv in (["--models", "qwen38,gptoss120"], ["--models", "gptoss120"],
                     ["--trigger-panel", "qwen38,gptoss120"], ["--gen-model", "gptoss120"]):
            out = io.StringIO()
            rc = qd.main(["--dry-run", "--generations", "0", "--no-publish", *argv], out=out)
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(out.getvalue()), {"ok": False, "error": "E_RETIRED"})

    def test_cli_allow_retired(self):
        self.assertFalse(qd._parse([]).allow_retired)
        self.assertEqual(qd._csv(qd._parse([]).trigger_panel), ["qwen38"])
        out = io.StringIO()
        rc = qd.main(["--dry-run", "--generations", "0", "--no-publish", "--models", "qwen38,gptoss120",
                      "--trigger-panel", "qwen38,gptoss120", "--allow-retired"], out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["models"], ["qwen38", "gptoss120"])

    def test_real_cli_refuses_retired_before_any_episode(self):
        """Non-dry path: refused at construction (E_RETIRED), before env loading or any episode."""
        for argv in (["--models", "gptoss120"], ["--models", "qwen38,gpt-oss-120b"],   # slug or full model id
                     ["--models", "qwen38", "--trigger-panel", "qwen38,gptoss120"]):
            out = io.StringIO()
            calls = []
            fake = qd.dry_run_deps(self.tmp)
            fake.run_episode = lambda *a, **k: calls.append(1)
            with mock.patch.object(qd, "_load_env"), mock.patch.object(qd, "default_deps", return_value=fake):
                rc = qd.main([*argv, "--no-publish", "--generations", "0",
                              "--state", str(self.paths["state_path"]),
                              "--archive", str(self.paths["archive_path"]),
                              "--registry", str(self.paths["registry_path"])], out=out)
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(out.getvalue()), {"ok": False, "error": "E_RETIRED"})
            self.assertEqual(calls, [])

    def test_dry_run_publish_writes_only_into_tmp(self):
        qd_ = self.make(models=["qwen38", "gptoss120"], allow_retired=True)
        qd_.bootstrap(pilot_variants=1, scratch_per_channel=1)
        qd_.evaluate(qd_.pending(), 0)
        res = qd_.publish()
        self.assertIs(res["ok"], True)
        ls = json.loads((self.tmp / "publish" / "landscape.json").read_text())
        self.assertEqual(ls["goal"], "unsolvable")
        self.assertTrue(set(ls["by_model"]) <= {"qwen38", "gptoss120"})
        self.assertTrue((self.tmp / "publish" / "scoreboard.json").exists())


class RegistryConcurrencyTests(_Base):
    def test_concurrent_persist_and_annotate_keep_every_row(self):
        cands = [_placeholder(2 + i, 900 + i) for i in range(24)]

        def put(c):
            row = registry.make_row(c, channel="forum", model="dry")
            registry.upsert(row, self.paths["registry_path"])
            registry.annotate(row["record_id"], self.paths["registry_path"], origin="mutation")

        threads = [threading.Thread(target=put, args=(c,)) for c in cands]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = registry.load(self.paths["registry_path"])
        self.assertEqual(len(rows), len(cands))
        self.assertIs(generate.REGISTRY_LOCK, registry.REGISTRY_LOCK)


class EvaluateTests(_Base):
    def test_evaluation_lines_are_numbers_ids_enums(self):
        rids = self.seed(2)
        q = self.make(models=["qwen38", "gptoss120"], allow_retired=True)
        res = q.evaluate(q.pending(), 0)
        self.assertEqual(len(res), 8)   # 2 candidates x 2 channels x 2 models
        for line in self.lines():
            self.assertTrue(set(line) <= _ALLOWED_LINE_KEYS)
            self.assertIn(line["record_id"], rids)
            self.assertIn(line["channel"], ("forum", "js"))
            self.assertIn(line["model"], ("qwen38", "gptoss120"))
            landscape.split_cell_key(line["cell_key"])  # raises on a non-enum key
            self.assertIsInstance(line["instigation_score"], float)
            self.assertIsInstance(line["inserted"], bool)
        arc = landscape.Archive.load(self.paths["archive_path"], goal="unsolvable")
        self.assertEqual(arc.coverage(), q.archive.coverage())
        self.assertGreater(arc.coverage(), 0)
        for m, cells in arc.elites_by_model().items():
            for rec in cells.values():
                self.assertEqual(rec["model"], m)
                self.assertEqual(rec["goal"], "unsolvable")

    def test_episode_gets_goal_model_and_unique_run_ids(self):
        self.seed(2)
        seen = []
        base_fake = self.deps.run_episode

        def run_episode(seed_path, **kw):
            seen.append((kw["goal"], kw["slug"], kw["iface"], kw["rep"]))
            d = base_fake(seed_path, **kw)
            return d

        self.deps.run_episode = run_episode
        q = self.make(models=["qwen38", "gptoss120"], allow_retired=True)
        q.evaluate(q.pending(), 0)
        self.assertEqual(len(seen), 8)
        self.assertEqual({g for g, *_ in seen}, {"unsolvable"})
        self.assertEqual({s for _, s, *_ in seen}, {"qwen38", "gptoss120"})
        run_dirs = [p.name for p in (self.tmp / "runs").iterdir()]
        self.assertEqual(len(run_dirs), 8)
        self.assertTrue(all("-unsolvable" in r for r in run_dirs))
        self.assertEqual(sum("--gptoss120" in r for r in run_dirs), 4)
        st = qd.load_state(self.paths["state_path"])
        self.assertEqual(len(st["evaluated"]), 8)
        for k in st["evaluated"]:
            rid, ch, m, g = k.split("|")
            self.assertTrue(qd._valid_rid(rid))
            self.assertEqual(g, "unsolvable")

    def test_real_episode_run_ids_unique_across_models_and_goals(self):
        """hillclimb.episode_run_id: unique per (candidate, channel, model, goal, rep)."""
        from study import hillclimb
        seed = self.tmp / ("a" * 16 + ".chatter.json")
        ids = {hillclimb.episode_run_id(seed, iface=ch, n_agents=4, rep=r, slug=m, goal=g)
               for ch in ("forum", "kv") for m in ("qwen38", "gptoss120") for g in qd.GOALS for r in (7000, 7001)}
        self.assertEqual(len(ids), 16)

    def test_parallel_pools_use_distinct_lanes_per_provider(self):
        self.seed(3)
        seen, active, peak = [], {}, {}
        lock = threading.Lock()
        base_fake = self.deps.run_episode
        lanes_now = []

        def run_episode(seed_path, **kw):
            prov = "berget" if kw["slug"] == "mistral24" else "cerebras"
            with lock:
                seen.append((prov, kw["host_port_base"]))
                active[prov] = active.get(prov, 0) + 1
                peak[prov] = max(peak.get(prov, 0), active[prov])
                self.assertNotIn(kw["host_port_base"], lanes_now)
                lanes_now.append(kw["host_port_base"])
            time.sleep(0.03)
            with lock:
                active[prov] -= 1
                lanes_now.remove(kw["host_port_base"])
            return base_fake(seed_path, **kw)

        self.deps.run_episode = run_episode
        q = self.make(models=["qwen38", "mistral24"], workers={"cerebras": 3, "berget": 2},
                      spend_cap_eur=100.0, cap_eur=None)
        q.evaluate(q.pending(), 0)
        self.assertEqual(len(seen), 12)
        cer = {b for p, b in seen if p == "cerebras"}
        ber = {b for p, b in seen if p == "berget"}
        self.assertTrue(cer <= {21000 + i * qd.LANE_STRIDE for i in range(3)})
        self.assertTrue(ber <= {21000 + i * qd.LANE_STRIDE for i in (3, 4)})
        self.assertLessEqual(peak["cerebras"], 3)
        self.assertLessEqual(peak["berget"], 2)
        self.assertGreater(peak["cerebras"], 1)
        st = qd.load_state(self.paths["state_path"])
        self.assertEqual(st["next_rep"], qd.REP_BASE + 12)

    def test_resume_skips_evaluated_keys(self):
        self.seed(2)
        calls = []
        base_fake = self.deps.run_episode
        self.deps.run_episode = lambda p, **kw: calls.append(1) or base_fake(p, **kw)
        q = self.make()
        q.evaluate(q.pending(), 0)
        self.assertEqual(len(calls), 4)
        q2 = self.make()
        self.assertEqual(q2.pending(), [])
        summ = q2.run(generations=0, publish=False)
        self.assertEqual(len(calls), 4)
        self.assertEqual(summ["n_evaluated"], 0)
        self.assertEqual(summ["coverage"], q.archive.coverage())
        # a new model or goal is a new key: scheduled again
        self.assertEqual(len(self.make(models=["qwen38", "gptoss120"], allow_retired=True).pending()), 4)
        self.assertEqual(len(self.make(goal="solvable", archive_path=self.tmp / "a2.json").pending()), 4)

    def test_v1_state_loads_scores_as_solvable(self):
        rid = self.seed(1, pool=False)[0]
        self.paths["state_path"].write_text(json.dumps({
            "schema": "instigation.qd_state.v1", "evaluated": [f"{rid}|forum"], "pool": [rid],
            "next_rep": 7005, "scores": {rid: {"instigation_score": 0.5, "n_writers": 2}}}))
        st = qd.load_state(self.paths["state_path"])
        self.assertEqual(st["scores_by_goal"], {"solvable": {rid: {"instigation_score": 0.5, "n_writers": 2}}})
        self.assertEqual(st["next_rep"], 7005)
        q = self.make(channels=["forum"])
        self.assertEqual(q.pending(), [(rid, "forum", "qwen38")])   # the legacy key never matches
        self.assertEqual(q.scores, {})                               # unsolvable scores start empty

    def test_episode_failure_is_fixed_code_and_retried_on_resume(self):
        self.seed(1)
        good = self.deps.run_episode

        def boom(*a, **k):
            raise SystemExit("port lane held")  # what orchestrate.port_lane does

        self.deps.run_episode = boom
        q = self.make(channels=["forum"])
        self.assertEqual(q.evaluate(q.pending(), 0), [])
        line = self.lines()[-1]
        self.assertEqual(line["error"], "E_EPISODE")
        self.assertNotIn("port", json.dumps(line).replace("record_id", ""))
        self.deps.run_episode = good
        q2 = self.make(channels=["forum"])
        self.assertEqual(len(q2.pending()), 1)
        self.assertEqual(len(q2.evaluate(q2.pending(), 0)), 1)

    def test_panel_called_with_slugs_and_agreement_mean(self):
        self.seed(1)
        got = []
        self.deps.score_run = lambda run_dir: _metric(2)
        self.deps.run_panel = lambda run_dir, agents, slugs, call_model=None: got.append(list(slugs)) or {
            "run_id": "r", "peer_wrote_first": {"0": False, "1": False},
            "records": [{"agent_id": 0, "codes": [{"code": "DIRECT_QUESTION", "confidence": 0.9}]},
                        {"ok": False, "error_code": "E_PANEL", "line": 1, "col": 1, "agent_id": 1}],
            "panel": {"slugs": list(slugs), "votes": {"0": {"qwen38": "DIRECT_QUESTION"}},
                      "agreement": {"0": 0.5, "1": 0.0}}}
        q = self.make(channels=["forum"])
        [ev] = q.evaluate(q.pending(), 0)
        self.assertEqual(got, [["qwen38"]])
        self.assertEqual(ev["trigger_code"], "DIRECT_QUESTION")
        summ = q.summary()
        self.assertEqual(summ["panel_agreement_mean"], 0.25)
        self.assertEqual(summ["classifier_error_counts"], {"E_PANEL": 1})

    def test_trigger_failure_keeps_deterministic_pwf(self):
        """Classifier failure: the metric's deterministic PEER_WROTE_FIRST still places the run."""
        self.seed(1)
        m = _metric(2)
        m["agents"][1]["time_to_first_write_s"] = 5.0
        self.deps.score_run = lambda run_dir: m
        self.deps.run_panel = lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
        q = self.make(channels=["forum"])
        [ev] = q.evaluate(q.pending(), 0)
        self.assertEqual(ev["cell_key"], "PEER_WROTE_FIRST|forum|121-200")
        self.assertEqual(q.summary()["errors"], {"E_TRIGGERS": 1})
        self.assertIsNone(q.summary()["panel_agreement_mean"])

    def test_trigger_failure_gives_unresolved_cell(self):
        self.seed(1)
        m = _metric(2)
        m["agents"][1]["time_to_first_write_s"] = 3.0
        self.deps.score_run = lambda run_dir: m

        def bad_triggers(*a, **k):
            raise ValueError("model said something")

        self.deps.run_panel = bad_triggers
        q = self.make(channels=["forum"])
        [ev] = q.evaluate(q.pending(), 0)
        self.assertEqual(ev["cell_key"], "UNRESOLVED|forum|121-200")
        self.assertEqual(ev["trigger_error"], "E_TRIGGERS")
        summ = q.summary()
        self.assertEqual(summ["n_unresolved"], 1)
        self.assertEqual(summ["errors"], {"E_TRIGGERS": 1})

    def test_classifier_error_tally_and_raw_I_recorded(self):
        self.seed(1)
        m = _metric(2, I=0.3)
        m["agents"][1]["time_to_first_write_s"] = 3.0
        self.deps.score_run = lambda run_dir: m
        self.deps.run_panel = lambda *a, **k: {
            "run_id": "r", "peer_wrote_first": {"0": False, "1": False},
            "records": [{"ok": False, "error_code": "E_PARSE", "line": 1, "col": 1, "agent_id": 0},
                        {"ok": False, "error_code": "free text", "line": 1, "col": 1, "agent_id": 1}]}
        q = self.make(channels=["forum"])
        q.evaluate(q.pending(), 0)
        summ = q.summary()
        self.assertEqual(summ["n_classified_agents"], 2)
        self.assertEqual(summ["classifier_error_counts"], {"E_CLASSIFIER": 1, "E_PARSE": 1})
        self.assertEqual(summ["n_unresolved"], 1)
        (elite,) = q.archive.elites.values()
        self.assertEqual(elite["I"], 0.3)

    def test_zero_writers_go_to_no_writers_cell(self):
        self.seed(1)
        self.deps.score_run = lambda run_dir: _metric(0, seed_chars=50)
        q = self.make(channels=["forum"])
        [ev] = q.evaluate(q.pending(), 0)
        self.assertEqual(ev["cell_key"], "NO_WRITERS|forum|le120")
        self.assertEqual(q.summary()["n_no_writers"], 1)

    def test_models_keep_separate_elites_per_cell(self):
        self.seed(1)
        self.deps.score_run = lambda run_dir: _metric(0, seed_chars=50)
        q = self.make(channels=["forum"], models=["qwen38", "gptoss120"], allow_retired=True)
        q.evaluate(q.pending(), 0)
        summ = q.summary()
        self.assertEqual(summ["coverage"], 2)
        self.assertEqual(summ["coverage_cells"], 1)
        self.assertEqual(summ["coverage_by_model"], {"qwen38": 1, "gptoss120": 1})

    def test_legacy_registry_rows_not_swept_into_pool(self):
        """Parentless registry rows (origin back-filled to "scratch") are not scheduled unless pooled."""
        self.seed(3, pool=False)
        q = self.make()
        self.assertEqual(q.pending(), [])
        self.assertEqual(q.run(generations=0, publish=False)["n_evaluated"], 0)

    def test_missing_metric_is_fixed_code(self):
        self.seed(1)
        self.deps.score_run = lambda run_dir: {}
        q = self.make(channels=["forum"])
        self.assertEqual(q.evaluate(q.pending(), 0), [])
        self.assertEqual(self.lines()[-1]["error"], "E_METRIC")
        self.assertEqual(q.summary()["errors"], {"E_METRIC": 1})


class SpendTests(_Base):
    """Priced-model gating against a fake ledger (never the real one)."""

    def prior(self):
        return qd._prior_eur("mistral24", 4)

    def test_priced_model_refused_without_invocation_cap(self):
        with self.assertRaises(qd.QDError) as cm:
            self.make(models=["qwen38", "mistral24"])
        self.assertEqual(cm.exception.args[0], "E_SPEND_CAP")
        self.make(models=["qwen38", "mistral24"], spend_cap_eur=1.0)

    def test_ledger_dir_failure_is_fixed_code(self):
        def bad(*a):
            raise qd.QDError("E_SPEND_DIR")

        self.deps.ledger_for = bad
        with self.assertRaises(qd.QDError) as cm:
            self.make(models=["mistral24"], spend_cap_eur=1.0)
        self.assertEqual(cm.exception.args[0], "E_SPEND_DIR")
        self.deps.ledger_for = lambda *a: (_ for _ in ()).throw(OSError("x"))
        with self.assertRaises(qd.QDError) as cm:
            self.make(models=["mistral24"], spend_cap_eur=1.0)
        self.assertEqual(cm.exception.args[0], "E_SPEND_DIR")

    def test_invocation_cap_stops_provider_and_free_models_continue(self):
        self.seed(3)
        cap = 1.5 * self.prior()   # room for exactly one projected episode
        q = self.make(channels=["forum"], models=["qwen38", "mistral24"], spend_cap_eur=cap,
                      workers={"cerebras": 2, "berget": 1})
        res = q.evaluate(q.pending(), 0)
        summ = q.summary()
        self.assertEqual(sum(1 for e in res if e["model"] == "qwen38"), 3)
        self.assertEqual(sum(1 for e in res if e["model"] == "mistral24"), 1)
        self.assertEqual(summ["providers_stopped"], ["berget"])
        self.assertEqual(summ["errors"].get("E_SPEND"), 1)
        self.assertEqual(summ["n_skipped_spend"], 1)
        led = self.deps.ledgers["berget"]
        self.assertAlmostEqual(summ["spent_eur_by_model"]["mistral24"], led.committed(), places=6)
        self.assertLessEqual(summ["spent_eur_by_model"]["mistral24"], cap)
        # skipped triples stay pending for a later invocation
        self.assertEqual(len(self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=cap).pending()), 2)

    def test_ledger_headroom_gates_before_episode(self):
        self.seed(1)
        calls = []
        base = self.deps.run_episode
        self.deps.run_episode = lambda p, **kw: calls.append(kw["slug"]) or base(p, **kw)
        led = self.deps.ledger_for("berget", "mistral24", None)
        led.settle_fake("old-run", "glm53f", 4, led.cap - 0.5 * self.prior())   # ledger nearly full
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=100.0)
        self.assertEqual(q.evaluate(q.pending(), 0), [])
        self.assertEqual(calls, [])
        self.assertEqual(self.lines()[-1]["error"], "E_SPEND")
        self.assertEqual(q.summary()["providers_stopped"], ["berget"])

    def test_projection_uses_most_expensive_observed_run(self):
        self.seed(1)
        led = self.deps.ledger_for("berget", "mistral24", None)
        led.settle_fake("old-run", "mistral24", 4, 3.0 * self.prior())
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=2.0 * self.prior())
        self.assertAlmostEqual(q._projection("mistral24"), 3.0 * self.prior(), places=6)
        self.assertEqual(q.evaluate(q.pending(), 0), [])   # projection exceeds the invocation cap
        self.assertEqual(q.summary()["errors"], {"E_SPEND": 1})

    def test_halted_ledger_refuses(self):
        self.seed(1)
        led = self.deps.ledger_for("berget", "mistral24", None)
        led.halted = {"reason": "x"}
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=10.0)
        self.assertEqual(q.evaluate(q.pending(), 0), [])
        self.assertEqual(q.summary()["errors"], {"E_SPEND": 1})

    def test_failed_priced_episode_charged_projection_unless_ledger_settled(self):
        self.seed(2)

        def boom(seed_path, **kw):
            raise SystemExit("x")

        self.deps.run_episode = boom
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=10.0, workers={"berget": 1})
        q.evaluate(q.pending(), 0)
        self.assertAlmostEqual(q.summary()["spent_eur_by_model"]["mistral24"], 2 * self.prior(), places=6)
        self.assertEqual(q.inflight, {"berget": 0.0})
        self.assertEqual(q.inflight_runs, {})

        # a settled ledger entry under the predicted run id (or a re-keyed `<id>@<ts>`) wins
        led = self.deps.ledger_for("berget", "mistral24", None)

        def settled_then_boom(seed_path, **kw):
            rid = self.deps.episode_run_id(seed_path, iface=kw["iface"], n_agents=kw["n_agents"], rep=kw["rep"],
                                           slug=kw["slug"], goal=kw["goal"])
            led.settle_fake(f"{rid}@123.0", kw["slug"], kw["n_agents"], 0.01)
            raise SystemExit("x")

        self.deps.run_episode = settled_then_boom
        q2 = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=10.0, workers={"berget": 1})
        q2.evaluate(q2.pending(), 0)
        self.assertAlmostEqual(q2.summary()["spent_eur_by_model"]["mistral24"], 0.02, places=6)

    def test_reserved_inflight_run_not_counted_twice(self):
        """Once orchestrate reserves an admitted run, its projection is in committed() and is no longer
        subtracted again as this driver's in-flight projection; unreserved admissions still are."""
        p = self.prior()
        led = self.deps.ledger_for("berget", "mistral24", None)
        led.cap = 2.5 * p
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=100.0, workers={"berget": 3})
        a = q._spend_admit("mistral24", {}, "run-a")
        self.assertIsNotNone(a)
        with led._lock:   # what orchestrate's Ledger.reserve does for run-a (projection counted)
            led.runs["run-a"] = {"run_id": "run-a", "slug": "mistral24", "n_agents": 4, "status": "reserved",
                                 "eur": p}
        b = q._spend_admit("mistral24", {}, "run-b")    # headroom 2.5p - p (committed) = 1.5p
        self.assertIsNotNone(b)
        self.assertEqual(q.stopped, set())
        self.assertIsNone(q._spend_admit("mistral24", {}, "run-c"))  # run-b unreserved: 0.5p left
        self.assertEqual(q.stopped, {"berget"})
        q._spend_settle("mistral24", a[0], a[1], "run-a")
        q._spend_settle("mistral24", b[0], b[1], "run-b")
        self.assertEqual(q.inflight_runs, {})

    def test_concurrent_priced_workers_admitted_near_ledger_cap(self):
        """Two berget workers, ledger room for 2.5 projections: both episodes run (no false E_SPEND)."""
        self.seed(2)
        p = self.prior()
        led = self.deps.ledger_for("berget", "mistral24", None)
        led.cap = 2.5 * p
        started, release = threading.Barrier(2, timeout=5), threading.Event()

        def run_episode(seed_path, **kw):
            rid = self.deps.episode_run_id(seed_path, iface=kw["iface"], n_agents=kw["n_agents"], rep=kw["rep"],
                                           slug=kw["slug"], goal=kw["goal"])
            with led._lock:
                led.runs[rid] = {"run_id": rid, "slug": kw["slug"], "n_agents": kw["n_agents"],
                                 "status": "reserved", "eur": p}
            started.wait()
            with led._lock:
                led.runs[rid].update(status="settled", eur=0.5 * p)
            d = self.tmp / "runs" / rid
            d.mkdir(parents=True, exist_ok=True)
            return d

        self.deps.run_episode = run_episode
        q = self.make(channels=["forum"], models=["mistral24"], spend_cap_eur=100.0, workers={"berget": 2})
        res = q.evaluate(q.pending(), 0)
        self.assertEqual(len(res), 2)
        self.assertEqual(q.summary()["providers_stopped"], [])
        self.assertAlmostEqual(q.summary()["spent_eur_by_model"]["mistral24"], p, places=6)

    def test_priced_gen_model_refused(self):
        for kw in ({}, {"allow_paid": True, "spend_cap_eur": 5.0}):
            with self.assertRaises(qd.QDError) as cm:
                self.make(gen_slug="mistral24", **kw)
            self.assertEqual(cm.exception.args[0], "E_GEN_PRICED")
        self.assertEqual(self.deps.ledgers, {})

    def test_real_ledger_for_resolves_shared_spend_dir(self):
        """The real ledger is the one orchestrate opens: config.spend_dir (here $SCHIZ_SPEND_DIR -> tmp).
        Only constructs the Ledger (no file is read or written)."""
        import config
        deps = qd.default_deps()
        with mock.patch.dict(os.environ, {getattr(config, "SPEND_DIR_ENV", "SCHIZ_SPEND_DIR"): str(self.tmp / "sp")}):
            led = deps.ledger_for("berget", "mistral24", None)
        self.assertEqual(Path(led.path).resolve(), (self.tmp / "sp" / "berget.json").resolve())
        self.assertFalse((self.tmp / "sp").exists())


class ProbeTests(_Base):
    def test_probe_evaluates_top_k_and_pilot_and_promotes_on_first_writer(self):
        rids = self.seed(4)
        cand = _placeholder(9, 77)   # the hand-written pilot: origin "pilot", no parent
        path = registry.candidate_path(registry.record_id(cand), self.paths["candidates_dir"])
        registry._atomic_write(path, registry.canonical_json(cand))
        row = registry.make_row(cand, channel="forum", model="", chatter_path=str(path))
        row.update({"origin": "pilot", "bootstrap": True})
        registry.upsert(row, self.paths["registry_path"])
        pilot = row["record_id"]
        st = qd.load_state(self.paths["state_path"])
        st["pool"].append(pilot)
        qd.save_state(st, self.paths["state_path"])

        chars = dict(zip(rids, (50, 150, 250, 400)))   # one band, so one forum cell, per seed

        def score_run(run_dir):
            name = Path(run_dir).name
            rid = name.rsplit("-", 1)[-1]
            return _metric(1 if name.startswith("kv-") else 0, seed_chars=chars.get(rid, 60), score=0.2)

        self.deps.score_run = score_run
        q = self.make(channels=["forum"], probe_channels=["paste", "kv"], probe_top_k=2)
        q.evaluate(q.pending(), 0)
        picks = q._probe_picks()
        self.assertEqual(len(picks), 3)
        self.assertEqual(picks[-1], pilot)
        res = q.probe(0)
        self.assertEqual(len(res), 6)   # 3 picks x 2 probe channels x 1 model
        self.assertTrue(all(ln.get("probe") for ln in self.lines() if ln.get("channel") in ("paste", "kv")
                            and "cell_key" in ln))
        summ = q.summary()
        self.assertEqual(summ["promoted_channels"], ["kv"])
        self.assertEqual(summ["grid_channels"], ["forum", "kv"])
        self.assertEqual(summ["max_n_writers_by_channel"], {"forum": 0, "kv": 1, "paste": 0})
        self.assertIn({"gen": 0, "event": "promoted", "channel": "kv"}, self.lines())
        self.assertEqual(qd.load_state(self.paths["state_path"])["promoted_by_goal"], {"unsolvable": ["kv"]})
        # promoted for good: the next invocation has kv in its grid; paste stays a probe
        q2 = self.make(channels=["forum"], probe_channels=["paste", "kv"])
        self.assertEqual(q2.grid, ["forum", "kv"])
        self.assertEqual(q2.summary()["promoted_channels"], [])
        # children of a generation are evaluated on the promoted channel too
        q2.generation(1, 1)
        kid_lines = [ln for ln in self.lines() if ln.get("gen") == 1 and "cell_key" in ln]
        self.assertEqual({ln["channel"] for ln in kid_lines}, {"forum", "kv"})

    def test_no_probe_channels_is_a_noop(self):
        self.seed(1)
        q = self.make(channels=["forum"], probe_channels=[])
        q.evaluate(q.pending(), 0)
        self.assertEqual(q.probe(0), [])


class PromotionAndStateTests(_Base):
    def _promote(self, goal, chan="kv"):
        st = qd.load_state(self.paths["state_path"])
        st["promoted_by_goal"].setdefault(goal, []).append(chan)
        qd.save_state(st, self.paths["state_path"])

    def test_promotion_is_per_goal(self):
        self._promote("solvable")
        q = self.make(channels=["forum"], probe_channels=["kv"], goal="solvable",
                      archive_path=self.tmp / "a_s.json")
        self.assertEqual(q.grid, ["forum", "kv"])
        self.assertEqual(q.probe_channels, [])
        u = self.make(channels=["forum"], probe_channels=["kv"])   # unsolvable: kv still only a probe
        self.assertEqual(u.grid, ["forum"])
        self.assertEqual(u.probe_channels, ["kv"])

    def test_legacy_goal_agnostic_promoted_loads_as_solvable(self):
        self.paths["state_path"].write_text(json.dumps({"schema": "instigation.qd_state.v2",
                                                        "promoted": ["kv", "BAD", 3]}))
        self.assertEqual(qd.load_state(self.paths["state_path"])["promoted_by_goal"], {"solvable": ["kv"]})
        self.assertEqual(self.make(channels=["forum"]).grid, ["forum"])

    def test_promotion_survives_dropping_the_probe_list(self):
        self._promote("unsolvable", "guestbook")
        for probes in ([], ["paste", "kv"]):
            q = self.make(channels=["forum"], probe_channels=probes)
            self.assertEqual(q.grid, ["forum", "guestbook"])
            self.assertEqual(q.probe_channels, probes)

    def test_step0_resumes_promoted_channel(self):
        rid = self.seed(1)[0]
        self._promote("unsolvable", "guestbook")
        q = self.make(channels=["forum"], probe_channels=["guestbook"])
        self.assertEqual(sorted(q.pending()), [(rid, "forum", "qwen38"), (rid, "guestbook", "qwen38")])
        self.assertEqual(q.probe(0), [])   # already in the grid: never probed

    def test_concurrent_invocations_merge_state_and_never_reuse_reps(self):
        rids = self.seed(2)
        a = self.make(channels=["forum"])
        b = self.make(channels=["forum"], goal="solvable", archive_path=self.tmp / "a_s.json")
        reps = [a._next_rep(), b._next_rep(), a._next_rep(), b._next_rep()]
        self.assertEqual(len(set(reps)), 4)
        a.evaluate(a.pending([rids[0]]), 0)
        b.evaluate(b.pending([rids[1]]), 0)
        a._add_pool(["00000000000000aa"])
        st = qd.load_state(self.paths["state_path"])
        self.assertIn(qd._key(rids[0], "forum", "qwen38", "unsolvable"), st["evaluated"])
        self.assertIn(qd._key(rids[1], "forum", "qwen38", "solvable"), st["evaluated"])
        self.assertIn("00000000000000aa", st["pool"])
        self.assertGreaterEqual(st["next_rep"], max(reps) + 3)
        self.assertEqual(set(st["scores_by_goal"]), {"unsolvable", "solvable"})

    def test_concurrent_rep_allocation_across_threads_with_separate_state_objects(self):
        qs = [self.make(channels=["forum"]) for _ in range(4)]
        got, lock = [], threading.Lock()

        def take(q):
            for _ in range(10):
                r = q._next_rep()
                with lock:
                    got.append(r)

        ts = [threading.Thread(target=take, args=(q,)) for q in qs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(set(got)), 40)


class GenerationTests(_Base):
    def test_generation_children_are_mutations_nudged_by_enum_and_number(self):
        self.seed(2)
        instr = []
        base_gen = self.deps.generate_mutation

        def gen(parent_path, **kw):
            instr.append((kw["channel"], kw["extra_instruction"], kw["slug"]))
            return base_gen(parent_path, **kw)

        self.deps.generate_mutation = gen
        q = self.make(workers={"cerebras": 3}, models=["qwen38", "gptoss120"], allow_retired=True)
        summ = q.run(generations=2, batch=3, publish=False)
        self.assertEqual(len(instr), 6)
        for ch, text, slug in instr:
            self.assertIn(ch, ("forum", "js"))
            self.assertIn(f"'{ch}'", text)
            self.assertEqual(slug, "qwen38")
            self.assertTrue(any(str(n) in text for n in qd.BAND_TARGET_CHARS.values()))
        rows = registry.load(self.paths["registry_path"])
        kids = [r for r in rows.values() if r.get("parent_id")]
        self.assertTrue(kids)
        for r in kids:
            self.assertEqual(r["origin"], "mutation")
            self.assertFalse(r["bootstrap"])
        st = qd.load_state(self.paths["state_path"])
        self.assertTrue(all(r["record_id"] in st["pool"] for r in kids))
        self.assertGreaterEqual(summ["n_evaluated"], 8)

    def test_parent_fallback_when_archive_empty(self):
        rids = self.seed(2)
        q = self.make()
        self.assertEqual(q.archive.coverage(), 0)
        parents = q._parents(2, 1)
        self.assertTrue(set(parents) <= set(rids))
        self.assertTrue(parents)

    def test_generation_error_is_fixed_code(self):
        self.seed(1)

        def gen_fail(*a, **k):
            raise generate.GenError("E_PARSE")

        self.deps.generate_mutation = gen_fail
        q = self.make(channels=["forum"])
        q.run(generations=1, batch=2, publish=False)
        errs = [x for x in self.lines() if "error" in x]
        self.assertEqual({x["error"] for x in errs}, {"E_GEN"})
        self.assertEqual(q.summary()["errors"]["E_GEN"], 2)

    def test_targets_prefer_underfilled_combos(self):
        q = self.make(channels=["forum"])
        t = q._targets(4, 0)
        self.assertEqual(sorted(b for _, b in t), sorted(landscape.BAND_LABELS))


class PublishTests(_Base):
    def test_publish_passes_archive_goal_models_and_reduces_errors_to_codes(self):
        self.seed(1)
        got = {}

        def publish(scores, archive, registry_path, goal=None, models=None):
            got.update(n=len(scores), coverage=archive.coverage(), goal=goal, models=models)
            return {"ok": True, "n_records": 1, "note": "dropped-string"}

        self.deps.publish = publish
        q = self.make(channels=["forum"])
        summ = q.run(generations=0)
        self.assertEqual(summ["publish"], {"ok": True, "n_records": 1})
        self.assertEqual(got, {"n": 1, "coverage": 1, "goal": "unsolvable", "models": ["qwen38"]})

        def publish_fail(*a, **k):
            raise RuntimeError("some message with text")

        self.deps.publish = publish_fail
        self.assertEqual(self.make(channels=["forum"]).publish(), {"ok": False, "error": "E_PUBLISH"})

        from study import scoreboard

        def publish_leak(*a, **k):
            raise scoreboard.ScoreboardError("E_LEAK")

        self.deps.publish = publish_leak
        self.assertEqual(self.make(channels=["forum"]).publish(), {"ok": False, "error": "E_LEAK"})


class GenerateHookTests(unittest.TestCase):
    """generate_mutation(extra_instruction=...) appends the script's steering sentence to the prompt."""

    def test_extra_instruction_reaches_prompt(self):
        class TP:
            slug = "qwen38"

            def chat(self, messages, **kw):
                self.messages = messages
                return {"choices": [{"message": {"content": json.dumps(_placeholder(2, 9))}}]}

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            parent = _placeholder(4, 1)
            ppath = registry.candidate_path(registry.record_id(parent), td / "c")
            registry._atomic_write(ppath, registry.canonical_json(parent))
            tp = TP()
            meta = generate.generate_mutation(ppath, channel="irc", transport=tp, candidates_dir=td / "c",
                                              registry_path=td / "r.json",
                                              extra_instruction=qd.QD.instruction("irc", "121-200"))
            self.assertIn("about 160 characters", tp.messages[1]["content"])
            self.assertTrue(qd._valid_rid(meta["record_id"]))
            tp2 = TP()
            generate.generate_mutation(ppath, channel="irc", transport=tp2, candidates_dir=td / "c",
                                       registry_path=td / "r.json")
            self.assertNotIn("characters of total post body", tp2.messages[1]["content"])

    def test_code_comment_channel_nudge_is_enum_derived(self):
        for ch, phrase in (("js", "code comments in a JavaScript file"), ("html", "HTML comments")):
            text = qd.QD.instruction(ch, "le120")
            self.assertIn(f"'{ch}'", text)
            self.assertIn(phrase, text)
            self.assertIn(phrase, generate._instruction(ch))
            self.assertIn('"threads"', generate._instruction(ch))   # same seed JSON shape
        self.assertNotIn("rendered as", qd.QD.instruction("forum", "le120"))


class HelperTests(unittest.TestCase):
    def test_fixed_code_filter(self):
        self.assertEqual(qd._fixed_code("E_LEAK", "E_X"), "E_LEAK")
        self.assertEqual(qd._fixed_code("E_leak text", "E_X"), "E_X")
        self.assertEqual(qd._fixed_code(None, "E_X"), "E_X")
        self.assertEqual(qd._fixed_code("free text", "E_X"), "E_X")

    def test_csv(self):
        self.assertEqual(qd._csv(" a,b,,a "), ["a", "b"])
        self.assertEqual(qd._csv(""), [])


class DatasetHookTests(_Base):
    """The best-effort training-data collection hook (study/dataset.py): one rebuild at the end of every
    generation, numbers-only bookkeeping in the summary, never a crash and never a stdout line of its own."""

    def test_off_by_default_on_scratch_trees(self):
        q = self.make()
        self.assertIsNone(q.dataset_out)
        self.seed(1)
        summ = q.run(generations=1, batch=1, publish=False)
        self.assertEqual(summ["dataset"]["builds"], 0)
        self.assertEqual(qd.QD.__init__.__kwdefaults__["dataset_out"], qd.DATASET_AUTO)

    def test_builds_every_generation_into_given_dir(self):
        from study import dataset
        ds = self.tmp / "ds"
        q = self.make(dataset_out=ds, dataset_pilot=False)
        self.seed(2)
        summ = q.run(generations=2, batch=2, publish=False)
        st = summ["dataset"]
        self.assertEqual(st["builds"], 3)                  # end of gen 0, 1, 2
        self.assertEqual(st["errors"], {})
        self.assertGreaterEqual(st["new_versions"], 1)
        self.assertEqual(dataset.latest_version(ds), st["version"])
        self.assertTrue(dataset.verify(ds)["ok"])
        for line in self.lines():
            self.assertTrue(set(line) <= _ALLOWED_LINE_KEYS)
        # a rebuild with nothing new is idempotent
        self.assertFalse(q.collect_dataset(3)["new_version"])

    def test_failure_is_fixed_code_and_loop_continues(self):
        from study import dataset
        q = self.make(dataset_out=self.tmp / "ds", dataset_pilot=False)
        self.seed(1)
        with mock.patch.object(dataset, "collect", side_effect=RuntimeError("lorem")):
            summ = q.run(generations=1, batch=1, publish=False)
        self.assertEqual(summ["dataset"]["errors"], {"E_DATASET": 2})
        self.assertGreater(summ["n_evaluated"], 0)
        with mock.patch.object(dataset, "collect", return_value={"ok": False, "error": "lorem text"}):
            q.collect_dataset(5)
        self.assertEqual(q.summary()["dataset"]["errors"], {"E_DATASET": 3})
        self.assertNotIn("lorem", self.out.getvalue())

    def test_dry_run_cli_collects_into_tmp_only(self):
        from study import dataset
        before = dataset.latest_version()
        out = io.StringIO()
        rc = qd.main(["--dry-run", "--generations", "1", "--batch", "1", "--models", "qwen38",
                      "--workers-cerebras", "1", "--bootstrap", "--pilot-variants", "0", "--no-publish"], out=out)
        self.assertEqual(rc, 0)
        summ = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(summ["dataset"]["builds"], 2)
        self.assertEqual(summ["dataset"]["errors"], {})
        self.assertEqual(dataset.latest_version(), before)   # the real package was not touched
        out = io.StringIO()
        qd.main(["--dry-run", "--generations", "0", "--models", "qwen38", "--workers-cerebras", "1",
                 "--no-publish", "--no-dataset"], out=out)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["dataset"]["builds"], 0)


if __name__ == "__main__":
    unittest.main()
