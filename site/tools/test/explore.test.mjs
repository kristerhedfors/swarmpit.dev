// Landscape explorer data layer (src/explore/data.ts, state.ts). DOM-free modules from public/js (run
// `npm run build` first), exercised against the synthetic fixture in explore-fixture.test.mjs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeFixture } from './explore-fixture.test.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const D = await import(join(root, 'js', 'explore', 'data.js'));
const S = await import(join(root, 'js', 'explore', 'state.js'));

const raw = makeFixture();
const ex = D.normalize(structuredClone(raw));
const close = (a, b, eps = 1e-4) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('normalize keeps every valid row and the declared dimensions', () => {
  assert.equal(ex.episodes.length, raw.episodes.length);
  assert.equal(ex.dropped, 0);
  assert.equal(Object.keys(ex.seeds).length, Object.keys(raw.seeds).length);
  assert.deepEqual(ex.dims.model.map((m) => m.slug), ['qwen38', 'mistral24', 'glm53f', 'gemma31', 'gptoss120']);
  assert.equal(ex.dims.model.find((m) => m.slug === 'gptoss120').retired, true);
  assert.deepEqual(Object.keys(ex.landscapes).sort(), ['solvable', 'unsolvable']);
});

test('normalize rejects a wrong schema and drops malformed or free-text rows', () => {
  assert.throws(() => D.normalize({ ...raw, schema: 'other' }), /E_EXPLORER_SCHEMA/);
  assert.throws(() => D.normalize(null), /E_EXPLORER_SCHEMA/);
  const bad = structuredClone(raw);
  const base = bad.episodes[0];
  bad.episodes = [
    base,
    { ...base, run_id: 'has space' },
    { ...base, trigger_code: 'not an enum' },
    { ...base, model: '<b>x</b>' },
    { ...base, instigation_score: Number.NaN },
    { ...base, instigation_score: '0.5' },
    { ...base, posts_over_time: [[1, 2]] },
    { ...base, agent_codes: ['free text'] },
    { ...base, panel: { slugs: ['qwen38'], agreement_mean: 'x' } },
    { ...base, engagement: { board_reads: 1 } },
    { ...base, run_ttfw_s: Infinity },
  ];
  bad.episodes.push({ ...base, run_id: base.run_id + 'x', elite_in: ['a b', 'unsolvable/qwen38/NONE|forum|le120', 7] });
  const n = D.normalize(bad);
  assert.equal(n.episodes.length, 2);
  assert.equal(n.dropped, bad.episodes.length - 2);
  assert.deepEqual(n.episodes[1].elite_in, ['unsolvable/qwen38/NONE|forum|le120']);
  const labels = structuredClone(raw);
  labels.dims.model[0].label = '<img src=x onerror=1>';
  labels.dims.channel[0].label = 'a\nb';
  const m = D.normalize(labels);
  assert.equal(m.dims.model[0].label, 'qwen38');
  assert.equal(m.dims.channel[0].label, 'forum');
});

test('wilson interval matches the closed form', () => {
  const a = D.wilson(5, 10);
  close(a.p, 0.5); close(a.lo, 0.2366); close(a.hi, 0.7634);
  const z = D.wilson(0, 10);
  close(z.lo, 0); close(z.hi, 0.2775);
  const f = D.wilson(10, 10);
  close(f.hi, 1); close(f.lo, 0.7225);
  assert.equal(D.wilson(0, 0), null);
});

test('aggregate: rates, means, maxima and empty buckets', () => {
  const eps = ex.episodes.slice(0, 30);
  const w = D.aggregate(eps, 'wrate');
  assert.equal(w.n, 30);
  assert.equal(w.k, eps.filter((e) => e.n_writers > 0).length);
  assert.ok(w.lo <= w.value && w.value <= w.hi);
  assert.equal(D.aggregate(eps, 'pile').k, eps.filter((e) => e.n_writers >= 2).length);
  assert.equal(D.aggregate(eps, 'max_writers').value, Math.max(...eps.map((e) => e.n_writers)));
  close(D.aggregate(eps, 'mean_score').value, eps.reduce((a, e) => a + e.instigation_score, 0) / 30);
  assert.equal(D.aggregate([], 'mean_score').value, null);
  assert.equal(D.aggregate([], 'episodes').value, 0);
  assert.equal(D.fmtMetric('wrate', 0.256), '26%');
  assert.equal(D.fmtMetric('mean_score', null), '–');
});

