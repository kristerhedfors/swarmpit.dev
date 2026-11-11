// Synthetic explorer.json fixture (schema instigation.explorer.v1): ids, enums and numbers only, generated
// deterministically here. Used by explore.test.mjs and for local previews of explore.html. It never touches
// real study data.
import { test } from 'node:test';
import assert from 'node:assert/strict';

export const TRIGGERS = ['SAW_PEER_POSTS', 'SAW_SEED_ONLY', 'PEER_WROTE_FIRST', 'EMPTY_BOARD', 'DIRECT_QUESTION', 'ADDRESSED_BY_NAME',
  'CALL_TO_ACTION', 'TASK_HINT', 'REPLY_TO_SELF', 'UNPROMPTED', 'NONE', 'UNRESOLVED', 'NO_WRITERS'];
export const BANDS = ['le120', '121-200', '201-320', 'gt320'];

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}
const hex = (n) => (Math.imul(n, 2654435761) >>> 0).toString(16).padStart(8, '0') + n.toString(16).padStart(8, '0');
const band = (c) => (c <= 120 ? 'le120' : c <= 200 ? '121-200' : c <= 320 ? '201-320' : 'gt320');

export function makeFixture({ nSeeds = 40, seed = 7 } = {}) {
  const r = rng(seed);
  const models = [
    { slug: 'qwen38', label: 'Qwen 3.8 27B', origin: 'china', size_b: 27, provider: 'cerebras', retired: false },
    { slug: 'mistral24', label: 'Mistral Small 3.2 24B', origin: 'eu', size_b: 24, provider: 'berget', retired: false },
    { slug: 'glm53f', label: 'GLM-5.3 Flash', origin: 'china', size_b: null, provider: 'berget', retired: false },
    { slug: 'gemma31', label: 'Gemma 4 31B', origin: 'us', size_b: 31, provider: 'berget', retired: false },
    { slug: 'gptoss120', label: 'GPT-OSS 120B', origin: 'us', size_b: 120, provider: 'cerebras', retired: true },
  ];
  const channels = [{ id: 'forum', label: 'Forum' }, { id: 'js', label: 'JS comment' }, { id: 'html', label: 'HTML comment' },
    { id: 'guestbook', label: 'Guestbook' }, { id: 'kv', label: 'KV store' }, { id: 'paste', label: 'Paste' }];
  const seeds = {};
  const ids = [];
  for (let i = 0; i < nSeeds; i++) {
    const id = hex(0xa000 + i * 7919);
    const gen = i < 10 ? 0 : 1 + Math.floor((i - 10) / 10);
    const parent = gen === 0 ? null : ids[Math.floor(r() * Math.min(ids.length, gen * 10))];
    const chars = 60 + Math.floor(r() * 400);
    seeds[id] = { seed_chars: chars, length_band: band(chars), origin: gen === 0 ? (i < 2 ? 'pilot' : 'scratch') : 'mutation',
      bootstrap: i < 2, parent_id: parent, generation: gen, gen_model: gen === 0 && i < 2 ? null : 'qwen38' };
    ids.push(id);
  }
  const episodes = [];
  let k = 0;
  for (const id of ids) {
    const s = seeds[id];
    const runs = [['unsolvable', 'qwen38', 'forum'], ['unsolvable', 'gptoss120', 'forum'], ['unsolvable', 'qwen38', ['js', 'html'][k % 2]]];
    if (s.generation === 0) runs.push(['unsolvable', 'glm53f', 'forum'], ['unsolvable', 'mistral24', 'forum'], ['unsolvable', 'gemma31', 'forum'], ['solvable', 'qwen38', 'forum']);
    if (k % 9 === 0) runs.push(['unsolvable', 'qwen38', ['guestbook', 'kv', 'paste'][k % 3]]);
    for (const [goal, model, channel] of runs) {
      k++;
      const n = 4;
      const p = model === 'gptoss120' ? 0.02 : model === 'glm53f' ? 0.4 : 0.25;
      const writers = Array.from({ length: n }, () => r() < p);
      const nw = writers.filter(Boolean).length;
      const ttfw = nw ? 20 + r() * 200 : null;
      const pile = nw >= 2 ? (ttfw ?? 0) + r() * 100 : null;
      const pot = [];
      if (nw) { let posts = 0; for (let w = 1; w <= nw; w++) { posts += 1 + Math.floor(r() * 3); pot.push([Math.round((ttfw + (w - 1) * 40) * 10) / 10, posts, w]); } }
      const code = nw ? TRIGGERS[Math.floor(r() * 10)] : 'NO_WRITERS';
      const score = nw ? Math.round((0.1 + 0.15 * nw + r() * 0.1) * 1000) / 1000 : Math.round(r() * 0.05 * 1000) / 1000;
      episodes.push({
        run_id: `${channel}-multi4-r0${goal === 'unsolvable' ? '-unsolvable' : ''}--${model}-c${id.slice(-6)}`, record_id: id, goal, model, channel,
        rep: 0, generation: s.generation, n_agents: n, budget: 40, n_writers: nw, write_fraction: nw / n, S_mean: Math.round(r() * 1000) / 1000,
        I: Math.round(score * 900) / 1000, instigation_score: score, run_ttfw_s: ttfw, run_first_write_step: nw ? 3 + Math.floor(r() * 30) : null,
        pile_in_onset_t_s: pile, posts_over_time: pot,
        engagement: { board_reads: Math.floor(r() * 20), relay_connections: Math.floor(r() * 3), board_refs: Math.floor(r() * 5), agents_engaged: Math.floor(r() * 5), engagement_score: Math.round(r() * 1000) / 1000 },
        trigger_code: code, agent_codes: writers.map((w) => (w ? TRIGGERS[Math.floor(r() * 10)] : null)),
        panel: k % 4 ? { slugs: ['qwen38', 'glm53f', 'gemma31'], agreement_mean: Math.round(r() * 100) / 100 } : null,
        classifier_errors: k % 17 === 0 ? 1 : 0, elite_in: [],
      });
    }
  }
  // landscapes: unsolvable v2 per model, solvable v1 flat
  const byModel = {};
  const best = {};
  for (const e of episodes) {
    if (e.goal !== 'unsolvable') continue;
    const key = `${e.trigger_code}|${e.channel}|${seeds[e.record_id].length_band}`;
    const cell = { record_id: e.record_id, instigation_score: e.instigation_score, length: seeds[e.record_id].seed_chars, trigger_code: e.trigger_code,
      channel: e.channel, n_writers: e.n_writers, engagement: { ...e.engagement }, model: e.model };
    const m = (byModel[e.model] ??= { cells: {}, coverage: 0, tallies: {}, origin: 'x', size_b: null });
    if (!m.cells[key] || m.cells[key].instigation_score < cell.instigation_score) m.cells[key] = cell;
    if (!best[key] || best[key].instigation_score < cell.instigation_score) best[key] = cell;
  }
  for (const [slug, m] of Object.entries(byModel)) {
    m.coverage = Object.keys(m.cells).length;
    for (const [key, c] of Object.entries(m.cells)) {
      const ep = episodes.find((e) => e.record_id === c.record_id && e.model === slug && e.goal === 'unsolvable' && e.instigation_score === c.instigation_score);
      ep?.elite_in.push(key); // exporter format: plain cell key (goal/model live on the episode)
    }
  }
  const solv = {};
  for (const e of episodes) {
    if (e.goal !== 'solvable') continue;
    const key = `${e.trigger_code}|${e.channel}|${seeds[e.record_id].length_band}`;
    if (!solv[key] || solv[key].instigation_score < e.instigation_score) {
      solv[key] = { record_id: e.record_id, instigation_score: e.instigation_score, length: seeds[e.record_id].seed_chars,
        trigger_code: e.trigger_code, channel: e.channel, n_writers: e.n_writers, engagement: { ...e.engagement } };
    }
  }
  const axes = { trigger_code: TRIGGERS, channel: ['forum', 'irc', 'wiki', 'cache', 'guestbook', 'paste', 'kv', 'js', 'html'], length_band: BANDS };
  return {
    schema: 'instigation.explorer.v1', generated_ts: 1791849600,
    dims: { model: models, channel: channels, length_band: BANDS, trigger_code: TRIGGERS, goal: ['solvable', 'unsolvable'], origin: ['pilot', 'scratch', 'mutation'] },
    seeds, episodes,
    landscapes: {
      unsolvable: { schema: 'instigation.landscape.v2', generated_ts: 1791849600, goal: 'unsolvable', models: Object.keys(byModel), cells: best,
        by_model: byModel, coverage: 0, coverage_cells: Object.keys(best).length, mean_elite_score: 0, n_cells_possible: 13 * 9 * 4,
        axes: { ...axes, model: models.map((m) => m.slug), goal: ['solvable', 'unsolvable'] }, tallies: {}, leaderboard: [] },
      solvable: { schema: 'instigation.landscape.v1', generated_ts: 1791849600, cells: solv, coverage: Object.keys(solv).length,
        mean_elite_score: 0, n_cells_possible: 13 * 9 * 4, axes, tallies: {}, leaderboard: [] },
    },
  };
}

test('fixture is deterministic and uses only ids, enums and numbers', () => {
  const a = makeFixture(), b = makeFixture();
  assert.deepEqual(a, b);
  assert.ok(a.episodes.length > 100);
  const strs = [];
  const walk = (o) => { if (typeof o === 'string') strs.push(o); else if (Array.isArray(o)) o.forEach(walk); else if (o && typeof o === 'object') Object.entries(o).forEach(([k, v]) => { strs.push(k); walk(v); }); };
  walk(a);
  for (const s of strs) assert.match(s, /^[A-Za-z0-9 .|/:()_-]{1,120}$/);
});
