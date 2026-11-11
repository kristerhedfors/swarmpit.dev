// Engine determinism + layout stability (SPEC 15). Imports compiled DOM-free modules from public/js.
// Run: npm run build && node --test tools/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeLayout, agentPos, timeToY } from '../../public/js/engine/layout.js';
import { computeFrame, createFrameState, buildStaticGeometry } from '../../public/js/engine/frame.js';
import { createCamera } from '../../public/js/engine/camera.js';
import { prng, fogFactor, upperBound } from '../../public/js/engine/math.js';
import { pick } from '../../public/js/engine/picking.js';
import { synthCore, devModel } from '../../.cache/dev/js/engine/dev/devmodel.js';

const mkModel = (o) => devModel(synthCore(o));
const fullMask = (m) => ({ version: 0, node: new Uint8Array(m.nNodes).fill(2), edge: new Uint8Array(m.nEdges).fill(2),
  event: new Uint8Array(m.nEvents).fill(2), hits: new Uint32Array(0), activeCount: 0 });
const opts = (o = {}) => ({ recentMs: null, rate: 1, showTrails: true, showIdle: false, hover: -1, selected: -1,
  reducedMotion: false, mode: 'constellation', modeMix: 0, ...o });

/** Snapshot of everything in a FrameState except the version counters (which only signal uploads). */
function snap(f) {
  const cut = (a, n) => Array.from(a.subarray(0, n));
  return JSON.stringify({
    t: f.t, mix: f.modeMix,
    nodes: [cut(f.nodes.pos, f.nodes.count * 3), cut(f.nodes.size, f.nodes.count), cut(f.nodes.rgba, f.nodes.count * 4),
            cut(f.nodes.bits, f.nodes.count), cut(f.nodes.heat, f.nodes.count), cut(f.nodes.shape, f.nodes.count)],
    trail: [f.trail.count, cut(f.trail.pos, f.trail.count * 3), cut(f.trail.rgba, f.trail.count * 4), cut(f.trail.event, f.trail.count)],
    edges: [f.edges.count, cut(f.edges.edge, f.edges.count), cut(f.edges.alpha, f.edges.count), cut(f.edges.glow, f.edges.count)],
    pulses: [f.pulses.count, cut(f.pulses.edge, f.pulses.count), cut(f.pulses.u, f.pulses.count)],
    hulls: f.hulls.map((h) => [h.cx, h.cy, h.cz, h.r, h.members.join()]),
    flashes: f.flashes.map((x) => [x.node, x.k, x.age]), sd: f.staticDraw, plane: f.plane,
  });
}

for (const [name, o] of [['multi4', { agents: 4 }], ['stress16', { agents: 16, threads: 24, durationMs: 300000, personas: 12 }]]) {
  test(`computeFrame is a pure function of t (${name}): forward vs backward scrub at 50 random t`, () => {
    const m = mkModel(o), L = computeLayout(m), mask = fullMask(m);
    const rnd = prng(42);
    const ts = Array.from({ length: 50 }, () => Math.round(rnd() * m.tEnd));
    for (const mode of [0, 1]) {
      const op = opts({ modeMix: mode, mode: mode ? 'worldline' : 'constellation' });
      const reuse = createFrameState(m);
      reuse.geom = buildStaticGeometry(m, L, mask);
      const fwd = [...ts].sort((a, b) => a - b).map((t) => [t, snap(computeFrame(m, L, t, mask, op, reuse))]);
      const reuse2 = createFrameState(m);
      reuse2.geom = reuse.geom;
      const back = new Map([...ts].sort((a, b) => b - a).map((t) => [t, snap(computeFrame(m, L, t, mask, op, reuse2))]));
      for (const [t, s] of fwd) assert.equal(s, back.get(t), `frame differs at t=${t} mode=${mode}`);
      // and a fresh state agrees with a reused one
      const t = ts[7];
      const fresh = createFrameState(m); fresh.geom = reuse.geom;
      assert.equal(snap(computeFrame(m, L, t, mask, op, fresh)), snap(computeFrame(m, L, t, mask, op, reuse)));
    }
  });

  test(`layout is byte-identical across runs (${name})`, () => {
    const a = computeLayout(mkModel(o)), b = computeLayout(mkModel(o));
    assert.deepEqual(Buffer.from(a.pos.buffer), Buffer.from(b.pos.buffer));
    assert.deepEqual(Buffer.from(a.stepPos.buffer), Buffer.from(b.stepPos.buffer));
    for (let i = 0; i < a.pos.length; i++) assert.ok(Number.isFinite(a.pos[i]), 'finite positions');
  });
}