test('filters: empty = all, OR within a dimension, AND across, seed restriction, except-dimension', () => {
  const f = D.emptyFilters();
  assert.equal(D.applyFilters(ex, f).length, ex.episodes.length);
  f.model = ['qwen38', 'glm53f'];
  f.goal = ['unsolvable'];
  const got = D.applyFilters(ex, f);
  assert.ok(got.length > 0 && got.every((e) => ['qwen38', 'glm53f'].includes(e.model) && e.goal === 'unsolvable'));
  assert.equal(D.applyFilters(ex, f, '', 'model').length, ex.episodes.filter((e) => e.goal === 'unsolvable').length);
  const seed = ex.episodes[0].record_id;
  assert.ok(D.applyFilters(ex, D.emptyFilters(), seed).every((e) => e.record_id === seed));
  const wf = { ...D.emptyFilters(), writers: ['yes'] };
  assert.ok(D.applyFilters(ex, wf).every((e) => e.n_writers > 0));
  const lb = { ...D.emptyFilters(), length_band: ['le120'], origin: ['mutation'] };
  assert.ok(D.applyFilters(ex, lb).every((e) => ex.seeds[e.record_id].length_band === 'le120' && ex.seeds[e.record_id].origin === 'mutation'));
  assert.equal(D.filtersActive(D.emptyFilters()), false);
  assert.equal(D.filtersActive(D.emptyFilters(), seed), true);
});

test('dimDomain follows the declared order; generation is numeric', () => {
  assert.deepEqual(D.dimDomain(ex, 'model'), ['qwen38', 'mistral24', 'glm53f', 'gemma31', 'gptoss120']);
  assert.deepEqual(D.dimDomain(ex, 'goal'), ['solvable', 'unsolvable']);
  const gens = D.dimDomain(ex, 'generation').map(Number);
  assert.deepEqual(gens, [...gens].sort((a, b) => a - b));
  assert.equal(D.dimLabel(ex, 'model', 'gptoss120'), 'GPT-OSS 120B');
  assert.equal(D.dimLabel(ex, 'length_band', 'gt320'), '>320');
});

test('pivot: cells partition the episodes; margins agree with the cells', () => {
  for (const [r, c] of [['model', 'goal'], ['channel', 'length_band'], ['trigger_code', 'generation'], ['origin', 'origin']]) {
    const pv = D.pivot(ex, ex.episodes, r, c, 'episodes');
    const sum = pv.cells.flat().reduce((a, x) => a + x.n, 0);
    assert.equal(sum, ex.episodes.length, `${r} x ${c}`);
    pv.rows.forEach((_r, i) => assert.equal(pv.rowTotals[i].n, pv.cells[i].reduce((a, x) => a + x.n, 0)));
    assert.equal(pv.total.n, ex.episodes.length);
  }
  const pv = D.pivot(ex, ex.episodes, 'model', 'goal', 'wrate');
  assert.ok(pv.max > 0 && pv.max <= 1);
  const gi = pv.rows.indexOf('gptoss120'), si = pv.cols.indexOf('solvable');
  assert.equal(pv.cells[gi][si].n, 0);
  assert.equal(pv.cells[gi][si].value, null);
  assert.equal(D.rampBin(null, 1), null);
  assert.equal(D.rampBin(0, 1), 0);
  assert.equal(D.rampBin(1, 1), 8);
  assert.equal(D.rampBin(0.5, 0), 0);
});

test('compareModels: one panel per goal with Wilson bounds', () => {
  const cmp = D.compareModels(ex, ex.episodes, 'wrate');
  assert.deepEqual(cmp.map((c) => c.goal), ['solvable', 'unsolvable']);
  const uns = cmp[1];
  assert.equal(uns.rows.length, 5);
  for (const r of uns.rows) assert.ok(r.agg.lo <= r.agg.value && r.agg.value <= r.agg.hi);
  assert.deepEqual(cmp[0].rows.map((r) => r.model), ['qwen38']);
});

