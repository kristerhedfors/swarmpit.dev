// Constellation layout (SPEC 5.2). Deterministic: integer-free IEEE arithmetic only, ids hashed with FNV-1a.
// DOM-free: imported by the Node tests.

import type { Layout, RunModel, Vec3 } from '../types.js';
import { NODE_KINDS, EDGE_KINDS } from '../types.js';
import { clamp, hash01, hash32, lerp, smoothstep, upperBound, v3 } from './math.js';

const K_TASK = 0, K_BOARD = 1, K_AGENT = 2, K_THREAD = 3, K_POST = 4, K_PERSONA = 5, K_HANDLE = 6;
export const KIND = { TASK: K_TASK, BOARD: K_BOARD, AGENT: K_AGENT, THREAD: K_THREAD, POST: K_POST,
                      PERSONA: K_PERSONA, HANDLE: K_HANDLE } as const;
void NODE_KINDS;

/** Engine-private extension of Layout: lookup tables shared by frame.ts, labels and the renderers. */
export interface EngineLayout extends Layout {
  nodeAgent: Int16Array;      // agent id per node, -1 otherwise (agent nodes only)
  agentNode: Int32Array;      // node index per agent
  edgeLive: Uint8Array;       // 1 when the edge is a live-to-live social edge
  edgeBend: Float32Array;     // reciprocal-pair bend
  fireEdge: Int32Array;       // edge per fire (CSR inverse)
  fireTs: Float64Array;       // fire times in fireByT order (binary search)
  stepPos: Float32Array;      // 3 per event: agent constellation position at the event time (agent events)
  scratch: Float32Array;      // partner accumulators for cohesion (nAgents)
  tEnd: number; prehistoryMs: number;
}

export const TASK_POS: readonly number[] = [-14, 2, 0];
export const BOARD_POS: readonly number[] = [14, 0, 0];

