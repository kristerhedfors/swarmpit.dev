// Tests for the dataset viewer (lab/study/dataset_viewer/). Uses ONLY the placeholder fixture package:
// dataset_viewer/fixture/make_fixture.py writes a synthetic lorem-ipsum study tree into a tmp dir and
// runs the REAL builder (study/dataset.py) over it, so the viewer is tested against exactly what the
// builder emits -- never a real dataset package.
//
//   node --test lab/study/test_dataset_viewer.mjs
//
// The UI smoke test drives headless Chromium through playwright-core when it can be resolved:
//   PLAYWRIGHT_CORE=/tmp/dsv-pw/node_modules/playwright-core   (default candidates are tried too)
//   CHROME_PATH=<chromium executable>   (default: the ms-playwright chromium-1217 cache on macOS)
// Screenshots go to /tmp/dataset-viewer-shots/ (placeholder fixture only).

import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import * as L from "./dataset_viewer/lib.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VIEWER = path.join(HERE, "dataset_viewer");
const MAKE = path.join(VIEWER, "fixture", "make_fixture.py");
const SHOTS = "/tmp/dataset-viewer-shots";
const FILES = ["seeds.jsonl", "evaluations.jsonl", "responses.jsonl", "pairs.jsonl", "sft.jsonl"];

let TMP;
let PKG;
let ROOT;
const read = (f) => L.parseJsonl(fs.readFileSync(path.join(PKG, f), "utf8"));

before(() => {
  TMP = fs.mkdtempSync(path.join(os.tmpdir(), "dsviewer-"));
  PKG = path.join(TMP, "package");
  ROOT = path.join(TMP, "dataset-root");
  execFileSync("python3", [MAKE, "--out", PKG, "--root", ROOT], { stdio: ["ignore", "ignore", "ignore"] });
  execFileSync("python3", [MAKE, "--out", path.join(TMP, "package2")], {
    stdio: ["ignore", "ignore", "ignore"], env: { ...process.env, PYTHONHASHSEED: "7" } });
});
after(() => { if (TMP) fs.rmSync(TMP, { recursive: true, force: true }); });

// ------------------------------------------------------------------ pure helpers