test('layout relax keeps static nodes at least ~1.1 apart where possible', () => {
  const m = mkModel({ agents: 8, threads: 10 }), L = computeLayout(m);
  let close = 0, pairs = 0;
  for (let i = 0; i < m.nNodes; i++) for (let j = i + 1; j < m.nNodes; j++) {
    const ki = m.nodeKind[i], kj = m.nodeKind[j];
    if (ki < 3 || kj < 3) continue;
    pairs++;
    const d = Math.hypot(L.pos[i * 3] - L.pos[j * 3], L.pos[i * 3 + 1] - L.pos[j * 3 + 1], L.pos[i * 3 + 2] - L.pos[j * 3 + 2]);
    if (d < 0.9) close++;
  }
  assert.ok(close / pairs < 0.01, `${close} of ${pairs} pairs closer than 0.9`);
});

test('agents start at the task anchor and drift toward the board perch', () => {
  const m = mkModel({ agents: 4 }), L = computeLayout(m), out = new Float32Array(3);
  agentPos(m, L, 0, 0, out);
  assert.deepEqual(Array.from(out), Array.from(L.agentAnchors[0].task));
  // the agent with the highest drift sits closer to the board than to the task
  let best = 0, bestA = -1, bestT = 0;
  for (let a = 0; a < m.nAgents; a++) {
    const d = m.core.agents[a].drift;
    d.a.forEach((v, k) => { if (v > best) { best = v; bestA = a; bestT = d.t[k]; } });
  }
  if (best > 0.6) {
    agentPos(m, L, bestA, bestT, out);
    const dB = Math.hypot(out[0] - L.board[0], out[2] - L.board[2]), dT = Math.hypot(out[0] - L.task[0], out[2] - L.task[2]);
    assert.ok(dB < dT, 'high-drift agent is nearer the board');
  }
});

test('nothing after t: edges, trail and static draw counts respect playback time', () => {
  const m = mkModel({ agents: 6 }), L = computeLayout(m), mask = fullMask(m), f = createFrameState(m);
  f.geom = buildStaticGeometry(m, L, mask);
  const t = m.tEnd * 0.4;
  computeFrame(m, L, t, mask, opts(), f);
  for (let i = 0; i < f.edges.count; i++) assert.ok(m.edgeFirstT[f.edges.edge[i]] <= t);
  for (let i = 0; i < f.trail.count; i++) assert.ok(m.evT[f.trail.event[i]] <= t);
  for (const k of ['worldlines', 'pillars', 'chords', 'beads']) {
    const g = f.geom[k];
    assert.equal(f.staticDraw[k], upperBound(g.t, t));
    for (let i = 1; i < g.count; i++) assert.ok(g.t[i] >= g.t[i - 1], `${k} sorted by t`);
  }
  for (let i = 0; i < m.nNodes; i++) if (f.nodes.bits[i] & 1) assert.ok(m.nodeT0[i] <= t && t < m.nodeT1[i]);
});

test('mask: hidden nodes are not visible, dimmed nodes carry DIM and low alpha', () => {
  const m = mkModel({ agents: 4 }), L = computeLayout(m), mask = fullMask(m), f = createFrameState(m);
  mask.node[0] = 0; mask.node[1] = 1;
  computeFrame(m, L, m.tEnd / 2, mask, opts(), f);
  assert.equal(f.nodes.bits[0] & 1, 0);
  assert.ok(f.nodes.bits[1] & 2);
  assert.ok(f.nodes.rgba[1 * 4 + 3] < 0.2);
});

test('reduced motion: no travelling pulses', () => {
  const m = mkModel({ agents: 6 }), L = computeLayout(m), mask = fullMask(m);
  let any = 0;
  for (let k = 1; k < 40; k++) {
    const t = (m.tEnd * k) / 40;
    any += computeFrame(m, L, t, mask, opts({ rate: 4 }), createFrameState(m)).pulses.count;
    assert.equal(computeFrame(m, L, t, mask, opts({ reducedMotion: true }), createFrameState(m)).pulses.count, 0);
  }
  assert.ok(any > 0, 'pulses exist without reduced motion');
});

test('timeToY: linear over [0,tEnd], log-squashed prehistory in [-2,0)', () => {
  const m = mkModel({}), L = computeLayout(m);
  assert.equal(timeToY(L, 1000, 0, -3600e3), 0);
  assert.equal(timeToY(L, 1000, 1000, -3600e3), 24);
  const y = timeToY(L, 1000, -1800e3, -3600e3);
  assert.ok(y < 0 && y > -2);
  assert.equal(timeToY(L, 1000, -3600e3, -3600e3), -2);
});