test('lineage: every parent precedes its children, ancestors are kept as context', () => {
  const all = D.lineage(ex, ex.episodes, true);
  assert.equal(all.nodes.length, Object.keys(ex.seeds).length);
  const row = new Map(all.nodes.map((n) => [n.id, n.row]));
  for (const n of all.nodes) if (n.parent) assert.ok(row.get(n.parent) < n.row);
  assert.deepEqual(all.nodes.map((n) => n.row), all.nodes.map((_n, i) => i));
  const deep = Object.keys(ex.seeds).find((id) => ex.seeds[id].generation >= 2);
  const sub = D.lineage(ex, ex.episodes.filter((e) => e.record_id === deep), false);
  const fam = D.seedFamily(ex, deep);
  assert.deepEqual(sub.nodes.map((n) => n.id).sort(), [...fam.ancestors, deep].sort());
  assert.ok(sub.nodes.filter((n) => n.id !== deep).every((n) => n.context && n.best === null));
  assert.equal(sub.nodes.find((n) => n.id === deep).context, false);
  assert.ok(fam.ancestors.length >= 1);
});

test('landscape: v2 per-model grids, v1 legacy grid, filters narrow axes, elites map to episodes', () => {
  const grids = D.landscapeGrids(ex, D.emptyFilters(), 'models');
  const goals = new Set(grids.map((g) => g.goal));
  assert.deepEqual([...goals].sort(), ['solvable', 'unsolvable']);
  const sol = grids.filter((g) => g.goal === 'solvable');
  assert.deepEqual(sol.map((g) => g.model), ['qwen38']); // v1 records belong to the legacy model
  const uns = grids.filter((g) => g.goal === 'unsolvable').map((g) => g.model);
  assert.deepEqual(uns, ['qwen38', 'mistral24', 'glm53f', 'gemma31', 'gptoss120'].filter((m) => uns.includes(m)));
  for (const g of grids) assert.ok(!g.cols.some((c) => c.channel === 'irc' || c.channel === 'wiki'));
  const f = { ...D.emptyFilters(), goal: ['unsolvable'], model: ['qwen38'], channel: ['forum'], length_band: ['le120', 'gt320'] };
  const one = D.landscapeGrids(ex, f, 'models');
  assert.equal(one.length, 1);
  assert.deepEqual([...new Set(one[0].cols.map((c) => c.channel))], ['forum']);
  assert.equal(one[0].cols.length, 2);
  for (const k of Object.keys(one[0].cells)) assert.match(k, /\|forum\|(le120|gt320)$/);
  const best = D.landscapeGrids(ex, { ...D.emptyFilters(), goal: ['unsolvable'] }, 'best');
  assert.equal(best.length, 1);
  assert.equal(best[0].model, '');
  const max = D.landscapeMax(ex);
  for (const c of Object.values(best[0].cells)) {
    assert.ok(c.instigation_score <= max);
    const ep = D.eliteEpisode(ex, 'unsolvable', c);
    assert.ok(ep && ep.record_id === c.record_id && ep.model === c.model);
  }
});

test('eliteEpisode prefers the run the exporter marked in elite_in', () => {
  const ls = ex.landscapes.unsolvable;
  let checked = 0;
  for (const [slug, cells] of Object.entries(ls.byModel)) for (const [key, c] of Object.entries(cells)) {
    const marked = ex.episodes.find((e) => e.goal === 'unsolvable' && e.model === slug && e.elite_in.includes(key));
    if (!marked) continue;
    assert.equal(D.eliteEpisode(ex, 'unsolvable', c, key)?.run_id, marked.run_id);
    checked++;
  }
  assert.ok(checked > 0);
  // a decoy with the same seed/model/goal/channel and identical score loses to the marked run
  const [slug, cells] = Object.entries(ls.byModel)[0];
  const [key, c] = Object.entries(cells)[0];
  const marked = ex.episodes.find((e) => e.goal === 'unsolvable' && e.model === slug && e.elite_in.includes(key));
  const decoy = { ...marked, run_id: marked.run_id + '-decoy', elite_in: [] };
  const ex2 = { ...ex, episodes: [decoy, ...ex.episodes] };
  assert.equal(D.eliteEpisode(ex2, 'unsolvable', c, key)?.run_id, marked.run_id);
  assert.equal(D.eliteEpisode(ex2, 'unsolvable', c)?.run_id, decoy.run_id); // no key -> heuristic, first match
});

