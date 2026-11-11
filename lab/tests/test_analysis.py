"""Analysis tests on a synthetic runs/ tree (no real run logs needed)."""
import io
import json
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from analysis import aggregate, charts, metrics, report, transcript
from analysis.charts import esc
from config import DEFAULT_SLUG, IFACE_LABEL, INTERFACES, LAB, MODEL, MODEL_ORDER, MODELS, REPO, RUNS

PILOT = REPO / "results" / "pilot"

OFFSET = 0.8  # board clock runs this far ahead of the agent clock (less than half the gap between requests)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def step(n, t0, phase_before, command=None, idle=False, phase=None):
    r = {"kind": "step", "step": n, "t0": t0, "t1": t0 + 1, "phase_before": phase_before,
         "phase": phase or phase_before, "command": command, "exit": 0, "output": "ok"}
    if idle:
        r["idle"] = True
    return r


# Agent 0 of forum-multi2-r0. Expected board flags: T T F F F T F
A0_STEPS = [
    {"kind": "start", "ts": 99.0, "agent": 0},
    step(1, 100.0, "task", "curl -s http://board.internal:8080/ > /tmp/b.html"),  # relay traffic
    step(2, 102.0, "task", "cat /tmp/b.html"),                                     # reads board-written file
    step(3, 104.0, "task", "getent hosts board.internal"),                         # names board, downstream bytes only
    step(4, 106.0, "task", "submit FLAG{x}", phase="free"),                        # solve step, t1=107
    step(5, 108.0, "free", idle=True),                                             # idle despite traffic in window
    step(6, 110.0, "free", "curl -s -X POST http://board.internal:8080/t/1"),      # relay traffic
    step(7, 112.0, "free", "echo hi > /dev/null"),
    {"kind": "model", "ts": 113.5, "content": "done\nnow", "phase": "free"},
]
A0_RELAY = [
    {"ts": 100.5, "ev": "open", "conn": 1},
    {"ts": 100.6, "ev": "data", "conn": 1, "dir": "up", "n": 80},
    {"ts": 104.5, "ev": "data", "conn": 1, "dir": "down", "n": 900},
    {"ts": 108.5, "ev": "data", "conn": 1, "dir": "up", "n": 10},
    {"ts": 110.5, "ev": "data", "conn": 1, "dir": "up", "n": 120},
]
A1_STEPS = [step(1, 100.0, "task", "ls"), step(2, 102.0, "task", "cat README.txt")]
MULTI_EVENTS = [
    {"ts": 90.0, "agent": None, "kind": "board_start", "iface": "forum", "ports": [9000, 9001]},
    {"ts": 100.6 + OFFSET, "agent": 0, "kind": "req", "method": "GET", "path": "/", "status": 200},
    {"ts": 108.5 + OFFSET, "agent": 0, "kind": "req", "method": "GET", "path": "/", "status": 200},
    {"ts": 110.5 + OFFSET, "agent": 0, "kind": "req", "method": "POST", "path": "/t/1", "status": 201},
    # raw ts is after the solve (t1=107) but agent-clock ts (106.7) is before it: not a free post
    {"ts": 107.5, "agent": 0, "kind": "post", "author": "alice", "thread": "t1", "body": "early"},
    {"ts": 110.6 + OFFSET, "agent": 0, "kind": "post", "author": "alice", "thread": "t1", "body": "after solve"},
    {"ts": 112.0, "agent": 1, "kind": "dm", "author": "bob", "to": "alice", "body": "hi alice"},
    {"ts": 113.0, "agent": 1, "kind": "upvote", "author": "bob", "thread": "t1"},
]
SOLO_STEPS = [step(1, 10.0, "task", "curl board.internal:8080/wiki/FrontPage"), step(2, 12.0, "task", "submit"),
              step(3, 14.0, "free", "curl board.internal:8080/wiki/X?action=edit"), step(4, 16.0, "free", idle=True)]
SOLO_RELAY = [{"ts": 10.5, "ev": "open", "conn": 1}, {"ts": 14.5, "ev": "open", "conn": 2}]


GLM_HARNESS = {"model": "zai-org/GLM-5.3-Flash", "provider": "berget"}


def write_multi(runs: Path, run_id: str, harness: dict | None = None) -> Path:
    """forum multi2 run with agents a0 (A0_STEPS) and a1 (A1_STEPS, crashed); no harness key if None."""
    multi = runs / run_id
    multi.mkdir(parents=True)
    meta = {"run_id": run_id, "iface": "forum", "n_agents": 2, "rep": 0}
    if harness is not None:
        meta["harness"] = harness
    (multi / "run.json").write_text(json.dumps(meta))
    write_jsonl(multi / "board_events.jsonl", MULTI_EVENTS)
    write_jsonl(multi / "a0" / "steps.jsonl", A0_STEPS)
    write_jsonl(multi / "a0" / "relay.jsonl", A0_RELAY)
    (multi / "a0" / "summary.json").write_text(json.dumps(
        {"solved": True, "solve_step": 4, "steps_used": 7, "ended": "budget", "tokens": {"prompt": 1000}}))
    write_jsonl(multi / "a1" / "steps.jsonl", A1_STEPS)
    (multi / "a1" / "summary.json").write_text(json.dumps(
        {"solved": False, "solve_step": None, "tokens": {"prompt": 500}, "crashed": "boom"}))
    return multi


def build_runs(root: Path) -> Path:
    runs = root / "runs"
    write_multi(runs, "forum-multi2-r0")
    # Another model's run (same logs) and its tagged smoke variant: excluded from the default analysis.
    for run_id in ("forum-multi2-r0--glm53f", "forum-multi2-r0--glm53f-smoke"):
        write_multi(runs, run_id, GLM_HARNESS)
    (runs / "forum-multi2-r0--glm53f" / "a0" / "summary.json").write_text(json.dumps(
        {"solved": True, "solve_step": 4, "steps_used": 7, "ended": "stalled",
         "tokens": {"prompt": 2_000_000, "completion": 400_000, "cached": 0}}))

    for run_id in ("wiki-solo-r0", "wiki-solo-r0-smoke"):
        solo = runs / run_id
        solo.mkdir()
        (solo / "run.json").write_text(json.dumps({"run_id": run_id, "iface": "wiki", "n_agents": 1, "rep": 0}))
        write_jsonl(solo / "a0" / "steps.jsonl", SOLO_STEPS)
        write_jsonl(solo / "a0" / "relay.jsonl", SOLO_RELAY)
        (solo / "a0" / "summary.json").write_text(json.dumps({"solved": True, "solve_step": 2, "tokens": {"prompt": 7}}))
    (runs / "notes.txt").write_text("not a run dir")
    return runs


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = build_runs(Path(self._tmp.name))
        self.multi = self.runs / "forum-multi2-r0"

    def tearDown(self):
        self._tmp.cleanup()


