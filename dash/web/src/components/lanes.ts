// Trajectory swimlanes on Canvas 2D (CONTRACT 6.2.3). One lane per agent plus a board lane on top.
// Two stacked canvases: the base layer holds the marks (redrawn on view / data / selection change),
// the overlay holds cursor + hover (redrawn on every scrub frame, cheap).
import { fitCanvas, h, onResize, tooltip } from '../dom';
import type { LaneIndex, RunModel } from '../lib/board';
import { agentColor, ARC_DASH, critical, cv, onThemeChange, stepColor, withAlpha } from '../lib/colors';
import { clockT, dur, trunc } from '../lib/format';
import { clamp, lastLEnum, ticks } from '../lib/time';
import type { TimelineEvent } from '../lib/types';

export type XMode = 'time' | 'step';
export interface LanesOpts {
  onCursor: (t: number) => void;
  onSelect: (lane: number, stepIdx: number | null) => void;
  onView?: (v0: number, v1: number) => void;
}

const LABEL_W = 92, AXIS_H = 20, BOARD_H = 30, PAD_R = 10;

export class Lanes {
  el: HTMLDivElement;
  private base = h('canvas');
  private over = h('canvas');
  private m: RunModel | null = null;
  private xMode: XMode = 'time';
  private v0 = 0; private v1 = 1;
  private W = 600; private H = 200; private laneH = 36;
  private cursor = 0;
  private selLane: number | null = null; private selIdx: number | null = null;
  private hover: { x: number; y: number } | null = null;
  private showArcs = false;
  private dirty = true; private raf = 0;
  private tip = tooltip();
  private cleanup: (() => void)[] = [];
  private arcsCache: ReturnType<RunModel['arcs']> | null = null;
  private arcsGraph: unknown = null;

  constructor(private opts: LanesOpts) {
    this.el = h('div', { class: 'lanes-box', tabindex: '0', 'aria-label': 'Agent trajectory lanes. Arrow keys scrub time.' }, this.base, this.over);
    this.cleanup.push(onResize(this.el, (w) => { this.W = Math.max(300, w); this.layout(); }));
    this.cleanup.push(onThemeChange(() => this.redraw()));
    this.bindPointer();
  }

  destroy() { this.cleanup.forEach((f) => f()); cancelAnimationFrame(this.raf); this.tip.hide(); }

  setModel(m: RunModel, keepView = true) {
    const first = !this.m;
    this.m = m;
    this.arcsCache = null;
    if (first || !keepView) this.resetView(); else this.clampView();
    this.layout();
  }
  setXMode(x: XMode) { if (x === this.xMode) return; this.xMode = x; this.resetView(); }
  getXMode() { return this.xMode; }
  setArcs(on: boolean) { this.showArcs = on; this.redraw(); }
  setCursor(t: number) { this.cursor = t; this.drawOverlay(); }
  setSelected(lane: number | null, idx: number | null) { this.selLane = lane; this.selIdx = idx; this.redraw(); }
  getView(): [number, number] { return [this.v0, this.v1]; }

  /** Domain in x units (seconds or steps). */
  private domain(): [number, number] {
    if (!this.m) return [0, 1];
    if (this.xMode === 'step') return [0, this.m.maxStep];
    return [this.m.tMin, this.m.tMax];
  }
  resetView() { const [a, b] = this.domain(); const pad = (b - a) * 0.01; this.v0 = a - pad; this.v1 = b + pad; this.emitView(); this.redraw(); }
  /** Follow-live: keep the right edge at the head. */
  followHead() {
    const [, b] = this.domain();
    const span = this.v1 - this.v0;
    if (b > this.v1 - span * 0.02) { this.v1 = b + span * 0.02; this.v0 = this.v1 - span; this.emitView(); this.redraw(); }
  }
  private clampView() {
    const [a, b] = this.domain(); const span = b - a || 1;
    const w = clamp(this.v1 - this.v0, span / 2000, span * 1.2);
    this.v0 = clamp(this.v0, a - span * 0.1, b + span * 0.1 - w); this.v1 = this.v0 + w;
  }
  private emitView() { this.opts.onView?.(this.v0, this.v1); }

