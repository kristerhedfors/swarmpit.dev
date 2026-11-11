// computeFrame: the scene as a pure function of (model, layout, t, mask, opts). Zero allocations when `reuse`
// is passed (except growth of the small hull/flash pools on first use). DOM-free.

import type { AgentState, FilterMask, FrameOpts, FrameState, Layout, RunModel, SegmentBuffer, StaticGeometry,
  SwarmState, ThemeColors } from '../types.js';
import { NB, EF, EVENT_KINDS, EDGE_KINDS, STEP_CATS } from '../types.js';
import { agentPos, KIND, timeToY, type EngineLayout } from './layout.js';
import { clamp, easeOut, lerp, upperBound, v3 } from './math.js';
import { readTheme } from './palette.js';

/** Engine-private bits beyond NB (fit in Uint16). */
export const NBX = { FREE: 2048, SOLVED: 4096 } as const;
export const SHAPE = { TASK: 0, BOARD: 1, AGENT: 2, THREAD: 3, POST: 4, PERSONA: 5, HANDLE: 6, TRAIL: 7, HULL: 8 } as const;
export const NODE_SIZE = [1.7, 1.3, 1.0, 0.7, 0.42, 0.6, 0.32] as const;
export const GLOW_MS = 1500;
export const TRAIL_MAX = 64;

const EK = (k: (typeof EVENT_KINDS)[number]) => EVENT_KINDS.indexOf(k);
const K_CMD = EK('cmd'), K_BOARD = EK('board'), K_SUBMIT = EK('submit'), K_IDLE = EK('idle'), K_POST = EK('post'),
  K_THINK = EK('think');
const E = (k: (typeof EDGE_KINDS)[number]) => EDGE_KINDS.indexOf(k);
const E_CONTAINS = E('contains'), E_AUTHORED = E('authored'), E_WORKS = E('works'), E_READS = E('reads'),
  E_WRITES = E('writes'), E_EXPOSURE = E('exposure'), E_MENTION = E('mention');

export interface EngineFrame extends FrameState {
  aux: Float32Array;            // per node: task solved fraction, agent step/budget
  edgeDrawn: Uint8Array;        // per edge, 1 when drawn this frame
  cumW: Float64Array;           // CSR prefix sum of fire weights (per run)
  leak: Uint8Array;             // per node: post carries secret_leak
  agentState: AgentState;       // scratch
  swarm: SwarmState;            // latest swarmAt
  geom: StaticGeometry | null;  // set by the engine when worldline geometry is built
  hullXY: Float32Array;         // filled by the engine: projected hull ring top (x, y, depth) per hull
}

const DEFAULT_THEME = readTheme(null);
export const themeOf = (L: Layout): ThemeColors => (L as EngineLayout & { colors?: ThemeColors }).colors ?? DEFAULT_THEME;

export function createFrameState(m: RunModel): EngineFrame {
  const n = m.nNodes, ne = m.nEdges, tc = Math.max(1, m.nAgents) * TRAIL_MAX;
  const cumW = new Float64Array(m.fireW.length + 1);
  for (let i = 0; i < m.fireW.length; i++) cumW[i + 1] = cumW[i]! + m.fireW[i]!;
  const leak = new Uint8Array(n);
  m.core.nodes.forEach((r, i) => { if (r.flags?.includes('secret_leak')) leak[i] = 1; });
  return {
    t: 0, mode: 'constellation', modeMix: 0, version: { pos: 0, style: 0 },
    nodes: { count: n, pos: new Float32Array(n * 3), size: new Float32Array(n), rgba: new Float32Array(n * 4),
             shape: new Uint8Array(n), bits: new Uint16Array(n), heat: new Float32Array(n) },
    trail: { count: 0, pos: new Float32Array(tc * 3), size: new Float32Array(tc), rgba: new Float32Array(tc * 4),
             event: new Int32Array(tc) },
    edges: { count: 0, edge: new Int32Array(ne), alpha: new Float32Array(ne), glow: new Float32Array(ne),
             bend: new Float32Array(ne) },
    pulses: { count: 0, edge: new Int32Array(4096), u: new Float32Array(4096), strength: new Float32Array(4096) },
    staticDraw: { worldlines: 0, pillars: 0, chords: 0, beads: 0 },
    plane: { y: 0, radius: 18, alpha: 0 },
    hulls: [], flashes: [],
    aux: new Float32Array(n), edgeDrawn: new Uint8Array(ne), cumW, leak,
    agentState: { id: 0, status: 'not_started', phase: 'task', step: 0, calls: 0, solved: false, boardSteps: 0, posts: 0,
                  boardShare: 0, tokens: [0, 0, 0], lastStep: -1, lastThink: -1, lastEvent: -1, drift: 0 },
    swarm: { onBoard: [], clusters: [], liveLive: 0, onsetT: null },
    geom: null, hullXY: new Float32Array(64 * 3),
  };
}

