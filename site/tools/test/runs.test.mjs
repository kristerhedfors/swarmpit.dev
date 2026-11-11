// Model facet: selector choose / end-card sibling and next stay within one model; the model chip row is hidden
// for a single-model index. Imports compiled DOM-free modules from public/js (run `npm run build` first).
// Fixtures are tiny synthetic cards built here (ids and numbers only).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const { chooseRun, siblingRun, nextRun, listedModels, modelChips, primaryModel, modelLabel, shortModel } =
  await import(join(root, 'js', 'data', 'runs.js'));

const Q = 'qwen-3.8-27b', G = 'zai-org/GLM-5.3-Flash', M = 'mistralai/Mistral-Small-3.2-24B-Instruct-2506';
const card = (id, model, iface, pop, rep, o = {}) => ({ id, model, iface, pop, rep, hidden: false, caveats: [], ...o });
const L = [
  card('forum-solo-r0', Q, 'forum', 'solo', 0), card('forum-multi4-r0', Q, 'forum', 'multi', 0),
  card('forum-multi4-r1', Q, 'forum', 'multi', 1), card('wiki-multi4-r1', Q, 'wiki', 'multi', 1),
  card('irc-multi4-r1', Q, 'irc', 'multi', 1, { caveats: ['irc_inbound_broken'] }),
  card('forum-multi4-r1--glm53f', G, 'forum', 'multi', 1), card('cache-multi4-r1--glm53f', G, 'cache', 'multi', 1),
  card('wiki-solo-r0--glm53f', G, 'wiki', 'solo', 0),
];
const models = [{ id: Q, slug: 'qwen38', label: 'Qwen 3.8 27B', provider: 'cerebras', nRuns: 5 },
  { id: G, slug: 'glm53f', label: 'GLM-5.3 Flash', provider: 'berget', nRuns: 3 }];

test('chooseRun matches model, iface, pop and rep', () => {
  assert.equal(chooseRun(L, Q, 'forum', 'multi', 1).id, 'forum-multi4-r1');
  assert.equal(chooseRun(L, G, 'forum', 'multi', 1).id, 'forum-multi4-r1--glm53f');
  assert.equal(chooseRun(L, G, 'forum', 'solo', 0).id, 'forum-multi4-r1--glm53f', 'falls back within the iface');
  assert.equal(chooseRun(L, G, 'irc', 'multi', 1), undefined, 'no cross-model fallback for an iface chip');
  assert.equal(chooseRun(L, M, 'forum', 'multi', 1), undefined);
});

test('a model chip keeps iface, pop and rep where possible', () => {
  assert.equal(chooseRun(L, G, 'forum', 'multi', 1, true).id, 'forum-multi4-r1--glm53f');
  assert.equal(chooseRun(L, G, 'irc', 'multi', 1, true).id, 'forum-multi4-r1--glm53f', 'same pop and rep, other iface');
  assert.equal(chooseRun(L, G, 'irc', 'solo', 0, true).id, 'wiki-solo-r0--glm53f');
  assert.equal(chooseRun(L, Q, 'cache', 'multi', 1, true).id, 'forum-multi4-r1');
});

test('end-card sibling and next stay within the model', () => {
  const qf = L[2], gf = L[5];
  assert.equal(siblingRun(L, qf).id, 'wiki-multi4-r1');
  assert.equal(siblingRun(L, L[3]).id, 'forum-multi4-r1', 'skips the broken irc run and wraps');
  assert.equal(siblingRun(L, gf).id, 'cache-multi4-r1--glm53f', 'not the qwen wiki run');
  assert.equal(siblingRun(L, L[6]).id, 'forum-multi4-r1--glm53f');
  assert.equal(nextRun(L, L[4]).id, 'forum-solo-r0', 'wraps inside qwen, never into glm');
  assert.equal(nextRun(L, L[7]).id, 'forum-multi4-r1--glm53f');
  assert.equal(nextRun([L[0]], L[0]), null);
  const hidden = card('forum-solo-r0--glm53f-smoke', G, 'forum', 'solo', 0, { hidden: true, tag: '-smoke' });
  assert.equal(nextRun(L, hidden).id, 'forum-multi4-r1--glm53f', 'a hidden run continues in its model');
  const synth = card('_synth-500', 'synthetic', 'forum', 'multi', 0, { hidden: true, caveats: ['synthetic'] });
  assert.equal(nextRun(L, synth).id, 'forum-solo-r0');
});