export function computeLayout(m: RunModel): EngineLayout {
  const n = m.nNodes, nodes = m.core.nodes;
  const pos = new Float32Array(n * 3);
  const task = v3(TASK_POS[0], TASK_POS[1], TASK_POS[2]);
  const board = v3(BOARD_POS[0], BOARD_POS[1], BOARD_POS[2]);
  const bx = board[0]!, bz = board[2]!;
  const nodeAgent = new Int16Array(n).fill(-1);
  const agentNode = new Int32Array(m.nAgents).fill(-1);

  // threads: seed first (t0 null), then t0, then id
  const threads: number[] = [];
  for (let i = 0; i < n; i++) {
    const k = m.nodeKind[i]!;
    if (k === K_THREAD) threads.push(i);
    if (k === K_AGENT) {
      const a = nodes[i]!.agent ?? -1;
      nodeAgent[i] = a;
      if (a >= 0 && a < m.nAgents) agentNode[a] = i;
    }
    if (k === K_TASK) pos.set(task, i * 3);
    if (k === K_BOARD) pos.set(board, i * 3);
  }
  threads.sort((p, q) => {
    const a = nodes[p]!.t0, b = nodes[q]!.t0;
    const sa = a === null ? -Infinity : a, sb = b === null ? -Infinity : b;
    if (sa !== sb) return sa - sb;
    const ia = nodes[p]!.id, ib = nodes[q]!.id;
    return ia < ib ? -1 : ia > ib ? 1 : 0;
  });
  const theta = new Float64Array(n);
  const J = Math.max(1, threads.length);
  threads.forEach((ti, j) => {
    const slug = nodes[ti]!.id;
    const th = (2 * Math.PI * j) / J + 0.2 * hash01(slug);
    theta[ti] = th;
    pos[ti * 3] = bx + 6 * Math.cos(th);
    pos[ti * 3 + 1] = 0;
    pos[ti * 3 + 2] = bz + 6 * Math.sin(th);
  });

  // posts: phyllotaxis tower above their thread
  const perThread = new Int32Array(n);
  const authorSin = new Float64Array(n), authorCos = new Float64Array(n), authorN = new Int32Array(n);
  for (let i = 0; i < n; i++) {
    if (m.nodeKind[i] !== K_POST) continue;
    const rec = nodes[i]!;
    const th = rec.thread;
    let cx = bx, cz = bz, base = hash01(rec.id) * 2 * Math.PI;
    let k = 0;
    if (th !== undefined && th >= 0 && th < n) {
      cx = pos[th * 3]!; cz = pos[th * 3 + 2]!; base = theta[th]!;
      k = perThread[th]!++;
    }
    const ang = base + 2.39996 * k, r = 1.3 + 0.22 * k;
    const px = cx + r * Math.cos(ang), pz = cz + r * Math.sin(ang);
    pos[i * 3] = px; pos[i * 3 + 1] = 0.9 + 0.55 * k; pos[i * 3 + 2] = pz;
    const au = rec.author;
    if (au !== undefined && au >= 0 && au < n) {
      const a2 = Math.atan2(pz - bz, px - bx);
      authorSin[au]! += Math.sin(a2); authorCos[au]! += Math.cos(a2); authorN[au]!++;
    }
  }
  // personas + handles on the outer ring
  for (let i = 0; i < n; i++) {
    const k = m.nodeKind[i];
    if (k !== K_PERSONA && k !== K_HANDLE) continue;
    const id = nodes[i]!.id, h = hash01(id);
    let ang = h * 2 * Math.PI;
    if (k === K_PERSONA && authorN[i]! > 0) ang = Math.atan2(authorSin[i]!, authorCos[i]!) + 0.15 * h;
    pos[i * 3] = bx + 11.5 * Math.cos(ang);
    pos[i * 3 + 1] = k === K_PERSONA ? 3.5 : 1.5;
    pos[i * 3 + 2] = bz + 11.5 * Math.sin(ang);
  }
  relax(m, pos);

  // agent anchors
  const agentAnchors: Layout['agentAnchors'] = [];
  for (let a = 0; a < m.nAgents; a++) {
    const phi = (2 * Math.PI * a) / Math.max(1, m.nAgents);
    agentAnchors.push({
      phi,
      task: v3(task[0]! + 4, task[1]! + 5 * Math.cos(phi), task[2]! + 5 * Math.sin(phi)),
      board: v3(board[0]! - 3, board[1]! + 4 + 1.5 * Math.cos(phi), board[2]! + 7 * Math.sin(phi)),
    });
  }

  // bounds over static nodes and anchors
  let mnx = Infinity, mny = Infinity, mnz = Infinity, mxx = -Infinity, mxy = -Infinity, mxz = -Infinity;
  const acc = (x: number, y: number, z: number) => {
    if (x < mnx) mnx = x; if (y < mny) mny = y; if (z < mnz) mnz = z;
    if (x > mxx) mxx = x; if (y > mxy) mxy = y; if (z > mxz) mxz = z;
  };
  for (let i = 0; i < n; i++) if (m.nodeKind[i] !== K_AGENT) acc(pos[i * 3]!, pos[i * 3 + 1]!, pos[i * 3 + 2]!);
  for (const an of agentAnchors) { acc(an.task[0]!, an.task[1]!, an.task[2]!); acc(an.board[0]!, an.board[1]!, an.board[2]!); }
  acc(bx - 9, 0, bz - 9); acc(bx + 9, 0, bz + 9);
  // biased slightly toward the task core: at the default yaw it sits nearer the camera and projects wider
  const center = v3((mnx + mxx) / 2 * 0.88 + task[0]! * 0.12, (mny + mxy) / 2, (mnz + mxz) / 2);
  let radius = 1;
  const accR = (x: number, y: number, z: number) => {
    const d = Math.hypot(x - center[0]!, y - center[1]!, z - center[2]!);
    if (d > radius) radius = d;
  };
  for (let i = 0; i < n; i++) if (m.nodeKind[i] !== K_AGENT) accR(pos[i * 3]!, pos[i * 3 + 1]!, pos[i * 3 + 2]!);
  for (const an of agentAnchors) { accR(an.task[0]!, an.task[1]!, an.task[2]!); accR(an.board[0]!, an.board[1]!, an.board[2]!); }
  accR(bx + 9, 0, bz); accR(bx - 9, 0, bz);

  // edge tables
  const ne = m.nEdges, edges = m.core.edges;
  const edgeLive = new Uint8Array(ne), edgeBend = new Float32Array(ne);
  const key = new Map<number, number>();
  for (let e = 0; e < ne; e++) {
    edgeLive[e] = edges[e]!.liveLive ? 1 : 0;
    key.set(m.edgeSrc[e]! * 65536 + m.edgeDst[e]!, e);
  }
  for (let e = 0; e < ne; e++) {
    if (key.has(m.edgeDst[e]! * 65536 + m.edgeSrc[e]!)) edgeBend[e] = 0.25;
  }
  const nf = m.fireT.length;
  const fireEdge = new Int32Array(nf);
  for (let e = 0; e < ne; e++) for (let f = m.fireStart[e]!; f < m.fireStart[e + 1]!; f++) fireEdge[f] = e;
  const fireTs = new Float64Array(m.fireByT.length);
  for (let i = 0; i < fireTs.length; i++) fireTs[i] = m.fireT[m.fireByT[i]!]!;

  const L: EngineLayout = {
    pos, agentAnchors, task, board, boardRadius: 9,
    worldline: { height: 24, prehistory: 2 },
    bounds: { center, radius },
    cohesionWindowMs: clamp(m.tEnd / 8, 10000, 60000),
    nodeAgent, agentNode, edgeLive, edgeBend, fireEdge, fireTs,
    stepPos: new Float32Array(m.nEvents * 3),
    scratch: new Float32Array(Math.max(1, m.nAgents)),
    tEnd: m.tEnd, prehistoryMs: m.core.run.prehistoryMs,
  };
  const tmp = v3();
  for (let i = 0; i < m.nEvents; i++) {
    const a = m.evA[i]!;
    if (a < 0 || a >= m.nAgents) continue;
    agentPos(m, L, a, m.evT[i]!, tmp);
    L.stepPos.set(tmp, i * 3);
  }
  return L;
}

