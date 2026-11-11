// WebGL2 renderer (SPEC 7.2): backdrop, floor/now-plane, stars, worldline segments, instanced edges with
// a node-position texture, SDF node billboards, halos and pulses. Rebuilds itself after context loss.

import type { Renderer, RunModel, SegmentBuffer, StaticGeometry, ThemeColors } from '../../types.js';
import { EDGE_KINDS } from '../../types.js';
import { prng } from '../math.js';
import { SHAPE, type EngineFrame } from '../frame.js';
import { program, vao, type Prog } from './context.js';
import * as S from './shaders.js';

const NODE_STRIDE = 32, EDGE_STRIDE = 20, SEG_STRIDE = 48;

export interface EngineRenderer extends Renderer {
  drawCalls: number;
  onRestore: (() => void) | null;
  lost: boolean;
  /** Set by the engine before draw: camera distance (fog), board centre, layout centre (plane, stars), per-edge bend. */
  ctx: { camDistance: number; board: ArrayLike<number>; center: ArrayLike<number>; bend: Float32Array };
}

export function createWebGL2Renderer(canvas: HTMLCanvasElement): EngineRenderer | null {
  const gl0 = canvas.getContext('webgl2', { antialias: true, alpha: false, premultipliedAlpha: true,
                                            powerPreference: 'high-performance' });
  if (!gl0) return null;
  const gl: WebGL2RenderingContext = gl0;

  let P: Record<'back' | 'floor' | 'stars' | 'edge' | 'node' | 'pulse' | 'seg', Prog>;
  let empty: WebGLVertexArrayObject, posTex: WebGLTexture;
  let nodeBuf: WebGLBuffer, edgeBuf: WebGLBuffer, pulseBuf: WebGLBuffer, starBuf: WebGLBuffer;
  let nodeVao: WebGLVertexArrayObject, edgeVao: WebGLVertexArrayObject, pulseVao: WebGLVertexArrayObject,
    starVao: WebGLVertexArrayObject;
  let segs: { vao: WebGLVertexArrayObject; buf: WebGLBuffer; count: number }[] = [];
  let model: RunModel | null = null, theme: ThemeColors | null = null, geom: StaticGeometry | null = null;
  let nodeCap = 0, edgeCap = 0;
  let nf = new Float32Array(0), nu8 = new Uint8Array(0), nu16 = new Uint16Array(0);
  let ef = new Float32Array(0), eu8 = new Uint8Array(0), eu16 = new Uint16Array(0);
  const pulseData = new ArrayBuffer(20 * 4096);
  const pf = new Float32Array(pulseData), pu8 = new Uint8Array(pulseData), pu16 = new Uint16Array(pulseData);
  let texW = 1, texH = 1, texData = new Float32Array(4);
  let edgeSrc = new Uint16Array(0), edgeDst = new Uint16Array(0), edgeKF = new Uint8Array(0);
  const ek = new Float32Array(30);

  const r: EngineRenderer = {
    kind: 'webgl2', drawCalls: 0, onRestore: null, lost: false,
    ctx: { camDistance: 46, board: [14, 0, 0], center: [0, 0, 0], bend: new Float32Array(0) },
    setTopology(m, th) {
      model = m; theme = th;
      const ne = m.nEdges;
      edgeSrc = new Uint16Array(m.edgeSrc); edgeDst = new Uint16Array(m.edgeDst);
      edgeKF = new Uint8Array(ne * 2);
      for (let e = 0; e < ne; e++) {
        const rec = m.core.edges[e]!;
        edgeKF[e * 2] = m.edgeKind[e]!;
        edgeKF[e * 2 + 1] = (rec.liveLive ? 1 : 0) | (rec.inferred ? 2 : 0);
      }
      EDGE_KINDS.forEach((k, i) => ek.set(th.edges[k], i * 3));
      const n = Math.max(1, m.nNodes);
      texW = Math.min(1024, n); texH = Math.ceil(n / 1024);
      texData = new Float32Array(texW * texH * 4);
      if (!r.lost) { allocTex(); buildStars(); }
    },
    uploadStatic(g) { geom = g; if (!r.lost) uploadSegs(); },
    resize(w, h, dpr) {
      const W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(h * dpr));
      if (canvas.width !== W) canvas.width = W;
      if (canvas.height !== H) canvas.height = H;
    },
    draw(f0, cam, o) {
      if (r.lost || !model || !theme) return;
      const f = f0 as EngineFrame, th = theme;
      let dc = 0;
      const W = canvas.width, H = canvas.height;
      gl.viewport(0, 0, W, H);
      gl.clearColor(th.bg[0], th.bg[1], th.bg[2], 1);
      gl.clearDepth(1);
      gl.depthMask(true);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const fog = 1.2 / (r.ctx.camDistance * 2.5);
      const L = r.ctx.board, C = r.ctx.center;
      const common = (pr: Prog) => {
        gl.useProgram(pr.p);
        gl.uniformMatrix4fv(pr.u.uVP!, false, cam.viewProj);
        if (pr.u.uVPpx) gl.uniform2f(pr.u.uVPpx, W, H);
        if (pr.u.uDpr) gl.uniform1f(pr.u.uDpr, cam.dpr);
        if (pr.u.uFogDens) gl.uniform1f(pr.u.uFogDens, fog);
        if (pr.u.uProj11) gl.uniform1f(pr.u.uProj11, cam.proj[5]!);
        if (pr.u.uEye) gl.uniform3fv(pr.u.uEye, cam.eye);
      };
      // backdrop
      gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
      gl.useProgram(P.back.p);
      gl.uniform3fv(P.back.u.uBg!, th.bg); gl.uniform3fv(P.back.u.uTint!, th.accent);
      gl.bindVertexArray(empty);
      gl.drawArrays(gl.TRIANGLES, 0, 3); dc++;
      gl.enable(gl.BLEND); gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.depthMask(false);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      // floor (board disc) and now plane
      const mix = f.modeMix;
      common(P.floor);
      const floor = (cx: number, cy: number, cz: number, rad: number, a: number, step: number, c: readonly number[]) => {
        gl.uniform3f(P.floor.u.uCenter!, cx, cy, cz); gl.uniform1f(P.floor.u.uRadius!, rad);
        gl.uniform1f(P.floor.u.uAlpha!, a); gl.uniform1f(P.floor.u.uStep!, step);
        gl.uniform3f(P.floor.u.uColor!, c[0]!, c[1]!, c[2]!);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4); dc++;
      };
      if (mix < 1) floor(L[0]!, L[1]! - 0.02, L[2]!, 9, 1 - mix, 1.5, th.kinds.board);
      if (mix > 0) floor(C[0]!, f.plane.y, C[2]!, f.plane.radius, mix * 0.8, 3, th.text);
      // stars
      gl.blendFunc(gl.ONE, gl.ONE);
      common(P.stars);
      gl.uniform3fv(P.stars.u.uColor!, th.text);
      gl.bindVertexArray(starVao);
      gl.drawArrays(gl.POINTS, 0, 256); dc++;
      // position texture
      const np = f.nodes.count;
      for (let i = 0; i < np; i++) {
        texData[i * 4] = f.nodes.pos[i * 3]!; texData[i * 4 + 1] = f.nodes.pos[i * 3 + 1]!; texData[i * 4 + 2] = f.nodes.pos[i * 3 + 2]!;
      }
      gl.bindTexture(gl.TEXTURE_2D, posTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, texW, texH, gl.RGBA, gl.FLOAT, texData);
      // worldline segments
      if (mix > 0 && segs.length) {
        common(P.seg);
        gl.uniform1f(P.seg.u.uNowY!, f.plane.y); gl.uniform1f(P.seg.u.uAlpha!, mix);
        const counts = [f.staticDraw.worldlines, f.staticDraw.pillars, f.staticDraw.chords, f.staticDraw.beads];
        segs.forEach((s, i) => {
          const c = Math.min(counts[i]!, s.count);
          if (!c) return;
          gl.uniform1f(P.seg.u.uClamp!, i === 1 ? 1 : 0);
          gl.bindVertexArray(s.vao);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, c); dc++;
        });
      }
      // edges
      const ec = f.edges.count;
      if (ec) {
        if (ec > edgeCap) growEdges(ec);
        for (let i = 0; i < ec; i++) {
          const e = f.edges.edge[i]!, w = i * 5;
          eu16[w * 2] = edgeSrc[e]!; eu16[w * 2 + 1] = edgeDst[e]!;
          eu8[w * 4 + 4] = edgeKF[e * 2]!; eu8[w * 4 + 5] = edgeKF[e * 2 + 1]!;
          ef[w + 2] = f.edges.alpha[i]!; ef[w + 3] = f.edges.glow[i]!; ef[w + 4] = f.edges.bend[i]!;
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, eu8, 0, ec * EDGE_STRIDE);
        common(P.edge);
        gl.uniform3fv(P.edge.u.uEK!, ek); gl.uniform3fv(P.edge.u.uLive!, th.liveLive);
        gl.uniform1i(P.edge.u.uPos!, 0);
        gl.bindVertexArray(edgeVao);
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 16, ec); dc++;
      }
      // nodes + trail + hulls
      const tc = f.trail.count, hc = f.hulls.length, total = np + tc + hc;
      if (total > nodeCap) growNodes(total);
      const N = f.nodes;
      for (let i = 0; i < np; i++) {
        const w = i * 8;
        nf[w] = N.pos[i * 3]!; nf[w + 1] = N.pos[i * 3 + 1]!; nf[w + 2] = N.pos[i * 3 + 2]!; nf[w + 3] = N.size[i]!;
        col(nu8, w * 4 + 16, N.rgba, i * 4);
        nu8[w * 4 + 20] = N.shape[i]!; nu16[w * 2 + 11] = N.bits[i]!;
        nf[w + 6] = f.aux ? f.aux[i]! : 0; nf[w + 7] = N.heat[i]!;
      }
      const T = f.trail;
      for (let j = 0; j < tc; j++) {
        const w = (np + j) * 8;
        nf[w] = T.pos[j * 3]!; nf[w + 1] = T.pos[j * 3 + 1]!; nf[w + 2] = T.pos[j * 3 + 2]!; nf[w + 3] = T.size[j]!;
        col(nu8, w * 4 + 16, T.rgba, j * 4);
        nu8[w * 4 + 20] = SHAPE.TRAIL; nu16[w * 2 + 11] = 1; nf[w + 6] = 0; nf[w + 7] = 0;
      }
      for (let j = 0; j < hc; j++) {
        const h = f.hulls[j]!, w = (np + tc + j) * 8;
        nf[w] = h.cx; nf[w + 1] = h.cy; nf[w + 2] = h.cz; nf[w + 3] = h.r;
        const c = th.liveLive;
        nu8[w * 4 + 16] = c[0] * 255; nu8[w * 4 + 17] = c[1] * 255; nu8[w * 4 + 18] = c[2] * 255; nu8[w * 4 + 19] = 150;
        nu8[w * 4 + 20] = SHAPE.HULL; nu16[w * 2 + 11] = 1; nf[w + 6] = 0; nf[w + 7] = 0;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, nu8, 0, total * NODE_STRIDE);
      common(P.node);
      const nu = P.node.u;
      gl.uniform3fv(nu.uBg!, th.bg); gl.uniform3fv(nu.uLeak!, th.leak); gl.uniform3fv(nu.uAccent!, th.accent);
      gl.uniform3fv(nu.uLive!, th.liveLive); gl.uniform3fv(nu.uSolve!, th.cats.submit); gl.uniform3fv(nu.uText!, th.text);
      gl.uniform1f(nu.uHalo!, 0);
      gl.bindVertexArray(nodeVao);
      gl.depthMask(true);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, total); dc++;
      // halos
      gl.depthMask(false);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.uniform1f(nu.uHalo!, 1);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, np); dc++;
      // pulses
      const pc = f.pulses.count;
      if (pc) {
        for (let i = 0; i < pc; i++) {
          const e = f.pulses.edge[i]!, w = i * 5;
          pu16[w * 2] = edgeSrc[e]!; pu16[w * 2 + 1] = edgeDst[e]!;
          pu8[w * 4 + 4] = edgeKF[e * 2]!; pu8[w * 4 + 5] = edgeKF[e * 2 + 1]!;
          pf[w + 2] = r.ctx.bend[e] ?? 0; pf[w + 3] = f.pulses.u[i]!; pf[w + 4] = f.pulses.strength[i]!;
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, pulseBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, pu8, 0, pc * 20);
        common(P.pulse);
        gl.uniform3fv(P.pulse.u.uEK!, ek); gl.uniform3fv(P.pulse.u.uLive!, th.liveLive);
        gl.uniform1i(P.pulse.u.uPos!, 0);
        gl.bindVertexArray(pulseVao);
        gl.drawArrays(gl.POINTS, 0, pc); dc++;
      }
      gl.bindVertexArray(null);
      r.drawCalls = dc;
      void o;
    },
    dispose() {
      canvas.removeEventListener('webglcontextlost', onLost);
      canvas.removeEventListener('webglcontextrestored', onRestored);
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    },
  };

  function col(dst: Uint8Array, o: number, src: Float32Array, so: number) {
    dst[o] = src[so]! * 255; dst[o + 1] = src[so + 1]! * 255; dst[o + 2] = src[so + 2]! * 255; dst[o + 3] = src[so + 3]! * 255;
  }
  function growNodes(n: number) {
    nodeCap = Math.max(n, nodeCap * 2, 256);
    const b = new ArrayBuffer(nodeCap * NODE_STRIDE);
    nf = new Float32Array(b); nu8 = new Uint8Array(b); nu16 = new Uint16Array(b);
    gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuf);
    gl.bufferData(gl.ARRAY_BUFFER, b.byteLength, gl.DYNAMIC_DRAW);
  }
  function growEdges(n: number) {
    edgeCap = Math.max(n, edgeCap * 2, 256);
    const b = new ArrayBuffer(edgeCap * EDGE_STRIDE);
    ef = new Float32Array(b); eu8 = new Uint8Array(b); eu16 = new Uint16Array(b);
    gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuf);
    gl.bufferData(gl.ARRAY_BUFFER, b.byteLength, gl.DYNAMIC_DRAW);
  }
  function allocTex() {
    gl.bindTexture(gl.TEXTURE_2D, posTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, texW, texH, 0, gl.RGBA, gl.FLOAT, texData);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.activeTexture(gl.TEXTURE0);
  }
  function buildStars() {
    const rnd = prng(0x5eed);
    const d = new Float32Array(256 * 4);
    const sc = r.ctx.center;
    for (let i = 0; i < 256; i++) {
      const u = rnd() * 2 - 1, th = rnd() * Math.PI * 2, s = Math.sqrt(1 - u * u), R = 150 + rnd() * 60;
      d[i * 4] = (sc[0] ?? 0) + R * s * Math.cos(th); d[i * 4 + 1] = (sc[1] ?? 0) + R * u;
      d[i * 4 + 2] = (sc[2] ?? 0) + R * s * Math.sin(th); d[i * 4 + 3] = rnd() * rnd();
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, starBuf);
    gl.bufferData(gl.ARRAY_BUFFER, d, gl.STATIC_DRAW);
  }
  function uploadSegs() {
    for (const s of segs) { gl.deleteBuffer(s.buf); gl.deleteVertexArray(s.vao); }
    segs = [];
    if (!geom) return;
    const list: SegmentBuffer[] = [geom.worldlines, geom.pillars, geom.chords, geom.beads];
    for (const sb of list) {
      const d = new Float32Array(Math.max(1, sb.count) * 12);
      for (let i = 0; i < sb.count; i++) {
        const w = i * 12;
        d[w] = sb.a[i * 3]!; d[w + 1] = sb.a[i * 3 + 1]!; d[w + 2] = sb.a[i * 3 + 2]!;
        d[w + 3] = sb.b[i * 3]!; d[w + 4] = sb.b[i * 3 + 1]!; d[w + 5] = sb.b[i * 3 + 2]!;
        d[w + 6] = sb.rgba[i * 4]!; d[w + 7] = sb.rgba[i * 4 + 1]!; d[w + 8] = sb.rgba[i * 4 + 2]!; d[w + 9] = sb.rgba[i * 4 + 3]!;
        d[w + 10] = sb.width[i]!; d[w + 11] = sb.style[i]!;
      }
      const buf = gl.createBuffer()!;
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, d, gl.STATIC_DRAW);
      const v = vao(gl, P.seg, buf, SEG_STRIDE, [['aA', 3, gl.FLOAT, false, 0], ['aB', 3, gl.FLOAT, false, 12],
        ['aC', 4, gl.FLOAT, false, 24], ['aWS', 2, gl.FLOAT, false, 40]], 1);
      segs.push({ vao: v, buf, count: sb.count });
    }
  }
  function build() {
    P = {
      back: program(gl, S.BACKDROP_VS, S.BACKDROP_FS), floor: program(gl, S.FLOOR_VS, S.FLOOR_FS),
      stars: program(gl, S.STARS_VS, S.STARS_FS), edge: program(gl, S.EDGE_VS, S.EDGE_FS),
      node: program(gl, S.NODE_VS, S.NODE_FS), pulse: program(gl, S.PULSE_VS, S.PULSE_FS),
      seg: program(gl, S.SEG_VS, S.SEG_FS),
    };
    const g = gl;
    empty = g.createVertexArray()!;
    posTex = g.createTexture()!;
    nodeBuf = g.createBuffer()!; edgeBuf = g.createBuffer()!; pulseBuf = g.createBuffer()!; starBuf = g.createBuffer()!;
    nodeCap = 0; edgeCap = 0; segs = [];
    growNodes(256); growEdges(256);
    g.bindBuffer(g.ARRAY_BUFFER, pulseBuf);
    g.bufferData(g.ARRAY_BUFFER, pulseData.byteLength, g.DYNAMIC_DRAW);
    nodeVao = vao(g, P.node, nodeBuf, NODE_STRIDE, [['aP', 3, g.FLOAT, false, 0], ['aS', 1, g.FLOAT, false, 12],
      ['aC', 4, g.UNSIGNED_BYTE, true, 16], ['aSh', 1, g.UNSIGNED_BYTE, 'i', 20], ['aBi', 1, g.UNSIGNED_SHORT, 'i', 22],
      ['aXH', 2, g.FLOAT, false, 24]], 1);
    edgeVao = vao(g, P.edge, edgeBuf, EDGE_STRIDE, [['aSD', 2, g.UNSIGNED_SHORT, 'i', 0], ['aKF', 2, g.UNSIGNED_BYTE, 'i', 4],
      ['aAGB', 3, g.FLOAT, false, 8]], 1);
    pulseVao = vao(g, P.pulse, pulseBuf, 20, [['aSD', 2, g.UNSIGNED_SHORT, 'i', 0], ['aKF', 2, g.UNSIGNED_BYTE, 'i', 4],
      ['aBUS', 3, g.FLOAT, false, 8]], 0);
    starVao = vao(g, P.stars, starBuf, 16, [['aP', 3, g.FLOAT, false, 0], ['aB', 1, g.FLOAT, false, 12]], 0);
    allocTex();
    buildStars();
    if (geom) uploadSegs();
  }
  const onLost = (e: Event) => { e.preventDefault(); r.lost = true; };
  const onRestored = () => {
    try { build(); r.lost = false; r.onRestore?.(); } catch { /* stays lost */ }
  };
  canvas.addEventListener('webglcontextlost', onLost);
  canvas.addEventListener('webglcontextrestored', onRestored);
  try { build(); } catch (err) { console.warn('[engine] webgl2 init failed', err); return null; }
  return r;
}