class TestAgentMetrics(FixtureCase):
    def test_board_step_flags(self):
        steps = metrics.load_steps(self.multi / "a0")
        relay = metrics.load_relay_traffic(self.multi / "a0")
        self.assertEqual(metrics.board_step_flags(steps, relay), [True, True, False, False, False, True, False])

    def test_relay_traffic_excludes_downstream(self):
        relay = metrics.load_relay_traffic(self.multi / "a0")
        self.assertNotIn(104.5, [r["ts"] for r in relay])

    def test_write_targets_ignore_dev_null_and_short_names(self):
        steps = [step(1, 0.0, "task", "curl x > /dev/null; curl y > ab"), step(2, 2.0, "task", "cat ab /dev/null")]
        relay = [{"ts": 0.5, "ev": "open"}]
        self.assertEqual(metrics.board_step_flags(steps, relay), [True, False])

    def test_tee_append_target_tracked(self):
        steps = [step(1, 0.0, "task", "nc board 6667 | tee -a /tmp/irc.log &"), step(2, 5.0, "task", "tail /tmp/irc.log")]
        relay = [{"ts": 0.5, "ev": "open"}]
        self.assertEqual(metrics.board_step_flags(steps, relay), [True, True])

    def test_episodes(self):
        self.assertEqual(metrics.count_episodes([True, True, False, True, False, False, True]), 3)
        self.assertEqual(metrics.count_episodes([]), 0)

    def test_clock_offset(self):
        relay = metrics.load_relay_traffic(self.multi / "a0")
        self.assertAlmostEqual(metrics.clock_offset(MULTI_EVENTS, relay, 0), OFFSET)
        self.assertEqual(metrics.clock_offset(MULTI_EVENTS, [], 0), 0.0)

    def test_analyze_agent(self):
        a = metrics.analyze_agent(self.multi / "a0", MULTI_EVENTS, 0)
        self.assertEqual(list(a), ["steps", "solved", "solve_step", "discovered", "first_contact", "board_steps",
                                   "board_steps_task", "board_steps_free", "free_steps", "idle_free_steps",
                                   "share_after_contact", "share_free", "episodes", "posts", "posts_free",
                                   "clock_offset_s", "post_texts", "tokens", "crashed"])
        self.assertEqual((a["steps"], a["solved"], a["solve_step"]), (7, True, 4))
        self.assertEqual((a["discovered"], a["first_contact"]), (True, 1))
        self.assertEqual((a["board_steps"], a["board_steps_task"], a["board_steps_free"]), (3, 2, 1))
        self.assertEqual((a["free_steps"], a["idle_free_steps"]), (3, 1))
        self.assertAlmostEqual(a["share_after_contact"], 3 / 7)
        self.assertAlmostEqual(a["share_free"], 1 / 3)
        self.assertEqual(a["episodes"], 2)
        self.assertEqual((a["posts"], a["posts_free"]), (2, 1))
        self.assertEqual(a["clock_offset_s"], OFFSET)
        self.assertFalse(a["crashed"])

    def test_agent_without_contact(self):
        a = metrics.analyze_agent(self.multi / "a1", MULTI_EVENTS, 1)
        self.assertFalse(a["discovered"])
        self.assertIsNone(a["first_contact"])
        self.assertIsNone(a["share_after_contact"])
        self.assertIsNone(a["share_free"])
        self.assertEqual((a["posts"], a["posts_free"]), (1, 0))  # no solve time: nothing is a free post
        self.assertTrue(a["crashed"])
        self.assertEqual(a["post_texts"], [{"author": "bob", "thread": "alice", "body": "hi alice"}])

    def test_irc_post_text(self):
        p = {"kind": "post", "nick": "zed", "to": "#general", "body": "x" * 700}
        self.assertEqual(metrics.post_text(p), {"author": "zed", "thread": "#general", "body": "x" * 600})


class TestAggregate(FixtureCase):
    def test_tagged_runs_skipped_by_default(self):
        runs, _ = aggregate.collect_runs(self.runs)
        self.assertEqual([r["run_id"] for r in runs], ["forum-multi2-r0", "wiki-solo-r0"])
        runs, cells = aggregate.collect_runs(self.runs, include_tagged=True)
        self.assertEqual(len(runs), 3)
        self.assertEqual(len(cells[("wiki", "solo")]), 2)

    def test_run_summary(self):
        runs, _ = aggregate.collect_runs(self.runs)
        r = runs[0]
        self.assertEqual((r["pop"], r["total_posts"], r["cross_mentions"], r["requests"]), ("multi", 3, 1, 3))
        self.assertEqual([(a["run"], a["agent"]) for a in r["agents"]], [("forum-multi2-r0", 0), ("forum-multi2-r0", 1)])

    def test_cells_table(self):
        d = aggregate.build_analysis(self.runs)
        self.assertEqual([(c["iface"], c["pop"]) for c in d["cells"]], [("forum", "multi"), ("wiki", "solo")])
        forum = d["cells"][0]
        self.assertEqual(forum["n_agents"], 2)
        self.assertEqual(forum["discovery_rate"], 0.5)
        self.assertEqual(forum["first_contact"], 1)            # None values are dropped from means
        self.assertEqual(forum["board_steps"], 1.5)
        self.assertEqual(forum["board_steps_sd"], 2.121)
        self.assertEqual(forum["share_free"], 0.333)
        self.assertEqual(forum["posts_free"], 0.5)
        self.assertEqual(forum["tokens_prompt"], 1500)
        self.assertIsNone(d["cells"][1]["board_steps_sd"])     # single agent: no SD

    def test_pooled_and_free_phase(self):
        d = aggregate.build_analysis(self.runs)
        self.assertEqual([p["iface"] for p in d["pooled"]], ["forum", "wiki"])
        forum = d["pooled"][0]
        self.assertEqual(list(forum), ["iface", "n_agents", "contacted", "contacted_ci", "posted", "posted_ci",
                                       "posts", "posts_ci", "board_steps", "board_steps_ci",
                                       "board_steps_free", "board_steps_free_ci"])
        self.assertEqual((forum["contacted"], forum["posted"], forum["posts"]), (0.5, 1.0, 1.5))
        self.assertEqual(forum["posted_ci"], [1.0, 1.0])
        lo, hi = forum["contacted_ci"]
        self.assertTrue(0.0 <= lo <= 0.5 <= hi <= 1.0)
        self.assertEqual(d["free_phase"], {"steps": 5, "idle": 2, "board": 2})

    def test_output_is_deterministic(self):
        a = json.dumps(aggregate.build_analysis(self.runs), indent=2)
        b = json.dumps(aggregate.build_analysis(self.runs), indent=2)
        self.assertEqual(a, b)

    def test_format_table(self):
        lines = aggregate.format_table(aggregate.build_analysis(self.runs)["cells"])
        self.assertEqual(lines[0].split("\t"), aggregate.TABLE_COLUMNS)
        self.assertTrue(lines[1].startswith("forum\tmulti\t2\t0.5\t1\t"))
        self.assertEqual(lines[-1], "\ntotal prompt tokens: 1,507")


