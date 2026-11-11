import { BundleError, CORE_FORMAT, DETAIL_FORMAT, INDEX_FORMAT } from '../types.js';
import type { CoreBundle, DetailBundle, IndexBundle, Loader, RunCard } from '../types.js';
import { assertCore, assertDetail } from './loader.js';

/* An in-memory Loader (docs/browser-swarm.md §7): the browser lab feeds the player converted traces
 * ({card, core, detail} from convertRun, §5) instead of fetched bundles. Same contract as data/loader.ts: core() and
 * detail() validate the bundles (assertCore / assertDetail) and honour the abort signal; a card whose hash differs from
 * its stored core fails with BundleError('hash'). Entries can be (re)set at any time; set() with a new hash replaces
 * the bundles of a live trace. A provider (`resolve`) may supply entries lazily, e.g. by re-converting a stored zip. */

export interface MemoryEntry { card: RunCard; core: CoreBundle; detail: DetailBundle | null }
export type EntryResolver = (card: RunCard, signal?: AbortSignal) => Promise<MemoryEntry | null>;

export interface MemoryLoader extends Loader {
  set(e: MemoryEntry): void;
  delete(id: string): void;
  has(id: string): boolean;
  readonly cards: RunCard[];            // current cards, insertion order (the index's runs)
}

/** An empty-findings index over `cards` (the lab has no pooled findings). */
export function memoryIndex(cards: RunCard[] = [], generated = new Date().toISOString()): IndexBundle {
  return {
    format: INDEX_FORMAT, generated,
    generator: { tool: 'tools/export.py', obsSchema: 0, obsDerive: 0, codeVersions: [], ctf: 'keep', redactions: {}, truncation: {} },
    featured: cards[0]?.id ?? '',
    runs: cards,
    findings: { source: 'obs', note: '', pooled: [], cells: [], caveats: [], mismatches: [] },
  };
}

const aborted = (): DOMException => new DOMException('aborted', 'AbortError');
function guard<T>(p: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return p;
  return new Promise((res, rej) => {
    if (signal.aborted) { rej(aborted()); return; }
    signal.addEventListener('abort', () => rej(aborted()), { once: true });
    p.then(res, rej);
  });
}

export function createMemoryLoader(opts: { index?: IndexBundle; resolve?: EntryResolver } = {}): MemoryLoader {
  const idx = opts.index ?? memoryIndex();
  const entries = new Map<string, MemoryEntry>();
  const url = (c: RunCard, k: string): string => `memory:${c.id}.${c.hash}.${k}`;

  async function entry(card: RunCard, signal?: AbortSignal): Promise<MemoryEntry> {
    if (signal?.aborted) throw aborted();
    let e = entries.get(card.id);
    if (!e && opts.resolve) {
      const r = await guard(opts.resolve(card, signal), signal);
      if (r) { set(r); e = entries.get(card.id); }
    }
    if (!e) throw new BundleError('trace not in memory', url(card, 'core'), 'network');
    return e;
  }

  function set(e: MemoryEntry): void {
    if (!e || !e.card || typeof e.card.id !== 'string') throw new BundleError('entry without card', 'memory:', 'shape');
    if (e.core?.format !== CORE_FORMAT) throw new BundleError(`core format ${String(e.core?.format)}`, url(e.card, 'core'), 'version');
    if (e.detail && e.detail.format !== DETAIL_FORMAT) throw new BundleError(`detail format ${String(e.detail.format)}`, url(e.card, 'detail'), 'version');
    entries.set(e.card.id, e);
  }

  return {
    index: () => Promise.resolve(idx),
    async core(card, signal) {
      const e = await entry(card, signal);
      const u = url(card, 'core');
      const c = assertCore(e.core, u);
      if (c.hash !== card.hash) throw new BundleError('core hash differs from card', u, 'hash');
      return c;
    },
    async detail(card, onProgress, signal) {
      const e = await entry(card, signal);
      const u = url(card, 'detail');
      if (!e.detail) throw new BundleError('no detail bundle', u, 'network');
      const d = assertDetail(e.detail, card.hash, u);
      onProgress?.(1);
      return d;
    },
    prefetch(card) { if (!entries.has(card.id) && opts.resolve) entry(card).catch(() => undefined); },
    set,
    delete(id) { entries.delete(id); },
    has: (id) => entries.has(id),
    get cards() { return [...entries.values()].map((e) => e.card); },
  };
}