  private layout() {
    const n = this.m?.lanes.length ?? 1;
    this.laneH = n > 12 ? 20 : n > 6 ? 28 : 38;
    this.H = AXIS_H + (this.xMode === 'time' ? BOARD_H : 0) + n * this.laneH + 6;
    this.el.style.height = this.H + 'px';
    this.redraw();
  }

  private px(x: number) { return LABEL_W + ((x - this.v0) / (this.v1 - this.v0)) * (this.W - LABEL_W - PAD_R); }
  private inv(px: number) { return this.v0 + ((px - LABEL_W) / (this.W - LABEL_W - PAD_R)) * (this.v1 - this.v0); }
  private laneY(i: number) { return AXIS_H + (this.xMode === 'time' ? BOARD_H : 0) + i * this.laneH; }

  /** x coordinate (in x units) of an event. Step mode: steps occupy [step-1, step). */
  private ex(e: TimelineEvent): number | null {
    if (this.xMode === 'time') return e.t;
    return e.step != null ? e.step - 0.5 : null;
  }
  /** cursor in x units for a lane (step mode: the active step at cursor t). */
  private cursorX(l: LaneIndex | null): number | null {
    if (this.xMode === 'time' || !l || !this.m) return this.cursor;
    const i = lastLEnum(l.stepT, this.cursor);
    if (i < 0) return 0;
    const s = l.steps[i];
    const frac = s.t_end != null && s.t_end > s.t ? clamp((this.cursor - s.t) / (s.t_end - s.t), 0, 1) : 1;
    return (s.step ?? i + 1) - 1 + frac;
  }