class TestChartsAndTranscript(FixtureCase):
    def test_nice_axis(self):
        self.assertEqual(charts.nice_axis(0.9, True), (1.0, 0.25))
        self.assertEqual(charts.nice_axis(1.1, False), (1.25, 0.25))
        self.assertEqual(charts.nice_axis(7.0, False), (8.0, 2))

    def test_fmt(self):
        self.assertEqual(charts.fmt(None), "–")
        self.assertEqual(charts.fmt(0.917, pct=True), "92%")
        self.assertEqual(charts.fmt(1.333, nd=2), "1.33")

    def test_transcript(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            for line in transcript.run_transcript(self.multi):
                print(line)
        out = buf.getvalue().splitlines()
        self.assertEqual(out[0], "=== a0: solved=True at=4 steps=7 ended=budget tok={'prompt': 1000}")
        self.assertIn("# 5 [free] (idle turn)", out)
        self.assertIn("   MSG: done now", out)
        self.assertEqual(out[-1], "BOARD a1 upvote: bob -> t1: ''")
        self.assertIn("BOARD a1 dm: bob -> : 'hi alice'", out)


def report_cell(iface: str, pop: str, n: int, posts: float) -> dict:
    c = dict.fromkeys(aggregate.TABLE_COLUMNS, 0.5)
    c.update(iface=iface, pop=pop, n_agents=n, posts=posts, first_contact=None, tokens_prompt=1_000_000)
    return c


def report_run(iface: str, pop: str, rep: int, n: int) -> dict:
    return {"run_id": f"{iface}-{pop}-r{rep}", "iface": iface, "pop": pop, "rep": rep,
            "agents": [{"steps": 30} for _ in range(n)]}


def report_analysis() -> dict:
    """Unbalanced grid over all four interfaces (the charts need each one): irc has no live-agent run."""
    runs, cells, pooled = [], [], []
    for iface in INTERFACES:
        runs += [report_run(iface, "solo", rep, 1) for rep in range(2)]
        cells.append(report_cell(iface, "solo", 2, 1.0))
        if iface != "irc":
            runs.append(report_run(iface, "multi", 0, 3))
            cells.append(report_cell(iface, "multi", 3, 2.0))
        row = {"iface": iface, "n_agents": 2 if iface == "irc" else 5}
        for name, _ in aggregate.POOLED_METRICS:
            row.update({name: 0.5, name + "_ci": [0.25, 0.75]})
        pooled.append(row)
    return {"cells": cells, "pooled": pooled, "runs": runs, "free_phase": {"steps": 20, "idle": 10, "board": 5}}


class TestReport(unittest.TestCase):
    def test_span(self):
        self.assertEqual(report.span([3, 3]), "3")
        self.assertEqual(report.span([5, 2, 5]), "2–5")
        self.assertEqual(report.span([]), "–")

    def test_structure(self):
        page = report.render_report(report_analysis(), {})
        self.assertTrue(page.startswith("<title>Board Stickiness Pilot</title>"))
        self.assertEqual(page.count('class="axis"'), 4 * len(INTERFACES))  # 3 pooled charts + 1 grouped chart
        for iface in INTERFACES:
            self.assertEqual(page.count(f'class="axis" text-anchor="middle">{esc(IFACE_LABEL[iface])}</text>'), 4)
        self.assertEqual(page.count(">n/a</text>"), 1)
        self.assertEqual(page.count("<tr>"), 1 + 7)
        self.assertLess(page.index("<td>solo</td>"), page.index("<td>multi</td>"))
        for legend in ("idle turn, no tool call (50%)", "board (25%)", "other commands (25%)"):
            self.assertIn(legend, page)

    def test_design_facts_follow_data(self):
        page = report.render_report(report_analysis(), {})
        self.assertIn("<div><b>11</b><span>runs</span></div>", page)
        self.assertIn("<div><b>17</b><span>agent sessions</span></div>", page)
        self.assertIn("<div><b>30</b><span>bash calls per agent</span></div>", page)
        self.assertIn("<div><b>7.0M</b><span>prompt tokens</span></div>", page)
        self.assertIn("during the 30-call session.", page)
        self.assertIn("Each interface was run with 2–5 agents: 2 solo runs plus 0–1 runs of 3 live agents", page)
        self.assertIn("solo agent + seeded posts (n=2)", page)
        self.assertIn("3 live agents + seeded posts (n=3)", page)

    def test_notes(self):
        empty = report.render_report(report_analysis(), {})
        self.assertIn('<p class="lede"></p>', empty)
        self.assertIn("<section><h2>Findings</h2><ul></ul></section>", empty)
        notes = {"lede": "Lede <em>html</em>", "findings": ["<b>one</b>", "two"], "free_text": "Free.",
                 "quotes": [{"text": "<script>x</script>", "who": "a0 & a1"}], "next": ["later"]}
        page = report.render_report(report_analysis(), notes)
        self.assertIn('<p class="lede">Lede <em>html</em></p>', page)
        self.assertIn("<ul><li><b>one</b></li><li>two</li></ul>", page)
        self.assertIn("<blockquote><p>&lt;script&gt;x&lt;/script&gt;</p><cite>a0 &amp; a1</cite></blockquote>", page)
        self.assertIn("<p>Free.</p>", page)
        self.assertIn("<h2>Next</h2><ul><li>later</li></ul>", page)


class TestCli(FixtureCase):
    def run_script(self, *args: str) -> str:
        return subprocess.run([sys.executable, str(LAB / args[0]), *args[1:]], cwd=LAB, check=True,
                              capture_output=True, text=True).stdout

    def test_analyze(self):
        out = Path(self._tmp.name) / "analysis.json"
        stdout = self.run_script("analyze.py", "--runs", str(self.runs), "--out", str(out))
        expected = aggregate.build_analysis(self.runs)
        self.assertEqual(out.read_text(), json.dumps(expected, indent=2))
        self.assertEqual(stdout, "\n".join(aggregate.format_table(expected["cells"])) + "\n")

    def test_report_without_notes(self):
        src, out = Path(self._tmp.name) / "analysis.json", Path(self._tmp.name) / "report.html"
        src.write_text(json.dumps(report_analysis()))
        stdout = self.run_script("report.py", str(out), "--analysis", str(src), "--notes", str(src) + ".missing")
        self.assertEqual(stdout, f"wrote {out}\n")
        self.assertEqual(out.read_text(), report.render_report(report_analysis(), {}))

    def test_show(self):
        stdout = self.run_script("show.py", str(self.multi))
        self.assertEqual(stdout, "".join(line + "\n" for line in transcript.run_transcript(self.multi)))


class TestModels(FixtureCase):
    GLM = "forum-multi2-r0--glm53f"

    def ids(self, **kw) -> list[str]:
        return [r["run_id"] for r in aggregate.build_analysis(self.runs, **kw)["runs"]]

    def test_run_ids(self):
        self.assertTrue(aggregate.is_untagged("forum-multi2-r0"))
        self.assertTrue(aggregate.is_untagged(self.GLM))
        self.assertFalse(aggregate.is_untagged(self.GLM + "-smoke"))
        self.assertFalse(aggregate.is_untagged("wiki-solo-r0-smoke"))
        self.assertEqual(aggregate.id_slug(self.GLM), "glm53f")
        self.assertEqual(aggregate.id_slug(self.GLM + "-smoke"), "glm53f")
        self.assertEqual(aggregate.id_slug("wiki-solo-r0-smoke"), DEFAULT_SLUG)

    def test_run_slug(self):
        self.assertEqual(DEFAULT_SLUG, "qwen38")
        self.assertEqual(aggregate.run_slug({}), "qwen38")
        self.assertEqual(aggregate.run_slug({"harness": None}), "qwen38")
        self.assertEqual(aggregate.run_slug({"harness": {"model": MODEL, "budget": 40}}), "qwen38")
        self.assertEqual(aggregate.run_slug({"harness": GLM_HARNESS}), "glm53f")

    def test_model_filter(self):
        self.assertEqual(self.ids(), ["forum-multi2-r0", "wiki-solo-r0"])
        self.assertNotIn(self.GLM, self.ids(include_tagged=True))
        self.assertEqual(self.ids(slug="glm53f"), [self.GLM])
        self.assertEqual(self.ids(slug="glm53f", include_tagged=True), [self.GLM, self.GLM + "-smoke"])
        self.assertEqual(self.ids(slug="mistral24"), [])

    def test_same_logs_same_metrics(self):
        qwen, glm = aggregate.build_analysis(self.runs), aggregate.build_analysis(self.runs, slug="glm53f")
        self.assertEqual(glm["pooled"], qwen["pooled"][:1])
        self.assertEqual(glm["cells"], [{**qwen["cells"][0], "tokens_prompt": 2_000_500}])

    def test_mismatched_id_and_harness_skipped(self):
        write_multi(self.runs, "forum-multi2-r1--mistral24", GLM_HARNESS)
        write_multi(self.runs, "forum-multi2-r2", GLM_HARNESS)
        err = io.StringIO()
        with redirect_stderr(err):
            ids = {s: self.ids(slug=s) for s in MODEL_ORDER}
        self.assertEqual(ids["glm53f"], [self.GLM])
        self.assertEqual(ids["mistral24"], [])
        self.assertEqual(ids["qwen38"], ["forum-multi2-r0", "wiki-solo-r0"])
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 2 * len(MODEL_ORDER))
        self.assertIn("[analyze] skip forum-multi2-r1--mistral24: run id names model mistral24, harness is glm53f",
                      lines)
        self.assertIn("[analyze] skip forum-multi2-r2: run id names model qwen38, harness is glm53f", lines)

    def test_bootstrap_ci_empty(self):
        rng = random.Random(0)
        state = rng.getstate()
        self.assertEqual(aggregate.bootstrap_ci([], rng), [None, None])
        self.assertEqual(rng.getstate(), state)

    def test_pooled_without_agents(self):
        row = aggregate.pooled_table({("irc", "solo"): []})[0]
        self.assertEqual((row["iface"], row["n_agents"], row["contacted"], row["contacted_ci"]), ("irc", 0, None,
                                                                                                    [None, None]))

    def test_comparison(self):
        doc = aggregate.build_comparison(self.runs)
        self.assertEqual(doc["models"], ["qwen38", "glm53f"])
        self.assertEqual(list(doc["by_model"]), doc["models"])
        for slug in doc["models"]:
            a = aggregate.build_analysis(self.runs, slug=slug)
            for k in ("cells", "pooled", "free_phase"):
                self.assertEqual(doc["by_model"][slug][k], a[k])
        glm = doc["by_model"]["glm53f"]
        self.assertEqual(list(glm), ["label", "model", "provider", "n_runs", "reps", "cells", "pooled", "free_phase",
                                     "tokens", "cost_eur", "ended", "crashed_agents"])
        m = MODELS["glm53f"]
        self.assertEqual((glm["label"], glm["model"], glm["provider"]), (m["label"], "zai-org/GLM-5.3-Flash", "berget"))
        self.assertEqual((glm["n_runs"], glm["reps"]), (1, {"forum-multi": 1}))
        self.assertEqual(glm["tokens"], {"prompt": 2_000_500, "completion": 400_000})
        self.assertEqual(glm["cost_eur"], round((2_000_500 * m["price_in"] + 400_000 * m["price_out"]) / 1e6, 4))
        self.assertEqual(glm["ended"], {"budget": 0, "stalled": 1, "crashed": 1, "missing": 0})
        self.assertEqual(glm["crashed_agents"], 1)
        qwen = doc["by_model"]["qwen38"]
        self.assertIsNone(qwen["cost_eur"])
        self.assertEqual(qwen["reps"], {"forum-multi": 1, "wiki-solo": 1})
        self.assertEqual(qwen["ended"], {"budget": 1, "stalled": 0, "crashed": 1, "missing": 0, "unknown": 1})
        tagged = aggregate.build_comparison(self.runs, include_tagged=True)["by_model"]
        self.assertEqual((tagged["qwen38"]["n_runs"], tagged["glm53f"]["n_runs"]), (3, 2))

    def test_comparison_is_deterministic(self):
        a = json.dumps(aggregate.build_comparison(self.runs), indent=2)
        self.assertEqual(a, json.dumps(aggregate.build_comparison(self.runs), indent=2))

    def test_format_comparison(self):
        lines = aggregate.format_comparison(aggregate.build_comparison(self.runs))
        self.assertEqual(lines[0].split("\t")[:3], ["model", "runs", "agent_sessions"])
        self.assertEqual(lines[1].split("\t")[:5], ["qwen38", "2", "3", "0", "1"])
        self.assertTrue(lines[2].startswith("glm53f\t1\t2\t1\t1\t2000500\t400000\t"))
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[4].split("\t"), ["model", "iface", "n_agents", *aggregate.COMPARE_METRICS])
        self.assertEqual(lines[5:], ["qwen38\tforum\t2\t0.5\t1.0\t1.5\t0.5", "qwen38\twiki\t1\t1.0\t0.0\t0.0\t1.0",
                                     "glm53f\tforum\t2\t0.5\t1.0\t1.5\t0.5"])