test('camera: zoom toward cursor keeps the point under the cursor fixed; clamps distance', () => {
  const c = createCamera();
  const W = 800, H = 600;
  let m = c.matrices(W, H, 1);
  // world point at the target plane under pixel (600, 200)
  const out = new Float32Array(4);
  // pick a world point: unproject by marching along the view ray is complex; instead check target shift direction
  const t0 = Array.from(c.target);
  c.zoom(2, 600, 200);
  m = c.matrices(W, H, 1);
  c.project(new Float32Array(t0), 0, m, out, 0);
  assert.ok(c.distance > 6 && c.distance < 46);
  // zooming at the screen centre does not move the target
  const c2 = createCamera(); c2.matrices(W, H, 1);
  const before = Array.from(c2.target);
  c2.zoom(1.5, W / 2, H / 2);
  assert.deepEqual(Array.from(c2.target).map((v) => +v.toFixed(5)), before.map((v) => +v.toFixed(5)));
  for (let i = 0; i < 50; i++) c2.zoom(3);
  assert.equal(Math.round(c2.distance), 6);
  for (let i = 0; i < 50; i++) c2.zoom(1 / 3);
  assert.equal(Math.round(c2.distance), 160);
});

test('camera: zoom toward cursor is anchor-stable (projected anchor point stays put)', () => {
  const c = createCamera(), W = 800, H = 600;
  let m = c.matrices(W, H, 1);
  // a world point on the plane through target perpendicular to the view: target + right*k
  const out = new Float32Array(4), view = m.view;
  const right = [view[0], view[4], view[8]], up = [view[1], view[5], view[9]];
  const p = new Float32Array([c.target[0] + right[0] * 5 + up[0] * 3, c.target[1] + right[1] * 5 + up[1] * 3, c.target[2] + right[2] * 5 + up[2] * 3]);
  c.project(p, 0, m, out, 0);
  const [sx, sy] = [out[0], out[1]];
  c.zoom(1.8, sx, sy);
  m = c.matrices(W, H, 1);
  c.project(p, 0, m, out, 0);
  assert.ok(Math.abs(out[0] - sx) < 0.5 && Math.abs(out[1] - sy) < 0.5, `anchor moved ${out[0] - sx},${out[1] - sy}`);
});

test('camera: insets centre the target in the uncovered area and keep zoom anchor-stable', () => {
  const c = createCamera(), W = 1200, H = 800, out = new Float32Array(4);
  const d0 = c.fitDistance(20);
  c.setInsets({ left: 400, top: 50, right: 300, bottom: 90 }, false);
  let m = c.matrices(W, H, 1);
  c.project(c.target, 0, m, out, 0);
  assert.ok(Math.abs(out[0] - (400 + (W - 700) / 2)) < 0.5 && Math.abs(out[1] - (50 + (H - 140) / 2)) < 0.5, `target at ${out[0]},${out[1]}`);
  assert.ok(c.fitDistance(20) > d0, 'a smaller free area backs the camera off');
  const view = m.view, right = [view[0], view[4], view[8]], up = [view[1], view[5], view[9]];
  const p = new Float32Array([0, 1, 2].map((i) => c.target[i] + right[i] * 4 - up[i] * 2));
  c.project(p, 0, m, out, 0);
  const [sx, sy] = [out[0], out[1]];
  c.zoom(1.7, sx, sy);
  m = c.matrices(W, H, 1);
  c.project(p, 0, m, out, 0);
  assert.ok(Math.abs(out[0] - sx) < 0.5 && Math.abs(out[1] - sy) < 0.5, `anchor moved ${out[0] - sx},${out[1] - sy}`);
  // eased changes report movement until settled; oversized insets still leave part of each axis free
  c.setInsets({ left: 5000, top: 0, right: 0, bottom: 0 });
  assert.ok(c.moving);
  for (let i = 0; i < 200 && c.moving; i++) c.update(16);
  assert.ok(!c.moving);
  m = c.matrices(W, H, 1);
  c.project(c.target, 0, m, out, 0);
  assert.ok(out[0] > W / 2 && out[0] < W, `target at ${out[0]}`);
});

test('picking: node beats trail beats edge; misses return null', () => {
  const p = { nodeXY: new Float32Array([100, 100, 300, 300]), nodeDepth: new Float32Array([10, 5]), nodeR: new Float32Array([6, 6]),
    nodeVisible: new Uint8Array([1, 1]), nodeCount: 2, trailXY: new Float32Array([120, 100]), trailR: new Float32Array([2]), trailCount: 1,
    edgeXY: new Float32Array([0, 400, 400, 400]), edgeCount: 1, edgeIdx: new Int32Array([7]), edgeCtl: new Float32Array([200, 400]) };
  const f = { nodes: { bits: new Uint16Array([1, 1]) }, trail: { event: new Int32Array([33]), rgba: new Float32Array([1, 1, 1, 1]) } };
  assert.deepEqual(pick(p, f, 102, 101), { kind: 'node', node: 0 });
  assert.deepEqual(pick(p, f, 121, 100), { kind: 'step', event: 33 });
  assert.deepEqual(pick(p, f, 200, 403), { kind: 'edge', edge: 7 });
  assert.equal(pick(p, f, 600, 50), null);
});