/** 40 Jacobi iterations of pairwise repulsion among static non-hub nodes (min distance 1.1). */
function relax(m: RunModel, pos: Float32Array): void {
  const idx: number[] = [];
  for (let i = 0; i < m.nNodes; i++) {
    const k = m.nodeKind[i]!;
    if (k === K_THREAD || k === K_POST || k === K_PERSONA || k === K_HANDLE) idx.push(i);
  }
  const cnt = idx.length;
  if (cnt < 2) return;
  const hs = idx.map((i) => hash32(m.core.nodes[i]!.id));
  const disp = new Float64Array(cnt * 3);
  const order = idx.map((_, j) => j);
  const MIN = 1.1;
  for (let it = 0; it < 40; it++) {
    order.sort((p, q) => (pos[idx[p]! * 3]! - pos[idx[q]! * 3]!) || p - q);
    disp.fill(0);
    let moved = false;
    for (let oi = 0; oi < cnt; oi++) {
      const p = order[oi]!, ip = idx[p]! * 3;
      const px = pos[ip]!, py = pos[ip + 1]!, pz = pos[ip + 2]!;
      for (let oj = oi + 1; oj < cnt; oj++) {
        const q = order[oj]!, iq = idx[q]! * 3;
        let dx = pos[iq]! - px;
        if (dx >= MIN) break;
        let dy = pos[iq + 1]! - py, dz = pos[iq + 2]! - pz;
        let d = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (d >= MIN) continue;
        if (d < 1e-6) {
          const ang = ((hs[p]! ^ hs[q]!) >>> 0) / 4294967296 * 2 * Math.PI;
          dx = Math.cos(ang); dy = 0; dz = Math.sin(ang); d = 1e-6;
          const s = (MIN / 2);
          disp[p * 3]! -= dx * s; disp[p * 3 + 2]! -= dz * s; disp[q * 3]! += dx * s; disp[q * 3 + 2]! += dz * s;
          moved = true;
          continue;
        }
        const push = (MIN - d) / d * 0.5;
        disp[p * 3]! -= dx * push; disp[p * 3 + 1]! -= dy * push; disp[p * 3 + 2]! -= dz * push;
        disp[q * 3]! += dx * push; disp[q * 3 + 1]! += dy * push; disp[q * 3 + 2]! += dz * push;
        moved = true;
      }
    }
    if (!moved) break;
    for (let j = 0; j < cnt; j++) {
      const ip = idx[j]! * 3;
      pos[ip] = pos[ip]! + disp[j * 3]!;
      pos[ip + 1] = Math.max(0, pos[ip + 1]! + disp[j * 3 + 1]!);
      pos[ip + 2] = pos[ip + 2]! + disp[j * 3 + 2]!;
    }
  }
}