/** Last event index (from an ascending index list) with evT <= t, or -1. */
function lastLE(list: Uint32Array, evT: Float64Array, t: number): number {
  let lo = 0, hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (evT[list[mid]!]! <= t) lo = mid + 1; else hi = mid;
  }
  return lo - 1;
}

const tmp = v3();

export function computeFrame(m: RunModel, L0: Layout, t: number, mask: FilterMask, o: FrameOpts,
                             reuse?: FrameState): FrameState {
  const L = L0 as EngineLayout;
  const F = (reuse as EngineFrame | undefined) ?? createFrameState(m);
  const th = themeOf(L);
  const n = m.nNodes, mix = clamp(o.modeMix, 0, 1);
  const tEnd = m.tEnd, pre = m.core.run.prehistoryMs;
  const yNow = timeToY(L, tEnd, t, pre);
  F.t = t; F.mode = o.mode; F.modeMix = mix;
  F.version.pos++; F.version.style++;
  const N = F.nodes, pos = N.pos, bits = N.bits, rgba = N.rgba, size = N.size, heat = N.heat;
  const evT = m.evT, evD = m.core.events.d;
  const st = F.agentState;
  let taskNode = -1, solved = 0;

  // agents first (positions needed by posts? no; by hulls)
  for (let i = 0; i < n; i++) {
    const k = m.nodeKind[i]!;
    N.shape[i] = k;
    let b = 0;
    const present = m.nodeT0[i]! <= t && t < m.nodeT1[i]!;
    const mk = mask.node[i] ?? 2;
    size[i] = NODE_SIZE[k] ?? 0.5;
    F.aux[i] = 0;
    let px = L.pos[i * 3]!, py = L.pos[i * 3 + 1]!, pz = L.pos[i * 3 + 2]!;
    let c = th.kinds.handle, alpha = 1;
    // heat from the node's latest event
    const ne = m.nodeEvents[i];
    let hv = 0;
    if (ne && ne.length) {
      const j = lastLE(ne, evT, t);
      if (j >= 0) {
        const dt = t - evT[ne[j]!]!;
        if (dt < GLOW_MS) hv = 1 - dt / GLOW_MS;
      }
    }
    if (k === KIND.AGENT) {
      const a = L.nodeAgent[i]!;
      c = th.agents[((a % 8) + 8) % 8]!;
      if (a >= 0) {
        m.agentState(a, t, st);
        agentPos(m, L, a, t, tmp);
        px = tmp[0]!; py = tmp[1]!; pz = tmp[2]!;
        if (st.status === 'done' || st.status === 'crashed' || st.status === 'timed_out') { b |= NB.HOLLOW; alpha = 0.45; }
        if (st.status === 'dozing') b |= NB.DOZING;
        if (st.lastThink >= 0 && evT[st.lastThink]! + (evD[st.lastThink] ?? 0) > t &&
            m.evK[st.lastThink] === K_THINK && st.status === 'running') {
          b |= NB.THINKING;
          if (!o.reducedMotion) py += 0.15 * Math.sin(t * 0.004 + a);
        }
        if (st.phase === 'free') b |= NBX.FREE;
        if (st.solved) { b |= NBX.SOLVED; solved++; }
        F.aux[i] = clamp(st.step / Math.max(1, m.core.run.harness.budget), 0, 1);
        if (st.status === 'not_started') { b = 0; }
      }
      py = lerp(py, yNow, mix);
    } else if (k === KIND.POST) {
      const r = m.core.nodes[i]!;
      const ag = r.agent;
      c = ag !== undefined && ag >= 0 ? th.agents[ag % 8]! : th.kinds.post;
      if (r.seed) b |= NB.HOLLOW;
      if (F.leak[i]) b |= NB.LEAK;
      const t0 = m.nodeT0[i]!;
      if (t0 > -Infinity && t - t0 < 30000) b |= NB.NEW;
      const ty = timeToY(L, tEnd, r.seed ? (r.tSeed ?? -1) : Math.max(0, t0), pre);
      const tn = r.thread;
      if (tn !== undefined && mix > 0) {
        px = lerp(px, L.pos[tn * 3]!, mix); pz = lerp(pz, L.pos[tn * 3 + 2]!, mix);
      }
      py = lerp(py, ty, mix);
    } else {
      if (k === KIND.TASK) { c = th.kinds.task; taskNode = i; }
      else if (k === KIND.BOARD) c = th.kinds.board;
      else if (k === KIND.THREAD) c = th.kinds.thread;
      else if (k === KIND.PERSONA) { c = th.kinds.persona; b |= NB.HOLLOW; }
      py = lerp(py, yNow, mix);
    }
    if (present && mk > 0 && !(k === KIND.AGENT && L.nodeAgent[i]! >= 0 && st.status === 'not_started')) {
      b |= NB.VISIBLE;
      if (mk === 1) { b |= NB.DIM; alpha *= 0.15; }
      if (mk === 3) b |= NB.MATCH;
      if (hv > 0) b |= NB.ACTIVE;
      if (o.hover === i) b |= NB.HOVER;
      if (o.selected === i) b |= NB.PIN;
    } else {
      b = 0; size[i] = 0; alpha = 0; hv = 0;
    }
    bits[i] = b; heat[i] = hv;
    pos[i * 3] = px; pos[i * 3 + 1] = py; pos[i * 3 + 2] = pz;
    rgba[i * 4] = c[0]; rgba[i * 4 + 1] = c[1]; rgba[i * 4 + 2] = c[2]; rgba[i * 4 + 3] = alpha;
  }
  if (taskNode >= 0) {
    F.aux[taskNode] = m.nAgents ? solved / m.nAgents : 0;
    if (solved > 0) bits[taskNode]! |= NBX.SOLVED;
  }

  // trail dots
  const T = F.trail;
  let tc = 0;
  if (o.showTrails) {
    for (let a = 0; a < m.nAgents; a++) {
      const an = L.agentNode[a]!;
      if (an < 0 || !(bits[an]! & NB.VISIBLE)) continue;
      const list = m.agentEvents[a];
      if (!list) continue;
      let j = lastLE(list, evT, t), taken = 0;
      const start = tc;
      for (; j >= 0 && taken < TRAIL_MAX; j--) {
        const ev = list[j]!, ek = m.evK[ev]!;
        if (ek !== K_CMD && ek !== K_BOARD && ek !== K_SUBMIT && !(ek === K_IDLE && o.showIdle)) continue;
        const me = mask.event[ev] ?? 2;
        if (me === 0) continue;
        const cat = m.core.events.c[ev] ?? -1;
        const cc = th.cats[STEP_CATS[cat] ?? 'other'];
        const y = lerp(L.stepPos[ev * 3 + 1]!, timeToY(L, tEnd, evT[ev]!, pre), mix);
        T.pos[tc * 3] = L.stepPos[ev * 3]!; T.pos[tc * 3 + 1] = y; T.pos[tc * 3 + 2] = L.stepPos[ev * 3 + 2]!;
        T.size[tc] = ek === K_IDLE ? -0.12 : 0.16; // negative = hollow
        T.rgba[tc * 4] = cc[0]; T.rgba[tc * 4 + 1] = cc[1]; T.rgba[tc * 4 + 2] = cc[2];
        T.rgba[tc * 4 + 3] = me === 1 ? 0.12 : 1;
        T.event[tc] = ev;
        tc++; taken++;
      }
      for (let q = start; q < tc; q++) {
        const rank = taken > 1 ? (q - start) / (taken - 1) : 0; // 0 newest
        T.rgba[q * 4 + 3] = T.rgba[q * 4 + 3]! * (1 - 0.65 * rank);
      }
    }
  }
  T.count = tc;

  // edges
  const Ed = F.edges, drawn = F.edgeDrawn;
  drawn.fill(0);
  let ec = 0;
  const log8 = Math.log(8);
  for (let e = 0; e < m.nEdges; e++) {
    if (m.edgeFirstT[e]! > t) continue;
    const mk = mask.edge[e] ?? 2;
    if (mk === 0) continue;
    const s = m.edgeSrc[e]!, d = m.edgeDst[e]!;
    const bs = bits[s]!, bd = bits[d]!;
    if (!(bs & NB.VISIBLE) || !(bd & NB.VISIBLE)) continue;
    const fs = m.fireStart[e]!, fe = m.fireStart[e + 1]!;
    const j = upperBound(m.fireT, t, fs, fe);
    if (j === fs) continue;
    const W = F.cumW[j]! - F.cumW[fs]!, lastT = m.fireT[j - 1]!;
    const k = m.edgeKind[e]!;
    let al: number;
    if (k === E_CONTAINS || k === E_AUTHORED) al = 0.07;
    else if (k === E_WORKS) al = 0.12;
    else if (k === E_READS || k === E_WRITES) al = 0.15 + 0.6 * Math.min(1, Math.log1p(W) / log8);
    else if (k === E_EXPOSURE) al = 0.35;
    else al = 0.85;
    if (o.recentMs !== null && lastT < t - o.recentMs) al = Math.min(al, 0.1);
    if (mk === 1 || (bs & NB.DIM) || (bd & NB.DIM)) al *= 0.2;
    if (o.hover >= 0 && (s === o.hover || d === o.hover)) al = Math.min(1, al * 2 + 0.2);
    al *= 1 - 0.75 * mix; // worldline mode: chords carry the history
    Ed.edge[ec] = e; Ed.alpha[ec] = al;
    Ed.glow[ec] = t - lastT < GLOW_MS ? 1 - (t - lastT) / GLOW_MS : 0;
    Ed.bend[ec] = L.edgeBend[e]!;
    drawn[e] = 1;
    ec++;
  }
  Ed.count = ec;

  // pulses
  const P = F.pulses;
  let pc = 0;
  if (!o.reducedMotion && L.fireTs.length) {
    const win = clamp(600 * o.rate, 400, 6000);
    const hi = upperBound(L.fireTs, t);
    const lo = upperBound(L.fireTs, t - win, 0, hi);
    for (let q = lo; q < hi && pc < 4096; q++) {
      const f = m.fireByT[q]!, e = L.fireEdge[f]!;
      if (!drawn[e]) continue;
      const k = m.edgeKind[e]!;
      if (k === E_CONTAINS || k === E_AUTHORED) continue;
      const ev = m.fireEv[f]!;
      if (ev >= 0 && (mask.event[ev] ?? 2) < 2) continue;
      P.edge[pc] = e;
      P.u[pc] = easeOut((t - L.fireTs[q]!) / win);
      P.strength[pc] = Math.min(1, 0.35 + 0.65 * m.fireW[f]!);
      pc++;
    }
  }
  P.count = pc;

  // flashes (solve, leak, post) within the glow window; folded into heat for the halo pass
  let fc = 0;
  for (let i = m.eventAt(t); i >= 0 && t - evT[i]! < GLOW_MS && fc < 32; i--) {
    if ((mask.event[i] ?? 2) === 0) continue;
    const k = m.evK[i]!, f = m.evF[i]!;
    let kind: 'solve' | 'leak' | 'post' | null = null, node = m.evN[i]!;
    if (k === K_SUBMIT && (f & EF.SOLVE)) { kind = 'solve'; if (taskNode >= 0) node = taskNode; }
    else if (k === K_POST) kind = f & EF.LEAK ? 'leak' : 'post';
    if (!kind || node < 0 || node >= n) continue;
    const age = t - evT[i]!;
    let fl = F.flashes[fc];
    if (!fl) { fl = { node, k: kind, age }; F.flashes.push(fl); }
    fl.node = node; fl.k = kind; fl.age = age;
    const hv = 1 - age / GLOW_MS;
    if ((bits[node]! & NB.VISIBLE) && hv > heat[node]!) heat[node] = hv;
    fc++;
  }
  F.flashes.length = fc;

  // hulls (swarm clusters around current agent positions)
  const sw = m.swarmAt(t, F.swarm);
  F.swarm = sw;
  let hc = 0;
  for (const cl of sw.clusters) {
    let cx = 0, cy = 0, cz = 0, cn = 0;
    for (const a of cl) {
      const nd = L.agentNode[a];
      if (nd === undefined || nd < 0 || !(bits[nd]! & NB.VISIBLE)) continue;
      cx += pos[nd * 3]!; cy += pos[nd * 3 + 1]!; cz += pos[nd * 3 + 2]!; cn++;
    }
    if (cn < 2) continue;
    cx /= cn; cy /= cn; cz /= cn;
    let r = 0;
    for (const a of cl) {
      const nd = L.agentNode[a]!;
      if (nd < 0 || !(bits[nd]! & NB.VISIBLE)) continue;
      r = Math.max(r, Math.hypot(pos[nd * 3]! - cx, pos[nd * 3 + 1]! - cy, pos[nd * 3 + 2]! - cz));
    }
    let h = F.hulls[hc];
    if (!h) { h = { cx: 0, cy: 0, cz: 0, r: 0, members: [] }; F.hulls.push(h); }
    h.cx = cx; h.cy = cy; h.cz = cz; h.r = r + 1.5;
    h.members.length = 0;
    for (const a of cl) h.members.push(a);
    hc++;
  }
  F.hulls.length = hc;

  // worldline static draw counts + now plane
  const g = F.geom;
  F.staticDraw.worldlines = g ? upperBound(g.worldlines.t, t) : 0;
  F.staticDraw.pillars = g ? upperBound(g.pillars.t, t) : 0;
  F.staticDraw.chords = g ? upperBound(g.chords.t, t) : 0;
  F.staticDraw.beads = g ? upperBound(g.beads.t, t) : 0;
  F.plane.y = yNow; F.plane.radius = 18; F.plane.alpha = mix;
  return F;
}

