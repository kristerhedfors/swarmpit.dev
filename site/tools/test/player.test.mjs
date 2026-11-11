// Builder C tests: clock skip-quiet math, filters encode/decode round trip, model queries.
// Imports compiled DOM-free modules from public/js (run `npm run build` first).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const js = (p) => import(join(root, 'js', p));
const { buildModel, upperBound } = await js('data/model.js');
const { createFilterStore, encodeFilters, decodeFilters, defaultFilters, compileMask } = await js('data/filters.js');
const { createClock, skipMult } = await js('player/clock.js');
const { parseUrl, formatHash } = await js('player/url.js');
const { keyAction } = await js('player/keys.js');
const { assertCore, assertDetail } = await js('data/loader.js');

function loadRun(id) {
  const idx = JSON.parse(readFileSync(join(root, 'data', 'index.json'), 'utf8'));
  const card = idx.runs.find((r) => r.id === id) ?? idx.runs[0];
  const core = assertCore(JSON.parse(readFileSync(join(root, 'data', card.core), 'utf8')));
  const detail = assertDetail(JSON.parse(readFileSync(join(root, 'data', card.detail), 'utf8')), core.hash);
  return { idx, card, core, detail };
}
const hasData = (() => { try { return readdirSync(join(root, 'data', 'runs')).some((f) => f.endsWith('.core.json')); } catch { return false; } })();

test('skipMult: no skip in busy stretches, capped at 12, eases to 1 near the event', () => {
  assert.equal(skipMult(100, 300), 1);
  assert.equal(skipMult(0, 13000), 12);
  assert.equal(skipMult(5000, 400), 1);
  assert.ok(skipMult(5000, 2000) > 1 && skipMult(5000, 2000) < 12);
});

test('clock: seek clamps, scrub restores play state, end stops and play restarts', { skip: !hasData }, () => {
  const { card, core } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  const c = createClock();
  const seen = [];
  c.on('ended', () => seen.push('ended'));
  c.setModel(m);
  c.seek(-5, 'api'); assert.equal(c.t, 0);
  c.seek(1e12, 'api'); assert.equal(c.t, m.tEnd);
  c.seek(1000, 'api'); c.play();
  c.beginScrub(); assert.equal(c.playing, false); c.endScrub(); assert.equal(c.playing, true);
  assert.ok(c.advance(16)); assert.ok(Math.abs(c.t - 1016) < 1e-6);
  c.setRate(4); c.advance(10); assert.ok(Math.abs(c.t - 1056) < 1e-6);
  c.seek(m.tEnd - 5, 'api'); c.advance(100);
  assert.equal(c.t, m.tEnd); assert.equal(c.playing, false); assert.deepEqual(seen, ['ended']);
  c.play(); assert.equal(c.t, 0); assert.equal(c.playing, true);
});

test('clock: skip-quiet passes the boot gap quickly but never skips events', { skip: !hasData }, () => {
  const { card, core } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  const c = createClock();
  c.setModel(m);
  c.setSkipQuiet(true);
  const hidden = new Set([5, 9, 21, 19, 20, 4]); // think, idle, proto, connect, disconnect, retry
  const vis = []; for (let i = 0; i < m.nEvents; i++) if (!hidden.has(m.evK[i])) vis.push(m.evT[i]);
  // Find the longest quiet gap after the first visible event and time how long it takes in wall ms at 1x.
  let g0 = 0, g1 = 0;
  for (let i = 1; i < vis.length; i++) if (vis[i] - vis[i - 1] > g1 - g0) { g0 = vis[i - 1]; g1 = vis[i]; }
  c.seek(g0, 'api'); c.play();
  let wall = 0;
  while (c.t < g1 && wall < 60000) { c.advance(16); wall += 16; }
  assert.ok(c.t >= g1 - 1e-6, 'reached the event');
  assert.ok(Math.abs(c.t - g1) < 20, `did not overshoot (${c.t} vs ${g1})`);
  if (g1 - g0 > 5000) assert.ok(wall < (g1 - g0) / 2.5, `gap ${g1 - g0} ms took ${wall} ms wall`);
});

test('filters: encode/decode round trip and default is empty', () => {
  assert.equal(encodeFilters(defaultFilters()), '');
  const s = defaultFilters();
  s.agents = new Set([0, 2]); s.neighbourhoodOf = 7; s.recentMs = 30000; s.range = [1200, 9000]; s.mode = 'hide';
  s.text = 'flag~recovery & co'; s.phases = new Set(['free']); s.nodeKinds.delete('trail'); s.edgeKinds.add('exposure');
  s.eventKinds.add('think');
  const enc = encodeFilters(s);
  const d = decodeFilters(enc);
  assert.equal(encodeFilters(d), enc);
  assert.deepEqual([...d.agents].sort(), [0, 2]);
  assert.equal(d.text, s.text); assert.deepEqual(d.range, [1200, 9000]); assert.equal(d.mode, 'hide');
  assert.ok(!d.nodeKinds.has('trail') && d.edgeKinds.has('exposure') && d.eventKinds.has('think'));
});

