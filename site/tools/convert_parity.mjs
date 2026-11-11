#!/usr/bin/env node
// Structural parity: TS converter (src/convert, built to public/js/convert) vs the Python exporter
// (tools/export.py over a scratch obs ingest), per run dir (docs/browser-swarm.md §5).
//
//   node tools/convert_parity.mjs [--fixtures] [--runs] [--max N] [DIR ...]
//     default: dash/server/tests/fixtures/runs, plus runs/ when it exists (this checkout, else the main checkout)
//
// Needs `npm run build` and the dash/server venv (cd dash/server && uv sync). Both sides reduce their bundles to
// the same numbers-only projection (tools/convert_parity_py.py projection()): counts, times, indices, flags, enum
// codes and sha1 prefixes of ids. Output is ONLY run ids, PASS/FAIL, counts and src/types.ts field names; run
// content (bodies, commands, labels) never reaches stdout (CLAUDE.md chatter policy). Exit 1 on any FAIL.
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SITE = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = resolve(SITE, '..');
const FIXTURES = join(REPO, 'dash', 'server', 'tests', 'fixtures', 'runs');
const { convertRun } = await import(join(SITE, 'public', 'js', 'convert', 'index.js'));
const { assertCore, assertDetail } = await import(join(SITE, 'public', 'js', 'data', 'loader.js'));

function mainRuns() {
  if (existsSync(join(REPO, 'runs'))) return join(REPO, 'runs');
  const r = spawnSync('git', ['-C', REPO, 'rev-parse', '--git-common-dir'], { encoding: 'utf8' });
  const p = r.status === 0 ? resolve(REPO, r.stdout.trim(), '..', 'runs') : '';
  return p && existsSync(p) ? p : null;
}

const args = process.argv.slice(2);
let max = Infinity;
const dirs = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--fixtures') dirs.push(FIXTURES);
  else if (args[i] === '--runs') { const m = mainRuns(); if (m) dirs.push(m); }
  else if (args[i] === '--max') max = Number(args[++i]);
  else dirs.push(resolve(args[i]));
}
if (!dirs.length) { dirs.push(FIXTURES); const m = mainRuns(); if (m) dirs.push(m); }

const py = join(REPO, 'dash', 'server', '.venv', 'bin', 'python');
if (!existsSync(py)) { console.log('SKIP: dash/server/.venv missing (cd dash/server && uv sync)'); process.exit(2); }

const h8 = (s) => createHash('sha1').update(s, 'utf8').digest('hex').slice(0, 8);

