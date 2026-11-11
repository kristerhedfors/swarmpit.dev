// Findings slices per (model, goal) for the findings panel (src/data/findingsData.ts). DOM-free modules from
// public/js (run `npm run build` first). Fixtures are tiny synthetic rows built here (ids and numbers only).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const F = await import(join(root, 'js', 'data', 'findingsData.js'));

const Q = 'qwen-3.8-27b', G = 'zai-org/GLM-5.3-Flash';
const m = (v) => ({ v, lo: null, hi: null });
const sol = (model, iface, n, c, p, ps, bs, bsf) => ({ model, iface, nAgents: n,
  metrics: { contacted: m(c), posted: m(p), posts: m(ps), board_steps: m(bs), board_steps_free: m(bsf) } });
const uns = (model, iface, n, c, p, ps, bs, sb, le) => ({ model, goal: 'unsolvable', iface, nAgents: n,
  metrics: { contacted: m(c), posted: m(p), posts: m(ps), board_steps: m(bs), share_board: m(sb), live_edges: m(le) } });
const cell = (model, iface, pop, n, solveRate, o = {}) => ({ model, iface, pop, nAgents: n, discoveryRate: 0.5,
  shareAfterContact: null, posts: 1, solveRate, boardSteps: 2, excludedByDefault: false, ...o });

const f = {
  source: 'analysis.json', note: 'n', caveats: [], mismatches: [], models: [Q, G], goals: ['solvable', 'unsolvable'],
  pooled: [sol(Q, 'forum', 4, 1, 0.5, 1, 6, 3), sol(Q, 'wiki', 4, 0.5, 0, 0, 2, 1),
    uns(Q, 'forum', 4, 1, 1, 3, 20, 0.5, 2), uns(Q, 'wiki', 4, 0.5, 0.5, 1, 10, 0.25, 0), sol(G, 'forum', 2, 1, 1, 2, 4, 2)],
  cells: [cell(Q, 'forum', 'multi', 4, 1), cell(Q, 'wiki', 'multi', 4, 0.5),
    cell(Q, 'forum', 'multi', 4, 0, { goal: 'unsolvable' }), cell(Q, 'wiki', 'multi', 4, 0, { goal: 'unsolvable' }),
    cell(G, 'forum', 'multi', 2, 1)],
};

test('findingsGoals: explicit list, or derived from rows', () => {
  assert.deepEqual(F.findingsGoals(f), ['solvable', 'unsolvable']);
  const { goals, ...old } = f;
  assert.deepEqual(F.findingsGoals(old), ['solvable', 'unsolvable']);
  assert.deepEqual(F.findingsGoals({ ...old, pooled: old.pooled.filter((p) => !p.goal), cells: [] }), ['solvable']);
});

test('findingsView keeps one model and one goal; rows without model are the primary', () => {
  const v = F.findingsView(f, Q, 'unsolvable', Q);
  assert.deepEqual(v.pooled.map((p) => p.iface), ['forum', 'wiki']);
  assert.ok(v.pooled.every((p) => p.goal === 'unsolvable') && v.cells.every((c) => c.goal === 'unsolvable'));
  assert.equal(F.findingsView(f, G, 'unsolvable', Q).pooled.length, 0);
  const legacy = { ...f, pooled: [{ ...f.pooled[0], model: undefined }], cells: [] };
  assert.equal(F.findingsView(legacy, Q, 'solvable', Q).pooled.length, 1);
});

test('n/a metrics: free time does not exist under the unsolvable goal', () => {
  const v = F.findingsView(f, Q, 'unsolvable', Q);
  assert.equal(F.metricOf(v.pooled[0], 'board_steps_free'), null);
  assert.equal(F.weighted(v.pooled, 'board_steps_free'), null);
  assert.equal(F.weighted(v.pooled, 'share_board'), 0.375);
  assert.equal(F.solveRate(v), 0);
  assert.equal(F.solveRate(F.findingsView(f, Q, 'solvable', Q)), 0.75);
});

test('goal comparison per model', () => {
  const rows = F.goalComparison(f, Q, Q);
  const by = Object.fromEntries(rows.map((r) => [r.k, r.v]));
  assert.deepEqual(by.contacted, { solvable: 0.75, unsolvable: 0.75 });
  assert.deepEqual(by.posts, { solvable: 0.5, unsolvable: 2 });
  assert.deepEqual(by.board_steps, { solvable: 4, unsolvable: 15 });
  assert.deepEqual(by.solve_rate, { solvable: 0.75, unsolvable: 0 });
  assert.deepEqual(by.board_steps_free, { solvable: 2, unsolvable: null });
  assert.deepEqual(F.goalComparison(f, G, Q), [], 'glm has no unsolvable rows');
});

test('headline rows skip cells excluded by default', () => {
  const g = { ...f, cells: f.cells.map((c) => (c.iface === 'wiki' ? { ...c, excludedByDefault: true } : c)) };
  const v = F.findingsView(g, Q, 'unsolvable', Q);
  assert.deepEqual(F.headlineRows(v).map((p) => p.iface), ['forum']);
});
