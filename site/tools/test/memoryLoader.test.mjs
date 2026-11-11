// Module P (docs/browser-swarm.md §7): the in-memory Loader, scoped URL sync and the selector's Local-lane helpers.
// DOM-free; synthetic placeholder bundles only. Imports compiled modules from public/js (run `npm run build` first).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const js = (p) => import(join(root, 'js', p));
const { createMemoryLoader, memoryIndex } = await js('data/memoryLoader.js');
const { readScoped, formatScoped } = await js('player/url.js');
const { idStamp, localLabel, localOrder } = await js('ui/selector.js');
const { CORE_FORMAT, DETAIL_FORMAT, INDEX_FORMAT } = await js('types.js');

const cols = (n) => Object.fromEntries(['t', 'd', 'k', 'a', 'n', 'e', 'f', 's', 'c', 'l', 'x'].map((k) => [k, Array(n).fill(0)]));
function bundle(id, hash) {
  const card = { id, hash, core: '', detail: '', coreBytes: 0, detailBytes: 0, iface: 'forum', pop: 'multi', nAgents: 2, rep: 0,
    tag: '', model: 'placeholder-model', durationMs: 1000, hidden: false, caveats: [], density: [], score: 0, blurb: 'placeholder',
    stats: { discovered: 0, solved: 0, posts: 0, liveEdges: 0, leak: false, boardEvents: 0, events: 1, nodes: 2, swarmOnsetMs: null } };
  const core = { format: CORE_FORMAT, hash, run: { id }, strings: [''], nodes: [{ id: 'task', k: 0, label: 'task', t0: null }, { id: 'board', k: 1, label: 'board', t0: null }],
    edges: [{ s: 0, d: 1, k: 0 }], events: cols(1), fires: { e: [], t: [], w: [], ev: [] }, agents: [] };
  const detail = { format: DETAIL_FORMAT, hash, items: [], posts: {} };
  return { card, core, detail };
}

test('memoryIndex: valid empty-findings index', () => {
  const i = memoryIndex([]);
  assert.equal(i.format, INDEX_FORMAT);
  assert.deepEqual(i.runs, []);
  assert.equal(i.featured, '');
});

test('memory loader: core/detail validated, hash checked, set replaces, delete removes', async () => {
  const L = createMemoryLoader();
  const a = bundle('browser-forum-multi2-20261024120000-ab12', 'aaaaaaaa');
  L.set(a);
  assert.ok(L.has(a.card.id));
  const core = await L.core(a.card);
  assert.equal(core.hash, 'aaaaaaaa');
  assert.ok(Array.isArray(core.chapters));
  let prog = 0;
  const d = await L.detail(a.card, (f) => { prog = f; });
  assert.equal(d.hash, 'aaaaaaaa');
  assert.equal(prog, 1);
  await assert.rejects(L.core({ ...a.card, hash: 'bbbbbbbb' }), (e) => e.kind === 'hash');
  const b = bundle(a.card.id, 'cccccccc');
  L.set(b);
  assert.equal((await L.core(b.card)).hash, 'cccccccc');
  assert.equal(L.cards.length, 1);
  L.delete(a.card.id);
  await assert.rejects(L.core(b.card), (e) => e.kind === 'network');
  assert.throws(() => L.set({ ...a, core: { ...a.core, format: 'x' } }), (e) => e.kind === 'version');
});

test('memory loader: abort signal and lazy resolver', async () => {
  const a = bundle('r1', 'dddddddd');
  let calls = 0;
  const L = createMemoryLoader({ resolve: async (c) => { calls++; return c.id === 'r1' ? a : null; } });
  assert.equal((await L.core(a.card)).hash, 'dddddddd');
  await L.core(a.card);
  assert.equal(calls, 1);
  const ac = new AbortController();
  ac.abort();
  await assert.rejects(L.core(a.card, ac.signal), (e) => e.name === 'AbortError');
  await assert.rejects(L.core({ ...a.card, id: 'nope' }), (e) => e.kind === 'network');
});

test('scoped URL: only the key is read and written; other keys stay', () => {
  assert.equal(readScoped('trace', '', '#trace=browser-x-1'), 'browser-x-1');
  assert.equal(readScoped('trace', '', '#run=other'), undefined);
  assert.equal(readScoped('trace', '', '#trace=bad%20id'), undefined);
  assert.equal(formatScoped('trace', 'abc', ''), '#trace=abc');
  assert.equal(formatScoped('trace', 'abc', '#x=1'), '#x=1&trace=abc');
  assert.equal(formatScoped('trace', null, '#trace=abc'), '');
});

test('Local lane: stamp, label, newest first', () => {
  const c = bundle('browser-wiki-multi4-20261024140233-ab12', 'eeeeeeee').card;
  assert.equal(idStamp(c.id), '10-24 14:02');
  assert.equal(localLabel({ ...c, iface: 'wiki', nAgents: 4 }), 'wiki · 4 · 10-24 14:02');
  assert.equal(idStamp('forum-multi4-r1'), '');
  const old = { ...c, id: 'browser-forum-solo-20261023000000-0000' };
  assert.deepEqual(localOrder([old, c]).map((r) => r.id), [c.id, old.id]);
});
