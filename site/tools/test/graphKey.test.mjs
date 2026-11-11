// The live-graph key: every entry is complete, ids are unique, and every swatch renders to SVG.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GRAPH_KEY, GRAPH_KEY_FOOTNOTE } from '../../public/js/ui/graphKeyData.js';

test('graph key covers nodes, rings and lines with unique, complete entries', () => {
  assert.deepEqual(GRAPH_KEY.map((s) => s.title), ['Nodes', 'Rings & glows', 'Lines']);
  const ids = GRAPH_KEY.flatMap((s) => s.entries.map((e) => e.id));
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ['agent', 'task', 'board', 'thread', 'post', 'seed-post', 'persona', 'handle', 'trail',
    'arc-step', 'arc-free', 'arc-task', 'thinking', 'leak', 'hover', 'match', 'hull',
    'e-structure', 'e-readwrite', 'e-exposure', 'e-reply', 'e-mention', 'e-dm', 'e-upvote', 'e-livelive', 'e-pulse']) {
    assert.ok(ids.includes(id), `missing ${id}`);
  }
  for (const s of GRAPH_KEY) for (const e of s.entries) {
    assert.ok(e.label.length > 1 && e.desc.length > 10, e.id);
  }
  assert.ok(GRAPH_KEY_FOOTNOTE.length > 20);
});

test('every swatch renders to a self-contained SVG', async () => {
  const { swatchSvg } = await import('../../public/js/ui/graphKey.js');
  for (const s of GRAPH_KEY) for (const e of s.entries) {
    const out = swatchSvg(e.swatch);
    assert.match(out, /^<svg [^>]*aria-hidden="true"[^>]*>.*<\/svg>$/s, e.id);
    assert.doesNotMatch(out, /undefined|NaN/, e.id);
  }
});
