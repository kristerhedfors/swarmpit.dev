// createEngine: owns the only rAF loop (dirty-only), the renderer, camera, gestures, picking and tags.

import type { CreateEngine, EngineEvents, FilterMask, FrameOpts, GestureState, PickHit, ProjectedSet, RunModel,
  SceneEngine, SwarmState, ThemeColors, ViewMode } from '../types.js';
import { NB, SOCIAL_EDGE_KINDS, EDGE_KINDS } from '../types.js';
import { Emitter } from '../util/bus.js';
import { createCamera } from './camera.js';
import { attachGestures } from './gestures.js';
import { computeLayout, type EngineLayout } from './layout.js';
import { buildStaticGeometry, computeFrame, createFrameState, type EngineFrame } from './frame.js';
import { readTheme } from './palette.js';
import { pick } from './picking.js';
import { createLabels } from './labels.js';
import { createPerf, type PerfOverlay } from './perf.js';
import { bezierCtl, clamp, lerp, projectPoint } from './math.js';
import { createWebGL2Renderer, type EngineRenderer } from './gl/webgl2.js';
import { createCanvas2DRenderer } from './canvas2d.js';

const MODE_MS = 600;
const FOCUS_DIST = [24, 24, 14, 12, 8, 10, 8];

function emptyProjected(n: number, tc: number, ne: number): ProjectedSet {
  return { nodeXY: new Float32Array(n * 2), nodeDepth: new Float32Array(n), nodeR: new Float32Array(n),
    nodeVisible: new Uint8Array(n), nodeCount: n, trailXY: new Float32Array(tc * 2), trailR: new Float32Array(tc),
    trailCount: 0, edgeXY: new Float32Array(ne * 4), edgeCount: 0, edgeIdx: new Int32Array(ne),
    edgeCtl: new Float32Array(ne * 2) };
}
function defaultMask(m: RunModel): FilterMask {
  return { version: -1, node: new Uint8Array(m.nNodes).fill(2), edge: new Uint8Array(m.nEdges).fill(2),
    event: new Uint8Array(m.nEvents).fill(2), hits: new Uint32Array(0), activeCount: 0 };
}