/* ------------------------------------------------------------------ worldline static geometry */

interface SegTmp { t: number; a: number[]; b: number[]; c: number[]; w: number; s: number }

function pack(list: SegTmp[]): SegmentBuffer {
  list.sort((p, q) => p.t - q.t);
  const n = list.length;
  const sb: SegmentBuffer = { count: n, t: new Float64Array(n), a: new Float32Array(n * 3), b: new Float32Array(n * 3),
    rgba: new Float32Array(n * 4), width: new Float32Array(n), style: new Uint8Array(n) };
  list.forEach((s, i) => {
    sb.t[i] = s.t; sb.a.set(s.a, i * 3); sb.b.set(s.b, i * 3); sb.rgba.set(s.c, i * 4);
    sb.width[i] = s.w; sb.style[i] = s.s;
  });
  return sb;
}

/** Static worldline geometry (SPEC 5.3), sorted by segment end time. style 3 = round bead (engine-private). */
export function buildStaticGeometry(m: RunModel, L0: Layout, mask: FilterMask, theme?: ThemeColors): StaticGeometry {
  const L = L0 as EngineLayout, th = theme ?? themeOf(L);
  const tEnd = m.tEnd, pre = m.core.run.prehistoryMs, H = L.worldline.height;
  const y = (t: number) => timeToY(L, tEnd, t, pre);
  const wl: SegTmp[] = [], pil: SegTmp[] = [], ch: SegTmp[] = [], bd: SegTmp[] = [];
  const warm = th.liveLive;
  for (let a = 0; a < m.nAgents; a++) {
    const nd = L.agentNode[a]!;
    if (nd < 0 || (mask.node[nd] ?? 2) === 0) continue;
    const dimA = (mask.node[nd] ?? 2) === 1;
    const list = m.agentEvents[a]!;
    const ac = th.agents[a % 8]!;
    let prev: number[] | null = null;
    for (let j = 0; j < list.length; j++) {
      const ev = list[j]!;
      const me = mask.event[ev] ?? 2;
      if (me === 0) continue;
      const t = m.evT[ev]!;
      const p = [L.stepPos[ev * 3]!, y(t), L.stepPos[ev * 3 + 2]!];
      if (prev) {
        const free = (m.evF[ev]! & EF.FREE) !== 0;
        const c = free ? [lerp(ac[0], warm[0], 0.45), lerp(ac[1], warm[1], 0.45), lerp(ac[2], warm[2], 0.45)]
                       : [ac[0] * 0.8, ac[1] * 0.8, ac[2] * 0.8];
        const alpha = dimA || me === 1 ? 0.15 : 0.8;
        wl.push({ t, a: prev, b: p, c: [c[0]!, c[1]!, c[2]!, alpha], w: m.evK[ev] === K_BOARD ? 4 : 2, s: 0 });
      }
      prev = p;
    }
  }
  const seedMin = new Float64Array(m.nNodes).fill(0);
  for (let i = 0; i < m.nNodes; i++) {
    const r = m.core.nodes[i]!;
    if (m.nodeKind[i] !== KIND.POST) continue;
    const mk = mask.node[i] ?? 2;
    if (mk === 0) continue;
    const tn = r.thread ?? -1;
    const tt = r.seed ? (r.tSeed ?? -1) : Math.max(0, m.nodeT0[i]!);
    if (r.seed && tn >= 0) seedMin[tn] = Math.min(seedMin[tn]!, tt);
    const bx = tn >= 0 ? L.pos[tn * 3]! : L.pos[i * 3]!, bz = tn >= 0 ? L.pos[tn * 3 + 2]! : L.pos[i * 3 + 2]!;
    const c = r.agent !== undefined && r.agent >= 0 ? th.agents[r.agent % 8]! : th.kinds.post;
    const p = [bx, y(tt), bz];
    bd.push({ t: tt, a: p, b: p, c: [c[0], c[1], c[2], mk === 1 ? 0.2 : 0.95], w: r.seed ? 6 : 9, s: 3 });
  }
  for (let i = 0; i < m.nNodes; i++) {
    if (m.nodeKind[i] !== KIND.THREAD) continue;
    const mk = mask.node[i] ?? 2;
    if (mk === 0) continue;
    const x = L.pos[i * 3]!, z = L.pos[i * 3 + 2]!, t0 = Math.max(0, m.nodeT0[i]!);
    const c = th.kinds.thread, al = mk === 1 ? 0.12 : 0.5;
    pil.push({ t: t0, a: [x, y(t0), z], b: [x, H, z], c: [c[0], c[1], c[2], al], w: 2, s: 0 });
    if (seedMin[i]! < 0) pil.push({ t: seedMin[i]!, a: [x, y(seedMin[i]!), z], b: [x, 0, z], c: [c[0], c[1], c[2], al * 0.7], w: 1.5, s: 2 });
  }
  const tmpA = v3();
  const anchor = (node: number, t: number, ev: number): number[] => {
    const a = L.nodeAgent[node]!;
    if (a >= 0) {
      if (ev >= 0 && m.evA[ev] === a) return [L.stepPos[ev * 3]!, y(t), L.stepPos[ev * 3 + 2]!];
      agentPos(m, L, a, t, tmpA);
      return [tmpA[0]!, y(t), tmpA[2]!];
    }
    const thr = m.nodeKind[node] === KIND.POST ? (m.core.nodes[node]!.thread ?? node) : node;
    return [L.pos[thr * 3]!, y(t), L.pos[thr * 3 + 2]!];
  };
  for (let e = 0; e < m.nEdges; e++) {
    const k = m.edgeKind[e]!;
    if (k === E_CONTAINS || k === E_AUTHORED || k === E_WORKS) continue;
    const mk = mask.edge[e] ?? 2;
    if (mk === 0) continue;
    const s = m.edgeSrc[e]!, d = m.edgeDst[e]!;
    if ((mask.node[s] ?? 2) === 0 || (mask.node[d] ?? 2) === 0) continue;
    const rec = m.core.edges[e]!;
    const c = L.edgeLive[e] ? th.liveLive : th.edges[EDGE_KINDS[k]!];
    const style = k === E_MENTION ? 2 : (rec.inferred || k === E_EXPOSURE) ? 1 : 0;
    for (let f = m.fireStart[e]!; f < m.fireStart[e + 1]!; f++) {
      const t = m.fireT[f]!, ev = m.fireEv[f]!;
      if (t < 0) continue;
      ch.push({ t, a: anchor(s, t, ev), b: anchor(d, t, -1), c: [c[0], c[1], c[2], mk === 1 ? 0.1 : 0.55],
                w: L.edgeLive[e] ? 2.25 : 1.5, s: style });
    }
  }
  return { worldlines: pack(wl), pillars: pack(pil), chords: pack(ch), beads: pack(bd) };
}
