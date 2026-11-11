// Unified pointer/touch/wheel/Safari-gesture handling (SPEC 6.2) with cooperative engagement (SPEC 6.3).

import type { AttachGestures, GestureController, GestureState } from '../types.js';

interface Ptr { x: number; y: number; type: string }
interface Sample { t: number; dx: number; dy: number }

export const attachGestures: AttachGestures = (el, h, o) => {
  const ptrs = new Map<number, Ptr>();
  let state: GestureState = 'idle';
  let engaged = false;
  let p0x = 0, p0y = 0, pt0 = 0, pressType = 'mouse';
  let lastTap = { t: -1e9, x: 0, y: 0 };
  let longTimer = 0, hintTimer = 0;
  let pinchD = 0, pinchMx = 0, pinchMy = 0;
  let accDx = 0, accDy = 0, accKind: 'orbit' | 'pan' = 'orbit';
  let accZoom = 1, accZx = 0, accZy = 0, accPx = 0, accPy = 0;
  let hoverX = -1, hoverY = -1, hoverPending = false;
  let raf = 0;
  const samples: Sample[] = [];
  let gestureScale = 1;

  const set = (s: GestureState) => { if (s !== state) { state = s; h.state(s); } };
  const local = (e: { clientX: number; clientY: number }) => {
    const r = el.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top] as const;
  };
  const isEngaged = () => engaged || o.alwaysEngaged();
  const applyTouchAction = () => { el.style.touchAction = isEngaged() ? 'none' : 'pan-y'; };

  const flush = () => {
    raf = 0;
    if (accDx || accDy) {
      if (accKind === 'orbit') h.orbit(accDx, accDy); else h.pan(accDx, accDy);
      accDx = 0; accDy = 0;
    }
    if (accZoom !== 1) { h.zoom(accZoom, accZx, accZy); accZoom = 1; }
    if (accPx || accPy) { h.pan(accPx, accPy); accPx = 0; accPy = 0; }
    if (hoverPending) { hoverPending = false; h.hover(hoverX, hoverY); }
  };
  const schedule = () => { if (!raf) raf = requestAnimationFrame(flush); };
  const clearLong = () => { if (longTimer) { clearTimeout(longTimer); longTimer = 0; } };

  const pinchMetrics = () => {
    const it = ptrs.values();
    const a = it.next().value as Ptr, b = it.next().value as Ptr;
    return [Math.hypot(a.x - b.x, a.y - b.y), (a.x + b.x) / 2, (a.y + b.y) / 2] as const;
  };

  const onDown = (e: PointerEvent) => {
    const [x, y] = local(e);
    ptrs.set(e.pointerId, { x, y, type: e.pointerType });
    try { el.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    samples.length = 0;
    if (ptrs.size === 2) {
      clearLong();
      [pinchD, pinchMx, pinchMy] = pinchMetrics();
      set('pinch');
      return;
    }
    if (ptrs.size > 2) return;
    const touch = e.pointerType === 'touch';
    if (e.button === 1 || e.button === 2 || (e.button === 0 && e.shiftKey)) {
      set('pan');
      e.preventDefault();
      return;
    }
    if (touch && !isEngaged()) { set('press'); p0x = x; p0y = y; pt0 = performance.now(); pressType = 'touch'; return; }
    set('press');
    p0x = x; p0y = y; pt0 = performance.now(); pressType = e.pointerType;
    if (touch) longTimer = window.setTimeout(() => { if (state === 'press') h.longPress(p0x, p0y); }, 500);
  };

  const onMove = (e: PointerEvent) => {
    const [x, y] = local(e);
    const p = ptrs.get(e.pointerId);
    if (!p) {
      if (e.pointerType !== 'touch') { hoverX = x; hoverY = y; hoverPending = true; schedule(); }
      return;
    }
    const dx = x - p.x, dy = y - p.y;
    p.x = x; p.y = y;
    const now = performance.now();
    if (state === 'pinch' && ptrs.size >= 2) {
      const [d, mx, my] = pinchMetrics();
      if (pinchD > 0 && d > 0) { accZoom *= d / pinchD; accZx = mx; accZy = my; }
      accPx += mx - pinchMx; accPy += my - pinchMy;
      pinchD = d; pinchMx = mx; pinchMy = my; // re-baseline every move
      schedule();
      e.preventDefault();
      return;
    }
    if (state === 'press') {
      const thr = p.type === 'touch' ? 8 : 5;
      if (Math.hypot(x - p0x, y - p0y) > thr) {
        clearLong();
        if (p.type === 'touch' && !isEngaged()) { set('idle'); return; } // let the page scroll
        set('orbit');
      } else return;
    }
    if (state === 'orbit' || state === 'pan') {
      accKind = state; accDx += dx; accDy += dy;
      samples.push({ t: now, dx, dy });
      while (samples.length && now - samples[0]!.t > 80) samples.shift();
      schedule();
    }
  };

  const onUp = (e: PointerEvent) => {
    const had = ptrs.delete(e.pointerId);
    clearLong();
    if (!had) return;
    const [x, y] = local(e);
    if (state === 'pinch') {
      if (ptrs.size === 1) { set('orbit'); samples.length = 0; } // remaining pointer re-anchors (we use deltas)
      else if (ptrs.size === 0) set('idle');
      return;
    }
    if (state === 'press') {
      const now = performance.now();
      // Touch: >300 ms is a long press (its own gesture). Mouse/pen: any release that never crossed the
      // move threshold is a click, however slow.
      if (pressType !== 'touch' || now - pt0 < 300) {
        if (!isEngaged()) { engaged = true; applyTouchAction(); h.engage(); }
        h.tap(x, y, pressType);
        if (now - lastTap.t < 300 && Math.hypot(x - lastTap.x, y - lastTap.y) < 10) {
          h.doubleTap(x, y, pressType);
          lastTap.t = -1e9;
        } else lastTap = { t: now, x, y };
      }
      set('idle');
      return;
    }
    if ((state === 'orbit' || state === 'pan') && ptrs.size === 0) {
      const now = performance.now();
      while (samples.length && now - samples[0]!.t > 80) samples.shift();
      if (samples.length >= 2) {
        const span = Math.max(16, now - samples[0]!.t);
        let sx = 0, sy = 0;
        for (const s of samples) { sx += s.dx; sy += s.dy; }
        const vx = sx / span, vy = sy / span;
        if (Math.hypot(vx, vy) > 0.05) { h.fling(vx, vy, state); set('inertia'); set('idle'); return; }
      }
      set('idle');
    }
  };
  const onCancel = (e: PointerEvent) => { ptrs.delete(e.pointerId); clearLong(); if (!ptrs.size) set('idle'); };

  const onWheel = (e: WheelEvent) => {
    let dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 16; else if (e.deltaMode === 2) dy *= el.clientHeight || 800;
    const [x, y] = local(e);
    if (e.ctrlKey) {
      e.preventDefault();
      // Trackpad pinch arrives as many small pixel deltas; ctrl + mouse wheel as ~100 px notches (or line
      // mode). Large/discrete deltas get the gentle plain-wheel rate, small ones the pinch rate (clamped).
      const discrete = e.deltaMode !== 0 || Math.abs(dy) >= 50;
      accZoom *= discrete ? Math.exp(-dy * 0.0015) : Math.exp(-Math.sign(dy) * Math.min(Math.abs(dy), 10) * 0.012);
      accZx = x; accZy = y; schedule();
      return;
    }
    if (!isEngaged()) {
      h.hint(true);
      clearTimeout(hintTimer);
      hintTimer = window.setTimeout(() => h.hint(false), 1500);
      return;
    }
    e.preventDefault();
    accZoom *= Math.exp(-dy * 0.0015); accZx = x; accZy = y; schedule();
  };

  type GE = Event & { scale: number; clientX: number; clientY: number };
  // WebKit gesture events are only used for the macOS Safari trackpad (no pointers down). On iOS Safari a
  // two-finger pinch also produces pointer events, which the 'pinch' path already handles: applying both
  // would square the zoom rate and bypass the cooperative (tap-to-engage) rule.
  const onGStart = (e: Event) => { gestureScale = 1; if (ptrs.size === 0) e.preventDefault(); };
  const onGChange = (e: Event) => {
    if (ptrs.size > 0) return;
    e.preventDefault();
    const g = e as GE;
    const f = g.scale / (gestureScale || 1);
    gestureScale = g.scale;
    const [x, y] = local(g.clientX !== undefined ? g : { clientX: el.clientWidth / 2, clientY: el.clientHeight / 2 });
    accZoom *= f; accZx = x; accZy = y; schedule();
  };
  const onCtx = (e: Event) => e.preventDefault();
  const onLeave = (e: PointerEvent) => { if (e.pointerType !== 'touch' && !ptrs.size) h.leave(); };

  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
  el.addEventListener('pointercancel', onCancel);
  el.addEventListener('lostpointercapture', onCancel as EventListener);
  el.addEventListener('pointerleave', onLeave);
  el.addEventListener('wheel', onWheel, { passive: false });
  el.addEventListener('gesturestart', onGStart, { passive: false } as AddEventListenerOptions);
  el.addEventListener('gesturechange', onGChange, { passive: false } as AddEventListenerOptions);
  el.addEventListener('gestureend', onGStart, { passive: false } as AddEventListenerOptions);
  el.addEventListener('contextmenu', onCtx);
  applyTouchAction();

  const ctl: GestureController = {
    get engaged() { return isEngaged(); },
    setEngaged(on) { engaged = on; applyTouchAction(); if (!on) h.hint(false); },
    dispose() {
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onCancel);
      el.removeEventListener('lostpointercapture', onCancel as EventListener);
      el.removeEventListener('pointerleave', onLeave);
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('gesturestart', onGStart);
      el.removeEventListener('gesturechange', onGChange);
      el.removeEventListener('gestureend', onGStart);
      el.removeEventListener('contextmenu', onCtx);
      if (raf) cancelAnimationFrame(raf);
      clearLong(); clearTimeout(hintTimer);
    },
  };
  return ctl;
};