test('model chip row only with more than one listed model', () => {
  assert.deepEqual(modelChips(models, L).map((m) => m.slug), ['qwen38', 'glm53f']);
  assert.deepEqual(modelChips(models, L.filter((r) => r.model === Q)), []);
  const hiddenOnly = L.map((r) => (r.model === G ? { ...r, hidden: true } : r));
  assert.deepEqual(modelChips(models, hiddenOnly), []);
  const single = { runs: L.filter((r) => r.model === Q), models: [models[0]] };
  assert.deepEqual(modelChips(listedModels(single), single.runs), []);
});

test('models from index.models, or derived from cards for old indexes', () => {
  assert.equal(primaryModel({ runs: L, models }), Q);
  const old = { runs: [...L.filter((r) => r.model === Q), card('_synth-500', 'synthetic', 'forum', 'multi', 0,
    { hidden: true, caveats: ['synthetic'] })] };
  assert.deepEqual(listedModels(old).map((m) => [m.id, m.nRuns]), [[Q, 5]]);
  assert.equal(primaryModel(old), Q);
  assert.equal(primaryModel({ runs: [] }), Q);
  assert.equal(modelLabel({ runs: L, models }, G), 'GLM-5.3 Flash');
  assert.equal(modelLabel({ runs: L }, M), 'Mistral-Small-3.2-24B-Instruct-2506');
  assert.equal(shortModel(Q), Q);
});

// Goal facet (SPEC 4.11): cards without `goal` are solvable; unsolvable ids carry the lab's `-unsolvable` segment
// (`<iface>-<pop>-r<rep>[-unsolvable][--<slug>][-<tag>]`); `r3u` is only the rep-pill display label.
const R = await import(join(root, 'js', 'data', 'runs.js'));
const U = { goal: 'unsolvable', caveats: ['unsolvable'] };
const LG = [
  card('forum-multi4-r0', Q, 'forum', 'multi', 0), card('forum-multi4-r1', Q, 'forum', 'multi', 1),
  card('wiki-multi4-r1', Q, 'wiki', 'multi', 1),
  card('forum-multi4-r0-unsolvable', Q, 'forum', 'multi', 0, U), card('forum-multi4-r1-unsolvable', Q, 'forum', 'multi', 1, U),
  card('wiki-multi4-r1-unsolvable', Q, 'wiki', 'multi', 1, U),
  card('forum-multi4-r1--glm53f', G, 'forum', 'multi', 1),
  card('cache-multi4-r2-unsolvable--glm53f', G, 'cache', 'multi', 2, U),
];

test('goalOf, repLabel and goal chips', () => {
  assert.equal(R.goalOf({}), 'solvable');
  assert.equal(R.goalOf({ goal: 'unsolvable' }), 'unsolvable');
  assert.equal(R.goalOf({ goal: 'bogus' }), 'solvable');
  assert.equal(R.repLabel(LG[1]), 'r1');
  assert.equal(R.repLabel(LG[4]), 'r1u');
  assert.deepEqual(R.listedGoals(LG), ['solvable', 'unsolvable']);
  assert.deepEqual(R.listedGoals(LG, G), ['solvable', 'unsolvable']);
  assert.deepEqual(R.listedGoals(LG, M), []);
  assert.deepEqual(R.goalChips(LG), ['solvable', 'unsolvable']);
  assert.deepEqual(R.goalChips(L), [], 'no goal row for a solvable-only index');
  assert.deepEqual(R.goalChips(LG.map((r) => (R.goalOf(r) === 'unsolvable' ? { ...r, hidden: true } : r))), [],
    'hidden unsolvable runs do not raise the goal row');
});

