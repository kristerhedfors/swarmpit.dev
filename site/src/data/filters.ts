import { EDGE_KINDS, EF, EVENT_KINDS, NODE_KINDS } from '../types.js';
import type { EdgeKind, EventKind, FilterMask, FilterState, FilterStore, NodeKind, Phase, RunModel } from '../types.js';
import { Emitter } from '../util/bus.js';

export const HIDDEN_EVENT_KINDS: readonly EventKind[] = ['think', 'idle', 'proto', 'connect', 'disconnect', 'retry'];
export const ALL_NODE_KINDS: readonly (NodeKind | 'trail')[] = [...NODE_KINDS, 'trail'];
const ALWAYS_EDGES: readonly EdgeKind[] = ['contains', 'authored'];

export function defaultFilters(): FilterState {
  return {
    nodeKinds: new Set(ALL_NODE_KINDS),
    agents: null,
    neighbourhoodOf: null,
    eventKinds: new Set(EVENT_KINDS.filter((k) => !HIDDEN_EVENT_KINDS.includes(k))),
    edgeKinds: new Set(EDGE_KINDS.filter((k) => k !== 'exposure')),
    phases: new Set<Phase>(['task', 'free']),
    recentMs: null,
    range: null,
    text: '',
    mode: 'dim',
  };
}
export const DEFAULT_FILTERS: Readonly<FilterState> = defaultFilters();

const setEq = <T>(a: Set<T>, b: Set<T>): boolean => a.size === b.size && [...a].every((x) => b.has(x));
const bits = <T>(all: readonly T[], s: Set<T>): number => all.reduce((acc, k, i) => (s.has(k) ? acc + 2 ** i : acc), 0);
const unbits = <T>(all: readonly T[], v: number): Set<T> => new Set(all.filter((_, i) => Math.floor(v / 2 ** i) % 2 === 1));

export function activeCount(s: FilterState): number {
  const d = DEFAULT_FILTERS;
  return [!setEq(s.nodeKinds, d.nodeKinds), s.agents !== null, s.neighbourhoodOf !== null, !setEq(s.eventKinds, d.eventKinds),
    !setEq(s.edgeKinds, d.edgeKinds), !setEq(s.phases, d.phases), s.recentMs !== null, s.range !== null, s.text.trim() !== '']
    .filter(Boolean).length;
}

/** Neighbourhood: the node plus 1-hop neighbours over edges that ever fire. */
export function neighbourhood(m: RunModel, node: number): Uint8Array {
  const keep = new Uint8Array(m.nNodes);
  if (node < 0 || node >= m.nNodes) return keep.fill(1);
  keep[node] = 1;
  for (let e = 0; e < m.nEdges; e++) {
    if (!Number.isFinite(m.edgeFirstT[e]!)) continue;
    if (m.edgeSrc[e] === node) keep[m.edgeDst[e]!] = 1;
    else if (m.edgeDst[e] === node) keep[m.edgeSrc[e]!] = 1;
  }
  return keep;
}

export function compileMask(m: RunModel | null, s: FilterState, version: number): FilterMask {
  if (!m) return { version, node: new Uint8Array(0), edge: new Uint8Array(0), event: new Uint8Array(0), hits: new Uint32Array(0), activeCount: activeCount(s) };
  const off = s.mode === 'dim' ? 1 : 0;
  const N = m.nNodes, node = new Uint8Array(N);
  const nb = s.neighbourhoodOf !== null ? neighbourhood(m, s.neighbourhoodOf) : null;
  const q = s.text.trim();
  const found = q ? m.search(q) : null;
  const authorAgent = (i: number): number => m.core.nodes[i]?.agent ?? -1;
  for (let i = 0; i < N; i++) {
    const kind = NODE_KINDS[m.nodeKind[i]!]!;
    let ok = s.nodeKinds.has(kind);
    if (ok && s.agents) { const a = authorAgent(i); if (a >= 0 && !s.agents.has(a)) ok = false; }
    if (ok && nb && !nb[i]) ok = false;
    node[i] = ok ? (found?.nodes[i] ? 3 : 2) : off;
  }
  const edge = new Uint8Array(m.nEdges);
  for (let e = 0; e < m.nEdges; e++) {
    const k = EDGE_KINDS[m.edgeKind[e]!]!;
    const a = node[m.edgeSrc[e]!]!, b = node[m.edgeDst[e]!]!;
    if (!ALWAYS_EDGES.includes(k) && !s.edgeKinds.has(k)) edge[e] = off;
    else if (a === 0 || b === 0) edge[e] = 0;
    else edge[e] = a === 1 || b === 1 ? 1 : 2;
  }
  const V = m.nEvents, event = new Uint8Array(V);
  const kindOn = new Uint8Array(EVENT_KINDS.length);
  EVENT_KINDS.forEach((k, i) => { kindOn[i] = s.eventKinds.has(k) ? 1 : 0; });
  const hitSet = found ? new Uint8Array(V) : null;
  if (found && hitSet) for (const i of found.events) hitSet[i] = 1;
  const r = s.range;
  const both = s.phases.has('task') && s.phases.has('free');
  for (let i = 0; i < V; i++) {
    if (!kindOn[m.evK[i]!]) { event[i] = 0; continue; }
    const a = m.evA[i]!, t = m.evT[i]!, n = m.evN[i]!;
    let ok = true;
    if (s.agents && a >= 0 && !s.agents.has(a)) ok = false;
    else if (!both && a >= 0 && !s.phases.has(m.evF[i]! & EF.FREE ? 'free' : 'task')) ok = false;
    else if (r && (t < r[0] || t > r[1])) ok = false;
    else if (nb) {
      const an = a >= 0 ? m.core.agents[a]?.node ?? -1 : -1;
      if (!((n >= 0 && nb[n] && (an < 0 || nb[an])) || (n < 0 && an >= 0 && nb[an]))) ok = false;
    }
    event[i] = ok ? (hitSet?.[i] ? 3 : 2) : off;
  }
  const hits = found ? found.events.filter((i) => event[i]! >= 2) : new Uint32Array(0);
  return { version, node, edge, event, hits, activeCount: activeCount(s) };
}