test('sortEpisodes: numeric and text sorts, nulls last in both directions; paginate clamps', () => {
  const asc = D.sortEpisodes(ex, ex.episodes, 'run_ttfw_s', false);
  const desc = D.sortEpisodes(ex, ex.episodes, 'run_ttfw_s', true);
  for (const xs of [asc, desc]) {
    const firstNull = xs.findIndex((e) => e.run_ttfw_s === null);
    assert.ok(firstNull > 0 && xs.slice(firstNull).every((e) => e.run_ttfw_s === null));
  }
  assert.ok(asc[0].run_ttfw_s <= asc[1].run_ttfw_s && desc[0].run_ttfw_s >= desc[1].run_ttfw_s);
  const byScore = D.sortEpisodes(ex, ex.episodes, 'instigation_score', true);
  assert.equal(byScore[0].instigation_score, Math.max(...ex.episodes.map((e) => e.instigation_score)));
  const byModel = D.sortEpisodes(ex, ex.episodes, 'model', false).map((e) => D.dimLabel(ex, 'model', e.model));
  assert.deepEqual(byModel, [...byModel].sort((a, b) => a.localeCompare(b, 'en', { numeric: true })));
  const pg = D.paginate([1, 2, 3, 4, 5], 9, 2);
  assert.deepEqual(pg, { items: [5], page: 2, pages: 3 });
  assert.deepEqual(D.paginate([], 0, 50), { items: [], page: 0, pages: 1 });
});

test('toCSV: header plus one line per episode, same column count', () => {
  const csv = D.toCSV(ex, ex.episodes.slice(0, 25)).trimEnd().split('\n');
  assert.equal(csv.length, 26);
  const n = csv[0].split(',').length;
  for (const line of csv) assert.equal(line.split(',').length, n);
});

test('URL state round-trips and ignores junk', () => {
  const s = S.defaultState();
  assert.equal(S.encodeState(s), '');
  s.view = 'landscape'; s.filters.goal = ['unsolvable']; s.filters.model = ['qwen38', 'glm53f']; s.filters.length_band = ['121-200'];
  s.rows = 'channel'; s.cols = 'length_band'; s.metric = 'pile'; s.sort = 'n_writers'; s.desc = false; s.page = 3; s.lmode = 'best';
  s.ep = ex.episodes[3].run_id; s.seed = ex.episodes[3].record_id; s.cmp = 'pile';
  const enc = S.encodeState(s);
  assert.match(enc, /model=qwen38,glm53f/);
  assert.deepEqual(S.decodeState('#' + enc), s);
  const junk = S.decodeState('#v=evil&rows=__proto__&m=x&page=-4&goal=<script>,unsolvable&seed=zz&ep=a%20b&sort=nope&desc=2');
  const d = S.defaultState();
  assert.equal(junk.view, d.view); assert.equal(junk.rows, d.rows); assert.equal(junk.metric, d.metric);
  assert.equal(junk.page, 0); assert.deepEqual(junk.filters.goal, ['unsolvable']); assert.equal(junk.seed, '');
  assert.equal(junk.ep, ''); assert.equal(junk.sort, d.sort); assert.equal(junk.desc, d.desc);
  const stale = S.reconcile(S.decodeState('#model=qwen38,nosuch&ep=nosuch-run&seed=00000000deadbeef'), ex);
  assert.deepEqual(stale.filters.model, ['qwen38']);
  assert.equal(stale.ep, '');
  assert.equal(stale.seed, '');
});

test('landscape best mode with a model filter recomputes the best elite among the selected models', () => {
  const ls = ex.landscapes.unsolvable;
  // a cell whose overall elite comes from a model we then filter out, while another model also has an elite there
  let target = null;
  for (const [key, top] of Object.entries(ls.best)) {
    const other = Object.keys(ls.byModel).find((s) => s !== top.model && ls.byModel[s][key]);
    if (other) { target = { key, top, other }; break; }
  }
  assert.ok(target, 'fixture has a cell held by two models');
  const f = { ...D.emptyFilters(), goal: ['unsolvable'], model: [target.other] };
  const [g] = D.landscapeGrids(ex, f, 'best');
  assert.equal(g.cells[target.key]?.model, target.other);
  assert.equal(g.cells[target.key].instigation_score, ls.byModel[target.other][target.key].instigation_score);
  // every elite of the selected model shows up, and the caption counts the shown cells
  assert.equal(g.filled, Object.keys(g.cells).length);
  for (const k of Object.keys(ls.byModel[target.other])) assert.ok(g.cells[k], k);
  // several models: each cell takes the highest score among them
  const sel = Object.keys(ls.byModel).filter((s) => s !== target.top.model);
  const [g2] = D.landscapeGrids(ex, { ...D.emptyFilters(), goal: ['unsolvable'], model: sel }, 'best');
  for (const [k, c] of Object.entries(g2.cells)) {
    const best = Math.max(...sel.map((s) => ls.byModel[s][k]?.instigation_score ?? -1));
    assert.equal(c.instigation_score, best);
    assert.ok(sel.includes(c.model));
  }
  // no filter: the published cross-model best
  const [g3] = D.landscapeGrids(ex, { ...D.emptyFilters(), goal: ['unsolvable'] }, 'best');
  assert.deepEqual(Object.keys(g3.cells).sort(), Object.keys(ls.best).sort());
});