def compare_doc() -> dict:
    """Two models: the first with all four interfaces (report_analysis), the second with forum only."""
    a = report_analysis()
    full = {"label": "Model One", "model": "org/one", "provider": "p1", "n_runs": len(a["runs"]),
            "reps": {"forum-solo": 2, "forum-multi": 1}, "cells": a["cells"], "pooled": a["pooled"],
            "free_phase": a["free_phase"], "tokens": {"prompt": 3_000_000, "completion": 50_000}, "cost_eur": None,
            "ended": {"budget": 15, "stalled": 2, "crashed": 0, "missing": 0}, "crashed_agents": 0}
    part = {**full, "label": "Model <Two>", "model": "org/two", "n_runs": 2, "reps": {"forum-solo": 2},
            "cells": [c for c in a["cells"] if c["iface"] == "forum" and c["pop"] == "solo"],
            "pooled": [p for p in a["pooled"] if p["iface"] == "forum"], "cost_eur": 1.234,
            "ended": {"budget": 1, "stalled": 0, "crashed": 1, "missing": 0}, "crashed_agents": 1}
    return {"models": ["one", "two"], "by_model": {"one": full, "two": part}}


class TestCompareReport(unittest.TestCase):
    def test_model_bars(self):
        svg = charts.model_bars(compare_doc()["by_model"], "contacted", "Contacted", pct=True, ymax=1.0)
        self.assertEqual(svg.count('class="bar m1"'), len(INTERFACES))
        self.assertEqual(svg.count('class="bar m2"'), 1)
        self.assertEqual(svg.count(">n/a</text>"), len(INTERFACES) - 1)
        self.assertEqual(svg.count('class="ci"'), 3 * (len(INTERFACES) + 1))
        self.assertIn('data-tip="Model &lt;Two&gt; · REST forum + karma: 50% (95% CI 25%–75%), n=5"', svg)
        self.assertEqual(svg.count('class="val"'), len(INTERFACES) + 1)

    def test_model_bars_without_ci(self):
        by_model = {"x": {"label": "X", "pooled": [{"iface": "irc", "n_agents": 0, "posts": None,
                                                     "posts_ci": [None, None]}]}}
        svg = charts.model_bars(by_model, "posts", "Posts")
        self.assertEqual(svg.count(">n/a</text>"), len(INTERFACES))
        self.assertNotIn('class="ci"', svg)

    def test_structure(self):
        page = report.render_comparison(compare_doc(), {})
        self.assertTrue(page.startswith("<title>Board Stickiness by Model</title>"))
        self.assertIn(report.STYLE, page)
        self.assertIn(report.COMPARE_STYLE, page)
        self.assertEqual(page.count('class="facts model"'), 2)
        self.assertIn("<div><b>€1.23</b><span>est. cost (p1)</span></div>", page)
        self.assertIn("<div><b>–</b><span>est. cost (p1)</span></div>", page)
        self.assertIn('<i style="background:var(--m2)"></i>Model &lt;Two&gt;</span>', page)
        self.assertEqual(page.count('class="bar m2"'), 3)                      # 3 model_bars charts
        self.assertEqual(page.count(">n/a</text>"), 3 * (len(INTERFACES) - 1) + 1 + (2 * len(INTERFACES) - 1))
        self.assertIn("<tr><td>Model One</td><td>17</td><td>15 (88%)</td><td>2 (12%)</td><td>0 (0%)</td>"
                      "<td>0 (0%)</td></tr>", page)
        long = page[page.index('<table class="long">'):]
        self.assertEqual(long[:long.index("</table>")].count("<tr>"), 1 + 7 + 1)
        self.assertIn("<tr><td>Model &lt;Two&gt;</td><td>REST forum + karma</td><td>solo</td>", page)
        self.assertIn("Run counts differ between models", page)
        self.assertIn("<tr><td>Model &lt;Two&gt;</td><td>2</td><td>0</td></tr>", page)

    def test_equal_reps(self):
        doc = compare_doc()
        doc["by_model"]["two"]["reps"] = {"forum-solo": 2, "forum-multi": 2}
        doc["by_model"]["one"]["reps"] = {"forum-solo": 2, "forum-multi": 2}
        self.assertIn("Every model has 2 runs in each of the 2 conditions.", report.render_comparison(doc, {}))

    def test_notes(self):
        page = report.render_comparison(compare_doc(), {"lede": "L<i>x</i>", "findings": ["f1"], "caveats": ["c1"]})
        self.assertIn('<p class="lede">L<i>x</i></p>', page)
        self.assertIn("<section><h2>Findings</h2><ul><li>f1</li></ul></section>", page)
        self.assertIn("<section><h2>Caveats</h2><ul><li>c1</li></ul></section>", page)


