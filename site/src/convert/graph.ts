// The bundle's graph under construction: export.py Strings and RunBuilder's small utils (node / edge / fire /
// event), plus the "sort events, assign details" step that packs events and fires into columns (src/types.ts).
import { EDGE_KINDS, EVENT_KINDS, NODE_KINDS } from '../types.js';
import type { Detail, EdgeRec, NodeRec } from '../types.js';
import { type Dict, roundN, trunc } from './py.js';

export type NodeKindS = (typeof NODE_KINDS)[number];
export type EdgeKindS = (typeof EDGE_KINDS)[number];
export type EventKindS = (typeof EVENT_KINDS)[number];
const index = <K extends string>(ks: readonly K[]): Record<K, number> =>
  Object.fromEntries(ks.map((k, i) => [k, i])) as Record<K, number>;
const NK = index(NODE_KINDS);
const EK = index(EDGE_KINDS);
const VK = index(EVENT_KINDS);
/** Label truncation (export.py TRUNC). */
export const TRUNC = { l: 160, label: 48, preview: 240, title: 120, cmd: 600, out_head: 700, out_tail: 300, reason: 900,
                       content: 400, body: 1500, text: 400 } as const;

/** One event row before packing; `i` / `xi` (row and detail index) are assigned by pack(). */
export interface Ev { t: number; d: number; k: number; a: number; n: number; e: number; f: number; s: number; c: number;
                      l: string; x: Detail | null; lane: number; rank: number; seq: number; src: string; i: number; xi: number }
export interface EvOpts { a?: number | null; n?: number; e?: number; f?: number; s?: number | null; c?: number; label?: string;
                          x?: Detail | null; d?: number | null; lane?: number; rank?: number; seq?: number; src?: string }
export type GNode = NodeRec & Dict;
export type GEdge = EdgeRec & Dict;
type Fire = [t: number, w: number, ev: Ev | null];

/** Interned label strings; index 0 is ''. */
class Strings {
  readonly list: string[] = [''];
  private readonly idx = new Map<string, number>([['', 0]]);
  get(s: string): number {
    let i = this.idx.get(s);
    if (i === undefined) { i = this.list.length; this.idx.set(s, i); this.list.push(s); }
    return i;
  }
}

export class Graph {
  readonly strings = new Strings();
  readonly nodes: GNode[] = [];
  readonly edges: GEdge[] = [];
  readonly events: Ev[] = [];
  private readonly nidx = new Map<string, number>();
  private readonly eidx = new Map<string, number>();
  private readonly efires: Fire[][] = [];

  constructor(private readonly red: (s: string | null | undefined) => string) {}

  /** Node index by id. */
  id(nid: string): number | undefined { return this.nidx.get(nid); }

  node(nid: string, k: NodeKindS, label: string, t0: number | null, kw: Dict = {}): number {
    const i = this.nodes.length;
    const rec: Dict = { id: nid, k: NK[k], label: trunc(this.red(label), TRUNC.label), t0 };
    for (const [a, b] of Object.entries(kw)) if (b !== null && b !== undefined) rec[a] = b;
    this.nodes.push(rec as GNode);
    this.nidx.set(nid, i);
    return i;
  }

  /** The (s, d, k) edge, created on first use (its evidence text is that of the first use). */
  edge(s: number, d: number, k: EdgeKindS, inferred = false, evText: string | null = null): number {
    const key = `${s},${d},${EK[k]}`;
    let e = this.eidx.get(key);
    if (e === undefined) {
      e = this.edges.length;
      this.eidx.set(key, e);
      const rec: Dict = { s, d, k: EK[k] };
      if (inferred) rec.inferred = true;
      if (evText) rec.ev = this.strings.get(trunc(this.red(evText), TRUNC.l));
      if (this.nodes[s]!.k === NK.agent && this.nodes[d]!.k === NK.agent && s !== d) rec.liveLive = true;
      this.edges.push(rec as GEdge);
      this.efires.push([]);
    }
    return e;
  }

  fire(e: number, t: number, w = 1.0, ev: Ev | null = null): void { this.efires[e]!.push([Math.trunc(t), w, ev]); }

  event(t: number, k: EventKindS, o: EvOpts = {}): Ev {
    const a = o.a === undefined ? -1 : o.a;
    const ev: Ev = { t: Math.trunc(t), d: Math.max(0, Math.trunc(o.d || 0)), k: VK[k], a: a === null ? -1 : Math.trunc(a),
                     n: o.n ?? -1, e: o.e ?? -1, f: o.f ?? 0, s: o.s === null || o.s === undefined ? -1 : Math.trunc(o.s),
                     c: o.c ?? -1, l: o.label ?? '', x: o.x ?? null,
                     lane: o.lane ?? (a === null || a < 0 ? -1 : a), rank: o.rank ?? 3, seq: o.seq ?? 0, src: o.src ?? '',
                     i: -1, xi: -1 };
    this.events.push(ev);
    return ev;
  }

  /** Sort events (t, lane, rank, seq), number them and their details, intern labels; pack fires by (edge, t, ev);
   *  an edge whose fires share one weight != 1 carries it as `w`. */
  pack(): { events: Record<'t' | 'd' | 'k' | 'a' | 'n' | 'e' | 'f' | 's' | 'c' | 'l' | 'x', number[]>; items: Detail[];
            fires: Record<'e' | 't' | 'w' | 'ev', number[]> } {
    const evs = this.events;
    evs.sort((x, y) => x.t - y.t || x.lane - y.lane || x.rank - y.rank || x.seq - y.seq);
    evs.forEach((v, i) => { v.i = i; });
    const items: Detail[] = [];
    for (const v of evs) {
      if (v.x !== null) { v.xi = items.length; items.push(v.x); } else v.xi = -1;
    }
    const col = (f: (v: Ev) => number): number[] => evs.map(f);
    const events = { t: col((v) => v.t), d: col((v) => v.d), k: col((v) => v.k), a: col((v) => v.a), n: col((v) => v.n),
                     e: col((v) => v.e), f: col((v) => v.f), s: col((v) => v.s), c: col((v) => v.c),
                     l: col((v) => this.strings.get(trunc(v.l, TRUNC.l))), x: col((v) => v.xi) };
    const fires: [number, number, number, number][] = [];
    this.efires.forEach((fl, e) => { for (const [t, w, ev] of fl) fires.push([e, t, w, ev !== null ? ev.i : -1]); });
    fires.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[3] - b[3]);
    this.efires.forEach((fl, e) => {
      const ws = new Set(fl.map((x) => x[1]));
      if (ws.size === 1) {
        const w = [...ws][0]!;
        if (w !== 1.0) this.edges[e]!.w = roundN(w, 6);
      }
    });
    return { events, items,
             fires: { e: fires.map((x) => x[0]), t: fires.map((x) => x[1]), w: fires.map((x) => roundN(x[2], 6)),
                      ev: fires.map((x) => x[3]) } };
  }
}