describe("lib helpers", () => {
  test("parseJsonl skips and counts malformed lines without echoing them", () => {
    const { rows, bad } = L.parseJsonl('{"a":1}\n\nnot json\n[1,2]\n{"b":2}\n');
    assert.equal(rows.length, 2);
    assert.equal(bad, 2);
  });

  test("wilson interval brackets the rate", () => {
    assert.deepEqual(L.wilson(0, 0), [0, 0]);
    const [lo, hi] = L.wilson(3, 10);
    assert.ok(lo < 0.3 && hi > 0.3 && lo >= 0 && hi <= 1);
    assert.ok(Math.abs(lo - 0.1078) < 1e-3 && Math.abs(hi - 0.6032) < 1e-3);
  });

  test("ctxKey / parseCtxKey round-trip (agent count and budget are part of the context)", () => {
    const k = L.ctxKey({ goal: "unsolvable", model: "qwen38", channel: "forum", n_agents: 4, budget: 40, length_band: "le120" });
    assert.equal(k, "unsolvable|qwen38|forum|4|40");
    assert.equal(L.ctxKey(k), k);
    assert.deepEqual(L.parseCtxKey(k), { goal: "unsolvable", model: "qwen38", channel: "forum", n_agents: 4, budget: 40 });
    assert.notEqual(L.ctxKey({ goal: "unsolvable", model: "qwen38", channel: "forum", n_agents: 2, budget: 40 }), k);
    assert.equal(L.ctxKey({ goal: "g", model: "m", channel: "c" }), "g|m|c|-|-");
    assert.equal(L.ctxLabel(k), "unsolvable · qwen38 · forum · 4 agents · 40 steps");
  });

  test("hash state round-trips and is canonical", () => {
    const st = { view: "seeds", split: "train", ctx: "a|b|c", q: "" };
    const hs = L.serializeHash(st);
    assert.ok(hs.startsWith("#view=seeds&"));
    assert.deepEqual(L.parseHash(hs), { view: "seeds", split: "train", ctx: "a|b|c" });
    assert.equal(L.serializeHash({ split: "x", view: "seeds" }), L.serializeHash({ view: "seeds", split: "x" }));
    assert.equal(L.parseHash("").view, "overview");
  });

  const seeds = [
    { seed_id: "a", split: "train", provenance: { origin: "scratch", generation: 0, root_id: "a" }, stats: { length_band: "le120", seed_chars: 90 },
      outcomes: { n_evals: 4, n_evals_with_writers: 2, writer_rate: 0.5, writer_rate_ci95: [0.15, 0.85], pile_in_rate: 0.25, best_score: 0.7,
        by_context: { "g|m|forum": { n: 4 } } }, triggers: { TASK_HINT: 2 }, dedup: { near_dup_cluster: "nd-1" } },
    { seed_id: "b", split: "val", provenance: { origin: "mutation", generation: 1, parent_id: "a", root_id: "a" }, stats: { length_band: "gt320", seed_chars: 400 },
      outcomes: { n_evals: 1, n_evals_with_writers: 0, writer_rate: 0, pile_in_rate: 0, best_score: 0.1, by_context: { "g|m|js": { n: 1 } } },
      triggers: {}, dedup: { near_dup_cluster: "nd-1" } },
    { seed_id: "c", split: "train", provenance: { origin: "pilot", generation: 0, root_id: "c" }, stats: { length_band: "le120", seed_chars: 50 },
      outcomes: { n_evals: 10, n_evals_with_writers: 9, writer_rate: 0.9, pile_in_rate: 0, best_score: 0.9, by_context: {} },
      triggers: { UNPROMPTED: 9 }, dedup: { near_dup_cluster: null } },
    { seed_id: "d", split: "test", provenance: { origin: "scratch", generation: 0, root_id: "d" }, stats: { length_band: "le120", seed_chars: 60 },
      outcomes: { n_evals: 0, n_evals_with_writers: 0, writer_rate: null, pile_in_rate: null, best_score: null, by_context: {} },
      triggers: {}, dedup: { near_dup_cluster: null } },
  ];

  test("filterSeeds applies every facet", () => {
    const ids = (f) => L.filterSeeds(seeds, f).map((s) => s.seed_id).join("");
    assert.equal(ids({}), "abcd");
    assert.equal(ids({ split: "train" }), "ac");
    assert.equal(ids({ origin: "mutation" }), "b");
    assert.equal(ids({ gen: "0" }), "acd");
    assert.equal(ids({ band: "gt320" }), "b");
    assert.equal(ids({ trig: "TASK_HINT" }), "a");
    assert.equal(ids({ ctx: "g|m|js" }), "b");
    assert.equal(ids({ label: "instigated" }), "ac");
    assert.equal(ids({ label: "not_instigated" }), "b", "never-evaluated d is not a negative");
    assert.equal(ids({ label: "unevaluated" }), "d");
    assert.equal(ids({ label: "pile_in" }), "a");
    assert.equal(ids({ cluster: "*" }), "ab");
    assert.equal(ids({ cluster: "none" }), "cd");
    assert.equal(ids({ cluster: "nd-1", split: "val" }), "b");
    assert.equal(ids({ q: "A" }), "ab"); // id / root / parent match
  });

  test("labels are judged within the selected context", () => {
    const two = [
      { seed_id: "x", outcomes: { n_evals: 2, n_evals_with_writers: 1, pile_in_rate: 0.5, by_context: {
        "g|m|forum|4|40": { n: 1, writers_any: 1, max_writers: 2 }, "g|m|irc|4|40": { n: 1, writers_any: 0, max_writers: 0 } } } },
      { seed_id: "y", outcomes: { n_evals: 1, n_evals_with_writers: 1, pile_in_rate: 0, by_context: {
        "g|m|forum|4|40": { n: 1, writers_any: 1, max_writers: 1 } } } },
    ];
    const ids = (f) => L.filterSeeds(two, f).map((s) => s.seed_id).join("");
    assert.equal(ids({ label: "instigated" }), "xy");
    assert.equal(ids({ ctx: "g|m|irc|4|40", label: "instigated" }), "", "x instigated only in forum");
    assert.equal(ids({ ctx: "g|m|irc|4|40", label: "not_instigated" }), "x");
    assert.equal(ids({ ctx: "g|m|forum|4|40", label: "pile_in" }), "x");
    assert.equal(ids({ ctx: "g|m|irc|4|40", label: "unevaluated" }), "", "the ctx facet already requires the context");
    assert.deepEqual(L.seedLabel(two[1], "g|m|irc|4|40"), { evaluated: false, instigated: false, pile_in: false });
  });

  test("labelBalance never counts unevaluated seeds as negatives and splits by split", () => {
    const lb = L.labelBalance({ seeds, evaluations: [
      { split: "train", outcome: { n_writers: 2 } }, { split: "val", outcome: { n_writers: 0 } }] });
    assert.deepEqual(lb.seeds, { total: 4, evaluated: 3, unevaluated: 1, instigated: 2, not_instigated: 1, pile_in: 1 });
    assert.deepEqual(lb.bySplit.test.seeds, { total: 1, evaluated: 0, unevaluated: 1, instigated: 0, not_instigated: 0, pile_in: 0 });
    assert.deepEqual(lb.bySplit.train.evaluations, { total: 1, instigated: 1, not_instigated: 0, pile_in: 1 });
    assert.equal(lb.bySplit.val.evaluations.not_instigated, 1);
  });

  test("sortSeeds sorts by key and direction, deterministic ties, CI fallback", () => {
    const abc = seeds.slice(0, 3);
    assert.deepEqual(L.sortSeeds(abc, "writer_rate").map((s) => s.seed_id), ["c", "a", "b"]);
    assert.deepEqual(L.sortSeeds(abc, "writer_rate", "asc").map((s) => s.seed_id), ["b", "a", "c"]);
    assert.deepEqual(L.sortSeeds(abc, "n_evals").map((s) => s.seed_id), ["c", "a", "b"]);
    assert.deepEqual(L.sortSeeds(abc, "bogus").map((s) => s.seed_id), ["c", "a", "b"]);
    // c has no CI in the row: Wilson fallback for 9/10
    assert.ok(Math.abs(L.seedCi(seeds[2])[0] - L.wilson(9, 10)[0]) < 1e-12);
    assert.deepEqual(L.sortSeeds(abc, "ci_lo").map((s) => s.seed_id), ["c", "a", "b"]);
  });

  test("facetValues orders enums and lists clusters", () => {
    const fv = L.facetValues(seeds);
    assert.deepEqual(fv.split, ["train", "val", "test"]);
    assert.deepEqual(fv.origin, ["pilot", "scratch", "mutation"]);
    assert.deepEqual(fv.band, ["le120", "gt320"]);
    assert.deepEqual(fv.cluster, ["nd-1"]);
    assert.deepEqual(fv.trig, ["TASK_HINT", "UNPROMPTED"]);
  });

  test("buildIndex + ancestry follow lineage and survive cycles", () => {
    const idx = L.buildIndex({ seeds, evaluations: [{ eval_id: "e1", seed_id: "a" }],
      responses: [{ eval_id: "e1", seed_id: "a", t_rel_s: 5 }, { eval_id: "e1", seed_id: "a", t_rel_s: 1 }],
      pairs: [{ chosen_seed_id: "a", rejected_seed_id: "b" }] });
    assert.deepEqual(idx.children.get("a"), ["b"]);
    assert.deepEqual(idx.clusters.get("nd-1"), ["a", "b"]);
    assert.deepEqual(L.ancestry(idx, "b"), ["a", "b"]);
    assert.deepEqual(idx.respByEval.get("e1").map((r) => r.t_rel_s), [1, 5]);
    assert.equal(idx.pairsBySeed.get("b").length, 1);
    const cyc = L.buildIndex({ seeds: [{ seed_id: "x", provenance: { parent_id: "y" } }, { seed_id: "y", provenance: { parent_id: "x" } }] });
    assert.deepEqual(L.ancestry(cyc, "x"), ["y", "x"]);
  });

  test("coverageMatrix computes writer rate per goal|model|setup x channel (setups never pooled)", () => {
    const c = (channel, n_agents = 4) => ({ goal: "g", model: "m", channel, n_agents, budget: 40 });
    const cm = L.coverageMatrix([
      { seed_id: "a", context: c("forum"), outcome: { n_writers: 2 } },
      { seed_id: "b", context: c("forum"), outcome: { n_writers: 0 } },
      { seed_id: "a", context: c("js"), outcome: { n_writers: 0 } },
      { seed_id: "a", context: c("forum", 2), outcome: { n_writers: 1 } },
    ]);
    assert.deepEqual(cm.rows, ["g|m|2|40", "g|m|4|40"]);
    assert.deepEqual(cm.cols, ["forum", "js"]);
    assert.deepEqual(cm.cells["g|m|4|40||forum"], { n: 2, writers_any: 1, rate: 0.5, seeds: 2 });
    assert.equal(cm.cells["g|m|4|40||js"].rate, 0);
    assert.equal(cm.cells["g|m|2|40||forum"].rate, 1);
    assert.equal(L.coverageCtx("g|m|4|40", "js"), L.ctxKey(c("js")));
  });

  test("selectRows keeps pairs only when both seeds are selected; export has a header row", () => {
    const data = { pairs: [{ chosen_seed_id: "a", rejected_seed_id: "b" }, { chosen_seed_id: "a", rejected_seed_id: "c" }],
      responses: [{ seed_id: "a" }, { seed_id: "b" }] };
    const sel = new Set(["a", "c"]);
    assert.equal(L.selectRows(data, "pairs", sel).length, 1);
    assert.equal(L.selectRows(data, "responses", sel).length, 1);
    // the context facet restricts every per-context file, not only the seed list
    const cx = (channel) => ({ goal: "g", model: "m", channel, n_agents: 4, budget: 40 });
    const d2 = {
      evaluations: [{ seed_id: "a", context: cx("irc") }, { seed_id: "a", context: cx("forum") }],
      responses: [{ seed_id: "a", context: cx("forum") }],
      pairs: [{ chosen_seed_id: "a", rejected_seed_id: "c", context: cx("irc") }, { chosen_seed_id: "a", rejected_seed_id: "c", context: cx("forum") }],
      sft: [{ seed_id: "a", context: { ...cx("forum"), length_band: "le120" } }, { seed_id: "c", context: { ...cx("irc"), length_band: "le120" } }],
    };
    const f = { ctx: "g|m|irc|4|40" };
    for (const [file, n] of [["evaluations", 1], ["responses", 0], ["pairs", 1], ["sft", 1]]) {
      const got = L.selectRows(d2, file, sel, f);
      assert.equal(got.length, n, file);
      assert.ok(got.every((r) => L.ctxKey(r.context) === f.ctx), file);
    }
    assert.equal(L.selectRows(d2, "evaluations", sel).length, 2, "no ctx facet: every context");
    const text = L.exportJsonl([{ seed_id: "a" }], { filter: { split: "train" }, version: "v1" });
    const lines = text.trim().split("\n").map((l) => JSON.parse(l));
    assert.deepEqual(lines[0], { _header: { filter: { split: "train" }, version: "v1", rows: 1 } });
    assert.equal(lines.length, 2);
    assert.equal(L.exportFilename("seeds", "2026-10-13-ab/../x", { split: "t" }), "swarmpit-instigation_2026-10-13-ab_.._x_seeds_subset.jsonl");
  });

  test("safeDataBase only accepts same-origin relative paths", () => {
    assert.equal(L.safeDataBase(null), "data/");
    assert.equal(L.safeDataBase("pkg/v1"), "pkg/v1/");
    for (const bad of ["https://evil.example/", "//evil.example/", "javascript:alert(1)", "data:text/plain,x", "a\\b", "a b", "a?x=1"]) {
      assert.equal(L.safeDataBase(bad), "data/", bad);
    }
  });

  test("builder shapes: singleton clusters and per-agent code arrays", () => {
    const ss = [{ seed_id: "a", dedup: { near_dup_cluster: "a" } }, { seed_id: "b", dedup: { near_dup_cluster: "c" } },
      { seed_id: "c", dedup: { near_dup_cluster: "c" } }];
    L.markSingletonClusters(ss);
    assert.deepEqual(ss.map(L.seedCluster), [null, "c", "c"]);
    assert.equal(JSON.stringify(ss[0]), '{"seed_id":"a","dedup":{"near_dup_cluster":"a"}}', "marker is not exported");
    assert.deepEqual(L.filterSeeds(ss, { cluster: "none" }).map((s) => s.seed_id), ["a"]);
    assert.deepEqual(L.perAgentCodes(["TASK_HINT", null]), [{ agent: 0, code: "TASK_HINT" }, { agent: 1, code: null }]);
    assert.deepEqual(L.perAgentCodes([{ agent_id: 3, code: "NONE" }]), [{ agent: 3, code: "NONE" }]);
    assert.deepEqual(L.perAgentCodes(undefined), []);
  });

  test("boardModel coerces odd shapes to strings", () => {
    const b = L.boardModel({ board_name: 5, threads: [{ slug: "s", posts: [{ body: { x: 1 } }, null] }, null] });
    assert.equal(b.board_name, "5");
    assert.equal(b.threads.length, 2);
    assert.equal(typeof b.threads[0].posts[0].body, "string");
    assert.equal(b.threads[0].posts[1].ago_min, null);
    assert.deepEqual(L.boardModel(null), { board_name: "", threads: [] });
  });
});

