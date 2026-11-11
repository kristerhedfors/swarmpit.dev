import { BundleError, CORE_FORMAT, DETAIL_FORMAT, INDEX_FORMAT } from '../types.js';
import type { CoreBundle, DetailBundle, IndexBundle, Loader, RunCard } from '../types.js';

function fail(msg: string, url: string, kind: BundleError['kind'] = 'shape'): never {
  throw new BundleError(msg, url, kind);
}
const isArr = Array.isArray;

export function assertIndex(x: unknown, url = 'index.json'): IndexBundle {
  const o = x as IndexBundle;
  if (!o || typeof o !== 'object') fail('index is not an object', url);
  if (o.format !== INDEX_FORMAT) fail(`index format ${String(o.format)}`, url, 'version');
  if (!isArr(o.runs)) fail('index.runs missing', url);
  return o;
}

export function assertCore(x: unknown, url = 'core'): CoreBundle {
  const o = x as CoreBundle;
  if (!o || typeof o !== 'object') fail('core is not an object', url);
  if (o.format !== CORE_FORMAT) fail(`core format ${String(o.format)}`, url, 'version');
  if (typeof o.hash !== 'string' || !o.run || !isArr(o.nodes) || !isArr(o.edges) || !isArr(o.strings) || !isArr(o.agents))
    fail('core missing fields', url);
  const ev = o.events, n = ev?.t?.length;
  if (typeof n !== 'number') fail('events.t missing', url);
  for (const k of ['d', 'k', 'a', 'n', 'e', 'f', 's', 'c', 'l', 'x'] as const)
    if (!isArr(ev[k]) || ev[k].length !== n) fail(`events.${k} length`, url);
  const fl = o.fires?.e?.length;
  if (typeof fl !== 'number') fail('fires missing', url);
  for (const k of ['t', 'w', 'ev'] as const) if (!isArr(o.fires[k]) || o.fires[k].length !== fl) fail(`fires.${k} length`, url);
  const N = o.nodes.length, E = o.edges.length;
  for (const e of o.edges) if (!(e.s >= 0 && e.s < N && e.d >= 0 && e.d < N)) fail('edge endpoint out of range', url);
  for (let i = 0; i < fl; i++) if (!((o.fires.e[i] as number) < E)) fail('fire edge out of range', url);
  if (!isArr(o.chapters)) o.chapters = [];
  if (!isArr(o.segments)) o.segments = [];
  if (!o.series) o.series = { binMs: 1000, density: [], boardShare: [] };
  return o;
}

export function assertDetail(x: unknown, hash: string, url = 'detail'): DetailBundle {
  const o = x as DetailBundle;
  if (!o || typeof o !== 'object') fail('detail is not an object', url);
  if (o.format !== DETAIL_FORMAT) fail(`detail format ${String(o.format)}`, url, 'version');
  if (o.hash !== hash) fail('detail hash mismatch', url, 'hash');
  if (!isArr(o.items) || typeof o.posts !== 'object') fail('detail missing fields', url);
  return o;
}

async function getJSON(url: string, signal?: AbortSignal, onProgress?: (f: number) => void, size = 0): Promise<unknown> {
  let res: Response;
  try {
    res = await fetch(url, { signal, credentials: 'same-origin' });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    fail(`network error: ${(e as Error).message}`, url, 'network');
  }
  if (!res.ok) fail(`HTTP ${res.status}`, url, 'network');
  let text: string;
  if (onProgress && res.body) {
    const total = Number(res.headers.get('content-length')) || size;
    const rd = res.body.getReader();
    const parts: Uint8Array[] = [];
    let got = 0;
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      if (total) onProgress(Math.min(0.99, got / total));
    }
    const buf = new Uint8Array(got);
    let o = 0;
    for (const p of parts) { buf.set(p, o); o += p.length; }
    text = new TextDecoder().decode(buf);
  } else text = await res.text();
  try { return JSON.parse(text); } catch { fail('invalid JSON', url); }
}

export function createLoader(base: URL): Loader {
  let idx: Promise<IndexBundle> | null = null;
  const cores = new Map<string, Promise<CoreBundle>>(); // LRU(4), insertion order
  const url = (p: string): string => new URL(p, base).href;

  function core(card: RunCard, signal?: AbortSignal): Promise<CoreBundle> {
    const key = card.core;
    let p = cores.get(key);
    if (p) { cores.delete(key); cores.set(key, p); }
    else {
      const u = url(card.core);
      // Fetch without the caller's signal so a prefetch/abort race never poisons the cache.
      p = getJSON(u).then((j) => {
        const c = assertCore(j, u);
        if (c.hash !== card.hash) fail('core hash differs from index', u, 'hash');
        return c;
      });
      p.catch(() => cores.delete(key));
      cores.set(key, p);
      while (cores.size > 4) cores.delete(cores.keys().next().value as string);
    }
    if (!signal) return p;
    return new Promise((res, rej) => {
      const ab = (): void => rej(new DOMException('aborted', 'AbortError'));
      if (signal.aborted) return ab();
      signal.addEventListener('abort', ab, { once: true });
      p!.then(res, rej);
    });
  }

  return {
    index() {
      if (!idx) {
        const u = url('index.json');
        idx = getJSON(u).then((j) => assertIndex(j, u));
        idx.catch(() => { idx = null; });
      }
      return idx;
    },
    core,
    async detail(card, onProgress, signal) {
      const u = url(card.detail);
      const j = await getJSON(u, signal, onProgress, card.detailBytes);
      const d = assertDetail(j, card.hash, u);
      onProgress?.(1);
      return d;
    },
    prefetch(card) { core(card).catch(() => undefined); },
  };
}