function clone(s: FilterState): FilterState {
  return { ...s, nodeKinds: new Set(s.nodeKinds), agents: s.agents ? new Set(s.agents) : null, eventKinds: new Set(s.eventKinds),
    edgeKinds: new Set(s.edgeKinds), phases: new Set(s.phases), range: s.range ? [s.range[0], s.range[1]] : null };
}

export function encodeFilters(s: FilterState): string {
  const d = DEFAULT_FILTERS, out: string[] = [];
  if (!setEq(s.nodeKinds, d.nodeKinds)) out.push('n' + bits(ALL_NODE_KINDS, s.nodeKinds).toString(36));
  if (s.agents) out.push('a' + [...s.agents].sort((x, y) => x - y).join('.'));
  if (s.neighbourhoodOf !== null) out.push('h' + s.neighbourhoodOf);
  if (!setEq(s.eventKinds, d.eventKinds)) out.push('e' + bits(EVENT_KINDS, s.eventKinds).toString(36));
  if (!setEq(s.edgeKinds, d.edgeKinds)) out.push('g' + bits(EDGE_KINDS, s.edgeKinds).toString(36));
  if (!setEq(s.phases, d.phases)) out.push('p' + bits(['task', 'free'] as const, s.phases));
  if (s.recentMs !== null) out.push('r' + s.recentMs);
  if (s.range) out.push('b' + Math.round(s.range[0]) + '.' + Math.round(s.range[1]));
  if (s.mode === 'hide') out.push('m1');
  if (s.text) out.push('q' + encodeURIComponent(s.text).replace(/~/g, '%7E'));
  return out.join('~');
}

export function decodeFilters(q: string): FilterState {
  const s = defaultFilters();
  for (const part of q.split('~')) {
    const k = part[0], v = part.slice(1);
    if (!k) continue;
    const num = (x: string): number => Number(x);
    switch (k) {
      case 'n': s.nodeKinds = unbits(ALL_NODE_KINDS, parseInt(v, 36) || 0); break;
      case 'a': s.agents = new Set(v ? v.split('.').map(num).filter(Number.isFinite) : []); break;
      case 'h': if (Number.isFinite(num(v))) s.neighbourhoodOf = num(v); break;
      case 'e': s.eventKinds = unbits(EVENT_KINDS, parseInt(v, 36) || 0); break;
      case 'g': s.edgeKinds = unbits(EDGE_KINDS, parseInt(v, 36) || 0); break;
      case 'p': s.phases = unbits(['task', 'free'] as const, num(v) || 0); break;
      case 'r': if (Number.isFinite(num(v))) s.recentMs = num(v); break;
      case 'b': { const [x, y] = v.split('.').map(num); if (Number.isFinite(x) && Number.isFinite(y)) s.range = [x!, y!]; break; }
      case 'm': s.mode = v === '1' ? 'hide' : 'dim'; break;
      case 'q': try { s.text = decodeURIComponent(v); } catch { s.text = v; } break;
    }
  }
  return s;
}

export function createFilterStore(): FilterStore {
  const bus = new Emitter<{ change: { state: Readonly<FilterState>; mask: FilterMask } }>();
  let state = defaultFilters(), model: RunModel | null = null, ver = 0;
  let mask = compileMask(null, state, ver);
  const recompile = (): void => {
    mask = compileMask(model, state, ++ver);
    bus.emit('change', { state, mask });
  };
  return {
    on: bus.on.bind(bus), emit: bus.emit.bind(bus),
    get: () => state,
    set(patch) { state = { ...clone(state), ...patch }; recompile(); },
    reset() { state = defaultFilters(); recompile(); },
    bind(m) {
      model = m;
      if (state.neighbourhoodOf !== null && state.neighbourhoodOf >= m.nNodes) state.neighbourhoodOf = null;
      if (state.range && state.range[0] > m.tEnd) state.range = null;
      recompile();
    },
    mask: () => mask,
    encode: () => encodeFilters(state),
    decode(q) { state = decodeFilters(q); recompile(); },
  };
}
