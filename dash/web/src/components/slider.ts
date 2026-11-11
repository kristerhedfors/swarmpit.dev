// Time slider (CONTRACT 6.2.3): full-width track with an event-density histogram, markers for first
// contacts / solves / posts / swarm onset, the lanes viewport bracket, a draggable cursor, and
// play/pause at 1x/4x/16x/64x run time with snap-to-next-event stepping.
import { fitCanvas, h, onResize, tooltip } from '../dom';
import type { RunModel } from '../lib/board';
import { agentColor, critical, cv, onThemeChange, stepColor } from '../lib/colors';
import { clockT } from '../lib/format';
import { clamp } from '../lib/time';

export const SPEEDS = [1, 4, 16, 64];
const H = 44, PAD = 8;

export class Slider {
  el: HTMLDivElement;
  private c = h('canvas', { role: 'slider', tabindex: '0', 'aria-label': 'Run time cursor', 'aria-valuemin': '0' });
  private timeEl = h('span', { class: 'time' }, '0:00.0');
  private playBtn = h('button', { class: 'btn icon', title: 'Play / pause (Space)', 'aria-label': 'Play' }, '▶');
  private speedSel = h('select', { title: 'Playback speed (- / =)', 'aria-label': 'Playback speed' }, ...SPEEDS.map((s) => h('option', { value: String(s) }, s + '×')));
  private followBtn = h('button', { class: 'btn', title: 'Follow live head (f)', 'aria-pressed': 'false', style: { display: 'none' } }, '● follow live');
  private m: RunModel | null = null;
  private t = 0;
  private W = 600;
  private view: [number, number] | null = null;
  private playing = false; private speed = 4; private lastFrame = 0; private raf = 0;
  private follow = false;
  private tip = tooltip();
  private cleanup: (() => void)[] = [];

  constructor(private onT: (t: number) => void, private onFollow?: (on: boolean) => void) {
    this.speedSel.value = String(this.speed);
    const prev = h('button', { class: 'btn icon', title: 'Previous event (,)', 'aria-label': 'Previous event', onclick: () => this.snap(-1) }, '⏮');
    const next = h('button', { class: 'btn icon', title: 'Next event (.)', 'aria-label': 'Next event', onclick: () => this.snap(1) }, '⏭');
    this.playBtn.addEventListener('click', () => this.toggle());
    this.speedSel.addEventListener('change', () => { this.speed = Number(this.speedSel.value); });
    this.followBtn.addEventListener('click', () => this.setFollow(!this.follow));
    this.el = h('div', { class: 'slider-box' },
      h('div', { class: 'slider-ctl' }, this.playBtn, prev, next, this.speedSel, this.timeEl, h('span', { class: 'muted', style: { fontSize: '11px' } }, 'drag or ←/→ to scrub · Shift ×10 · Alt ×0.1'), h('span', { class: 'grow' }), this.followBtn),
      this.c);
    this.cleanup.push(onResize(this.el, (w) => { this.W = Math.max(200, w - 20); this.draw(); }));
    this.cleanup.push(onThemeChange(() => this.draw()));
    this.bind();
  }
  destroy() { this.cleanup.forEach((f) => f()); cancelAnimationFrame(this.raf); this.tip.hide(); }

  setModel(m: RunModel) { this.m = m; this.followBtn.style.display = m.live ? '' : 'none'; this.draw(); }
  setT(t: number) { this.t = t; this.timeEl.textContent = clockT(t); this.c.setAttribute('aria-valuenow', t.toFixed(1)); this.draw(); }
  setView(v0: number, v1: number) { this.view = [v0, v1]; this.draw(); }
  isPlaying() { return this.playing; }
  isFollowing() { return this.follow; }
  setFollow(on: boolean) { this.follow = on; this.followBtn.setAttribute('aria-pressed', String(on)); this.followBtn.classList.toggle('on', on); this.onFollow?.(on); }
  speedStep(d: 1 | -1) { const i = clamp(SPEEDS.indexOf(this.speed) + d, 0, SPEEDS.length - 1); this.speed = SPEEDS[i]; this.speedSel.value = String(this.speed); }

  toggle() {
    this.playing = !this.playing;
    this.playBtn.textContent = this.playing ? '❚❚' : '▶';
    this.playBtn.setAttribute('aria-label', this.playing ? 'Pause' : 'Play');
    if (this.playing) {
      if (this.m && this.t >= this.m.tMax - 0.01) this.onT(this.m.tMin);
      this.lastFrame = performance.now();
      const loop = (now: number) => {
        if (!this.playing || !this.m) return;
        const dt = ((now - this.lastFrame) / 1000) * this.speed; this.lastFrame = now;
        let nt = this.t + dt;
        if (nt >= this.m.tMax) { nt = this.m.tMax; if (!this.m.live) this.toggle(); }
        this.onT(nt);
        this.raf = requestAnimationFrame(loop);
      };
      this.raf = requestAnimationFrame(loop);
    } else cancelAnimationFrame(this.raf);
  }

  snap(dir: 1 | -1) { if (!this.m) return; const t = this.m.nextEventT(this.t, dir); if (t != null) this.onT(t); }

