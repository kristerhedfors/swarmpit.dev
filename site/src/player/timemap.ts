import { BundleError, NARRATION_FORMAT, NARRATION_INDEX_FORMAT } from '../types.js';
import type { NarrationCue, NarrationIndex, NarrationManifest } from '../types.js';

/* Narration time map (NARRATION.md 2.1 and 3.5). Pure and DOM-free.
 * map = [[audioMs, runMs], ...]: audioMs strictly increasing, runMs non-decreasing, starts at [0, 0].
 * Validation errors carry fixed messages only (never cue text). */

type Map = readonly (readonly [number, number])[];

/** Largest i with map[i][0] <= a (map non-empty, a >= map[0][0]). */
function segOf(map: Map, a: number): number {
  let lo = 0, hi = map.length - 1;
  while (lo < hi) { const md = (lo + hi + 1) >>> 1; if (map[md]![0] <= a) lo = md; else hi = md - 1; }
  return lo;
}

/** Audio position -> run time. Linear between points; clamps outside [0, durationMs]. */
export function a2r(map: Map, aMs: number): number {
  const n = map.length;
  if (!n) return 0;
  if (!(aMs > map[0]![0])) return map[0]![1];
  const last = map[n - 1]!;
  if (aMs >= last[0]) return last[1];
  const i = segOf(map, aMs);
  const [a0, r0] = map[i]!, [a1, r1] = map[i + 1]!;
  return r0 + (r1 - r0) * (aMs - a0) / (a1 - a0);
}

/** Run time -> the smallest audio position whose run time is >= rMs (a hold maps to its start). */
export function r2a(map: Map, rMs: number): number {
  const n = map.length;
  if (!n) return 0;
  if (!(rMs > map[0]![1])) return map[0]![0];
  const last = map[n - 1]!;
  if (rMs > last[1]) rMs = last[1];
  // First point with runMs >= r (runMs is non-decreasing); j >= 1 because map[0][1] < r.
  let lo = 0, hi = n - 1;
  while (lo < hi) { const md = (lo + hi) >>> 1; if (map[md]![1] >= rMs) hi = md; else lo = md + 1; }
  const [a0, r0] = map[lo - 1]!, [a1, r1] = map[lo]!;
  return r1 === r0 ? a0 : a0 + (a1 - a0) * (rMs - r0) / (r1 - r0);
}

/** Index of the cue playing at audio position a (audioMs <= a < endMs), or -1. Cues are sorted and disjoint. */
export function cueAt(cues: readonly NarrationCue[], aMs: number): number {
  let lo = 0, hi = cues.length;
  while (lo < hi) { const md = (lo + hi) >>> 1; if (cues[md]!.audioMs <= aMs) lo = md + 1; else hi = md; }
  const i = lo - 1;
  return i >= 0 && aMs < cues[i]!.endMs ? i : -1;
}

const RUN_ID = /^[A-Za-z0-9._-]{1,80}$/;
const TPL_ID = /^[GE](\.[A-Za-z][A-Za-z0-9_]{0,31}){1,3}$/;
const VOICES = new Set(['guide', 'expert']);
const MAX_SLOPE = 8, MAX_POINTS = 600, MAX_CUES = 80, MAX_TEXT = 240;
const isU = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;
const esc = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function fail(msg: string, url: string, kind: BundleError['kind'] = 'shape'): never {
  throw new BundleError(msg, url, kind);
}