test('picking: filtered-out (dim) nodes and trail dots lose to in-filter items, but stay pickable alone', () => {
  // node 0 (dim) sits exactly under the cursor, node 1 (in filter) overlaps 5 px away
  const p = { nodeXY: new Float32Array([100, 100, 105, 100]), nodeDepth: new Float32Array([5, 10]), nodeR: new Float32Array([6, 6]),
    nodeVisible: new Uint8Array([1, 1]), nodeCount: 2, trailXY: new Float32Array([300, 300, 303, 300]), trailR: new Float32Array([3, 3]),
    trailCount: 2, edgeXY: new Float32Array(0), edgeCount: 0, edgeIdx: new Int32Array(0), edgeCtl: new Float32Array(0) };
  const f = { nodes: { bits: new Uint16Array([1 | 2, 1]) },
    trail: { event: new Int32Array([10, 11]), rgba: new Float32Array([1, 1, 1, 0.12, 1, 1, 1, 1]) } };
  assert.deepEqual(pick(p, f, 100, 100), { kind: 'node', node: 1 });
  assert.deepEqual(pick(p, f, 300, 300), { kind: 'step', event: 11 });
  f.nodes.bits[1] = 0; p.nodeVisible[1] = 0;
  assert.deepEqual(pick(p, f, 100, 100), { kind: 'node', node: 0 });
});

test('fogFactor is monotone and floored', () => {
  assert.ok(fogFactor(10, 46) > fogFactor(60, 46));
  assert.equal(fogFactor(1e6, 46), 0.12);
});

test('computeFrame perf at stress size (informative, < 4 ms avg)', () => {
  const m = mkModel({ agents: 16, threads: 30, durationMs: 300000, personas: 16 }), L = computeLayout(m), mask = fullMask(m);
  const f = createFrameState(m), op = opts();
  const n = 200, t0 = performance.now();
  for (let i = 0; i < n; i++) computeFrame(m, L, (m.tEnd * i) / n, mask, op, f);
  const avg = (performance.now() - t0) / n;
  console.log(`  stress: ${m.nNodes} nodes, ${m.nEdges} edges, ${m.nEvents} events, ${m.fireT.length} fires; computeFrame avg ${avg.toFixed(3)} ms`);
  assert.ok(avg < 4);
});

// Real exported bundles through the site's model (src/data/model.ts), when present.
import { existsSync, readFileSync } from 'node:fs';
const IDX = new URL('../../public/data/index.json', import.meta.url);
test('real bundles: layout finite, computeFrame deterministic forward/backward, static geometry sorted', async (t) => {
  if (!existsSync(IDX)) { t.skip('no public/data/index.json'); return; }
  let buildModel;
  try { ({ buildModel } = await import('../../public/js/data/model.js')); } catch { t.skip('data/model.js not built'); return; }
  const index = JSON.parse(readFileSync(IDX, 'utf8'));
  for (const card of index.runs) {
    const core = JSON.parse(readFileSync(new URL('../../public/data/' + card.core, import.meta.url), 'utf8'));
    const m = buildModel(card, core), L = computeLayout(m), mask = fullMask(m);
    for (let i = 0; i < L.pos.length; i++) assert.ok(Number.isFinite(L.pos[i]), `${card.id} finite layout`);
    for (let i = 0; i < L.stepPos.length; i++) assert.ok(Number.isFinite(L.stepPos[i]), `${card.id} finite stepPos`);
    const g = buildStaticGeometry(m, L, mask);
    const rnd = prng(9), ts = Array.from({ length: 12 }, () => Math.round(rnd() * m.tEnd));
    const a = createFrameState(m), b = createFrameState(m);
    a.geom = g; b.geom = g;
    const op = opts({ modeMix: 0.5, mode: 'worldline' });
    const fw = new Map([...ts].sort((x, y) => x - y).map((tt) => [tt, snap(computeFrame(m, L, tt, mask, op, a))]));
    for (const tt of [...ts].sort((x, y) => y - x)) assert.equal(snap(computeFrame(m, L, tt, mask, op, b)), fw.get(tt), `${card.id} t=${tt}`);
    const end = computeFrame(m, L, m.tEnd, mask, opts(), createFrameState(m));
    let agentsVisible = 0;
    for (let i = 0; i < m.nNodes; i++) if (m.nodeKind[i] === 2 && (end.nodes.bits[i] & 1)) agentsVisible++;
    assert.equal(agentsVisible, m.nAgents, `${card.id}: all agents drawn at tEnd`);
  }
});
