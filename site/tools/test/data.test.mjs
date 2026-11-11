// Builder A: every published bundle validates against src/types.ts shapes, references resolve, sort orders hold.
// Run: node --test tools/test/   (zero deps; reads public/data only)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const SITE = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const DATA = join(SITE, 'public', 'data');
const TYPES = readFileSync(join(SITE, 'src', 'types.ts'), 'utf8');

function table(name) {
  const m = TYPES.match(new RegExp(`export const ${name} = \\[([\\s\\S]*?)\\] as const`));
  assert.ok(m, `table ${name} in types.ts`);
  return [...m[1].replace(/\/\/[^\n]*/g, '').matchAll(/'([^']+)'/g)].map((x) => x[1]);
}
const NODE_KINDS = table('NODE_KINDS');
const EDGE_KINDS = table('EDGE_KINDS');
const EVENT_KINDS = table('EVENT_KINDS');
const STEP_CATS = table('STEP_CATS');
const CHAPTER_KINDS = table('CHAPTER_KINDS');
const LEAK = 4;

const json = (p) => JSON.parse(readFileSync(p, 'utf8'));
const haveData = existsSync(join(DATA, 'index.json'));
const index = haveData ? json(join(DATA, 'index.json')) : null;
const isInt = (v) => Number.isInteger(v);

test('index.json shape', { skip: !haveData && 'run tools/export.py first' }, () => {
  assert.equal(index.format, 'swarmpit.index/1');
  assert.equal(index.generator.tool, 'tools/export.py');
  assert.ok(['keep', 'mask'].includes(index.generator.ctf));
  assert.ok(index.runs.some((r) => r.id === index.featured && !r.hidden), 'featured is listed');
  assert.ok(statSync(join(DATA, 'index.json')).size <= 256 * 1024);
  const listed = index.runs.filter((r) => !r.hidden);
  const f0 = index.findings;
  const ifo = ['forum', 'wiki', 'cache', 'irc'];
  // ordered by model (index.models order, primary first), goal (solvable first), iface, pop, rep
  const mo = (index.models ?? []).map((m) => m.id);
  const gi = (r) => (r.goal === 'unsolvable' ? 1 : 0);
  const key = (r) => [mo.indexOf(r.model), gi(r), ifo.indexOf(r.iface), r.pop === 'solo' ? 0 : 1, r.rep];
  const le = (a, b) => { for (let k = 0; k < a.length; k++) if (a[k] !== b[k]) return a[k] < b[k]; return true; };
  for (let i = 1; i < listed.length; i++) assert.ok(le(key(listed[i - 1]), key(listed[i])), 'runs ordered');
  if (index.models) {
    const real = listed.filter((r) => !r.caveats.includes('synthetic'));
    assert.deepEqual(new Set(mo), new Set(real.map((r) => r.model)), 'index.models match listed cards');
    for (const m of index.models) assert.equal(m.nRuns, real.filter((r) => r.model === m.id).length, m.slug);
  }
  // goal is written only as 'unsolvable' (absent = solvable), on cards and findings rows alike
  for (const x of [...index.runs, ...f0.cells, ...f0.pooled]) assert.ok(x.goal === undefined || x.goal === 'unsolvable');
  for (const r of index.runs) assert.equal(r.goal === 'unsolvable', r.caveats.includes('unsolvable'), `${r.id} goal caveat`);
  const ck = f0.cells.map((c) => `${c.model}|${gi(c)}|${c.iface}|${c.pop}`), pk = f0.pooled.map((p) => `${p.model}|${gi(p)}|${p.iface}`);
  assert.equal(new Set(ck).size, ck.length, 'cells unique per (model, goal, iface, pop)');
  assert.equal(new Set(pk).size, pk.length, 'pooled unique per (model, goal, iface)');
  const hasU = [...f0.cells, ...f0.pooled].some((x) => x.goal === 'unsolvable');
  assert.equal(Boolean(f0.goals), hasU, 'findings.goals only with unsolvable rows');
  for (const r of index.runs) {
    assert.equal(r.density.length, 64);
    assert.match(r.hash, /^[0-9a-f]{8}$/);
    assert.equal(r.core, `runs/${r.id}.${r.hash}.core.json`);
    assert.equal(r.detail, `runs/${r.id}.${r.hash}.detail.json`);
    for (const k of ['discovered', 'solved', 'posts', 'liveEdges', 'boardEvents', 'events', 'nodes']) assert.ok(isInt(r.stats[k]), k);
  }
  const f = index.findings;
  assert.ok(['analysis.json', 'obs'].includes(f.source));
  assert.ok(Array.isArray(f.mismatches) && Array.isArray(f.caveats) && f.pooled.length > 0 && f.cells.length > 0);
  for (const p of f.pooled) {
    const ks = p.goal === 'unsolvable' ? ['contacted', 'posted', 'posts', 'board_steps', 'share_board', 'live_edges']
      : ['contacted', 'posted', 'posts', 'board_steps', 'board_steps_free'];
    for (const k of ks) assert.equal(typeof p.metrics[k].v, 'number');
    if (p.goal === 'unsolvable') assert.equal(p.metrics.board_steps_free, undefined, 'free time is n/a');
  }
});

