import { RATES } from '../types.js';
import type { PlaybackRate, UrlState } from '../types.js';

/** Query and hash keys in one set (a hash key wins over the same query key). */
function urlParams(search: string, hash: string): URLSearchParams {
  const p = new URLSearchParams(search.replace(/^\?/, ''));
  new URLSearchParams(hash.replace(/^#/, '')).forEach((v, k) => p.set(k, v));
  return p;
}
/** history.replaceState to hash h (with '' dropping the hash), unless it is already current. */
function replaceHash(h: string): void {
  if (h === location.hash) return;
  try { history.replaceState(history.state, '', location.pathname + location.search + h); } catch { /* sandboxed */ }
}

/** Parse '#run=..&t=..' plus '?renderer=2d&perf=1' (either location works for every key). */
export function parseUrl(search: string, hash: string): UrlState {
  const p = urlParams(search, hash);
  const o: UrlState = {};
  const run = p.get('run');
  if (run && /^[\w.-]{1,80}$/.test(run)) o.run = run;
  const t = Number(p.get('t'));
  if (p.has('t') && Number.isFinite(t) && t >= 0) o.t = t * 1000;
  const r = Number(p.get('rate'));
  if ((RATES as readonly number[]).includes(r)) o.rate = r as PlaybackRate;
  const mode = p.get('mode');
  if (mode === 'worldline' || mode === 'constellation') o.mode = mode;
  const sel = p.get('sel');
  if (sel) o.sel = sel;
  const f = p.get('f');
  if (f) o.f = f;
  const ren = p.get('renderer');
  if (ren === '2d' || ren === 'canvas2d') o.renderer = 'canvas2d';
  else if (ren === 'webgl2' || ren === 'gl') o.renderer = 'webgl2';
  if (p.get('perf') === '1') o.perf = true;
  return o;
}

export const readUrl = (): UrlState => parseUrl(location.search, location.hash);

/** Hash string for the shareable state (t in seconds, 1 decimal). */
export function formatHash(s: UrlState): string {
  const p: string[] = [];
  if (s.run) p.push('run=' + encodeURIComponent(s.run));
  if (s.t !== undefined && s.t > 0) p.push('t=' + (s.t / 1000).toFixed(1));
  if (s.rate !== undefined && s.rate !== 1) p.push('rate=' + s.rate);
  if (s.mode === 'worldline') p.push('mode=worldline');
  if (s.sel) p.push('sel=' + encodeURIComponent(s.sel));
  if (s.f) p.push('f=' + encodeURIComponent(s.f));
  if (s.renderer === 'canvas2d') p.push('renderer=2d');
  if (s.perf) p.push('perf=1');
  return p.length ? '#' + p.join('&') : '';
}

let last = 0, pending: UrlState | null = null, timer = 0;
/** history.replaceState, at most 4 times per second (trailing write guaranteed). */
export function writeUrl(s: UrlState): void {
  pending = s;
  const now = performance.now();
  const flush = (): void => {
    timer = 0;
    if (!pending) return;
    const h = formatHash(pending);
    pending = null;
    last = performance.now();
    replaceHash(h);
  };
  if (now - last >= 250) flush();
  else if (!timer) timer = window.setTimeout(flush, 250 - (now - last));
}

/* Scoped URL sync (PlayerOpts.urlKey, docs/browser-swarm.md §7): a host page that embeds the player next to its own
 * state (the browser lab) keeps only `#<key>=<id>` in the hash, never t/rate/mode/sel/f, and other hash keys are left
 * alone. Ids use the same charset as `run` (parseUrl). */
const SCOPED_ID = /^[\w.-]{1,120}$/;

/** The id under `key` in the hash (or query), or undefined. */
export function readScoped(key: string, search = location.search, hash = location.hash): string | undefined {
  const v = urlParams(search, hash).get(key);
  return v && SCOPED_ID.test(v) ? v : undefined;
}

/** The hash with `key` set to id (or removed when id is null); other keys keep their order. */
export function formatScoped(key: string, id: string | null, hash: string): string {
  const p = new URLSearchParams(hash.replace(/^#/, ''));
  if (id) p.set(key, id); else p.delete(key);
  const s = p.toString();
  return s ? '#' + s : '';
}

/** writeUrl for the scoped key (unthrottled: written on run changes only). */
export function writeScoped(key: string, id: string | null): void {
  replaceHash(formatScoped(key, id, location.hash));
}