/** Mirror of convert_parity_py.py projection(). */
function proj(core, detail, card) {
  const run = core.run, ev = core.events;
  const items = detail.items.map((it) => it.t === 'step' ? [it.t, it.exit ?? null, it.outLen ?? null, it.via ?? null, (it.submit ?? []).length]
    : it.t === 'think' ? [it.t, it.nTools ?? null, ...(it.tok ?? [])]
    : it.t === 'req' ? [it.t, it.status ?? null, it.bytes ?? null, it.method ?? null] : [it.t]);
  const pk = Object.keys(detail.posts).sort();
  const u = (v) => (v === undefined ? null : v);
  return {
    run: { nAgents: run.nAgents, rep: run.rep, tEndMs: run.tEndMs, wallMs: run.wallMs, prehistoryMs: run.prehistoryMs,
           caveats: run.caveats.map((c) => c.code), harness: run.harness, pop: run.pop, iface: run.iface, goal: u(run.goal),
           oldHarness: String(run.codeVersion).startsWith('pre-'), tagged: run.tag !== '', boardName: h8(run.boardName) },
    nodeIds: core.nodes.map((n) => h8(n.id)),
    nodes: core.nodes.map((n) => [n.k, n.t0, u(n.t1), u(n.agent), u(n.thread), u(n.author), u(n.postId), n.seed ? 1 : 0,
                                  u(n.tSeed), u(n.bodyLen), u(n.upvotes0), (n.flags ?? []).join('|'), 'names' in n ? n.names.length : null]),
    edges: core.edges.map((e) => [e.s, e.d, e.k, e.w ?? 1, e.inferred ? 1 : 0, e.liveLive ? 1 : 0, 'ev' in e ? 1 : 0]),
    events: { ...Object.fromEntries([...'tdkanefsc'].map((k) => [k, ev[k]])), x: ev.x.map((x) => (x >= 0 ? 1 : 0)) },
    fires: core.fires,
    agents: core.agents.map((a) => [a.id, a.node, a.names.length, a.status, a.startT, a.endT, a.solveT, a.solveStep, a.stepsUsed,
                                    a.modelCalls, a.ended, a.tokens.prompt, a.tokens.completion, a.tokens.cached, a.tokens.reasoning,
                                    a.metrics.discovered, a.metrics.firstContact, a.metrics.boardSteps, a.metrics.shareAfterContact,
                                    a.metrics.posts, a.metrics.episodes, a.metrics.secretLeak, a.drift.t, a.drift.a]),
    chapters: core.chapters.map((c) => [c.t, c.k, u(c.agent), u(c.node), u(c.ev), c.p, c.merged ?? 1]),
    segments: core.segments.map((s) => [s.t0, s.t1, s.title]),
    series: core.series,
    card: { stats: card.stats, density: card.density, score: card.score, caveats: card.caveats, durationMs: card.durationMs,
            nAgents: card.nAgents, pop: card.pop, iface: card.iface, goal: u(card.goal), blurb: h8(card.blurb) },
    detail: { items, postKeys: pk.map(h8), leakSpans: pk.map((k) => detail.posts[k].leakSpans.length),
              mentions: pk.map((k) => detail.posts[k].mentions.length) },
    nStrings: core.strings.length,
  };
}

// column names (src/types.ts field names) for tuple rows, so a diff reports e.g. `nodes.t0 (3)`
const COLS = {
  nodes: ['k', 't0', 't1', 'agent', 'thread', 'author', 'postId', 'seed', 'tSeed', 'bodyLen', 'upvotes0', 'flags', 'names'],
  edges: ['s', 'd', 'k', 'w', 'inferred', 'liveLive', 'ev'],
  agents: ['id', 'node', 'names', 'status', 'startT', 'endT', 'solveT', 'solveStep', 'stepsUsed', 'modelCalls', 'ended',
           'tokens.prompt', 'tokens.completion', 'tokens.cached', 'tokens.reasoning', 'metrics.discovered',
           'metrics.firstContact', 'metrics.boardSteps', 'metrics.shareAfterContact', 'metrics.posts', 'metrics.episodes',
           'metrics.secretLeak', 'drift.t', 'drift.a'],
  chapters: ['t', 'k', 'agent', 'node', 'ev', 'p', 'merged'],
  segments: ['t0', 't1', 'title'],
  'detail.items': ['t', 'v1', 'v2', 'v3', 'v4', 'v5', 'v6'],
};
const eq = (a, b) => {
  if (typeof a === 'number' && typeof b === 'number') return a === b || Math.abs(a - b) <= 1e-9;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => eq(x, b[i]));
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a).filter((k) => a[k] !== null && a[k] !== undefined).sort();
    const kb = Object.keys(b).filter((k) => b[k] !== null && b[k] !== undefined).sort();
    return eq(ka, kb) && ka.every((k) => eq(a[k], b[k]));
  }
  return (a ?? null) === (b ?? null);
};
/** Field-name diff: [[name, mismatching entry count]]. */
function diff(p, q, soft) {
  const out = [];
  const add = (name, n) => { if (n) out.push([name, n]); };
  for (const key of Object.keys(p)) {
    if (soft.has(key)) continue;
    const a = p[key], b = q[key];
    if (COLS[key]) {
      if (a.length !== b.length) { add(`${key}.length`, Math.abs(a.length - b.length) || 1); continue; }
      COLS[key].forEach((c, j) => add(`${key}.${c}`, a.filter((row, i) => !eq(row[j], b[i][j])).length));
    } else if (key === 'detail') {
      const ia = a.items, ib = b.items;
      if (ia.length !== ib.length) add('detail.items.length', Math.abs(ia.length - ib.length));
      else COLS['detail.items'].forEach((c, j) => add(`detail.items.${c}`, ia.filter((r, i) => !eq(r[j], ib[i][j])).length));
      for (const k of ['postKeys', 'leakSpans', 'mentions']) if (!eq(a[k], b[k])) add(`detail.posts.${k}`, 1);
    } else if (a && typeof a === 'object' && !Array.isArray(a)) {
      for (const k of new Set([...Object.keys(a), ...Object.keys(b ?? {})])) {
        const x = a[k], y = b?.[k];
        if (Array.isArray(x) && Array.isArray(y) && x.length === y.length && x.length && typeof x[0] !== 'object')
          add(`${key}.${k}`, x.filter((v, i) => !eq(v, y[i])).length);
        else if (!eq(x, y)) add(`${key}.${k}`, 1);
      }
    } else if (!eq(a, b)) add(key, Array.isArray(a) && Array.isArray(b) && a.length === b.length ? a.filter((v, i) => !eq(v, b[i])).length : 1);
  }
  return out;
}