for (const card of index?.runs ?? []) {
  test(`bundle ${card.id}`, () => {
    const coreRaw = readFileSync(join(DATA, card.core), 'utf8');
    const core = JSON.parse(coreRaw);
    const detail = json(join(DATA, card.detail));
    assert.equal(core.format, 'swarmpit.run/1');
    assert.equal(detail.format, 'swarmpit.detail/1');
    assert.equal(core.hash, card.hash);
    assert.equal(detail.hash, core.hash);
    // hash = sha256(core JSON without hash)[:8], compact separators, key order preserved
    const { hash, ...rest } = core;
    assert.equal(createHash('sha256').update(JSON.stringify(rest)).digest('hex').slice(0, 8), hash);
    assert.equal(core.run.goal, card.goal, 'core and card agree on the goal (absent = solvable)');

    const { run, strings, nodes, edges, events: ev, fires, agents, chapters, segments, series } = core;
    assert.equal(strings[0], '');
    assert.equal(run.id, card.id);
    assert.equal(card.durationMs, run.tEndMs);
    assert.equal(agents.length, run.nAgents);
    const nN = nodes.length;

    // nodes: kind order task, board, agents, threads, posts, personas, handles
    assert.equal(nodes[0].id, 'task');
    assert.equal(nodes[1].id, 'board');
    for (let i = 1; i < nN; i++) assert.ok(nodes[i].k >= nodes[i - 1].k, `node kind order at ${i}`);
    assert.equal(new Set(nodes.map((n) => n.id)).size, nN, 'unique node ids');
    for (const n of nodes) {
      assert.ok(n.k >= 0 && n.k < NODE_KINDS.length);
      assert.ok(n.label.length <= 48);
      assert.ok(n.t0 === null || isInt(n.t0));
      const kind = NODE_KINDS[n.k];
      if (kind === 'agent') assert.match(n.id, /^a\d+$/);
      if (kind === 'thread') assert.match(n.id, /^th:/);
      if (kind === 'post') {
        assert.match(n.id, /^[spd]\d+$/);
        assert.equal(NODE_KINDS[nodes[n.thread].k], 'thread');
        assert.ok(['agent', 'persona', 'handle'].includes(NODE_KINDS[nodes[n.author].k]));
        assert.ok((n.preview ?? '').length <= 240);
        if (n.seed) assert.ok(n.tSeed <= 0 && n.t0 === null);
      }
      if (kind === 'persona') assert.match(n.id, /^seed:/);
      if (kind === 'handle') assert.match(n.id, /^name:/);
    }

    // edges unique by (s,d,k)
    const seen = new Set();
    edges.forEach((e, i) => {
      assert.ok(e.s >= 0 && e.s < nN && e.d >= 0 && e.d < nN, `edge ${i}`);
      assert.ok(e.k >= 0 && e.k < EDGE_KINDS.length);
      const k = `${e.s},${e.d},${e.k}`;
      assert.ok(!seen.has(k), `duplicate edge ${k}`);
      seen.add(k);
      if (e.liveLive) assert.ok(NODE_KINDS[nodes[e.s].k] === 'agent' && NODE_KINDS[nodes[e.d].k] === 'agent');
    });

    // events: equal-length int columns, sorted by t, refs in range, t in [0, tEnd]
    const L = ev.t.length;
    for (const c of 'tdkanefsclx') {
      assert.equal(ev[c].length, L, `events.${c}`);
      assert.ok(ev[c].every(isInt), `events.${c} ints`);
    }
    for (let i = 0; i < L; i++) {
      if (i) assert.ok(ev.t[i] >= ev.t[i - 1], 'events sorted');
      assert.ok(ev.t[i] >= 0 && ev.t[i] <= run.tEndMs + 1);
      assert.ok(ev.k[i] < EVENT_KINDS.length && ev.n[i] >= -1 && ev.n[i] < nN && ev.e[i] >= -1 && ev.e[i] < edges.length);
      assert.ok(ev.a[i] >= -1 && ev.a[i] < run.nAgents && ev.l[i] >= 0 && ev.l[i] < strings.length);
      assert.ok(ev.c[i] >= -1 && ev.c[i] < STEP_CATS.length && ev.x[i] >= -1 && ev.x[i] < detail.items.length);
      if (EVENT_KINDS[ev.k[i]] === 'idle') assert.equal(ev.x[i], -1, 'idle steps carry no detail');
      if (EVENT_KINDS[ev.k[i]] === 'post' && ev.f[i] & LEAK) {
        assert.ok((nodes[ev.n[i]].flags ?? []).includes('secret_leak'));
      }
    }
    const xs = ev.x.filter((x) => x >= 0);
    assert.deepEqual(xs, xs.map((_, i) => i), 'detail items in event order');

    // fires sorted by (e, t); every edge fires; ev links valid
    const F = fires.e.length;
    for (const c of ['t', 'w', 'ev']) assert.equal(fires[c].length, F);
    for (let i = 1; i < F; i++) {
      assert.ok(fires.e[i] > fires.e[i - 1] || (fires.e[i] === fires.e[i - 1] && fires.t[i] >= fires.t[i - 1]), 'fires sorted');
    }
    assert.equal(new Set(fires.e).size, edges.length, 'every edge fires');
    assert.ok(fires.ev.every((x) => x >= -1 && x < L));

    agents.forEach((a, i) => {
      assert.equal(a.id, i);
      assert.equal(nodes[a.node].id, `a${i}`);
      assert.equal(a.drift.t.length, a.drift.a.length);
      assert.ok(a.drift.a.every((v) => v >= 0 && v <= 1));
    });
    for (let i = 1; i < chapters.length; i++) assert.ok(chapters[i].t >= chapters[i - 1].t);
    for (const c of chapters) {
      assert.ok(CHAPTER_KINDS.includes(c.k) && [1, 2, 3].includes(c.p));
      if (c.node !== undefined) assert.ok(c.node >= 0 && c.node < nN);
      if (c.ev !== undefined) assert.ok(c.ev >= 0 && c.ev < L);
    }
    assert.equal(segments[0].t0, 0);
    assert.ok(segments.at(-1).t1 >= run.tEndMs);
    for (let i = 1; i < segments.length; i++) assert.equal(segments[i].t0, segments[i - 1].t1);
    assert.equal(series.boardShare.length, run.nAgents);
    for (const row of series.boardShare) assert.equal(row.length, series.density.length);

    // detail
    for (const it of detail.items) assert.ok(['step', 'think', 'req', 'line'].includes(it.t));
    const postIds = new Set(nodes.filter((n) => NODE_KINDS[n.k] === 'post').map((n) => n.id));
    for (const [id, p] of Object.entries(detail.posts)) {
      assert.ok(postIds.has(id), `post ${id}`);
      assert.ok(p.body.length <= 1500);
      for (const [a, b] of p.leakSpans) assert.ok(a >= 0 && a < b && b <= p.body.length);
    }

    // budgets and public safety (belt and braces next to check_public.py)
    assert.ok(coreRaw.length <= (card.id.startsWith('_synth') ? 900 : 400) * 1024);
    assert.ok(statSync(join(DATA, card.detail)).size <= 600 * 1024);
    for (const lit of ['/Users/', 'krister', '.env']) assert.ok(!coreRaw.includes(lit), lit);
  });
}

test('no orphan bundle files', { skip: !haveData && 'run tools/export.py first' }, () => {
  const want = new Set(index.runs.flatMap((r) => [r.core.split('/')[1], r.detail.split('/')[1]]));
  assert.deepEqual(new Set(readdirSync(join(DATA, 'runs'))), want);
});

test('chapter markers sit on their event and solves keep their own markers', { skip: !haveData && 'run tools/export.py first' }, () => {
  for (const f of readdirSync(join(DATA, 'runs')).filter((x) => x.endsWith('.core.json'))) {
    const c = JSON.parse(readFileSync(join(DATA, 'runs', f), 'utf8'));
    for (const ch of c.chapters) if (Number.isInteger(ch.ev)) assert.equal(ch.t, c.events.t[ch.ev], `${f} chapter ${ch.k} at ${ch.t}`);
    const solved = c.agents.filter((a) => a.solveT !== null).length;
    if (solved) assert.ok(c.chapters.some((ch) => ch.k === 'solve'), `${f}: ${solved} solve(s) but no solve chapter`);
  }
});