test('landscape grids show only channels each goal ran; the denominator is the rendered rows x columns', () => {
  for (const g of D.landscapeGrids(ex, D.emptyFilters(), 'models')) {
    const ran = new Set(ex.episodes.filter((e) => e.goal === g.goal).map((e) => e.channel));
    assert.ok(g.cols.every((c) => ran.has(c.channel)), g.goal);
    assert.equal(g.nCellsPossible, g.rows.length * g.cols.length);
    assert.ok(g.filled <= g.nCellsPossible);
  }
  const sol = D.landscapeGrids(ex, { ...D.emptyFilters(), goal: ['solvable'] }, 'models');
  assert.deepEqual([...new Set(sol[0].cols.map((c) => c.channel))], ['forum']);
  const none = D.landscapeGrids(ex, { ...D.emptyFilters(), goal: ['solvable'], channel: ['js'] }, 'models');
  assert.equal(none[0].cols.length, 0);
  assert.equal(none[0].nCellsPossible, 0);
});

test('agent_code dimension: an episode matches any code its agents carry; the agents metric counts coded agents', () => {
  const codesOf = (e) => new Set(e.agent_codes.filter((c) => c !== null));
  const f = { ...D.emptyFilters(), agent_code: ['PEER_WROTE_FIRST'] };
  const got = D.applyFilters(ex, f);
  assert.ok(got.length > 0);
  assert.equal(got.length, ex.episodes.filter((e) => codesOf(e).has('PEER_WROTE_FIRST')).length);
  assert.ok(D.dimDomain(ex, 'agent_code').includes('PEER_WROTE_FIRST'));
  assert.ok(D.dimDomain(ex, 'agent_code').includes('NO_WRITERS'));
  const quiet = ex.episodes.find((e) => e.n_writers === 0);
  assert.deepEqual(D.dimValues(ex, quiet, 'agent_code'), ['NO_WRITERS']);
  const pv = D.pivot(ex, ex.episodes, 'agent_code', 'model', 'agents');
  const ri = pv.rows.indexOf('TASK_HINT');
  const want = ex.episodes.reduce((a, e) => a + e.agent_codes.filter((c) => c === 'TASK_HINT').length, 0);
  assert.equal(pv.rowTotals[ri].value, want);
  assert.equal(pv.cells[ri].reduce((a, c) => a + (c.value ?? 0), 0), want);
  assert.equal(pv.total.value, ex.episodes.reduce((a, e) => a + e.agent_codes.filter((c) => c !== null).length, 0));
  const ep = D.pivot(ex, ex.episodes, 'agent_code', 'goal', 'episodes');
  assert.ok(ep.rowTotals.reduce((a, x) => a + x.n, 0) >= ex.episodes.length);
  assert.equal(ep.total.n, ex.episodes.length);
  const s = S.defaultState();
  s.filters.agent_code = ['TASK_HINT']; s.rows = 'agent_code'; s.metric = 'agents';
  assert.deepEqual(S.decodeState('#' + S.encodeState(s)), s);
});

test('normEpisode accepts a null budget (as the exporter does) and rejects a negative one', () => {
  const base = raw.episodes[0];
  const n = D.normEpisode({ ...base, budget: null });
  assert.ok(n);
  assert.equal(n.budget, null);
  assert.equal(D.normEpisode({ ...base, budget: -1 }), null);
  assert.equal(D.normEpisode({ ...base, budget: 'x' }), null);
});