function readRunDir(dir) {
  const files = new Map();
  const walk = (d) => {
    for (const n of readdirSync(d)) {
      const p = join(d, n);
      const st = statSync(p);
      if (st.isDirectory()) walk(p);
      else if (st.isFile()) files.set(relative(dir, p).split('\\').join('/'), new Uint8Array(readFileSync(p)));
    }
  };
  walk(dir);
  return files;
}

// nStrings depends on tools/redact.py rewriting labels (not ported): informational only
const SOFT = new Set(['nStrings']);
let pass = 0, fail = 0, skipped = 0;
const fieldTally = new Map();
for (const root of dirs) {
  console.log(`== ${relative(REPO, root) || root}`);
  const r = spawnSync(py, [join(SITE, 'tools', 'convert_parity_py.py'), '--runs', root,
                           '--db', join(SITE, '.cache', `parity-${h8(root)}.duckdb`)],
                      { encoding: 'utf8', maxBuffer: 1 << 30, stdio: ['ignore', 'pipe', 'pipe'] });
  let res;
  try { res = JSON.parse(r.stdout); } catch { console.log(`  python driver failed (exit ${r.status})`); fail++; continue; }
  for (const [rid, code] of Object.entries(res.errors)) { console.log(`  ${rid} SKIP python ${code}`); skipped++; }
  const seed = res.seed && existsSync(res.seed) ? new Uint8Array(readFileSync(res.seed)) : null;
  let n = 0;
  for (const [rid, { dir, proj: pp }] of Object.entries(res.runs)) {
    if (n++ >= max) break;
    const files = readRunDir(join(root, dir));
    // obs falls back to the lab checkout's default seed for run dirs without seed.json (Config.seed_file)
    if (!files.has('seed.json') && seed) files.set('seed.json', seed);
    let tp;
    try {
      const { card, core, detail } = await convertRun(files, { runId: rid });
      assertCore(JSON.parse(JSON.stringify(core)));
      assertDetail(detail, core.hash);
      tp = proj(core, detail, card);
    } catch (e) {
      console.log(`  ${rid} FAIL converter ${e?.constructor?.name ?? 'error'}`);
      fail++;
      continue;
    }
    const d = diff(pp, tp, SOFT);
    if (!d.length) { pass++; console.log(`  ${rid} PASS`); }
    else {
      fail++;
      console.log(`  ${rid} FAIL ${d.map(([k, c]) => `${k}(${c})`).join(' ')}`);
      for (const [k] of d) fieldTally.set(k, (fieldTally.get(k) ?? 0) + 1);
    }
  }
}
console.log(`parity: ${pass} pass, ${fail} fail, ${skipped} skipped`);
if (fieldTally.size) console.log('fields: ' + [...fieldTally].sort((a, b) => b[1] - a[1]).map(([k, c]) => `${k}=${c}`).join(' '));
process.exit(fail ? 1 : 0);
