// CPU picking over the projected set (SPEC 7.4). Priority: node > trail dot > social edge (dim items last).

import type { FrameState, PickFn, PickHit, ProjectedSet } from '../types.js';
import { NB } from '../types.js';

function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const vx = bx - ax, vy = by - ay, l2 = vx * vx + vy * vy;
  const u = l2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / l2)) : 0;
  return Math.hypot(px - ax - u * vx, py - ay - u * vy);
}

export const pick: PickFn = (p: ProjectedSet, f: FrameState, x: number, y: number): PickHit | null => {
  // Filtered-out ("dim") nodes and trail dots stay inspectable, but anything in the filter wins first:
  // in-filter node > in-filter trail dot > dim node > dim trail dot > social edge.
  let best = -1, bestScore = Infinity, bestDepth = Infinity, bestDim = true;
  const bits = f.nodes.bits;
  for (let i = 0; i < p.nodeCount; i++) {
    if (!p.nodeVisible[i]) continue;
    const r = p.nodeR[i]!, d = Math.hypot(p.nodeXY[i * 2]! - x, p.nodeXY[i * 2 + 1]! - y);
    if (d > Math.max(10, r + 4)) continue;
    const dim = i < bits.length && (bits[i]! & NB.DIM) !== 0;
    if (dim && !bestDim) continue;
    const s = d / Math.max(r, 1), z = p.nodeDepth[i]!;
    if ((!dim && bestDim) || s < bestScore - 1e-3 || (Math.abs(s - bestScore) <= 1e-3 && z < bestDepth)) {
      best = i; bestScore = s; bestDepth = z; bestDim = dim;
    }
  }
  if (best >= 0 && !bestDim) return { kind: 'node', node: best };
  let bestT = -1, bestTS = Infinity, bestTDim = true;
  const rgba = f.trail.rgba;
  for (let i = 0; i < p.trailCount; i++) {
    const r = p.trailR[i]!, d = Math.hypot(p.trailXY[i * 2]! - x, p.trailXY[i * 2 + 1]! - y);
    if (d > Math.max(7, r + 4)) continue;
    const dim = (rgba[i * 4 + 3] ?? 1) < 0.5; // event mask 1 -> alpha 0.12
    if (dim && !bestTDim) continue;
    const s = d / Math.max(r, 1);
    if ((!dim && bestTDim) || s < bestTS) { bestT = i; bestTS = s; bestTDim = dim; }
  }
  if (bestT >= 0 && !bestTDim) return { kind: 'step', event: f.trail.event[bestT]! };
  if (best >= 0) return { kind: 'node', node: best };
  if (bestT >= 0) return { kind: 'step', event: f.trail.event[bestT]! };
  let bestE = -1, bestD = 6;
  const ctl = p.edgeCtl, idx = p.edgeIdx;
  for (let i = 0; i < p.edgeCount; i++) {
    const ax = p.edgeXY[i * 4]!, ay = p.edgeXY[i * 4 + 1]!, bx = p.edgeXY[i * 4 + 2]!, by = p.edgeXY[i * 4 + 3]!;
    const cx = ctl ? ctl[i * 2]! : (ax + bx) / 2, cy = ctl ? ctl[i * 2 + 1]! : (ay + by) / 2;
    let qx = ax, qy = ay;
    for (let k = 1; k <= 8; k++) {
      const u = k / 8, w0 = (1 - u) * (1 - u), w1 = 2 * u * (1 - u), w2 = u * u;
      const rx = w0 * ax + w1 * cx + w2 * bx, ry = w0 * ay + w1 * cy + w2 * by;
      const d = segDist(x, y, qx, qy, rx, ry);
      if (d < bestD) { bestD = d; bestE = idx ? idx[i]! : i; }
      qx = rx; qy = ry;
    }
  }
  return bestE >= 0 ? { kind: 'edge', edge: bestE } : null;
};