  redraw() { this.dirty = true; if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; if (this.dirty) { this.dirty = false; this.drawBase(); } this.drawOverlay(); }); }

  // ------------------------------------------------------------------ base layer
  private drawBase() {
    const ctx = fitCanvas(this.base, this.W, this.H);
    const m = this.m;
    ctx.clearRect(0, 0, this.W, this.H);
    if (!m) return;
    const grid = cv('--grid'), muted = cv('--text-muted'), sec = cv('--text-secondary'), surf = cv('--surface-1');
    ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textBaseline = 'middle';
    // axis + gridlines
    const tk = ticks(this.v0, this.v1, Math.max(3, Math.floor((this.W - LABEL_W) / 90)));
    ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.fillStyle = muted; ctx.textAlign = 'center';
    for (const v of tk) {
      const x = Math.round(this.px(v)) + 0.5;
      if (x < LABEL_W || x > this.W - PAD_R) continue;
      ctx.beginPath(); ctx.moveTo(x, AXIS_H - 3); ctx.lineTo(x, this.H); ctx.stroke();
      ctx.fillText(this.xMode === 'time' ? clockT(v).replace(/\.0$/, '') : String(v), x, AXIS_H / 2);
    }
    ctx.textAlign = 'left';
    ctx.fillText(this.xMode === 'time' ? 't (m:s)' : 'step', 6, AXIS_H / 2);
    ctx.save();
    ctx.beginPath(); ctx.rect(LABEL_W, 0, this.W - LABEL_W - PAD_R + 4, this.H); ctx.clip();

    // free-phase washes + lane separators
    m.lanes.forEach((l, i) => {
      const y = this.laneY(i);
      const fx = this.xMode === 'time' ? l.solve : (() => { const k = l.steps.findIndex((s) => s.phase === 'free'); return k < 0 ? null : (l.steps[k].step ?? k + 1); })();
      if (fx != null) {
        const endX = this.xMode === 'time' ? (l.end ?? m.tMax) : m.maxStep;
        ctx.fillStyle = cv('--free-wash');
        ctx.fillRect(this.px(fx), y + 1, this.px(endX) - this.px(fx), this.laneH - 2);
        ctx.strokeStyle = stepColor('submit'); ctx.lineWidth = 2;
        const x = Math.round(this.px(fx));
        ctx.beginPath(); ctx.moveTo(x, y + 2); ctx.lineTo(x, y + this.laneH - 2); ctx.stroke();
      }
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(LABEL_W, y + this.laneH + 0.5); ctx.lineTo(this.W, y + this.laneH + 0.5); ctx.stroke();
    });
    if (this.xMode === 'time') {
      ctx.strokeStyle = cv('--axis');
      ctx.beginPath(); ctx.moveTo(LABEL_W, AXIS_H + BOARD_H + 0.5); ctx.lineTo(this.W, AXIS_H + BOARD_H + 0.5); ctx.stroke();
    }

    const minW = 3;
    const crit = critical();
    m.lanes.forEach((l, i) => {
      const y = this.laneY(i);
      const bandY = y + Math.round(this.laneH * 0.42), bandH = Math.max(6, Math.round(this.laneH * 0.42));
      // api_retry hatched gaps (time mode): from the retry record to the next model call
      if (this.xMode === 'time') for (const r of l.records) {
        if (r.kind !== 'api_retry') continue;
        const k = lastLEnum(l.modelT, r.t) + 1;
        const t1 = k < l.models.length ? l.models[k].t : r.t + 5;
        this.hatch(ctx, this.px(r.t), bandY - 2, Math.max(4, this.px(t1) - this.px(r.t)), bandH + 4, muted);
      }
      // model ticks (time mode): height encodes completion tokens
      if (this.xMode === 'time') {
        const [a, b] = this.visibleRange(l.modelT);
        ctx.strokeStyle = withAlpha(sec.startsWith('#') ? sec : '#52514e', 0.55); ctx.lineWidth = 1;
        ctx.beginPath();
        for (let k = a; k < b; k++) {
          const e = l.models[k];
          const hh = e.tokens != null ? 3 + (Math.sqrt(e.tokens) / Math.sqrt(m.maxTokens)) * (this.laneH * 0.3) : this.laneH * 0.18;
          const x = Math.round(this.px(e.t)) + 0.5;
          ctx.moveTo(x, bandY - 2); ctx.lineTo(x, bandY - 2 - hh);
        }
        ctx.stroke();
      }
      // steps
      const [a, b] = this.xMode === 'time' ? this.visibleRange(l.stepT) : [0, l.steps.length];
      for (let k = a; k < b; k++) {
        const e = l.steps[k];
        let x0: number, x1: number;
        if (this.xMode === 'time') { x0 = this.px(e.t); x1 = Math.max(x0 + minW, this.px(e.t_end ?? e.t)); }
        else { const s = e.step ?? k + 1; x0 = this.px(s - 1) + 1; x1 = this.px(s) - 1; if (x1 - x0 < 1) x1 = x0 + 1; }
        if (x1 < LABEL_W || x0 > this.W) continue;
        const cls = l.stepClass[k];
        if (cls === 'idle') {
          ctx.strokeStyle = muted; ctx.lineWidth = 1.25;
          ctx.strokeRect(Math.round(x0) + 0.5, bandY + 2.5, Math.max(2, x1 - x0 - 1), bandH - 5);
        } else {
          ctx.fillStyle = stepColor(cls);
          roundRect(ctx, x0, bandY, x1 - x0, bandH, Math.min(2, (x1 - x0) / 2));
          ctx.fill();
        }
        const fl = l.stepFlag[k];
        if (fl) {
          ctx.strokeStyle = crit; ctx.lineWidth = 1.5;
          roundRect(ctx, x0 - 1.5, bandY - 1.5, x1 - x0 + 3, bandH + 3, 2.5); ctx.stroke();
          if (fl & 1) { ctx.fillStyle = crit; ctx.font = 'bold 9px system-ui'; ctx.fillText('↻', x0, bandY + bandH + 6); ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif'; }
        }
        if (this.selLane === l.lane && this.selIdx === k) {
          ctx.strokeStyle = cv('--text-primary'); ctx.lineWidth = 2;
          roundRect(ctx, x0 - 3, bandY - 3, x1 - x0 + 6, bandH + 6, 3); ctx.stroke();
        }
      }
      // step mode: board glyphs above the band
      if (this.xMode === 'step') for (const e of l.board) {
        const x = this.ex(e); if (x == null) continue;
        this.glyph(ctx, e, this.px(x), y + 7, agentColor(l.lane), surf);
      }
      // start / end brackets (time mode)
      if (this.xMode === 'time') {
        ctx.strokeStyle = sec; ctx.lineWidth = 1;
        if (l.start != null) { const x = Math.round(this.px(l.start)) + 0.5; ctx.beginPath(); ctx.moveTo(x + 3, bandY - 1); ctx.lineTo(x, bandY - 1); ctx.lineTo(x, bandY + bandH + 1); ctx.lineTo(x + 3, bandY + bandH + 1); ctx.stroke(); }
        if (l.end != null) { const x = Math.round(this.px(l.end)) + 0.5; ctx.beginPath(); ctx.moveTo(x - 3, bandY - 1); ctx.lineTo(x, bandY - 1); ctx.lineTo(x, bandY + bandH + 1); ctx.lineTo(x - 3, bandY + bandH + 1); ctx.stroke(); }
      }
    });

    // board lane (time mode)
    if (this.xMode === 'time') {
      const [a, b] = this.visibleRange(m.boardLaneT);
      const yc = AXIS_H + BOARD_H / 2;
      for (let k = a; k < b; k++) {
        const e = m.boardLane[k];
        this.glyph(ctx, e, this.px(e.t), yc + (e.kind === 'req' ? 5 : e.kind === 'post' || e.kind === 'dm' ? -5 : 0), agentColor(e.lane), surf);
      }
    }

    // swarm onset rule
    if (m.swarmOnset != null && this.xMode === 'time') {
      const x = Math.round(this.px(m.swarmOnset)) + 0.5;
      ctx.strokeStyle = cv('--text-primary'); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, AXIS_H); ctx.lineTo(x, this.H); ctx.stroke();
      ctx.fillStyle = cv('--text-primary'); ctx.font = '600 10px system-ui'; ctx.textAlign = 'left';
      ctx.fillText('swarm onset', x + 3, AXIS_H + 6);
      ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    }

    // reply / mention arcs
    if (this.showArcs && this.xMode === 'time') {
      if (!this.arcsCache || this.arcsGraph !== m.graph) { this.arcsCache = m.arcs(); this.arcsGraph = m.graph; }
      const arcs = this.arcsCache;
      // neutral ink (step marks own the categorical slots); edge kind is carried by the dash pattern
      const ink = cv('--text-primary');
      ctx.lineWidth = 1.25;
      ctx.globalAlpha = 0.8;
      for (const a of arcs) {
        const fi = m.lanes.findIndex((l) => l.lane === a.from), ti = m.lanes.findIndex((l) => l.lane === a.to);
        if (fi < 0 || ti < 0) continue;
        const x = this.px(a.t);
        const y0 = this.laneY(fi) + this.laneH / 2, y1 = this.laneY(ti) + this.laneH / 2;
        ctx.strokeStyle = ink;
        ctx.setLineDash(ARC_DASH[a.kind] ?? []);
        ctx.beginPath(); ctx.moveTo(x, y0); ctx.quadraticCurveTo(x + 18 + Math.abs(fi - ti) * 8, (y0 + y1) / 2, x, y1); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = ink; ctx.beginPath(); ctx.arc(x, y1, 2.5, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
    ctx.restore();

    // lane labels (text tokens; colour carried by the dot beside them)
    ctx.textAlign = 'left';
    if (this.xMode === 'time') { ctx.fillStyle = sec; ctx.fillText('board', 22, AXIS_H + BOARD_H / 2); this.boardIcon(ctx, 10, AXIS_H + BOARD_H / 2, muted); }
    m.lanes.forEach((l, i) => {
      const y = this.laneY(i) + this.laneH / 2;
      ctx.fillStyle = agentColor(l.lane); ctx.beginPath(); ctx.arc(12, y, 4.5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = this.selLane === l.lane ? cv('--text-primary') : sec;
      ctx.font = (this.selLane === l.lane ? '600 ' : '') + '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      ctx.fillText(l.label, 22, this.laneH > 26 && l.names[0] ? y - 6 : y);
      if (this.laneH > 26 && l.names[0]) { ctx.fillStyle = muted; ctx.font = '10px system-ui'; ctx.fillText(trunc(l.names[0], 12), 22, y + 7); }
      ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    });
  }

  private visibleRange(ts: Float64Array): [number, number] {
    const pad = (this.v1 - this.v0) * 0.02;
    const a = Math.max(0, lastLEnum(ts, this.v0 - pad - 30)); // steps can extend leftwards up to their duration
    const b = Math.min(ts.length, lastLEnum(ts, this.v1 + pad) + 1);
    return [a, b];
  }

  private glyph(ctx: CanvasRenderingContext2D, e: TimelineEvent, x: number, y: number, color: string, surf: string) {
    if (x < LABEL_W - 4 || x > this.W) return;
    const leak = e.flags?.includes('secret_leak');
    ctx.lineWidth = 1.5;
    switch (e.kind) {
      case 'post': case 'irc_cmd':
        if (e.kind === 'irc_cmd' && e.cat !== 'write') { ctx.strokeStyle = color; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.stroke(); break; }
        ctx.fillStyle = color; ctx.strokeStyle = surf; ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.lineWidth = 2; ctx.stroke(); break;
      case 'upvote':
        ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x, y - 4.5); ctx.lineTo(x + 4.5, y + 3.5); ctx.lineTo(x - 4.5, y + 3.5); ctx.closePath(); ctx.fill(); break;
      case 'dm':
        ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x, y - 5); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 5); ctx.lineTo(x - 5, y); ctx.closePath(); ctx.fill(); break;
      case 'delete':
        ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(x - 3.5, y - 3.5); ctx.lineTo(x + 3.5, y + 3.5); ctx.moveTo(x + 3.5, y - 3.5); ctx.lineTo(x - 3.5, y + 3.5); ctx.stroke(); break;
      case 'board_start':
        ctx.fillStyle = cv('--text-muted'); ctx.fillRect(Math.round(x) - 1, y - 7, 2, 14); break;
      case 'irc_join':
        ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); ctx.stroke(); break;
      default: // req = read
        if (e.cat === 'write') { ctx.fillStyle = withAlpha(color, 0.35); ctx.strokeStyle = color; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
        else { ctx.strokeStyle = color; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.stroke(); }
    }
    if (leak) { ctx.strokeStyle = critical(); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke(); }
  }
  private boardIcon(ctx: CanvasRenderingContext2D, x: number, y: number, c: string) {
    ctx.strokeStyle = c; ctx.lineWidth = 1.25; ctx.strokeRect(x - 5, y - 4, 10, 8); ctx.beginPath(); ctx.moveTo(x - 3, y - 1); ctx.lineTo(x + 3, y - 1); ctx.moveTo(x - 3, y + 1.5); ctx.lineTo(x + 1, y + 1.5); ctx.stroke();
  }
  private hatch(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, hh: number, c: string) {
    ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, hh); ctx.clip();
    ctx.strokeStyle = withAlpha(c.startsWith('#') ? c : '#898781', 0.6); ctx.lineWidth = 1;
    ctx.beginPath(); for (let k = -hh; k < w + hh; k += 4) { ctx.moveTo(x + k, y + hh); ctx.lineTo(x + k + hh, y); } ctx.stroke();
    ctx.restore();
  }

  // ------------------------------------------------------------------ overlay
  private drawOverlay() {
    const ctx = fitCanvas(this.over, this.W, this.H);
    ctx.clearRect(0, 0, this.W, this.H);
    const m = this.m; if (!m) return;
    const ink = cv('--text-primary');
    ctx.strokeStyle = ink; ctx.lineWidth = 1.5;
    if (this.xMode === 'time') {
      const x = Math.round(this.px(this.cursor)) + 0.5;
      if (x >= LABEL_W && x <= this.W) {
        ctx.beginPath(); ctx.moveTo(x, AXIS_H - 4); ctx.lineTo(x, this.H); ctx.stroke();
        ctx.fillStyle = ink; ctx.beginPath(); ctx.moveTo(x - 5, 0); ctx.lineTo(x + 5, 0); ctx.lineTo(x, 6); ctx.closePath(); ctx.fill();
      }
    } else {
      m.lanes.forEach((l, i) => {
        const cx = this.cursorX(l); if (cx == null) return;
        const x = Math.round(this.px(cx)) + 0.5; const y = this.laneY(i);
        ctx.beginPath(); ctx.moveTo(x, y + 1); ctx.lineTo(x, y + this.laneH - 1); ctx.stroke();
      });
    }
    // hover crosshair (time readout)
    if (this.hover && this.hover.x > LABEL_W) {
      const x = Math.round(this.hover.x) + 0.5;
      ctx.strokeStyle = withAlpha(cv('--text-muted').startsWith('#') ? cv('--text-muted') : '#898781', 0.7); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, AXIS_H); ctx.lineTo(x, this.H); ctx.stroke();
    }
  }

  // ------------------------------------------------------------------ interaction
  private hit(x: number, y: number): { lane: number; idx: number | null; ev: TimelineEvent | null } | null {
    const m = this.m; if (!m || x < LABEL_W) return null;
    const xv = this.inv(x);
    const tol = ((this.v1 - this.v0) / (this.W - LABEL_W)) * 6;
    if (this.xMode === 'time' && y >= AXIS_H && y < AXIS_H + BOARD_H) {
      const ev = nearest(m.boardLane, m.boardLaneT, xv, tol);
      return { lane: -1, idx: null, ev };
    }
    const i = Math.floor((y - this.laneY(0)) / this.laneH);
    const l = m.lanes[i]; if (!l) return null;
    if (this.xMode === 'step') {
      const s = Math.floor(xv) + 1;
      const k = l.steps.findIndex((e) => (e.step ?? 0) === s);
      return { lane: l.lane, idx: k >= 0 ? k : null, ev: k >= 0 ? l.steps[k] : null };
    }
    const yIn = y - this.laneY(i);
    if (yIn < this.laneH * 0.4) {
      const mev = nearest(l.models, l.modelT, xv, tol);
      if (mev) return { lane: l.lane, idx: null, ev: mev };
    }
    // step whose [t, max(t_end, t+minW)] contains xv, else nearest within tol
    let best: number | null = null, bd = Infinity;
    const k0 = lastLEnum(l.stepT, xv + tol);
    for (let k = k0; k >= 0 && k > k0 - 6; k--) {
      const e = l.steps[k];
      const te = Math.max(e.t_end ?? e.t, e.t + tol / 2);
      const d = xv < e.t ? e.t - xv : xv > te ? xv - te : 0;
      if (d < bd) { bd = d; best = k; }
    }
    if (best != null && bd <= tol) return { lane: l.lane, idx: best, ev: l.steps[best] };
    return { lane: l.lane, idx: null, ev: null };
  }

  private showTip(cx: number, cy: number, hit: NonNullable<ReturnType<Lanes['hit']>>) {
    const e = hit.ev;
    if (!e) { this.tip.hide(); return; }
    const l = this.m!.lane(hit.lane);
    const cls = l && hit.idx != null ? l.stepClass[hit.idx] : null;
    const head = e.kind === 'step' || e.kind === 'idle'
      ? `step ${e.step ?? '?'} · ${cls === 'cmd' ? (e.cat ?? 'cmd') : cls}`
      : e.kind === 'model' ? `model call ${e.call ?? ''}${e.tokens != null ? ' · ' + e.tokens + ' completion tok' : ''}` : e.kind;
    this.tip.show(cx, cy,
      h('div', null, h('span', { class: 'v' }, clockT(e.t)), ' ', h('span', { class: 'k' }, `${e.lane < 0 ? 'board' : 'a' + e.lane} · ${head}`)),
      e.t_end != null && e.t_end > e.t ? h('div', { class: 'k' }, 'duration ', dur(e.t_end - e.t)) : null,
      e.phase ? h('div', { class: 'k' }, 'phase ', e.phase) : null,
      e.flags?.length ? h('div', null, ...e.flags.map((f) => h('span', { class: 'flag ' + f }, f))) : null,
      e.label ? h('pre', null, e.label) : null,
    );
  }

  private bindPointer() {
    const el = this.el;
    let drag: { x: number; v0: number; v1: number; moved: boolean } | null = null;
    el.addEventListener('pointermove', (ev) => {
      const r = el.getBoundingClientRect(); const x = ev.clientX - r.left, y = ev.clientY - r.top;
      if (drag) {
        const dx = x - drag.x;
        if (Math.abs(dx) > 3) drag.moved = true;
        if (drag.moved) {
          const dv = (dx / (this.W - LABEL_W - PAD_R)) * (drag.v1 - drag.v0);
          this.v0 = drag.v0 - dv; this.v1 = drag.v1 - dv; this.clampView(); this.emitView(); this.redraw();
          this.tip.hide();
          return;
        }
      }
      this.hover = { x, y };
      const hit = this.hit(x, y);
      if (hit) this.showTip(ev.clientX, ev.clientY, hit); else this.tip.hide();
      el.style.cursor = hit?.ev ? 'pointer' : 'crosshair';
      this.drawOverlay();
    });
    el.addEventListener('pointerleave', () => { this.hover = null; this.tip.hide(); this.drawOverlay(); });
    el.addEventListener('pointerdown', (ev) => {
      const r = el.getBoundingClientRect();
      drag = { x: ev.clientX - r.left, v0: this.v0, v1: this.v1, moved: false };
      el.setPointerCapture(ev.pointerId);
    });
    el.addEventListener('pointerup', (ev) => {
      const r = el.getBoundingClientRect(); const x = ev.clientX - r.left, y = ev.clientY - r.top;
      const d = drag; drag = null;
      if (!d || d.moved || x < LABEL_W) return;
      const hit = this.hit(x, y);
      if (hit && hit.lane >= 0 && hit.idx != null) {
        const e = this.m!.lane(hit.lane)!.steps[hit.idx];
        this.opts.onSelect(hit.lane, hit.idx);
        this.opts.onCursor(e.t_end ?? e.t);
      } else if (hit?.ev) {
        this.opts.onCursor(hit.ev.t);
        if (hit.lane >= 0) this.opts.onSelect(hit.lane, null);
      } else if (this.xMode === 'time') {
        this.opts.onCursor(this.inv(x));
      } else if (hit) {
        this.opts.onSelect(hit.lane, null);
      }
    });
    el.addEventListener('dblclick', () => this.resetView());
    el.addEventListener('wheel', (ev) => {
      if (!this.m) return;
      ev.preventDefault();
      const r = el.getBoundingClientRect(); const x = ev.clientX - r.left;
      if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
        const dv = (ev.deltaX / (this.W - LABEL_W)) * (this.v1 - this.v0);
        this.v0 += dv; this.v1 += dv;
      } else {
        const f = Math.exp(ev.deltaY * 0.0018);
        const xv = this.inv(clamp(x, LABEL_W, this.W - PAD_R));
        this.v0 = xv - (xv - this.v0) * f; this.v1 = xv + (this.v1 - xv) * f;
      }
      this.clampView(); this.emitView(); this.redraw();
    }, { passive: false });
  }
}

function nearest(evs: TimelineEvent[], ts: Float64Array, x: number, tol: number): TimelineEvent | null {
  if (!evs.length) return null;
  const i = lastLEnum(ts, x);
  let best: TimelineEvent | null = null, bd = Infinity;
  for (const k of [i - 1, i, i + 1, i + 2]) {
    if (k < 0 || k >= evs.length) continue;
    const d = Math.abs(evs[k].t - x);
    if (d < bd) { bd = d; best = evs[k]; }
  }
  return bd <= tol ? best : null;
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, hh: number, r: number) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, hh, r); else ctx.rect(x, y, w, hh);
}