/** Validates a manifest against the swarmpit.narration/1 invariants (2.1 #1-6). Unknown keys are ignored. */
export function assertNarration(x: unknown, url = 'narration'): NarrationManifest {
  const o = x as NarrationManifest;
  if (!o || typeof o !== 'object' || Array.isArray(o)) fail('narration is not an object', url);
  if (o.schema !== NARRATION_FORMAT) fail('narration schema', url, 'version');
  if (typeof o.runId !== 'string' || !RUN_ID.test(o.runId)) fail('narration runId', url);
  if (typeof o.runHash !== 'string' || typeof o.generator !== 'string') fail('narration header', url);
  if (typeof o.audio !== 'string' || !new RegExp(`^\\.\\./\\.\\./audio/narration/${esc(o.runId)}\\.[0-9a-f]{8}\\.mp3$`).test(o.audio))
    fail('narration audio url', url);
  if (!isU(o.audioBytes) || !isU(o.durationMs) || !isU(o.runEndMs) || o.durationMs < 1) fail('narration sizes', url);
  const tm = o.timeMap;
  if (!Array.isArray(tm) || tm.length < 2 || tm.length > MAX_POINTS) fail('narration timeMap length', url);
  for (let i = 0; i < tm.length; i++) {
    const p = tm[i];
    if (!Array.isArray(p) || p.length !== 2 || !isU(p[0]) || !isU(p[1])) fail('narration timeMap point', url);
    if (i === 0) { if (p[0] !== 0 || p[1] !== 0) fail('narration timeMap start', url); continue; }
    const q = tm[i - 1]!;
    const da = p[0] - q[0], dr = p[1] - q[1];
    if (da <= 0 || dr < 0) fail('narration timeMap not monotonic', url);
    if (dr > MAX_SLOPE * da) fail('narration timeMap slope', url);
  }
  const end = tm[tm.length - 1]!;
  if (end[0] !== o.durationMs || end[1] !== o.runEndMs) fail('narration timeMap end', url);
  const cues = o.cues;
  if (!Array.isArray(cues) || cues.length > MAX_CUES) fail('narration cues', url);
  let prevEnd = 0;
  for (const c of cues) {
    if (!c || typeof c !== 'object') fail('narration cue', url);
    if (!isU(c.audioMs) || !isU(c.endMs) || !isU(c.runMs)) fail('narration cue times', url);
    if (!(c.audioMs < c.endMs && c.endMs <= o.durationMs)) fail('narration cue range', url);
    if (c.audioMs < prevEnd) fail('narration cues overlap', url);
    prevEnd = c.endMs;
    if (Math.abs(a2r(tm, c.audioMs) - c.runMs) > 1) fail('narration cue anchor', url);
    if (!VOICES.has(c.voice)) fail('narration cue voice', url);
    if (typeof c.tpl !== 'string' || !TPL_ID.test(c.tpl)) fail('narration cue tpl', url);
    if (typeof c.text !== 'string' || c.text.length < 1 || c.text.length > MAX_TEXT) fail('narration cue text', url);
  }
  const s = o.sfx;
  if (!s || typeof s !== 'object' || !isU(s.dropped) || !s.counts || typeof s.counts !== 'object' || Array.isArray(s.counts))
    fail('narration sfx', url);
  for (const [k, v] of Object.entries(s.counts)) if (!/^[a-z]{1,16}$/.test(k) || !isU(v)) fail('narration sfx counts', url);
  return o;
}

/** Validates the narration index (2.2). Entries with a bad shape are dropped rather than failing the whole index. */
export function assertNarrationIndex(x: unknown, url = 'narration index'): NarrationIndex {
  const o = x as NarrationIndex;
  if (!o || typeof o !== 'object' || Array.isArray(o)) fail('narration index is not an object', url);
  if (o.schema !== NARRATION_INDEX_FORMAT) fail('narration index schema', url, 'version');
  if (!o.runs || typeof o.runs !== 'object' || Array.isArray(o.runs)) fail('narration index runs', url);
  const runs: NarrationIndex['runs'] = {};
  for (const [id, e] of Object.entries(o.runs)) {
    if (!RUN_ID.test(id) || !e || typeof e !== 'object') continue;
    if (typeof e.hash !== 'string' || typeof e.manifest !== 'string' || !isU(e.durationMs)) continue;
    if (e.manifest !== `${id}.json`) continue;
    runs[id] = { hash: e.hash, manifest: e.manifest, durationMs: e.durationMs };
  }
  return { schema: NARRATION_INDEX_FORMAT, runs };
}