  private px(t: number) { const m = this.m!; return PAD + ((t - m.tMin) / (m.tMax - m.tMin || 1)) * (this.W - 2 * PAD); }
  private inv(x: number) { const m = this.m!; return m.tMin + ((x - PAD) / (this.W - 2 * PAD)) * (m.tMax - m.tMin); }

  draw() {
    const ctx = fitCanvas(this.c, this.W, H);
    ctx.clearRect(0, 0, this.W, H);
    const m = this.m; if (!m) return;
    const trackY = 26;
    // histogram (single series -> neutral bars)
    const bins = Math.max(20, Math.floor((this.W - 2 * PAD) / 4));
    const d = m.density(bins);
    const mx = Math.max(1, ...d);
    const bw = (this.W - 2 * PAD) / bins;
    ctx.fillStyle = cv('--neutral-mark');
    for (let i = 0; i < bins; i++) {
      if (!d[i]) continue;
      const hh = Math.max(1, (d[i] / mx) * 18);
      ctx.fillRect(PAD + i * bw + 0.5, trackY - hh, Math.max(1, bw - 1), hh);
    }
    ctx.strokeStyle = cv('--axis'); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD, trackY + 0.5); ctx.lineTo(this.W - PAD, trackY + 0.5); ctx.stroke();
    // viewport bracket
    if (this.view) {
      const a = clamp(this.px(this.view[0]), PAD, this.W - PAD), b = clamp(this.px(this.view[1]), PAD, this.W - PAD);
      if (b - a < this.W - 2 * PAD - 2) { ctx.fillStyle = cv('--accent-wash'); ctx.fillRect(a, 2, Math.max(2, b - a), trackY - 2); }
    }
    // markers row under the track
    const my = trackY + 8;
    for (const p of m.posts) { if (p.is_seed || p.t == null) continue; ctx.fillStyle = agentColor(p.agent_id ?? -1); ctx.beginPath(); ctx.arc(this.px(p.t), my, 2.5, 0, Math.PI * 2); ctx.fill(); if (p.flags.includes('secret_leak')) { ctx.strokeStyle = critical(); ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(this.px(p.t), my, 4.5, 0, Math.PI * 2); ctx.stroke(); } }
    for (const fc of m.firstContacts) { const x = this.px(fc.t); ctx.strokeStyle = stepColor('read'); ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, trackY + 2); ctx.lineTo(x, trackY + 14); ctx.stroke(); }
    for (const l of m.lanes) if (l.solve != null) { const x = this.px(l.solve); ctx.fillStyle = stepColor('submit'); ctx.beginPath(); ctx.moveTo(x, trackY + 3); ctx.lineTo(x + 4, trackY + 9); ctx.lineTo(x, trackY + 15); ctx.lineTo(x - 4, trackY + 9); ctx.closePath(); ctx.fill(); }
    if (m.swarmOnset != null) { const x = Math.round(this.px(m.swarmOnset)) + 0.5; ctx.strokeStyle = cv('--text-primary'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    // cursor handle
    const x = Math.round(this.px(clamp(this.t, m.tMin, m.tMax))) + 0.5;
    ctx.strokeStyle = cv('--text-primary'); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = cv('--text-primary'); ctx.strokeStyle = cv('--surface-1');
    ctx.beginPath(); ctx.arc(x, trackY, 5.5, 0, Math.PI * 2); ctx.fill(); ctx.lineWidth = 2; ctx.stroke();
  }

  private bind() {
    let dragging = false;
    const at = (ev: PointerEvent) => { const r = this.c.getBoundingClientRect(); return clamp(this.inv(ev.clientX - r.left), this.m!.tMin, this.m!.tMax); };
    this.c.addEventListener('pointerdown', (ev) => { if (!this.m) return; dragging = true; this.c.setPointerCapture(ev.pointerId); if (this.follow) this.setFollow(false); this.onT(at(ev)); });
    this.c.addEventListener('pointermove', (ev) => {
      if (!this.m) return;
      if (dragging) { this.onT(at(ev)); return; }
      const t = at(ev);
      const r = this.c.getBoundingClientRect();
      const y = ev.clientY - r.top;
      if (y > 28) {
        // markers tooltip
        const near = (tt: number) => Math.abs(this.px(tt) - (ev.clientX - r.left)) < 5;
        const items: string[] = [];
        for (const fc of this.m.firstContacts) if (near(fc.t)) items.push(`a${fc.lane} first board contact (step ${fc.step ?? '?'})`);
        for (const l of this.m.lanes) if (l.solve != null && near(l.solve)) items.push(`a${l.lane} solved`);
        for (const p of this.m.posts) if (!p.is_seed && p.t != null && near(p.t)) items.push(`a${p.agent_id} posted${p.flags.includes('secret_leak') ? ' (secret leak)' : ''}`);
        if (this.m.swarmOnset != null && near(this.m.swarmOnset)) items.push('swarm onset');
        if (items.length) { this.tip.show(ev.clientX, ev.clientY, h('div', null, h('span', { class: 'v' }, clockT(t))), ...items.slice(0, 8).map((s) => h('div', { class: 'k' }, s))); return; }
      }
      this.tip.show(ev.clientX, ev.clientY, h('span', { class: 'v' }, clockT(t)));
    });
    this.c.addEventListener('pointerup', () => { dragging = false; });
    this.c.addEventListener('pointerleave', () => this.tip.hide());
  }
}