// ------------------------------------------------------------------ static safety

describe("static safety", () => {
  test("viewer code never writes markup or evaluates strings", () => {
    for (const f of ["viewer.js", "lib.js"]) {
      const src = fs.readFileSync(path.join(VIEWER, f), "utf8");
      for (const bad of [/\.innerHTML\b/, /\.outerHTML\b/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new\s+Function\s*\(/, /DOMParser/, /createContextualFragment/]) {
        assert.ok(!bad.test(src), `${f} matches ${bad}`);
      }
    }
  });

  test("index.html has a strict CSP, the adversarial banner, and no remote assets", () => {
    const html = fs.readFileSync(path.join(VIEWER, "index.html"), "utf8");
    assert.match(html, /Content-Security-Policy/);
    assert.match(html, /default-src 'none'/);
    assert.match(html, /script-src 'self'/);
    assert.ok(!/unsafe-inline|unsafe-eval/.test(html));
    assert.match(html, /Adversarial content — for defensive research; do not publish/);
    assert.ok(!/(src|href)=["']https?:/i.test(html), "no remote assets");
    assert.ok(!/<script>(?!<)/.test(html), "no inline script");
  });
});

// ------------------------------------------------------------------ fixture contract

describe("placeholder fixture package", () => {
  test("builder output for the placeholder tree is deterministic", () => {
    for (const f of [...FILES, "manifest.json", "schema.json", "DATASHEET.md"]) {
      const a = fs.readFileSync(path.join(PKG, f));
      const b = fs.readFileSync(path.join(TMP, "package2", f));
      assert.ok(a.equals(b), `${f} differs between two builds`);
    }
  });

  test("manifest sha256 and counts match the files", () => {
    const m = JSON.parse(fs.readFileSync(path.join(PKG, "manifest.json"), "utf8"));
    assert.equal(m.name, "swarmpit-instigation");
    assert.equal(m.schema, "instigation.dataset.v1");
    assert.match(m.version, /^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}$/);
    for (const f of [...FILES, "schema.json", "DATASHEET.md"]) {
      const sha = createHash("sha256").update(fs.readFileSync(path.join(PKG, f))).digest("hex");
      assert.equal(m.sha256[f], sha, f);
    }
    for (const f of FILES) assert.equal(m.counts.files[f], read(f).rows.length, f);
  });

  test("rows carry the contract fields with the right types", () => {
    const schema = JSON.parse(fs.readFileSync(path.join(PKG, "schema.json"), "utf8"));
    const typeOk = (v, t) => (Array.isArray(t) ? t : [t]).some((x) =>
      x === "null" ? v === null : x === "integer" ? Number.isInteger(v) : x === "array" ? Array.isArray(v)
        : x === "object" ? v !== null && typeof v === "object" && !Array.isArray(v) : typeof v === x);
    const check = (v, s, where) => {
      if (s.enum) assert.ok(s.enum.includes(v), `${where} enum`);
      if (s.type) assert.ok(typeOk(v, s.type), `${where} type`);
      if (s.pattern && typeof v === "string") assert.match(v, new RegExp(s.pattern), where);
      if (s.properties && v && typeof v === "object") {
        for (const k of s.required || []) assert.ok(k in v, `${where}.${k} missing`);
        for (const [k, sub] of Object.entries(s.properties)) if (k in v) check(v[k], sub, `${where}.${k}`);
      }
      if (s.items && Array.isArray(v)) v.forEach((x, i) => check(x, s.items, `${where}[${i}]`));
      if (s.additionalProperties && typeof s.additionalProperties === "object" && v && typeof v === "object") {
        for (const [k, x] of Object.entries(v)) check(x, s.additionalProperties, `${where}.<key>`);
      }
    };
    for (const f of FILES) {
      const { rows, bad } = read(f);
      assert.equal(bad, 0, f);
      assert.ok(rows.length > 0, f);
      rows.forEach((r, i) => check(r, schema.files[f], `${f}#${i}`));
    }
  });

  test("splits follow lineage roots; pairs never cross splits; references resolve", () => {
    const seeds = read("seeds.jsonl").rows;
    const byId = new Map(seeds.map((s) => [s.seed_id, s]));
    for (const s of seeds) {
      const root = byId.get(s.provenance.root_id);
      assert.ok(root, `root of ${s.seed_id}`);
      assert.equal(s.split, root.split, `split of ${s.seed_id}`);
      if (s.provenance.parent_id) assert.ok(byId.has(s.provenance.parent_id));
    }
    for (const p of read("pairs.jsonl").rows) {
      assert.equal(byId.get(p.chosen_seed_id).split, byId.get(p.rejected_seed_id).split);
      assert.ok(p.margin.n_writers > 0 || (p.margin.n_writers === 0 && p.margin.instigation_score > 0));
    }
    const evals = new Set(read("evaluations.jsonl").rows.map((e) => e.eval_id));
    for (const r of read("responses.jsonl").rows) { assert.ok(evals.has(r.eval_id)); assert.ok(byId.has(r.seed_id)); }
    // realistic shapes: every split, lineage depth, near-dup clusters
    assert.deepEqual(new Set(seeds.map((s) => s.split)), new Set(["train", "val", "test"]));
    assert.ok(Math.max(...seeds.map((s) => s.provenance.generation)) >= 2);
    assert.ok(new Set(seeds.map((s) => s.dedup.near_dup_cluster).filter(Boolean)).size >= 2);
  });
});

// ------------------------------------------------------------------ UI smoke (headless Chromium)

async function loadPlaywright() {
  const cands = [process.env.PLAYWRIGHT_CORE, "/tmp/dsv-pw/node_modules/playwright-core", "/tmp/pwx/node_modules/playwright-core"].filter(Boolean);
  for (const c of cands) {
    try {
      const mod = await import(pathToFileURL(path.join(c, "index.mjs")).href);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* try next */ }
  }
  return null;
}

const CHROME = process.env.CHROME_PATH
  || path.join(os.homedir(), "Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");

function startServer(pkgDir) {
  const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json", ".jsonl": "application/x-ndjson", ".md": "text/plain" };
  const viewerFiles = { "/": "index.html", "/index.html": "index.html", "/viewer.js": "viewer.js", "/lib.js": "lib.js", "/viewer.css": "viewer.css" };
  const srv = http.createServer((req, res) => {
    const p = decodeURIComponent(new URL(req.url, "http://x").pathname);
    if (p === "/favicon.ico") { res.writeHead(204); res.end(); return; }
    let f = null;
    if (viewerFiles[p]) f = path.join(VIEWER, viewerFiles[p]);
    else if (p.startsWith("/data/") && /^[A-Za-z0-9_.-]+$/.test(p.slice(6)) && !p.slice(6).startsWith(".")) f = path.join(pkgDir, p.slice(6));
    if (!f || !fs.existsSync(f)) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { "Content-Type": types[path.extname(f)] || "application/octet-stream", "Cache-Control": "no-store" });
    res.end(fs.readFileSync(f));
  });
  return new Promise((resolve) => srv.listen(0, "127.0.0.1", () => resolve(srv)));
}

describe("viewer UI smoke (placeholder fixture only)", async () => {
  const chromium = await loadPlaywright();
  const skip = !chromium ? "playwright-core not found" : !fs.existsSync(CHROME) ? "chromium executable not found" : false;
  let srv, browser, base, probePkg;

  before(async () => {
    if (skip) return;
    // A copy of the placeholder package with one neutral markup-shaped probe string injected into a
    // post body and a response body, to prove the viewer renders such text inertly.
    probePkg = path.join(TMP, "probe");
    fs.cpSync(PKG, probePkg, { recursive: true });
    const PROBE = '<img src=x id=probe-img onerror="window.__pwned=1"><b id=probe-b>lorem</b>';
    const seeds = read("seeds.jsonl").rows;
    seeds[0].content.threads[0].posts[0].body = PROBE;
    seeds[0].content.board_name = PROBE;
    fs.writeFileSync(path.join(probePkg, "seeds.jsonl"), seeds.map((s) => JSON.stringify(s)).join("\n") + "\n");
    const resps = read("responses.jsonl").rows;
    resps[0].body = PROBE;
    fs.writeFileSync(path.join(probePkg, "responses.jsonl"), resps.map((r) => JSON.stringify(r)).join("\n") + "\n");
    fs.mkdirSync(SHOTS, { recursive: true });
    srv = await startServer(PKG);
    base = `http://127.0.0.1:${srv.address().port}/`;
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
  });
  after(async () => { await browser?.close(); srv?.close(); });

  async function open(hash = "", { width = 1440, height = 1000, pkg = null } = {}) {
    const ctx = await browser.newContext({ viewport: { width, height }, acceptDownloads: true });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(`pageerror:${e.name}`));
    page.on("console", (m) => { if (m.type() === "error") errors.push(`console:${m.location()?.url?.split("/").pop()}`); });
    let url = base;
    if (pkg) {
      // serve the probe package from a second server instance
      const s2 = await startServer(pkg);
      ctx.on("close", () => s2.close());
      url = `http://127.0.0.1:${s2.address().port}/`;
    }
    await page.goto(url + hash);
    await page.waitForSelector("main h2");
    return { page, ctx, errors };
  }

  test("overview renders banner, counts, coverage matrix, integrity ok", { skip }, async () => {
    const { page, ctx, errors } = await open();
    assert.ok(await page.isVisible("#banner"));
    assert.match(await page.textContent("#banner"), /Adversarial content — for defensive research; do not publish/);
    assert.equal(await page.textContent("main h2"), "Overview");
    assert.ok((await page.locator("td.cell:not(.empty)").count()) > 10);
    assert.equal(await page.locator(".badge.bad").count(), 0, "no sha mismatches");
    assert.equal(await page.locator(".badge.ok").count(), 5);
    await page.screenshot({ path: path.join(SHOTS, "overview.png"), fullPage: true });
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("seeds table filters, sorts, pages and keeps state in the hash", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=seeds");
    const total = await page.locator("main tbody tr").count();
    assert.equal(total, 50);
    await page.selectOption("#f-split", "train");
    await page.waitForFunction(() => location.hash.includes("split=train"));
    await page.selectOption("#f-label", "instigated");
    await page.waitForFunction(() => location.hash.includes("label=instigated"));
    const countText = await page.textContent("#seed-count");
    const n = parseInt(countText, 10);
    assert.ok(n > 0 && n < read("seeds.jsonl").rows.length);
    await page.selectOption("#f-sort", "ci_lo");
    await page.waitForFunction(() => location.hash.includes("sort=ci_lo"));
    // reload keeps state
    await page.reload();
    await page.waitForSelector("main h2");
    assert.equal(await page.inputValue("#f-split"), "train");
    assert.equal(await page.inputValue("#f-sort"), "ci_lo");
    await page.screenshot({ path: path.join(SHOTS, "seeds.png"), fullPage: false });
    // cluster facet
    await page.goto(base + "#view=seeds&cluster=*");
    await page.waitForSelector("main h2");
    const clustered = await page.locator("main tbody tr").count();
    assert.ok(clustered >= 4);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("seed detail shows board, contexts, episodes, lineage; j/k/Esc navigate", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=seeds&origin=mutation&sort=generation");
    await page.click("main tbody tr:first-child a");
    await page.waitForFunction(() => location.hash.includes("view=seed&"));
    assert.ok((await page.locator(".board .post").count()) >= 1);
    assert.match(await page.textContent("main"), /Root → this/);
    assert.ok((await page.locator("main section h3", { hasText: "Outcomes per context" }).count()) === 1);
    const first = await page.evaluate(() => location.hash);
    await page.keyboard.press("j");
    await page.waitForFunction((h) => location.hash !== h, first);
    await page.keyboard.press("k");
    await page.waitForFunction((h) => location.hash === h, first);
    await page.screenshot({ path: path.join(SHOTS, "seed-detail.png"), fullPage: true });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => location.hash.startsWith("#view=seeds"));
    assert.match(await page.evaluate(() => location.hash), /origin=mutation/);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("pairs browser shows chosen vs rejected side by side", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=pairs");
    await page.click("main tbody tr:first-child td:first-child a");
    await page.waitForSelector(".pair .side.chosen .board");
    assert.equal(await page.locator(".pair .side").count(), 2);
    await page.screenshot({ path: path.join(SHOTS, "pairs.png"), fullPage: false });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !location.hash.includes("pair="));
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("responses browser filters by trigger code and context", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=responses");
    const all = await page.locator(".resp").count();
    assert.equal(all, 50);
    const opts = await page.locator("#r-trig option").allTextContents();
    await page.selectOption("#r-trig", opts[1]);
    await page.waitForFunction(() => location.hash.includes("rtrig="));
    const badges = await page.locator(".resp .badge").allTextContents();
    assert.ok(badges.length > 0 && badges.every((b) => b === opts[1]));
    await page.screenshot({ path: path.join(SHOTS, "responses.png"), fullPage: false });
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("export downloads a filtered JSONL with a header row", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=export&split=val");
    assert.match(await page.textContent("#export-filter"), /split=val/);
    const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#dl-seeds")]);
    const file = path.join(TMP, "dl.jsonl");
    await dl.saveAs(file);
    const lines = fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    assert.deepEqual(lines[0]._header.filter, { split: "val" });
    assert.equal(lines[0]._header.rows, lines.length - 1);
    assert.ok(lines.slice(1).every((r) => r.split === "val"));
    assert.match(dl.suggestedFilename(), /_seeds_subset\.jsonl$/);
    const [dl2] = await Promise.all([page.waitForEvent("download"), page.click("#dl-pairs")]);
    await dl2.saveAs(file);
    const pl = fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    assert.ok(pl.slice(1).every((r) => r.split === "val"));
    await page.screenshot({ path: path.join(SHOTS, "export.png"), fullPage: false });
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("export with a context facet restricts every per-context file to that context", { skip }, async () => {
    const ev = read("evaluations.jsonl").rows;
    const counts = {};
    for (const e of ev) { const k = L.ctxKey(e.context); counts[k] = (counts[k] || 0) + 1; }
    const ctx = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
    const { page, ctx: bctx, errors } = await open("#view=export&ctx=" + encodeURIComponent(ctx));
    const file = path.join(TMP, "dl-ctx.jsonl");
    for (const f of ["evaluations", "pairs", "sft", "responses"]) {
      const [dl] = await Promise.all([page.waitForEvent("download"), page.click(`#dl-${f}`)]);
      await dl.saveAs(file);
      const lines = fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
      assert.equal(lines[0]._header.filter.ctx, ctx, f);
      assert.ok(lines.slice(1).every((r) => L.ctxKey(r.context) === ctx), `${f} rows outside the context`);
    }
    assert.deepEqual(errors, []);
    await bctx.close();
  });

  test("keyboard: number keys switch views, '/' focuses search, skip link works", { skip }, async () => {
    const { page, ctx, errors } = await open();
    await page.keyboard.press("2");
    await page.waitForFunction(() => location.hash.startsWith("#view=seeds"));
    await page.keyboard.press("/");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "f-q");
    await page.keyboard.press("Escape");
    await page.keyboard.press("3");
    await page.waitForFunction(() => location.hash.startsWith("#view=pairs"));
    assert.equal(await page.getAttribute('#tabs a[data-view="pairs"]', "aria-current"), "page");
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("markup-shaped text is rendered inertly as text", { skip }, async () => {
    const { page, ctx, errors } = await open("", { pkg: probePkg });
    const seeds = L.parseJsonl(fs.readFileSync(path.join(probePkg, "seeds.jsonl"), "utf8")).rows;
    await page.goto(page.url().split("#")[0] + "#view=seed&id=" + seeds[0].seed_id);
    await page.waitForSelector(".board .post");
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#probe-img, #probe-b").count(), 0);
    assert.equal(await page.evaluate(() => window.__pwned ?? 0), 0);
    assert.match(await page.textContent(".board-name"), /<img src=x/);
    await page.goto(page.url().split("#")[0] + "#view=responses");
    await page.waitForSelector(".resp");
    assert.equal(await page.locator("#probe-img, #probe-b").count(), 0);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("served end to end by `dataset.py serve` (real server, CSP, placeholder package)", { skip }, async () => {
    const proc = spawn("python3", [path.join(HERE, "dataset.py"), "serve", "--out", ROOT, "--port", "0"],
      { cwd: path.join(HERE, ".."), stdio: ["ignore", "pipe", "ignore"] });
    try {
      const line = await new Promise((resolve, reject) => {
        let buf = "";
        proc.stdout.on("data", (c) => { buf += c; const i = buf.indexOf("\n"); if (i >= 0) resolve(buf.slice(0, i)); });
        proc.on("exit", () => reject(new Error("serve exited")));
        setTimeout(() => reject(new Error("serve timeout")), 15000);
      });
      const info = JSON.parse(line);
      assert.equal(info.ok, true);
      assert.equal(info.viewer, true);
      assert.match(info.url, /^http:\/\/127\.0\.0\.1:\d+\/$/);
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(`pageerror:${e.name}`));
      page.on("console", (m) => { if (m.type() === "error") errors.push(`console:${m.location()?.url?.split("/").pop()}`); });
      await page.goto(info.url);
      await page.waitForSelector("main h2");
      assert.equal(await page.locator(".badge.ok").count(), 5, "sha256 integrity ok over dataset.py serve");
      const seeds = read("seeds.jsonl").rows;
      const withEvals = seeds.find((s) => s.outcomes.n_evals_with_writers > 0 && s.provenance.parent_id);
      await page.goto(info.url + "#view=seed&id=" + withEvals.seed_id);
      await page.waitForSelector(".board .post");
      assert.ok((await page.locator(".episode").count()) >= 1);
      await page.goto(info.url + "#view=pairs");
      await page.waitForSelector("main tbody tr");
      await page.click("main tbody tr:first-child td:first-child a");
      await page.waitForSelector(".pair .side.chosen .board");
      assert.match(await page.textContent(".pair .side.chosen"), /max writers \d+/);
      await page.goto(info.url + "#view=responses");
      await page.waitForSelector(".resp");
      assert.deepEqual(errors, []);
      await ctx.close();
    } finally {
      proc.kill();
    }
  });

  test("phone layout renders without errors", { skip }, async () => {
    const { page, ctx, errors } = await open("#view=overview", { width: 390, height: 844 });
    assert.ok(await page.isVisible("#banner"));
    await page.screenshot({ path: path.join(SHOTS, "overview-phone.png"), fullPage: false });
    await page.goto(base + "#view=pairs");
    await page.waitForSelector("main h2");
    await page.click("main tbody tr:first-child td:first-child a");
    await page.waitForSelector(".pair .side.rejected");
    await page.screenshot({ path: path.join(SHOTS, "pairs-phone.png"), fullPage: false });
    assert.deepEqual(errors, []);
    await ctx.close();
  });
});