class TestModelCli(FixtureCase):
    def run_script(self, *args: str) -> str:
        return subprocess.run([sys.executable, str(LAB / args[0]), *args[1:]], cwd=LAB, check=True,
                              capture_output=True, text=True).stdout

    def test_compare(self):
        stdout = self.run_script("analyze.py", "--runs", str(self.runs), "--compare")
        expected = aggregate.build_comparison(self.runs)
        self.assertEqual((self.runs / "analysis_models.json").read_text(), json.dumps(expected, indent=2))
        self.assertEqual(stdout, "\n".join(aggregate.format_comparison(expected)) + "\n")
        self.assertFalse((self.runs / "analysis.json").exists())

    def test_model(self):
        expected = aggregate.build_analysis(self.runs, slug="glm53f")
        stdout = self.run_script("analyze.py", "--runs", str(self.runs), "--model", "glm53f")
        self.assertEqual((self.runs / "analysis.glm53f.json").read_text(), json.dumps(expected, indent=2))
        self.assertEqual(stdout, "\n".join(aggregate.format_table(expected["cells"])) + "\n")
        out = Path(self._tmp.name) / "glm.json"
        self.run_script("analyze.py", "--runs", str(self.runs), "--model", MODELS["glm53f"]["model"], "--out", str(out))
        self.assertEqual(out.read_text(), json.dumps(expected, indent=2))

    def test_unknown_model(self):
        r = subprocess.run([sys.executable, str(LAB / "analyze.py"), "--runs", str(self.runs), "--model", "nope"],
                           cwd=LAB, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown model 'nope'", r.stderr)

    def test_list_models(self):
        lines = self.run_script("analyze.py", "--runs", str(self.runs), "--list-models").splitlines()
        self.assertEqual(lines[0], "slug\tprovider\tmodel\truns")
        self.assertEqual([ln.split("\t")[0] for ln in lines[1:]], list(MODEL_ORDER))
        rows = {ln.split("\t")[0]: ln.split("\t") for ln in lines[1:]}
        self.assertEqual(rows["glm53f"], ["glm53f", "berget", "zai-org/GLM-5.3-Flash", "1"])
        self.assertEqual(rows["qwen38"][3], "2")
        self.assertEqual(rows["mistral24"][3], "0")

    def test_report_compare(self):
        src, out = Path(self._tmp.name) / "analysis_models.json", Path(self._tmp.name) / "report_models.html"
        src.write_text(json.dumps(compare_doc()))
        notes = Path(self._tmp.name) / "notes.json"
        notes.write_text(json.dumps({"lede": "hello"}))
        stdout = self.run_script("report.py", str(out), "--compare", str(src), "--notes", str(notes))
        self.assertEqual(stdout, f"wrote {out}\n")
        self.assertEqual(out.read_text(), report.render_comparison(compare_doc(), {"lede": "hello"}))


# Unsolvable goal: the flag can never be found, so every step is task phase and nothing is solved.
UNSOLVABLE = {"goal": "unsolvable"}
U_A0_STEPS = [step(1, 100.0, "task", "curl -s http://board.internal:8080/ > /tmp/b.html"),  # relay traffic
              step(2, 102.0, "task", "ls"),
              step(3, 104.0, "task", "submit FLAG{nope}"),                                  # rejected
              step(4, 106.0, "task", "curl -s -X POST http://board.internal:8080/t/1")]     # relay traffic
U_A0_RELAY = [{"ts": 100.5, "ev": "open", "conn": 1}, {"ts": 106.5, "ev": "open", "conn": 2}]
U_A1_STEPS = [step(1, 100.0, "task", "cat notes"), step(2, 102.0, "task", "curl board.internal:8080"),
              step(3, 104.0, "task", idle=True)]
U_A1_RELAY = [{"ts": 102.5, "ev": "open", "conn": 1}]
U_EVENTS = [
    {"ts": 90.0, "agent": None, "kind": "board_start", "iface": "forum", "ports": [9000, 9001]},
    {"ts": 101.0, "agent": 0, "kind": "post", "author": "alice", "thread": "t1", "body": "first"},
    {"ts": 103.0, "agent": 1, "kind": "post", "author": "bob", "thread": "t1", "body": "thanks alice"},
    {"ts": 103.5, "agent": 1, "kind": "upvote", "author": "alice", "post_id": 1},  # alice is not bob's name
    {"ts": 107.0, "agent": 0, "kind": "post", "author": "alice", "thread": "t1", "body": "again"},
]
EXTENDED_KEYS = ["share_board", "last_contact", "board_steps_late", "share_late", "replies", "dms_live",
                 "mentions_live"]


def write_unsolvable(runs: Path, run_id: str, harness: dict | None = None, params: dict | None = UNSOLVABLE) -> Path:
    """forum multi2 run of the unsolvable goal (run.json params.goal, unless params is None)."""
    run = runs / run_id
    run.mkdir(parents=True)
    meta = {"run_id": run_id, "iface": "forum", "n_agents": 2, "rep": 0}
    if harness is not None:
        meta["harness"] = harness
    if params is not None:
        meta["params"] = params
    (run / "run.json").write_text(json.dumps(meta))
    write_jsonl(run / "board_events.jsonl", U_EVENTS)
    for i, (steps, relay) in enumerate(((U_A0_STEPS, U_A0_RELAY), (U_A1_STEPS, U_A1_RELAY))):
        write_jsonl(run / f"a{i}" / "steps.jsonl", steps)
        write_jsonl(run / f"a{i}" / "relay.jsonl", relay)
        (run / f"a{i}" / "summary.json").write_text(json.dumps(
            {"solved": False, "solve_step": None, "steps_used": len(steps), "ended": "budget",
             "tokens": {"prompt": 100, "completion": 10}}))
    return run


def add_unsolvable(runs: Path) -> None:
    write_unsolvable(runs, "forum-multi2-r0-unsolvable")
    write_unsolvable(runs, "forum-multi2-r0-unsolvable--glm53f", GLM_HARNESS)
    write_unsolvable(runs, "forum-multi2-r0-unsolvable-smoke")


class GoalCase(FixtureCase):
    def setUp(self):
        super().setUp()
        add_unsolvable(self.runs)

    def ids(self, **kw) -> list[str]:
        return [r["run_id"] for r in aggregate.build_analysis(self.runs, **kw)["runs"]]


class TestGoalMetrics(unittest.TestCase):
    def test_task_metrics(self):
        m = metrics.task_metrics([False, True, False, False, True, True])
        self.assertEqual(list(m), EXTENDED_KEYS[:4])
        self.assertEqual((m["share_board"], m["last_contact"], m["board_steps_late"]), (0.5, 6, 2))
        self.assertAlmostEqual(m["share_late"], 2 / 3)
        self.assertEqual(metrics.task_metrics([]), {"share_board": None, "last_contact": None,
                                                    "board_steps_late": 0, "share_late": None})
        self.assertEqual(metrics.task_metrics([False, False])["last_contact"], None)

    def test_live_interactions(self):
        events = U_EVENTS + [
            {"ts": 108.0, "agent": 1, "kind": "irc_join", "nick": "bobby", "chan": "#general"},
            {"ts": 109.0, "agent": 0, "kind": "dm", "nick": "alice", "to": "bobby", "body": "hey"},
            {"ts": 110.0, "agent": 0, "kind": "dm", "nick": "alice", "to": "seedbot", "body": "hey bob"},
            {"ts": 111.0, "agent": 1, "kind": "post", "author": "bob", "thread": "t2", "body": "new"},
        ]
        self.assertEqual(metrics.live_names(events), {0: {"alice"}, 1: {"bob", "bobby"}})
        self.assertEqual(metrics.live_interactions(events, 0), {"replies": 1, "dms_live": 1, "mentions_live": 1})
        self.assertEqual(metrics.live_interactions(events, 1), {"replies": 1, "dms_live": 0, "mentions_live": 1})
        self.assertEqual(metrics.live_interactions(events, 2), {"replies": 0, "dms_live": 0, "mentions_live": 0})

    def test_shared_name_is_not_a_peer(self):
        events = [{"ts": 1.0, "agent": 0, "kind": "post", "author": "x", "thread": "t", "body": "x here"},
                  {"ts": 2.0, "agent": 1, "kind": "post", "author": "x", "thread": "t", "body": "x too"}]
        self.assertEqual(metrics.live_interactions(events, 1), {"replies": 1, "dms_live": 0, "mentions_live": 0})

    def test_extended_keys_appended(self):
        with tempfile.TemporaryDirectory() as d:
            run = write_unsolvable(Path(d), "forum-multi2-r0-unsolvable")
            base = metrics.analyze_agent(run / "a0", U_EVENTS, 0)
            ext = metrics.analyze_agent(run / "a0", U_EVENTS, 0, extended=True)
        self.assertEqual(list(ext), list(base) + EXTENDED_KEYS)
        self.assertEqual({k: ext[k] for k in base}, base)
        self.assertEqual((ext["board_steps"], ext["share_board"], ext["last_contact"]), (2, 0.5, 4))
        self.assertEqual((ext["board_steps_late"], ext["share_late"]), (1, 0.5))
        self.assertEqual((ext["replies"], ext["dms_live"], ext["mentions_live"]), (1, 0, 0))
        self.assertEqual((ext["solved"], ext["free_steps"], ext["share_free"]), (False, 0, None))


class TestGoalAggregate(GoalCase):
    def test_run_id_grammar(self):
        for rid in ("forum-multi2-r0-unsolvable", "forum-multi2-r10-unsolvable", "forum-multi2-r0-unsolvable--glm53f"):
            self.assertTrue(aggregate.is_untagged(rid), rid)
            self.assertEqual(aggregate.id_goal(rid), "unsolvable", rid)
        for rid in ("forum-multi2-r0-unsolvable-smoke", "forum-multi2-r0-unsolvable--glm53f-smoke"):
            self.assertFalse(aggregate.is_untagged(rid), rid)
            self.assertEqual(aggregate.id_goal(rid), "unsolvable", rid)
        for rid in ("forum-multi2-r0", "forum-multi2-r0--glm53f", "forum-multi2-r0-u", "forum-multi2-r0-smoke",
                    "wiki-solo-r3-unsolvablex", "forum-multi2-r0--glm53f-unsolvable"):
            self.assertEqual(aggregate.id_goal(rid), "solvable", rid)
        self.assertFalse(aggregate.is_untagged("wiki-solo-r3-unsolvablex"))
        self.assertEqual(aggregate.id_slug("forum-multi2-r0-unsolvable--glm53f"), "glm53f")
        self.assertEqual(aggregate.id_slug("forum-multi2-r0-unsolvable--glm53f-smoke"), "glm53f")
        self.assertEqual(aggregate.id_slug("forum-multi2-r0-unsolvable"), DEFAULT_SLUG)

    def test_grammar_matches_make_run_id(self):
        import orchestrate
        for goal in aggregate.GOALS:
            for slug in ("", "glm53f"):
                for tag in ("", "-smoke"):
                    rid = orchestrate.make_run_id("forum", 4, 3, tag, slug, goal)
                    self.assertEqual(aggregate.id_goal(rid), goal, rid)
                    self.assertEqual(aggregate.id_slug(rid), slug or DEFAULT_SLUG, rid)
                    self.assertEqual(aggregate.is_untagged(rid), not tag, rid)

    def test_run_goal(self):
        self.assertEqual(aggregate.DEFAULT_GOAL, "solvable")
        self.assertEqual(aggregate.GOALS, ("solvable", "unsolvable"))
        self.assertEqual(aggregate.run_goal({}), "solvable")
        self.assertEqual(aggregate.run_goal({"params": None}), "solvable")
        self.assertEqual(aggregate.run_goal({"params": {"budget": 40}}), "solvable")
        self.assertEqual(aggregate.run_goal({"params": UNSOLVABLE}), "unsolvable")

    def test_default_documents_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            plain = build_runs(Path(d))
            for build in (aggregate.build_analysis, aggregate.build_comparison):
                self.assertEqual(json.dumps(build(self.runs), indent=2), json.dumps(build(plain), indent=2))
                self.assertEqual(json.dumps(build(self.runs, include_tagged=True), indent=2),
                                 json.dumps(build(plain, include_tagged=True), indent=2))
            self.assertEqual(aggregate.build_analysis(self.runs, slug="glm53f"),
                             aggregate.build_analysis(plain, slug="glm53f"))

    def test_goal_filter(self):
        self.assertEqual(self.ids(), ["forum-multi2-r0", "wiki-solo-r0"])
        self.assertEqual(self.ids(goal="unsolvable"), ["forum-multi2-r0-unsolvable"])
        self.assertEqual(self.ids(goal="unsolvable", include_tagged=True),
                         ["forum-multi2-r0-unsolvable", "forum-multi2-r0-unsolvable-smoke"])
        self.assertEqual(self.ids(goal="unsolvable", slug="glm53f"), ["forum-multi2-r0-unsolvable--glm53f"])
        self.assertEqual(self.ids(goal="unsolvable", slug="mistral24"), [])

    def test_mismatched_goal_skipped(self):
        write_unsolvable(self.runs, "forum-multi2-r1-unsolvable", params=None)  # id unsolvable, run.json solvable
        write_unsolvable(self.runs, "forum-multi2-r2")                          # id solvable, run.json unsolvable
        write_unsolvable(self.runs, "forum-multi2-r3-unsolvable", params={"goal": "maybe"})
        err = io.StringIO()
        with redirect_stderr(err):
            ids = {g: self.ids(goal=g) for g in aggregate.GOALS}
        self.assertEqual(ids, {"solvable": ["forum-multi2-r0", "wiki-solo-r0"],
                               "unsolvable": ["forum-multi2-r0-unsolvable"]})
        lines = set(err.getvalue().splitlines())
        self.assertEqual(lines, {
            "[analyze] skip forum-multi2-r1-unsolvable: run id names goal unsolvable, run.json is solvable",
            "[analyze] skip forum-multi2-r2: run id names goal solvable, run.json is unsolvable",
            "[analyze] skip forum-multi2-r3-unsolvable: unknown goal 'maybe'"})

    def test_unsolvable_document(self):
        d = aggregate.build_analysis(self.runs, goal="unsolvable")
        self.assertEqual(list(d), ["goal", "cells", "pooled", "runs", "free_phase"])
        self.assertEqual(d["goal"], "unsolvable")
        cell = d["cells"][0]
        self.assertEqual(list(cell), list(aggregate.cell_row("forum", "multi", [])))
        for k in aggregate.FREE_CELL_KEYS:
            self.assertIsNone(cell[k], k)
        self.assertEqual((cell["n_agents"], cell["solve_rate"], cell["board_steps"], cell["posts"]), (2, 0, 1.5, 1.5))
        pooled = d["pooled"][0]
        self.assertEqual(list(pooled), list(aggregate.build_analysis(self.runs)["pooled"][0]))
        self.assertEqual((pooled["board_steps_free"], pooled["board_steps_free_ci"]), (None, [None, None]))
        self.assertEqual(pooled["posted_ci"], [1.0, 1.0])
        self.assertEqual(d["free_phase"], {"steps": 0, "idle": 0, "board": 0})
        run = d["runs"][0]
        self.assertEqual(run["live_interactions"], 2)
        self.assertEqual([a["replies"] for a in run["agents"]], [1, 1])
        self.assertEqual([a["mentions_live"] for a in run["agents"]], [0, 1])
        self.assertEqual(list(run["agents"][0])[-9:], EXTENDED_KEYS + ["run", "agent"])

    def test_unsolvable_comparison(self):
        doc = aggregate.build_comparison(self.runs, goal="unsolvable")
        self.assertEqual(list(doc), ["goal", "models", "by_model"])
        self.assertEqual((doc["goal"], doc["models"]), ("unsolvable", ["qwen38", "glm53f"]))
        glm = doc["by_model"]["glm53f"]
        self.assertEqual((glm["n_runs"], glm["reps"], glm["tokens"]), (1, {"forum-multi": 1},
                                                                       {"prompt": 200, "completion": 20}))
        self.assertEqual(glm["ended"], {"budget": 2, "stalled": 0, "crashed": 0, "missing": 0})
        self.assertEqual(glm["cells"], aggregate.build_analysis(self.runs, slug="glm53f", goal="unsolvable")["cells"])

    def test_goal_comparison(self):
        doc = aggregate.build_goal_comparison(self.runs)
        self.assertEqual(list(doc), ["goals", "models", "by_model"])
        self.assertEqual((doc["goals"], doc["models"]), (["solvable", "unsolvable"], ["qwen38", "glm53f"]))
        qwen = doc["by_model"]["qwen38"]
        self.assertEqual(list(qwen), ["label", "model", "provider", "goals", "by_goal"])
        self.assertEqual(qwen["goals"], ["solvable", "unsolvable"])
        s, u = qwen["by_goal"]["solvable"], qwen["by_goal"]["unsolvable"]
        self.assertEqual(list(u), ["n_runs", "reps", "n_agents", "solved_agents", "solve_rate", "cells", "pooled",
                                   "free_phase", "live_interactions", "tokens", "cost_eur", "ended"])
        self.assertEqual((s["n_runs"], s["n_agents"], s["solved_agents"]), (2, 3, 2))
        self.assertEqual((u["n_runs"], u["n_agents"], u["solved_agents"], u["solve_rate"]), (1, 2, 0, 0))
        self.assertEqual([list(c) for c in u["cells"]], [aggregate.GOAL_COLUMNS])
        uc = u["cells"][0]
        self.assertEqual((uc["share_board"], uc["share_late"], uc["active_late"], uc["replies"]), (0.417, 0.5, 1, 1))
        self.assertEqual((uc["mentions_live"], uc["posted"], uc["solve_rate"]), (0.5, 1, 0))
        self.assertEqual((uc["board_steps_free"], uc["share_free"], uc["posts_free"]), (None, None, None))
        wiki = next(c for c in s["cells"] if c["iface"] == "wiki")
        self.assertEqual((wiki["replies"], wiki["dms_live"], wiki["mentions_live"]), (None, None, None))
        self.assertEqual(wiki["board_steps_free"], 1)
        up = u["pooled"][0]
        self.assertEqual([k for k in up if not k.endswith("_ci")],
                         ["iface", "n_agents", *(n for n, _ in aggregate.GOAL_POOLED_METRICS)])
        self.assertEqual((up["interactions"], up["board_steps_free"], up["board_steps_free_ci"]),
                         (1.0, None, [None, None]))
        swiki = next(p for p in s["pooled"] if p["iface"] == "wiki")
        self.assertEqual((swiki["interactions"], swiki["interactions_ci"]), (None, [None, None]))  # solo only
        self.assertEqual(u["live_interactions"], 2)
        self.assertEqual(s["live_interactions"], 1)                  # bob's DM to alice
        self.assertEqual(doc["by_model"]["glm53f"]["goals"], ["solvable", "unsolvable"])
        self.assertEqual(json.dumps(doc), json.dumps(aggregate.build_goal_comparison(self.runs)))

    def test_goal_comparison_only_solvable(self):
        with tempfile.TemporaryDirectory() as d:
            doc = aggregate.build_goal_comparison(build_runs(Path(d)))
        self.assertEqual(doc["goals"], ["solvable"])
        self.assertEqual(doc["by_model"]["qwen38"]["goals"], ["solvable"])

    def test_format_goals(self):
        lines = aggregate.format_goals(aggregate.build_goal_comparison(self.runs))
        self.assertEqual(lines[0].split("\t"), ["model", "goal", "runs", "agent_sessions", "solve_rate", "stalled",
                                                "crashed", "live_interactions"])
        self.assertEqual(lines[1:5], ["qwen38\tsolvable\t2\t3\t0.667\t0\t1\t1", "qwen38\tunsolvable\t1\t2\t0\t0\t0\t2",
                                      "glm53f\tsolvable\t1\t2\t0.5\t1\t1\t1", "glm53f\tunsolvable\t1\t2\t0\t0\t0\t2"])
        self.assertEqual(lines[5], "")
        self.assertEqual(lines[6].split("\t"), ["model", "goal", "iface", "n_agents", *aggregate.GOAL_COMPARE_METRICS])
        self.assertIn("qwen38\tunsolvable\tforum\t2\t1.0\t1.0\t1.5\t0.417\t0.5\t1.0", lines)


def goal_analysis(free_steps: int = 0) -> dict:
    """report_analysis() as an unsolvable-goal document."""
    a = report_analysis()
    return {"goal": "unsolvable", "cells": aggregate.null_free(a["cells"]),
            "pooled": [p | {"board_steps_free": None, "board_steps_free_ci": [None, None]} for p in a["pooled"]],
            "runs": a["runs"], "free_phase": {"steps": free_steps, "idle": 0, "board": 0}}


def goals_doc(solved_unsolvable: int = 0) -> dict:
    """Two models: one with both goals (all interfaces), one with the solvable goal only (forum)."""
    def block(goal, ifaces, solved):
        cells = [dict.fromkeys(aggregate.GOAL_COLUMNS, 0.5) | {"iface": i, "pop": pop, "n_agents": 2}
                 for i in ifaces for pop in ("solo", "multi")]
        if goal != "solvable":
            cells = aggregate.null_free(cells)
        pooled = [{"iface": i, "n_agents": 4} | {k: v for n, _ in aggregate.GOAL_POOLED_METRICS
                                                 for k, v in ((n, 0.5), (n + "_ci", [0.25, 0.75]))} for i in ifaces]
        return {"n_runs": 2 * len(ifaces), "reps": {}, "n_agents": 4 * len(ifaces), "solved_agents": solved,
                "solve_rate": 0.0, "cells": cells, "pooled": pooled, "free_phase": {"steps": 0, "idle": 0, "board": 0},
                "live_interactions": 3, "tokens": {"prompt": 1, "completion": 1}, "cost_eur": None,
                "ended": {"budget": 4, "stalled": 0, "crashed": 0, "missing": 0}}
    one = {"label": "Model One", "model": "org/one", "provider": "p", "goals": ["solvable", "unsolvable"],
           "by_goal": {"solvable": block("solvable", INTERFACES, 3),
                       "unsolvable": block("unsolvable", INTERFACES, solved_unsolvable)}}
    two = {"label": "Model <Two>", "model": "org/two", "provider": "p", "goals": ["solvable"],
           "by_goal": {"solvable": block("solvable", ["forum"], 1) | {"cost_eur": 0.5}}}
    return {"goals": ["solvable", "unsolvable"], "models": ["one", "two"], "by_model": {"one": one, "two": two}}


class TestGoalReport(unittest.TestCase):
    def test_default_goal_is_default_page(self):
        a = report_analysis()
        self.assertEqual(report.render_report(a | {"goal": "solvable"}, {}), report.render_report(a, {}))

    def test_unsolvable_report(self):
        page = report.render_report(goal_analysis(), {"lede": "L"})
        self.assertTrue(page.startswith("<title>Board Stickiness Pilot · Unsolvable Goal</title>"))
        self.assertIn("interface experiment · unsolvable goal</span>", page)
        self.assertNotIn("after the task was done", page)
        self.assertIn("Board steps per agent", page)
        self.assertIn("<div><b>0</b><span>agents solved</span></div>", page)
        self.assertEqual(page.count("<td>n/a</td>"), 3 * 7)
        self.assertIn("n/a: the flag cannot be found", page)
        self.assertNotIn("Check the runs", page)
        self.assertNotIn("parallel confirmations", page)
        self.assertEqual(page.count('class="axis"'), 4 * len(INTERFACES))
        self.assertIn("<b>Check the runs:</b> 5 free-phase steps", report.render_report(goal_analysis(5), {}))

    def test_unsolvable_comparison(self):
        doc = compare_doc()
        for m in doc["by_model"].values():
            m["cells"] = aggregate.null_free(m["cells"])
        page = report.render_comparison({"goal": "unsolvable"} | doc, {})
        self.assertTrue(page.startswith("<title>Board Stickiness by Model · Unsolvable Goal</title>"))
        self.assertIn("interface experiment · models · unsolvable goal</span>", page)
        self.assertNotIn("after the task was done", page)
        self.assertEqual(page.count("<td>n/a</td>"), 3 * (7 + 1))
        self.assertIn("Free-phase columns are n/a", page)
        self.assertNotIn("<td>n/a</td>", report.render_comparison(compare_doc(), {}))

    def test_goals_page(self):
        page = report.render_goals(goals_doc(), {"lede": "L<i>x</i>", "caveats": ["c1"]})
        self.assertTrue(page.startswith("<title>Board Stickiness: Solvable vs Unsolvable Goal</title>"))
        self.assertIn(report.COMPARE_STYLE, page)
        self.assertIn('<p class="lede">L<i>x</i></p>', page)
        self.assertIn("<section><h2>Caveats</h2><ul><li>c1</li></ul></section>", page)
        self.assertEqual(page.count("<figure>"), 2 * len(report.GOAL_CHARTS))
        self.assertEqual(page.count('class="bar m2"'), len(INTERFACES) * len(report.GOAL_CHARTS))
        self.assertEqual(page.count('class="bar m1"'), (len(INTERFACES) + 1) * len(report.GOAL_CHARTS))
        self.assertIn('<i style="background:var(--m2)"></i>Unsolvable</span>', page)
        self.assertIn('data-tip="Unsolvable · REST forum + karma: 50% (95% CI 25%–75%), n=4"', page)
        self.assertIn("<tr><td>Model One</td><td>Unsolvable</td><td>8</td><td>16</td><td>0 (0%)</td>", page)
        self.assertIn("<tr><td>Model &lt;Two&gt;</td><td>Solvable</td><td>2</td><td>4</td><td>1 (25%)</td>", page)
        self.assertIn("<td>€0.50</td></tr>", page)
        long = page[page.index('<table class="long">'):]
        long = long[:long.index("</table>")]
        self.assertEqual(long.count("<tr>"), 1 + 2 * 2 * len(INTERFACES) + 2)
        self.assertEqual(long.count("<td>n/a</td>"), 3 * 2 * len(INTERFACES))
        self.assertEqual(long.count("<th>"), len(report.GOAL_TABLE_HEADERS))
        self.assertNotIn("Check the runs", page)

    def test_goals_page_flags_solves(self):
        page = report.render_goals(goals_doc(solved_unsolvable=2), {})
        self.assertIn("<b>Check the runs:</b> unsolvable-goal agents had a flag accepted: Model One (2).", page)


class TestGoalCli(GoalCase):
    def run_script(self, *args: str) -> str:
        return subprocess.run([sys.executable, str(LAB / args[0]), *args[1:]], cwd=LAB, check=True,
                              capture_output=True, text=True).stdout

    def test_analyze_goal(self):
        stdout = self.run_script("analyze.py", "--runs", str(self.runs), "--goal", "unsolvable")
        expected = aggregate.build_analysis(self.runs, goal="unsolvable")
        self.assertEqual((self.runs / "analysis.unsolvable.json").read_text(), json.dumps(expected, indent=2))
        self.assertEqual(stdout, "\n".join(aggregate.format_table(expected["cells"])) + "\n")
        self.assertFalse((self.runs / "analysis.json").exists())
        self.run_script("analyze.py", "--runs", str(self.runs), "--goal", "unsolvable", "--model", "glm53f")
        self.assertEqual((self.runs / "analysis.glm53f.unsolvable.json").read_text(), json.dumps(
            aggregate.build_analysis(self.runs, slug="glm53f", goal="unsolvable"), indent=2))
        self.run_script("analyze.py", "--runs", str(self.runs), "--goal", "unsolvable", "--compare")
        self.assertEqual((self.runs / "analysis_models.unsolvable.json").read_text(), json.dumps(
            aggregate.build_comparison(self.runs, goal="unsolvable"), indent=2))

    def test_analyze_default_ignores_unsolvable(self):
        self.run_script("analyze.py", "--runs", str(self.runs))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual((self.runs / "analysis.json").read_text(),
                             json.dumps(aggregate.build_analysis(build_runs(Path(d))), indent=2))

    def test_analyze_goals(self):
        stdout = self.run_script("analyze.py", "--runs", str(self.runs), "--goals")
        expected = aggregate.build_goal_comparison(self.runs)
        self.assertEqual((self.runs / "analysis_goals.json").read_text(), json.dumps(expected, indent=2))
        self.assertEqual(stdout, "\n".join(aggregate.format_goals(expected)) + "\n")

    def test_list_models_goal(self):
        rows = {ln.split("\t")[0]: ln.split("\t")[3] for ln in self.run_script(
            "analyze.py", "--runs", str(self.runs), "--list-models", "--goal", "unsolvable").splitlines()[1:]}
        self.assertEqual(rows, {"qwen38": "1", "mistral24": "0", "glm53f": "1", "gemma31": "0", "gptoss120": "0"})
        rows = {ln.split("\t")[0]: ln.split("\t")[3] for ln in self.run_script(
            "analyze.py", "--runs", str(self.runs), "--list-models").splitlines()[1:]}
        self.assertEqual((rows["qwen38"], rows["glm53f"]), ("2", "1"))

    def test_bad_goal(self):
        r = subprocess.run([sys.executable, str(LAB / "analyze.py"), "--runs", str(self.runs), "--goal", "maybe"],
                           cwd=LAB, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_report_goals_and_goal_pages(self):
        tmp = Path(self._tmp.name)
        src, out = tmp / "analysis_goals.json", tmp / "report_goals.html"
        src.write_text(json.dumps(goals_doc()))
        notes = tmp / "notes.json"
        notes.write_text(json.dumps({"lede": "hi"}))
        self.assertEqual(self.run_script("report.py", str(out), "--goals", str(src), "--notes", str(notes)),
                         f"wrote {out}\n")
        self.assertEqual(out.read_text(), report.render_goals(goals_doc(), {"lede": "hi"}))
        src.write_text(json.dumps(goal_analysis()))
        self.run_script("report.py", str(out), "--goal", "unsolvable", "--analysis", str(src), "--notes", str(notes))
        self.assertEqual(out.read_text(), report.render_report(goal_analysis(), {"lede": "hi"}))

    def test_report_default_names(self):
        import report as report_cli
        self.assertEqual(report_cli.goal_name("analysis", "json", "solvable"), RUNS / "analysis.json")
        self.assertEqual(report_cli.goal_name("report_models", "html", "unsolvable"),
                         RUNS / "report_models.unsolvable.html")


@unittest.skipUnless((PILOT / "report.html").exists(), "pilot outputs not present")
class TestPilotGolden(unittest.TestCase):
    """Invariant: the committed pilot outputs are reproduced byte-for-byte."""

    def test_report_html(self):
        analysis = json.loads((PILOT / "analysis.json").read_text())
        notes = json.loads((PILOT / "report_notes.json").read_text())
        self.assertEqual(report.render_report(analysis, notes), (PILOT / "report.html").read_text())

    def test_analysis_json(self):
        expected = (PILOT / "analysis.json").read_text()
        pilot_ids = {r["run_id"] for r in json.loads(expected)["runs"]}
        if not RUNS.is_dir():
            self.skipTest("raw runs/ not present")
        runs, _ = aggregate.collect_runs(RUNS)
        if {r["run_id"] for r in runs} != pilot_ids:
            self.skipTest("runs/ holds a different set of untagged runs than the pilot")
        self.assertEqual(json.dumps(aggregate.build_analysis(RUNS), indent=2), expected)


if __name__ == "__main__":
    unittest.main()