test('filters: mask semantics (agents, kinds, search hits, active count)', { skip: !hasData }, () => {
  const { card, core, detail } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  m.attachDetail(detail);
  const st = createFilterStore();
  let changes = 0;
  st.on('change', () => changes++);
  st.bind(m);
  assert.equal(st.mask().activeCount, 0);
  st.set({ agents: new Set([1]) });
  const mk = st.mask();
  assert.equal(mk.activeCount, 1);
  for (let i = 0; i < m.nEvents; i++) if (m.evA[i] >= 0 && m.evA[i] !== 1) assert.ok(mk.event[i] < 2);
  assert.equal(mk.node[core.agents[0].node], 1); // dim mode
  st.set({ mode: 'hide' }); assert.equal(st.mask().node[core.agents[0].node], 0);
  st.reset(); st.set({ text: 'FLAG{' });
  assert.ok(st.mask().hits.length > 0);
  for (const i of st.mask().hits) assert.equal(st.mask().event[i], 3);
  assert.ok(changes >= 4);
  const v = compileMask(m, defaultFilters(), 1);
  for (let i = 0; i < m.nEvents; i++) if (m.evK[i] === 5) assert.equal(v.event[i], 0); // think hidden by default
});

test('model: counts match the bundle (posts, upvotes, solve times, tEnd)', { skip: !hasData }, () => {
  const { card, core } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  assert.equal(m.tEnd, core.run.tEndMs);
  assert.equal(m.nAgents, card.nAgents);
  const livePosts = core.nodes.filter((n) => n.k === 4 && !n.seed && n.id.startsWith('p')).length;
  assert.equal(card.stats.posts, livePosts);
  let ups = 0; for (let i = 0; i < m.nEvents; i++) if (m.evK[i] === 15) ups++;
  // upvotes visible via nodeStats at tEnd equal upvotes0 + upvote events
  let byStats = 0, base = 0;
  core.nodes.forEach((n, i) => { if (n.k === 4) { byStats += m.nodeStats(i, m.tEnd).upvotes; base += n.upvotes0 ?? 0; } });
  assert.equal(byStats - base, ups);
  core.agents.forEach((a, i) => {
    const s = m.agentState(i, m.tEnd);
    assert.equal(s.solved, a.solveT !== null);
    if (a.solveT !== null) { assert.equal(m.agentState(i, a.solveT - 1).solved, false); assert.equal(m.agentState(i, a.solveT).phase, 'free'); }
    assert.equal(s.posts, core.nodes.filter((n) => n.k === 4 && n.agent === i && !n.seed).length);
  });
});

test('model: queries are pure in t (forward == backward) and eventAt/nextEvent agree', { skip: !hasData }, () => {
  const { card, core } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  const ts = Array.from({ length: 40 }, (_, i) => Math.round((i / 39) * m.tEnd));
  const snap = (t) => JSON.stringify([m.eventAt(t), m.swarmAt(t), [...Array(m.nAgents).keys()].map((a) => m.agentState(a, t))]);
  const fwd = ts.map(snap), bwd = [...ts].reverse().map(snap).reverse();
  assert.deepEqual(fwd, bwd);
  for (const t of ts) {
    const i = m.eventAt(t);
    assert.ok(i === -1 || m.evT[i] <= t); assert.ok(i + 1 >= m.nEvents || m.evT[i + 1] > t);
    const n = m.nextEvent(t, 1);
    assert.ok(n === -1 || m.evT[n] > t);
    const p = m.nextEvent(t, -1);
    assert.ok(p === -1 || m.evT[p] < t);
  }
  assert.equal(upperBound([1, 2, 2, 3], 2), 3);
  for (let e = 0; e < m.nEdges; e++) {
    const w = m.edgeWeightAt(e, m.tEnd);
    assert.equal(w.n, m.fireStart[e + 1] - m.fireStart[e]);
  }
});

test('model: search finds post bodies and commands once detail is attached', { skip: !hasData }, () => {
  const { card, core, detail } = loadRun('forum-multi4-r1');
  const m = buildModel(card, core);
  const before = m.search('whoami').events.length;
  m.attachDetail(detail);
  assert.ok(m.search('whoami').events.length >= before);
  assert.ok(m.search('').events.length === 0);
  assert.throws(() => m.attachDetail({ ...detail, hash: 'deadbeef' }));
});

test('url: parse/format round trip and junk rejection', () => {
  const u = parseUrl('?renderer=2d&perf=1', '#run=forum-multi4-r1&t=23.4&rate=2&mode=worldline&sel=a0&f=a0.2');
  assert.deepEqual(u, { run: 'forum-multi4-r1', t: 23400, rate: 2, mode: 'worldline', sel: 'a0', f: 'a0.2', renderer: 'canvas2d', perf: true });
  assert.deepEqual(parseUrl('', formatHash(u)), u);
  const bad = parseUrl('', '#run=../../etc&t=-3&rate=3&mode=x');
  assert.deepEqual(bad, {});
});

test('keys: YouTube map', () => {
  assert.equal(keyAction({ key: ' ' }), 'toggle');
  assert.equal(keyAction({ key: 'k' }), 'toggle');
  assert.equal(keyAction({ key: 'j' }), 'back10');
  assert.equal(keyAction({ key: 'ArrowRight' }), 'fwd5');
  assert.equal(keyAction({ key: '>' }), 'faster');
  assert.deepEqual(keyAction({ key: '7' }), { seekPct: 70 });
  assert.equal(keyAction({ key: 'C' }), 'follow');
  assert.equal(keyAction({ key: 'k', ctrlKey: true }), null);
});
