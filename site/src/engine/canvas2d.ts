// Canvas2D fallback renderer (SPEC 7.3): same Renderer interface and projection as WebGL2, painter-sorted,
// no glow. Supports worldline mode by drawing static segments up to the frame's draw counts.

import type { CameraMatrices, FrameState, RunModel, SegmentBuffer, StaticGeometry, ThemeColors } from '../types.js';
import { EDGE_KINDS, NB } from '../types.js';
import { fogFactor, projectPoint } from './math.js';
import { css } from './palette.js';
import { NBX, SHAPE, type EngineFrame } from './frame.js';
import type { EngineRenderer } from './gl/webgl2.js';

const DASH: Record<string, number[]> = { mention: [2, 4], exposure: [6, 4], dm: [13, 5], upvote: [8, 3, 2, 3] };

export function createCanvas2DRenderer(canvas: HTMLCanvasElement): EngineRenderer | null {
  const g = canvas.getContext('2d', { alpha: false });
  if (!g) return null;
  let model: RunModel | null = null, theme: ThemeColors | null = null, geom: StaticGeometry | null = null;
  let dpr = 1, cssW = 1, cssH = 1;
  let proj = new Float32Array(0), order = new Uint32Array(0);
  const tp = new Float32Array(3), q = new Float32Array(9);

  const r: EngineRenderer = {
    kind: 'canvas2d', drawCalls: 0, onRestore: null, lost: false,
    ctx: { camDistance: 46, board: [14, 0, 0], center: [0, 0, 0], bend: new Float32Array(0) },
    setTopology(m, th) {
      model = m; theme = th;
      proj = new Float32Array(m.nNodes * 3);
      order = new Uint32Array(m.nNodes);
    },
    uploadStatic(gm) { geom = gm; },
    resize(w, h, d) {
      cssW = w; cssH = h; dpr = d;
      canvas.width = Math.max(1, Math.round(w * d)); canvas.height = Math.max(1, Math.round(h * d));
    },
    draw(f0: FrameState, cam: CameraMatrices) {
      if (!model || !theme) return;
      const f = f0 as EngineFrame, th = theme, vp = cam.viewProj, W = cssW, H = cssH;
      const camD = r.ctx.camDistance;
      let dc = 0;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.globalAlpha = 1;
      g.fillStyle = css(th.bg);
      g.fillRect(0, 0, W, H);
      const pxw = cam.proj[5]! * H * 0.5;
      const P = (x: number, y: number, z: number, o: number) => { tp[0] = x; tp[1] = y; tp[2] = z; return projectPoint(vp, tp, 0, W, H, q, o); };
      // board disc / now plane rings
      const ring = (cx: number, cy: number, cz: number, rad: number, a: number, c: [number, number, number]) => {
        if (a <= 0.01) return;
        g.strokeStyle = css(c, a); g.lineWidth = 1;
        for (const rr of [rad, rad * 0.66, rad * 0.33]) {
          g.beginPath();
          for (let k = 0; k <= 48; k++) {
            const an = (k / 48) * Math.PI * 2;
            if (!P(cx + rr * Math.cos(an), cy, cz + rr * Math.sin(an), 0)) continue;
            if (k === 0) g.moveTo(q[0]!, q[1]!); else g.lineTo(q[0]!, q[1]!);
          }
          g.stroke(); dc++;
        }
      };
      const B = r.ctx.board, C = r.ctx.center;
      ring(B[0]!, B[1]!, B[2]!, 9, 0.35 * (1 - f.modeMix), th.kinds.board);
      ring(C[0]!, f.plane.y, C[2]!, f.plane.radius, 0.3 * f.modeMix, th.text);
      // worldline segments
      if (geom && f.modeMix > 0) {
        const lists: [SegmentBuffer, number, boolean][] = [[geom.worldlines, f.staticDraw.worldlines, false],
          [geom.pillars, f.staticDraw.pillars, true], [geom.chords, f.staticDraw.chords, false], [geom.beads, f.staticDraw.beads, false]];
        for (const [sb, cnt, clampY] of lists) {
          for (let i = 0; i < Math.min(cnt, sb.count); i++) {
            const ay = clampY ? Math.min(sb.a[i * 3 + 1]!, f.plane.y) : sb.a[i * 3 + 1]!;
            const by = clampY ? Math.min(sb.b[i * 3 + 1]!, f.plane.y) : sb.b[i * 3 + 1]!;
            if (!P(sb.a[i * 3]!, ay, sb.a[i * 3 + 2]!, 0) || !P(sb.b[i * 3]!, by, sb.b[i * 3 + 2]!, 3)) continue;
            const a = sb.rgba[i * 4 + 3]! * f.modeMix * fogFactor(q[2]!, camD);
            const c: [number, number, number] = [sb.rgba[i * 4]!, sb.rgba[i * 4 + 1]!, sb.rgba[i * 4 + 2]!];
            if (sb.style[i] === 3) {
              g.fillStyle = css(c, a); g.beginPath(); g.arc(q[0]!, q[1]!, sb.width[i]! / 2, 0, 7); g.fill();
            } else {
              g.strokeStyle = css(c, a); g.lineWidth = sb.width[i]!;
              g.setLineDash(sb.style[i] === 1 ? [6, 4] : sb.style[i] === 2 ? [2, 3] : []);
              g.beginPath(); g.moveTo(q[0]!, q[1]!); g.lineTo(q[3]!, q[4]!); g.stroke();
            }
            dc++;
          }
        }
        g.setLineDash([]);
      }
      // project nodes
      const N = f.nodes, n = N.count;
      let vis = 0;
      for (let i = 0; i < n; i++) {
        if (!(N.bits[i]! & NB.VISIBLE)) continue;
        if (!projectPoint(vp, N.pos, i * 3, W, H, proj, i * 3)) continue;
        order[vis++] = i;
      }
      // edges (quadratic through the projected Bezier midpoint)
      g.lineCap = 'round';
      for (let k = 0; k < f.edges.count; k++) {
        const e = f.edges.edge[k]!, s = model.edgeSrc[e]!, d = model.edgeDst[e]!;
        const pa = s * 3, pb = d * 3;
        if (proj[pa + 2]! <= 0 || proj[pb + 2]! <= 0) continue;
        const ax = N.pos[pa]!, ay = N.pos[pa + 1]!, az = N.pos[pa + 2]!, bx = N.pos[pb]!, by = N.pos[pb + 1]!, bz = N.pos[pb + 2]!;
        const dx = bx - ax, dy = by - ay, dz = bz - az, len = Math.hypot(dx, dy, dz);
        let sx = -dz, sz = dx;
        const sl = Math.hypot(sx, sz) || 1; sx /= sl; sz /= sl;
        const bend = f.edges.bend[k]!;
        const cx = (ax + bx) / 2 + sx * bend * len, cy = (ay + by) / 2 + 0.12 * len, cz = (az + bz) / 2 + sz * bend * len;
        // midpoint of the 3D bezier
        if (!P(0.25 * ax + 0.5 * cx + 0.25 * bx, 0.25 * ay + 0.5 * cy + 0.25 * by, 0.25 * az + 0.5 * cz + 0.25 * bz, 0)) continue;
        const x0 = proj[pa]!, y0 = proj[pa + 1]!, x1 = proj[pb]!, y1 = proj[pb + 1]!;
        const qx = 2 * q[0]! - (x0 + x1) / 2, qy = 2 * q[1]! - (y0 + y1) / 2;
        const kind = EDGE_KINDS[model.edgeKind[e]!]!, rec = model.core.edges[e]!;
        const c = rec.liveLive ? th.liveLive : th.edges[kind];
        const a = Math.min(1, f.edges.alpha[k]! + f.edges.glow[k]! * 0.5) * fogFactor(q[2]!, camD);
        g.strokeStyle = css(c, a);
        const k0 = model.edgeKind[e]!;
        g.lineWidth = (k0 <= 2 ? 1 : k0 <= 4 ? 1.4 : 2) * (rec.liveLive ? 1.5 : 1) + f.edges.glow[k]!;
        g.setLineDash(rec.inferred ? [6, 4] : DASH[kind] ?? []);
        g.beginPath(); g.moveTo(x0, y0); g.quadraticCurveTo(qx, qy, x1, y1); g.stroke();
        dc++;
      }
      g.setLineDash([]);
      // trail dots
      const T = f.trail;
      for (let j = 0; j < T.count; j++) {
        if (!projectPoint(vp, T.pos, j * 3, W, H, q, 0)) continue;
        const rad = Math.max(1, Math.min(5, Math.abs(T.size[j]!) * pxw / q[2]!));
        const c: [number, number, number] = [T.rgba[j * 4]!, T.rgba[j * 4 + 1]!, T.rgba[j * 4 + 2]!];
        const a = T.rgba[j * 4 + 3]!;
        g.beginPath(); g.arc(q[0]!, q[1]!, rad, 0, 7);
        if (T.size[j]! < 0) { g.strokeStyle = css(c, a); g.lineWidth = 1; g.stroke(); } else { g.fillStyle = css(c, a); g.fill(); }
      }
      // nodes, far to near
      const sub = order.subarray(0, vis);
      sub.sort((p1, p2) => proj[p2 * 3 + 2]! - proj[p1 * 3 + 2]!);
      for (let k = 0; k < vis; k++) {
        const i = sub[k]!, x = proj[i * 3]!, y = proj[i * 3 + 1]!, depth = proj[i * 3 + 2]!;
        const rad = Math.max(2, Math.min(24, N.size[i]! * pxw / depth));
        const bits = N.bits[i]!, sh = N.shape[i]!;
        const c: [number, number, number] = [N.rgba[i * 4]!, N.rgba[i * 4 + 1]!, N.rgba[i * 4 + 2]!];
        const a = N.rgba[i * 4 + 3]! * fogFactor(depth, camD);
        g.beginPath();
        shape(g, sh, x, y, rad);
        const hollow = (bits & NB.HOLLOW) !== 0 || sh === SHAPE.PERSONA || sh === SHAPE.BOARD;
        if (hollow) { g.strokeStyle = css(c, a); g.lineWidth = Math.max(1.5, rad * 0.25); g.stroke(); }
        else { g.fillStyle = css(c, a); g.fill(); }
        if ((sh === SHAPE.AGENT || sh === SHAPE.TASK) && f.aux[i]! > 0) {
          g.beginPath();
          g.arc(x, y, rad * 1.25, -Math.PI / 2, -Math.PI / 2 + f.aux[i]! * Math.PI * 2);
          g.strokeStyle = css(sh === SHAPE.TASK ? th.cats.submit : (bits & NBX.FREE) ? th.liveLive : th.text, a * 0.8);
          g.lineWidth = 2; g.stroke();
        }
        if (bits & NB.LEAK) { g.beginPath(); g.arc(x, y, rad * 1.35, 0, 7); g.strokeStyle = css(th.leak, a); g.lineWidth = 2; g.stroke(); }
        if (bits & (NB.HOVER | NB.PIN | NB.MATCH)) {
          g.beginPath(); g.arc(x, y, rad * 1.6, 0, 7);
          g.strokeStyle = css(bits & NB.MATCH && !(bits & (NB.HOVER | NB.PIN)) ? th.text : th.accent, 0.9);
          g.lineWidth = 2; g.stroke();
        }
        dc++;
      }
      // hulls
      for (const h of f.hulls) {
        if (!P(h.cx, h.cy, h.cz, 0)) continue;
        g.beginPath(); g.arc(q[0]!, q[1]!, h.r * pxw / q[2]!, 0, 7);
        g.strokeStyle = css(th.liveLive, 0.45); g.lineWidth = 1.5; g.stroke();
      }
      // pulses
      for (let k = 0; k < f.pulses.count; k++) {
        const e = f.pulses.edge[k]!, s = model.edgeSrc[e]! * 3, d = model.edgeDst[e]! * 3, u = f.pulses.u[k]!;
        const ax = N.pos[s]!, ay = N.pos[s + 1]!, az = N.pos[s + 2]!, bx = N.pos[d]!, by = N.pos[d + 1]!, bz = N.pos[d + 2]!;
        const len = Math.hypot(bx - ax, by - ay, bz - az), v = 1 - u, bend = r.ctx.bend[e] ?? 0;
        let sx = -(bz - az), sz = bx - ax; const sl = Math.hypot(sx, sz) || 1; sx /= sl; sz /= sl;
        const cx = (ax + bx) / 2 + sx * bend * len, cy = (ay + by) / 2 + 0.12 * len, cz = (az + bz) / 2 + sz * bend * len;
        if (!P(v * v * ax + 2 * u * v * cx + u * u * bx, v * v * ay + 2 * u * v * cy + u * u * by, v * v * az + 2 * u * v * cz + u * u * bz, 0)) continue;
        const kind = EDGE_KINDS[model.edgeKind[e]!]!;
        g.fillStyle = css(model.core.edges[e]!.liveLive ? th.liveLive : th.edges[kind], f.pulses.strength[k]! * (1 - u * 0.6));
        g.beginPath(); g.arc(q[0]!, q[1]!, 2 + 2 * f.pulses.strength[k]!, 0, 7); g.fill();
      }
      r.drawCalls = dc;
    },
    dispose() { /* nothing to release */ },
  };
  return r;
}

function shape(g: CanvasRenderingContext2D, sh: number, x: number, y: number, r: number) {
  if (sh === SHAPE.TASK) {
    for (let k = 0; k < 6; k++) {
      const a = Math.PI / 6 + (k * Math.PI) / 3;
      if (k === 0) g.moveTo(x + r * Math.cos(a), y + r * Math.sin(a)); else g.lineTo(x + r * Math.cos(a), y + r * Math.sin(a));
    }
    g.closePath();
  } else if (sh === SHAPE.THREAD) {
    g.moveTo(x, y - r); g.lineTo(x + r, y); g.lineTo(x, y + r); g.lineTo(x - r, y); g.closePath();
  } else if (sh === SHAPE.POST) {
    g.rect(x - r * 0.8, y - r * 0.8, r * 1.6, r * 1.6);
  } else {
    g.arc(x, y, sh === SHAPE.HANDLE ? r * 0.7 : r, 0, Math.PI * 2);
  }
}