export const createEngine: CreateEngine = (stage, clock, o) => {
  const em = new Emitter<EngineEvents>();
  if (getComputedStyle(stage).position === 'static') stage.style.position = 'relative';
  let canvas = document.createElement('canvas');
  canvas.className = 'stage-canvas';
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', '3D view of the agent swarm');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;outline-offset:-2px';
  stage.appendChild(canvas);
  const overlay = document.createElement('div');
  overlay.className = 'stage-overlay';
  overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none';
  stage.appendChild(overlay);
  const hintEl = document.createElement('div');
  hintEl.className = 'stage-hint';
  hintEl.textContent = 'ctrl + scroll / two fingers to zoom · click to explore';
  hintEl.hidden = true;
  overlay.appendChild(hintEl);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'stage-chip';
  chip.style.pointerEvents = 'auto';
  chip.dataset['tagAvoid'] = '';
  overlay.appendChild(chip);

  const mq = (q: string) => typeof matchMedia === 'function' && matchMedia(q).matches;
  const coarse = mq('(pointer: coarse)');
  let reduced = mq('(prefers-reduced-motion: reduce)');

  let renderer: EngineRenderer | null = null;
  let reason = '';
  if (o.prefer !== 'canvas2d') {
    try { renderer = createWebGL2Renderer(canvas); } catch (e) { reason = String(e); }
  }
  if (!renderer) {
    // a canvas that already holds a (broken) webgl context cannot give 2d: swap in a fresh one
    const c2 = canvas.cloneNode() as HTMLCanvasElement;
    canvas.replaceWith(c2); canvas = c2;
    try { renderer = createCanvas2DRenderer(canvas); } catch (e) { reason = String(e); }
  }
  const backend: SceneEngine['backend'] = renderer ? renderer.kind : 'none';
  queueMicrotask(() => em.emit('backend', reason && backend !== 'webgl2' ? { kind: backend, reason } : { kind: backend }));

  const camera = createCamera();
  camera.setReducedMotion(reduced);
  const labels = createLabels(overlay, coarse ? 18 : 64);
  const perf: PerfOverlay | null = o.perf ? createPerf(stage) : null;

  let model: RunModel | null = null, layout: EngineLayout | null = null, frame: EngineFrame | null = null;
  let theme: ThemeColors | null = null, mask: FilterMask | null = null, proj: ProjectedSet | null = null;
  let iface = '';
  const opts: FrameOpts = { recentMs: null, rate: 1, showTrails: true, showIdle: false, hover: -1, selected: -1,
    reducedMotion: reduced, mode: 'constellation', modeMix: 0 };
  let labelsOn = true, follow = false, engaged = false, interacted = false;
  let mode: ViewMode = 'constellation', modeFrom = 0, modeTo = 0, modeT = -1;
  let highlightSet: number[] = [];
  let dirtyFrame = true, dirtyDraw = true, raf = 0, last = 0, lastLabels = 0, inLoop = false;
  let userStopped = false, ioVisible = true;
  let cssW = 1, cssH = 1, dpr = 1, heavy = false;
  let hx = -1, hy = -1, hoverInside = false, hoverMoved = false, gestureActive = false;
  let lastHoverKey = '';

  const running = () => !userStopped && ioVisible && !document.hidden;
  // The loop is the single scheduler: while it runs, wake() only relies on the dirty flags (the loop
  // reschedules itself at the end), so listeners fired from clock.advance() cannot spawn extra loops.
  const wake = () => { if (!raf && !inLoop && running()) { last = performance.now(); raf = requestAnimationFrame(loop); } };
  const markDirty = () => { dirtyFrame = true; dirtyDraw = true; wake(); };

  const updateChip = () => {
    chip.hidden = !(engaged || coarse);
    chip.textContent = engaged ? '✕' : 'Tap to explore in 3D';
    chip.setAttribute('aria-label', engaged ? 'Stop exploring (release scroll)' : 'Explore the 3D view');
    stage.classList.toggle('is-engaged', engaged);
  };
  chip.addEventListener('click', (e) => { e.stopPropagation(); eng.setEngaged(!engaged); });

  const hitKey = (h: PickHit | null) => (h ? h.kind + (h.kind === 'node' ? h.node : h.kind === 'step' ? h.event : h.edge) : '');
  const noteInteract = () => {
    interacted = true;
    em.emit('cameraInteract', {});
  };

  const gestures = attachGestures(canvas, {
    orbit(dx, dy) { camera.halt(); camera.orbit(dx, dy); dirtyDraw = true; wake(); },
    pan(dx, dy) { camera.halt(); camera.pan(dx, dy); dirtyDraw = true; wake(); },
    zoom(f, x, y) { camera.halt(); camera.zoom(f, x, y); noteInteract(); dirtyDraw = true; wake(); },
    tap(x, y) {
      if (!proj || !frame) return;
      const hit = pick(proj, frame, x, y);
      opts.selected = hit && hit.kind === 'node' ? hit.node : -1;
      em.emit('select', { hit, x, y });
      markDirty();
    },
    doubleTap(x, y, type) {
      if (!proj || !frame) return;
      const hit = pick(proj, frame, x, y);
      if (hit && hit.kind === 'node') { noteInteract(); eng.focusNode(hit.node); em.emit('focus', { node: hit.node }); return; }
      if (type === 'touch') {
        const third = x / Math.max(1, cssW);
        if (third < 1 / 3) { em.emit('seekRequest', { dt: -10000 }); ripple(x, y, '« 10 s'); }
        else if (third > 2 / 3) { em.emit('seekRequest', { dt: 10000 }); ripple(x, y, '10 s »'); }
        else eng.resetView();
      }
    },
    longPress(x, y) {
      if (!proj || !frame) return;
      em.emit('hover', { hit: pick(proj, frame, x, y), x, y });
    },
    hover(x, y) { hx = x; hy = y; hoverInside = true; hoverMoved = true; dirtyDraw = true; wake(); },
    leave() {
      hoverInside = false;
      if (lastHoverKey) { lastHoverKey = ''; opts.hover = -1; em.emit('hover', { hit: null, x: -1, y: -1 }); markDirty(); }
    },
    fling(vx, vy, kind) { if (kind === 'orbit') camera.fling(vx, vy, 0, 0); else camera.fling(0, 0, vx, vy); wake(); },
    state(s: GestureState) {
      const active = s === 'orbit' || s === 'pan' || s === 'pinch';
      if (active && !gestureActive) noteInteract();
      gestureActive = active;
    },
    engage() { eng.setEngaged(true); },
    hint(show) { hintEl.hidden = !show; },
  }, { alwaysEngaged: () => {
    const fe = document.fullscreenElement;
    return (!!fe && fe.contains(stage)) || stage.closest('.theater') !== null;
  } });

  function ripple(x: number, y: number, text: string) {
    const r = document.createElement('div');
    r.className = 'stage-ripple';
    r.textContent = text;
    r.style.cssText = `position:absolute;left:${x}px;top:${y}px;transform:translate(-50%,-50%);pointer-events:none`;
    overlay.appendChild(r);
    setTimeout(() => r.remove(), 650);
  }

  canvas.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    let used = true;
    const arrows = engaged;
    if (k === 'w' || k === 'W' || (arrows && k === 'ArrowUp')) camera.orbit(0, -30);
    else if (k === 's' || k === 'S' || (arrows && k === 'ArrowDown')) camera.orbit(0, 30);
    else if (k === 'a' || k === 'A' || (arrows && k === 'ArrowLeft')) camera.orbit(-30, 0);
    else if (k === 'd' || k === 'D' || (arrows && k === 'ArrowRight')) camera.orbit(30, 0);
    else if (k === 'q' || k === 'Q') camera.zoom(1 / 1.15, cssW / 2, cssH / 2);
    else if (k === 'e' || k === 'E') camera.zoom(1.15, cssW / 2, cssH / 2);
    else if (k === 'r' || k === 'R') eng.resetView();
    else if (k === 'Enter') {
      if (!engaged) eng.setEngaged(true);
      const n = highlightSet.length === 1 ? highlightSet[0]! : opts.hover;
      if (n >= 0) { opts.selected = n; em.emit('select', { hit: { kind: 'node', node: n }, x: -1, y: -1 }); }
    } else if (k === 'Escape') { if (engaged) eng.setEngaged(false); used = false; }
    else used = false;
    if (used) { e.preventDefault(); e.stopPropagation(); noteInteract(); markDirty(); }
  });

  const onDocDown = (e: PointerEvent) => {
    if (!engaged) return;
    const boundary = stage.closest('.player') ?? stage;
    if (!boundary.contains(e.target as Node)) eng.setEngaged(false);
  };
  document.addEventListener('pointerdown', onDocDown, true);

  const resize = () => {
    const r = stage.getBoundingClientRect();
    cssW = Math.max(1, r.width); cssH = Math.max(1, r.height);
    dpr = Math.min(typeof devicePixelRatio === 'number' ? devicePixelRatio : 1, heavy ? 1.5 : 2);
    renderer?.resize(cssW, cssH, dpr);
    dirtyDraw = true; wake();
  };
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  ro?.observe(stage);
  let dprMq: MediaQueryList | null = null;
  const onDpr = () => { dprMq?.removeEventListener('change', onDpr); watchDpr(); resize(); };
  const watchDpr = () => {
    if (typeof matchMedia !== 'function') return;
    dprMq = matchMedia(`(resolution: ${devicePixelRatio}dppx)`);
    dprMq.addEventListener('change', onDpr);
  };
  watchDpr();
  const io = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver((es) => { for (const e of es) ioVisible = e.intersectionRatio >= 0.1; if (running()) markDirty(); },
      { threshold: [0, 0.1, 0.5] })
    : null;
  io?.observe(stage);
  const onVis = () => { if (running()) markDirty(); };
  document.addEventListener('visibilitychange', onVis);
  const rmq = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
  const onRm = () => eng.setOptions({ reducedMotion: !!rmq?.matches });
  rmq?.addEventListener('change', onRm);

  const offClock = [clock.on('state', markDirty), clock.on('seek', markDirty)];

  if (renderer) renderer.onRestore = () => {
    if (model && theme) { renderer!.setTopology(model, theme); renderer!.uploadStatic(frame?.geom ?? null); }
    markDirty();
  };

  function ensureGeom() {
    if (!model || !layout || !frame || !mask || frame.geom) return;
    frame.geom = buildStaticGeometry(model, layout, mask, theme ?? undefined);
    renderer?.uploadStatic(frame.geom);
  }

  function project(): void {
    if (!proj || !frame || !model) return;
    const cam = camera.matrices(cssW, cssH, dpr);
    const vp = cam.viewProj, N = frame.nodes, pxw = cam.proj[5]! * cssH * 0.5;
    const tmp = projTmp;
    for (let i = 0; i < N.count; i++) {
      const vis = (N.bits[i]! & NB.VISIBLE) !== 0 && projectPoint(vp, N.pos, i * 3, cssW, cssH, tmp, 0);
      proj.nodeVisible[i] = vis ? 1 : 0;
      if (!vis) continue;
      proj.nodeXY[i * 2] = tmp[0]!; proj.nodeXY[i * 2 + 1] = tmp[1]!; proj.nodeDepth[i] = tmp[2]!;
      proj.nodeR[i] = clamp(N.size[i]! * pxw / tmp[2]!, 2, 24);
    }
    const T = frame.trail;
    let tc = 0;
    for (let j = 0; j < T.count; j++) {
      // trail index stays aligned with frame.trail.event; points behind the camera land far off-screen
      projectPoint(vp, T.pos, j * 3, cssW, cssH, tmp, 0);
      proj.trailXY[j * 2] = tmp[0]!; proj.trailXY[j * 2 + 1] = tmp[1]!;
      proj.trailR[j] = tmp[2]! > 0 ? clamp(Math.abs(T.size[j]!) * pxw / tmp[2]!, 1, 5) : 0;
      tc = j + 1;
    }
    proj.trailCount = tc;
    let ec = 0;
    const ctl = proj.edgeCtl!, idx = proj.edgeIdx!;
    for (let k = 0; k < frame.edges.count; k++) {
      const e = frame.edges.edge[k]!;
      if (!socialKind[model.edgeKind[e]!]) continue;
      const s = model.edgeSrc[e]!, d = model.edgeDst[e]!;
      if (!proj.nodeVisible[s] || !proj.nodeVisible[d]) continue;
      bezierCtl(N.pos, s, d, frame.edges.bend[k]!, ctl3, 0);
      // projected quadratic through the projected 3D midpoint
      const mx = 0.25 * N.pos[s * 3]! + 0.5 * ctl3[0]! + 0.25 * N.pos[d * 3]!;
      const my = 0.25 * N.pos[s * 3 + 1]! + 0.5 * ctl3[1]! + 0.25 * N.pos[d * 3 + 1]!;
      const mz = 0.25 * N.pos[s * 3 + 2]! + 0.5 * ctl3[2]! + 0.25 * N.pos[d * 3 + 2]!;
      ctl3[0] = mx; ctl3[1] = my; ctl3[2] = mz;
      if (!projectPoint(vp, ctl3, 0, cssW, cssH, tmp, 0)) continue;
      const ax = proj.nodeXY[s * 2]!, ay = proj.nodeXY[s * 2 + 1]!, bx = proj.nodeXY[d * 2]!, by = proj.nodeXY[d * 2 + 1]!;
      proj.edgeXY[ec * 4] = ax; proj.edgeXY[ec * 4 + 1] = ay; proj.edgeXY[ec * 4 + 2] = bx; proj.edgeXY[ec * 4 + 3] = by;
      ctl[ec * 2] = 2 * tmp[0]! - (ax + bx) / 2; ctl[ec * 2 + 1] = 2 * tmp[1]! - (ay + by) / 2;
      idx[ec] = e; ec++;
    }
    proj.edgeCount = ec;
    // hull tag anchors: top of the ring
    for (let h = 0; h < frame.hulls.length && h < 64; h++) {
      const hl = frame.hulls[h]!;
      ctl3[0] = hl.cx; ctl3[1] = hl.cy + hl.r; ctl3[2] = hl.cz;
      const ok = projectPoint(vp, ctl3, 0, cssW, cssH, tmp, 0);
      frame.hullXY[h * 3] = tmp[0]!; frame.hullXY[h * 3 + 1] = tmp[1]! - 20; frame.hullXY[h * 3 + 2] = ok ? tmp[2]! : -1;
    }
  }
  const projTmp = new Float32Array(4), ctl3 = new Float32Array(3);
  // Overlays tags must keep clear of (HUD, captions, chip, floating panels): any [data-tag-avoid] in the player. Re-measured at
  // most every 200 ms (one layout read), which is plenty for elements that change on story beats.
  const avoid = new Float32Array(32);
  let avoidCount = 0, avoidAt = -Infinity;
  function measureAvoid(now: number): void {
    if (now - avoidAt < 200) return;
    avoidAt = now;
    const host = stage.closest('.player') ?? stage.parentElement ?? stage, o = overlay.getBoundingClientRect();
    avoidCount = 0;
    for (const el of host.querySelectorAll<HTMLElement>('[data-tag-avoid]')) {
      if (avoidCount >= 8) break;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      avoid[avoidCount * 4] = r.left - o.left - 4; avoid[avoidCount * 4 + 1] = r.top - o.top - 4;
      avoid[avoidCount * 4 + 2] = r.right - o.left + 4; avoid[avoidCount * 4 + 3] = r.bottom - o.top + 4;
      avoidCount++;
    }
  }
  const socialKind = EDGE_KINDS.map((k) => SOCIAL_EDGE_KINDS.includes(k));

  function loop(now: number) {
    raf = 0;
    if (!running()) return;
    let again = false;
    inLoop = true;
    try { again = tick(now); } finally { inLoop = false; }
    if (!raf && running() && (again || clock.playing || camera.moving || modeT >= 0 || dirtyFrame || dirtyDraw)) raf = requestAnimationFrame(loop);
  }
  function tick(now: number): boolean {
    const dt = Math.max(0, Math.min(now - last, 100));
    last = now;
    const t0 = performance.now();
    let tChanged = false;
    if (model) tChanged = clock.advance(dt);
    let camMoving = camera.update(dt);
    if (model && clock.playing && !engaged && !interacted && !reduced) {
      camera.yaw += 0.02 * dt / 1000; camMoving = true;
    }
    let modeAnimating = false;
    if (modeT >= 0) {
      modeT += dt;
      const u = Math.min(1, modeT / MODE_MS);
      opts.modeMix = lerp(modeFrom, modeTo, u * u * (3 - 2 * u));
      if (u >= 1) { modeT = -1; opts.modeMix = modeTo; }
      modeAnimating = true;
    }
    if (model && layout && frame && mask && (tChanged || dirtyFrame || modeAnimating)) {
      opts.mode = mode;
      computeFrame(model, layout, clock.t, mask, opts, frame);
      for (const n of highlightSet) if (n >= 0 && n < frame.nodes.count) frame.nodes.bits[n]! |= NB.HOVER;
      dirtyFrame = false; dirtyDraw = true;
    }
    if (follow && mode === 'worldline' && frame) {
      const ty = frame.plane.y;
      if (Math.abs(camera.target[1]! - ty) > 1e-3) { camera.target[1] = lerp(camera.target[1]!, ty, reduced ? 1 : 0.2); camMoving = true; }
    }
    if (camMoving) dirtyDraw = true;
    if (dirtyDraw && renderer && model && frame && layout) {
      const cam = camera.matrices(cssW, cssH, dpr);
      renderer.ctx.camDistance = camera.distance;
      renderer.draw(frame, cam, { reducedMotion: reduced, timeMs: now });
      project();
      if (!camMoving || now - lastLabels > 33) {
        measureAvoid(now);
        labels.update(proj!, frame, model, { selected: opts.selected, hover: opts.hover, focusDepth: camera.distance,
          enabled: labelsOn, coarse, avoid, avoidCount });
        lastLabels = now;
      }
      dirtyDraw = false;
      if (hoverInside && !gestureActive && proj) {
        const hit = pick(proj, frame, hx, hy), key = hitKey(hit);
        if (key !== lastHoverKey || hoverMoved) {
          const changed = key !== lastHoverKey;
          lastHoverKey = key;
          em.emit('hover', { hit, x: hx, y: hy });
          const hn = hit && hit.kind === 'node' ? hit.node : -1;
          if (changed && hn !== opts.hover) { opts.hover = hn; dirtyFrame = true; }
          canvas.style.cursor = hit ? 'pointer' : '';
        }
        hoverMoved = false;
      }
      const ms = performance.now() - t0;
      em.emit('frame', { t: clock.t, ms });
      perf?.record(ms, { draws: renderer.drawCalls, nodes: frame.nodes.count, edges: frame.edges.count,
        trail: frame.trail.count, pulses: frame.pulses.count, backend: renderer.kind, dpr });
    }
    return camMoving || modeAnimating;
  }

  const eng: SceneEngine & { on: Emitter<EngineEvents>['on']; emit: Emitter<EngineEvents>['emit'] } = {
    on: em.on.bind(em), emit: em.emit.bind(em),
    backend, camera, canvas,
    get mode() { return mode; },
    setRun(m) {
      const sameIface = iface === m.core.run.iface && model !== null;
      model = m; iface = m.core.run.iface;
      layout = computeLayout(m);
      theme = readTheme(stage, m.core.run.iface);
      (layout as EngineLayout & { colors?: ThemeColors }).colors = theme;
      frame = createFrameState(m);
      if (!mask || mask.node.length !== m.nNodes || mask.edge.length !== m.nEdges || mask.event.length !== m.nEvents) mask = defaultMask(m);
      proj = emptyProjected(m.nNodes, Math.max(1, m.nAgents) * 64, m.nEdges);
      opts.hover = -1; opts.selected = -1; highlightSet = []; lastHoverKey = '';
      heavy = m.nNodes + Math.min(m.nEvents, m.nAgents * 64) > 300;
      camera.home = { target: layout.bounds.center, radius: layout.bounds.radius };
      if (renderer) {
        renderer.ctx.board = layout.board; renderer.ctx.center = layout.bounds.center; renderer.ctx.bend = layout.edgeBend;
        renderer.setTopology(m, theme);
        renderer.uploadStatic(null);
      }
      if (mode === 'worldline') ensureGeom();
      resize();
      camera.matrices(cssW, cssH, dpr);
      if (!sameIface) camera.reset(false);
      canvas.setAttribute('aria-label', `3D view of ${m.core.run.id}: ${m.nAgents} agents`);
      labels.clear();
      markDirty();
    },
    setMask(mk) {
      mask = mk;
      if (model && (mk.node.length !== model.nNodes || mk.event.length !== model.nEvents)) mask = defaultMask(model);
      if (frame) { frame.geom = null; renderer?.uploadStatic(null); }
      if (mode === 'worldline' || opts.modeMix > 0) ensureGeom();
      markDirty();
    },
    setOptions(p) {
      if (p.recentMs !== undefined) opts.recentMs = p.recentMs;
      if (p.showTrails !== undefined) opts.showTrails = p.showTrails;
      if (p.showIdle !== undefined) opts.showIdle = p.showIdle;
      if (p.reducedMotion !== undefined) { reduced = p.reducedMotion; opts.reducedMotion = reduced; camera.setReducedMotion(reduced); }
      if (p.labels !== undefined) labelsOn = p.labels;
      opts.rate = clock.rate;
      markDirty();
    },
    setMode(m, animate) {
      if (m === mode && modeT < 0) return;
      mode = m;
      if (m === 'worldline') ensureGeom();
      modeFrom = opts.modeMix; modeTo = m === 'worldline' ? 1 : 0;
      if (!animate || reduced) { opts.modeMix = modeTo; modeT = -1; } else modeT = 0;
      if (m === 'constellation') follow = false;
      if (layout) {
        const c = layout.bounds.center, H = layout.worldline.height, wl = m === 'worldline';
        const tgt = new Float32Array([c[0]!, wl ? H * 0.42 : c[1]!, c[2]!]);
        const r = wl ? Math.max(layout.bounds.radius, H * 0.75) : layout.bounds.radius;
        camera.animateTo({ target: tgt, distance: camera.fitDistance(r) * (wl ? 1.1 : 1) }, animate ? MODE_MS : 0);
      }
      markDirty();
    },
    setFollow(on) { follow = on; markDirty(); },
    highlight(nodes) { highlightSet = nodes ? nodes.slice() : []; markDirty(); },
    focusNode(node, fo) {
      if (!frame || !model || node < 0 || node >= frame.nodes.count) return;
      const p = frame.nodes.pos, nudge = clamp(fo?.nudge ?? 1, 0, 1);
      const tgt = new Float32Array([p[node * 3]!, p[node * 3 + 1]!, p[node * 3 + 2]!]);
      const cur = camera.target;
      for (let i = 0; i < 3; i++) tgt[i] = lerp(cur[i]!, tgt[i]!, nudge);
      const d = FOCUS_DIST[model.nodeKind[node]!] ?? 12;
      camera.animateTo({ target: tgt, distance: lerp(camera.distance, d, nudge) }, fo?.ms ?? 450);
      wake();
    },
    resetView() { camera.reset(true); noteInteract(); wake(); },
    setEngaged(on) {
      if (engaged === on) return;
      engaged = on; gestures.setEngaged(on); updateChip();
      hintEl.hidden = true;
      em.emit('engaged', { on });
    },
    swarm(): SwarmState { return frame?.swarm ?? { onBoard: [], clusters: [], liveLive: 0, onsetT: null }; },
    select(hit) { opts.selected = hit && hit.kind === 'node' ? hit.node : -1; markDirty(); },
    setInsets(i, animate) { camera.setInsets(i, animate); dirtyDraw = true; wake(); },
    requestRender() { opts.rate = clock.rate; markDirty(); },
    start() { userStopped = false; markDirty(); },
    stop() { userStopped = true; if (raf) { cancelAnimationFrame(raf); raf = 0; } },
    dispose() {
      eng.stop();
      offClock.forEach((f) => f());
      gestures.dispose(); labels.dispose(); perf?.dispose(); renderer?.dispose();
      ro?.disconnect(); io?.disconnect();
      document.removeEventListener('pointerdown', onDocDown, true);
      document.removeEventListener('visibilitychange', onVis);
      rmq?.removeEventListener('change', onRm);
      dprMq?.removeEventListener('change', onDpr);
      canvas.remove(); overlay.remove();
    },
  };
  // keep pulse window in sync with playback rate changes
  clock.on('state', (s) => { opts.rate = s.rate; });
  updateChip();
  resize();
  return eng;
};