/** Agent position at t: lerp(task anchor, cohesive board perch, smoothstep(drift)). Pure in t. */
export function agentPos(m: RunModel, L0: Layout, agent: number, t: number, out: Vec3): Vec3 {
  const L = L0 as EngineLayout;
  const an = L.agentAnchors[agent];
  if (!an) { out[0] = 0; out[1] = 0; out[2] = 0; return out; }
  const a = m.driftAt(agent, t);
  let px = an.board[0]!, py = an.board[1]!, pz = an.board[2]!;
  if (a >= 0.5 && L.fireTs && L.fireTs.length) {
    const W = L.cohesionWindowMs, sc = L.scratch;
    sc.fill(0);
    let c = 0;
    const hi = upperBound(L.fireTs, t);
    const lo = upperBound(L.fireTs, t - W, 0, hi);
    for (let k = lo; k < hi; k++) {
      const f = m.fireByT[k]!, e = L.fireEdge[f]!;
      if (!L.edgeLive[e]) continue;
      const sa = L.nodeAgent[m.edgeSrc[e]!]!, da = L.nodeAgent[m.edgeDst[e]!]!;
      const j = sa === agent ? da : da === agent ? sa : -1;
      if (j < 0 || j === agent) continue;
      if (m.driftAt(j, t) < 0.5) continue;
      const wv = m.fireW[f]! * Math.exp(-(t - L.fireTs[k]!) / (W / 2));
      sc[j] = sc[j]! + wv; c += wv;
    }
    if (c > 0) {
      c = Math.min(1, c);
      let cx = px, cy = py, cz = pz, cn = 1;
      for (let j = 0; j < sc.length; j++) {
        if (sc[j]! <= 0) continue;
        const b = L.agentAnchors[j]!.board;
        cx += b[0]!; cy += b[1]!; cz += b[2]!; cn++;
      }
      const u = 0.5 * c;
      px = lerp(px, cx / cn, u); py = lerp(py, cy / cn, u); pz = lerp(pz, cz / cn, u);
    }
  }
  const s = smoothstep(a);
  out[0] = lerp(an.task[0]!, px, s);
  out[1] = lerp(an.task[1]!, py, s);
  out[2] = lerp(an.task[2]!, pz, s);
  return out;
}

/** Worldline height of time t (SPEC 5.3). */
export function timeToY(L: Layout, tEnd: number, t: number, prehistoryMs: number): number {
  const H = L.worldline.height, P = L.worldline.prehistory;
  if (t >= 0) return H * t / Math.max(1, tEnd);
  const den = Math.log1p(Math.max(1, -prehistoryMs) / 60000);
  return -P * Math.min(1, Math.log1p(-t / 60000) / (den || 1));
}

export const EDGE_K = Object.fromEntries(EDGE_KINDS.map((k, i) => [k, i])) as Record<(typeof EDGE_KINDS)[number], number>;