test('chooseRun with a goal never mixes goals', () => {
  assert.equal(chooseRun(LG, Q, 'forum', 'multi', 1, false, 'solvable').id, 'forum-multi4-r1');
  assert.equal(chooseRun(LG, Q, 'forum', 'multi', 1, false, 'unsolvable').id, 'forum-multi4-r1-unsolvable');
  assert.equal(chooseRun(LG, Q, 'wiki', 'multi', 0, false, 'unsolvable').id, 'wiki-multi4-r1-unsolvable', 'falls back within the goal');
  assert.equal(chooseRun(LG, G, 'forum', 'multi', 1, false, 'unsolvable'), undefined, 'iface chip: no goal fallback');
  assert.equal(chooseRun(LG, G, 'forum', 'multi', 1, true, 'unsolvable').id, 'cache-multi4-r2-unsolvable--glm53f');
  assert.equal(chooseRun(LG, M, 'forum', 'multi', 1, true, 'unsolvable'), undefined);
  assert.equal(chooseRun(LG, Q, 'forum', 'multi', 1).id, 'forum-multi4-r1', 'no goal: solvable cards come first');
});

test('goal chip keeps the model where it can', () => {
  assert.equal(R.chooseGoalRun(LG, 'unsolvable', Q, 'wiki', 'multi', 1).id, 'wiki-multi4-r1-unsolvable');
  assert.equal(R.chooseGoalRun(LG, 'solvable', Q, 'wiki', 'multi', 1).id, 'wiki-multi4-r1');
  assert.equal(R.chooseGoalRun(LG, 'unsolvable', G, 'forum', 'multi', 1).id, 'cache-multi4-r2-unsolvable--glm53f');
  const onlyQ = LG.filter((r) => !(r.model === Q && R.goalOf(r) === 'unsolvable'));
  assert.equal(R.chooseGoalRun(onlyQ, 'unsolvable', Q, 'forum', 'multi', 1).model, G, 'first model with that goal');
  assert.equal(R.chooseGoalRun(L, 'unsolvable', Q, 'forum', 'multi', 1), undefined);
});

test('end-card sibling and next stay within model and goal', () => {
  assert.equal(siblingRun(LG, LG[4]).id, 'wiki-multi4-r1-unsolvable');
  assert.equal(siblingRun(LG, LG[1]).id, 'wiki-multi4-r1');
  assert.equal(siblingRun(LG, LG[5]).id, 'forum-multi4-r1-unsolvable');
  assert.equal(nextRun(LG, LG[2]).id, 'forum-multi4-r0', 'solvable lane wraps without entering the unsolvable runs');
  assert.equal(nextRun(LG, LG[5]).id, 'forum-multi4-r0-unsolvable');
  assert.equal(nextRun(LG, LG[7]), null, "glm's only unsolvable run");
  const hidden = card('forum-multi4-r3-unsolvable-smoke', Q, 'forum', 'multi', 3, { ...U, hidden: true, tag: '-smoke' });
  assert.equal(nextRun(LG, hidden).id, 'forum-multi4-r0-unsolvable', 'a hidden unsolvable run continues in its goal');
  const hiddenG = card('wiki-solo-r0-unsolvable--glm53f-smoke', G, 'wiki', 'solo', 0, { ...U, hidden: true });
  const noGU = [...LG.filter((r) => r.id !== 'cache-multi4-r2-unsolvable--glm53f'), card('wiki-multi4-r1--glm53f', G, 'wiki', 'multi', 1)];
  assert.equal(nextRun(noGU, hiddenG).id, 'forum-multi4-r1--glm53f', 'no listed run of its goal: its model');
});
