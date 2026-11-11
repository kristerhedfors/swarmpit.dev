import { RATES } from '../types.js';
import type { ChapterRec, PlaybackRate, RunModel, TimelineBar, TimelineOptions } from '../types.js';
import { h, text, toggle, agentVar } from '../util/dom.js';
import { fmtT, trunc } from '../util/format.js';
import { ICON } from './icons.js';

export type ToggleName = 'filters' | 'labels' | 'captions' | 'fullscreen' | 'resetView' | 'mode' | 'feed' | 'narration';
export interface TimelineOpts extends TimelineOptions {
  onFeed: () => void;
  coarse: boolean;
}
export interface Timeline extends TimelineBar {
  setToggle(name: ToggleName, on: boolean): void;
  setBadge(n: number): void;
  setRange(r: [number, number] | null): void;
  busy(): boolean;
  closeMenu(): boolean;
  /** Narration button visibility (hidden unless the run has a valid narration manifest). */
  setNarrationAvailable(v: boolean): void;
  /** While narrating, speed and skip-quiet are set by the narration track and disabled here. */
  setNarrating(on: boolean): void;
}

const KIND_COLOR: Partial<Record<ChapterRec['k'], string>> = {
  leak: 'var(--leak)', solve: 'var(--series-3)', phaseFree: 'var(--series-3)', onset: 'var(--live-live)',
  crash: 'var(--status-critical)', end: 'var(--text-muted)', boot: 'var(--text-muted)',
};
const IFACE_SLOT: Record<string, number> = { forum: 1, wiki: 2, cache: 3, irc: 4 };

function btn(icon: string, label: string, cls = ''): HTMLButtonElement {
  const b = h('button.tl-btn', { type: 'button', 'aria-label': label, title: label, html: icon });
  if (cls) b.classList.add(...cls.split(' '));
  return b;
}

export function createTimeline(o: TimelineOpts): Timeline {
  const { clock } = o;
  let m: RunModel | null = null;
  let dur = 1;
  let range: [number, number] | null = null;
  let dragging = false, brushing = false, brushT0 = 0, menuOpen = false;
  let lastT = -1, lastSec = -1, lastSeg = -1;
  let markerData: { ch: ChapterRec; el: HTMLButtonElement }[] = [];
  let narrating = false;

  // --- track
  const heat = h('canvas.tl-heat', { 'aria-hidden': 'true' });
  const act = h('canvas.tl-act', { 'aria-hidden': 'true' });
  const buf = h('div.tl-fill.tl-buf');
  const hov = h('div.tl-fill.tl-hov');
  const prog = h('div.tl-fill.tl-prog');
  const layers = h('div.tl-layers', null, h('div.tl-fill.tl-rail'), buf, hov, prog);
  const markers = h('div.tl-markers');
  const thumb = h('div.tl-thumb');
  const brushX = h('button.tl-brush-x', { type: 'button', 'aria-label': 'Clear time range', html: ICON.close });
  const brush = h('div.tl-brush', { hidden: true }, brushX);
  const track = h('div.tl-track', {
    role: 'slider', tabindex: '0', 'aria-label': 'Seek', 'aria-valuemin': '0', 'aria-valuemax': '0', 'aria-valuenow': '0',
    'aria-valuetext': '0:00.0',
  }, layers, brush, markers, thumb);
  const pvTime = h('span.pv-time');
  const pvSeg = h('span.pv-seg');
  const pvEv = h('div.pv-ev');
  const preview = h('div.tl-preview', { 'aria-hidden': 'true', hidden: true }, h('div.pv-head', null, pvTime, pvSeg), pvEv);
  const trackWrap = h('div.tl-trackwrap', null, heat, act, track, preview);

  // --- controls row
  const bPlay = btn(ICON.play, 'Play (k)', 'tl-play');
  bPlay.setAttribute('aria-pressed', 'false');
  const bPrev = btn(ICON.prev, 'Previous event (,)');
  const bNext = btn(ICON.next, 'Next event (.)');
  const tCur = h('span.tl-cur', null, '0:00.0');
  const tDur = h('span.tl-dur', null, '0:00.0');
  const timeEl = h('span.tl-time', { 'aria-hidden': 'true' }, tCur, h('span.tl-sep', null, ' / '), tDur);
  const segTitle = h('span.tl-segtitle');
  const skipChip = h('span.tl-skip', { hidden: true, title: 'Skipping quiet stretch' }, h('span', { html: ICON.skip }), 'skip');
  const bSpeed = h('button.tl-btn.tl-speed', { type: 'button', 'aria-label': 'Playback speed', 'aria-haspopup': 'menu', 'aria-expanded': 'false', title: 'Playback speed (< >)' }, '1×');
  const rateItems = RATES.map((r) => h('button.mi', { type: 'button', role: 'menuitemradio', 'aria-checked': 'false', 'data-rate': String(r), tabindex: '-1' }, r === 1 ? 'Normal' : `${r}×`));
  const skipItem = h('button.mi.mi-skip', { type: 'button', role: 'menuitemcheckbox', 'aria-checked': 'false', tabindex: '-1' }, 'Skip quiet stretches');
  const menu = h('div.tl-menu', { role: 'menu', 'aria-label': 'Playback speed', hidden: true },
    h('div.mi-title', { 'aria-hidden': 'true' }, 'Playback speed'), rateItems, h('hr'), skipItem);
  const bNarr = btn(ICON.headphones, 'Narration (n)', 'tl-tog');
  bNarr.hidden = true;
  const bCC = btn(ICON.cc, 'Captions (c)', 'tl-tog');
  const bLabels = btn(ICON.tag, 'Labels (t)', 'tl-tog');
  const bMode = btn(ICON.worldline, 'Worldline view (v)', 'tl-tog');
  const bReset = btn(ICON.reset, 'Reset view (r)');
  const badge = h('span.tl-badge', { hidden: true });
  const bFilters = btn(ICON.filter, 'Filters', 'tl-tog');
  bFilters.appendChild(badge);
  const bFeed = btn(ICON.feed, 'Event feed', 'tl-tog');
  const bFull = btn(ICON.full, 'Fullscreen (f)');
  const toggles: Record<ToggleName, HTMLButtonElement> = { filters: bFilters, labels: bLabels, captions: bCC, fullscreen: bFull, resetView: bReset, mode: bMode, feed: bFeed, narration: bNarr };
  for (const k of ['filters', 'labels', 'captions', 'mode', 'feed', 'narration'] as const) toggles[k].setAttribute('aria-pressed', 'false');

  const row = h('div.tl-row', null, bPlay, bPrev, bNext, timeEl, segTitle, h('span.tl-spacer'), skipChip,
    h('div.tl-speedwrap', null, bSpeed, menu), bNarr, bCC, bLabels, bMode, bReset, bFilters, bFeed, bFull);
  const el = h('div.tl', { role: 'group', 'aria-label': 'Playback controls' }, trackWrap, row);

  // --- geometry helpers
  const rectW = (): DOMRect => track.getBoundingClientRect();
  const xToT = (clientX: number): number => {
    const r = rectW();
    return Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width))) * dur;
  };
  const pctOf = (t: number): number => Math.max(0, Math.min(100, (t / dur) * 100));

  // --- segments: gaps via CSS mask on the fill layers
  function buildSegments(): void {
    if (!m) return;
    const segs = m.core.segments;
    if (segs.length < 2) { layers.style.removeProperty('mask-image'); layers.style.removeProperty('-webkit-mask-image'); return; }
    const stops: string[] = [];
    segs.forEach((s, i) => {
      if (i === 0) return;
      const p = pctOf(s.t0).toFixed(3);
      stops.push(`#000 calc(${p}% - 1px)`, `transparent calc(${p}% - 1px)`, `transparent calc(${p}% + 1px)`, `#000 calc(${p}% + 1px)`);
    });
    const g = `linear-gradient(90deg, #000 0, ${stops.join(', ')}, #000 100%)`;
    layers.style.setProperty('mask-image', g);
    layers.style.setProperty('-webkit-mask-image', g);
  }

  function markerColor(ch: ChapterRec): string {
    if (ch.k === 'firstContact' || ch.k === 'contact') return `var(--series-${IFACE_SLOT[m?.card.iface ?? 'forum'] ?? 1})`;
    if (ch.k === 'post' || ch.k === 'dm' || ch.k === 'upvote') return ch.agent !== undefined ? agentVar(ch.agent) : 'var(--t2)';
    return KIND_COLOR[ch.k] ?? 'var(--t2)';
  }

  function buildMarkers(): void {
    markers.textContent = '';
    markerData = [];
    if (!m) return;
    const chs = m.core.chapters.filter((c) => c.p <= 2);
    // Merge markers within 1% of the width; keep the higher priority one.
    const merged: { ch: ChapterRec; n: number }[] = [];
    for (const ch of chs) {
      const last = merged[merged.length - 1];
      if (last && ch.t - last.ch.t < dur * 0.01) {
        last.n += ch.merged ?? 1;
        if (ch.p < last.ch.p) last.ch = ch;
      } else merged.push({ ch, n: ch.merged ?? 1 });
    }
    for (const { ch, n } of merged) {
      // ch.label already counts the exporter's own merge ("(+N more)"); only mention what we merged on top.
      const extra = n - (ch.merged ?? 1);
      const b = h('button.tl-marker', {
        type: 'button', tabindex: '-1', 'aria-label': `${fmtT(ch.t)} ${ch.label}${extra > 0 ? ` (+${extra} nearby)` : ''}`,
        'data-k': ch.k, style: `left:${pctOf(ch.t)}%;--mc:${markerColor(ch)}`,
      });
      if (ch.p === 1) b.classList.add('p1');
      b.addEventListener('pointerenter', () => o.onMarkerHover(ch));
      b.addEventListener('pointerleave', () => o.onMarkerHover(null));
      // Clicking a marker lands exactly on the chapter (not on the pointer x, which can be ~11 px off).
      b.addEventListener('pointerdown', (e) => {
        if (e.button > 0) return;
        e.stopPropagation(); e.preventDefault();
        track.focus({ preventScroll: true });
        clock.seek(ch.t, 'marker');
      });
      markers.appendChild(b);
      markerData.push({ ch, el: b });
    }
    refreshMarkers();
  }

  function refreshMarkers(): void {
    for (const { ch, el: b } of markerData) toggle(b, 'off', ch.ev !== undefined && ch.ev >= 0 && !o.isEventVisible(ch.ev));
  }

  function sizeCanvas(c: HTMLCanvasElement): CanvasRenderingContext2D | null {
    const r = c.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width * dpr)), hh = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
    const g = c.getContext('2d');
    g?.clearRect(0, 0, w, hh);
    return g;
  }

  function drawHeat(): void {
    const g = sizeCanvas(heat);
    if (!g || !m) return;
    const d = m.core.series.density, bin = m.core.series.binMs || 1000;
    if (!d.length) return;
    const W = heat.width, H = heat.height;
    // Resample to ~1 sample per 3 css px with a small box blur.
    const n = Math.max(8, Math.min(240, Math.round(W / 6)));
    const vals = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const t0 = (i / n) * dur, t1 = ((i + 1) / n) * dur;
      let s = 0, c = 0;
      for (let b = Math.floor(t0 / bin); b <= Math.min(d.length - 1, Math.floor(t1 / bin)); b++) { s += d[b] ?? 0; c++; }
      vals[i] = c ? s / c : 0;
    }
    const sm = vals.map((_, i) => ((vals[i - 1] ?? vals[i]!) + 2 * vals[i]! + (vals[i + 1] ?? vals[i]!)) / 4);
    const max = Math.max(1e-9, ...sm);
    g.beginPath();
    g.moveTo(0, H);
    sm.forEach((v, i) => g.lineTo(((i + 0.5) / n) * W, H - (v / max) * (H - 2)));
    g.lineTo(W, H);
    g.closePath();
    g.fillStyle = getComputedStyle(el).getPropertyValue('--heat').trim() || 'rgba(255,255,255,.18)';
    g.fill();
  }

  let actTicks: Float64Array | null = null, actColor = '';
  function drawActivity(): void {
    const g = sizeCanvas(act);
    if (!g || !actTicks) return;
    g.fillStyle = actColor;
    const W = act.width, H = act.height, dpr = W / Math.max(1, act.getBoundingClientRect().width);
    for (const t of actTicks) g.fillRect(Math.round((t / dur) * W), 0, Math.max(1, Math.round(2 * dpr)), H);
  }

  // --- preview
  function nearestEvent(t: number, px: number): number {
    if (!m) return -1;
    const span = (px / Math.max(1, rectW().width)) * dur;
    const lo = m.eventAt(t - span - 1) + 1, hi = m.eventAt(t + span);
    let best = -1, bd = Infinity;
    for (let i = Math.max(0, lo); i <= hi; i++) {
      if (!o.isEventVisible(i)) continue;
      const d = Math.abs(m.evT[i]! - t);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
  function segAt(t: number): number {
    if (!m) return -1;
    const s = m.core.segments;
    for (let i = s.length - 1; i >= 0; i--) if (t >= s[i]!.t0) return i;
    return -1;
  }
  function showPreview(clientX: number): void {
    if (!m) return;
    const r = rectW();
    const t = xToT(clientX);
    const x = Math.max(0, Math.min(r.width, clientX - r.left));
    hov.style.transform = `scaleX(${x / Math.max(1, r.width)})`;
    text(pvTime, fmtT(t));
    const si = segAt(t);
    text(pvSeg, si >= 0 ? m.core.segments[si]!.title : '');
    const i = nearestEvent(t, 8);
    pvEv.textContent = '';
    if (i >= 0) {
      const d = m.describe(i);
      pvEv.append(h('span.pv-icon', { style: `color:${agentVar(d.agent)}` }, d.icon), h('span.pv-chip', { style: `background:${agentVar(d.agent)}` }),
        h('span.pv-at', null, fmtT(m.evT[i]!)), ' ', trunc(d.short, 90));
    } else {
      let best: ChapterRec | null = null;
      for (const c of m.core.chapters) if (c.p <= 2 && (!best || Math.abs(c.t - t) < Math.abs(best.t - t))) best = c;
      if (best && Math.abs(best.t - t) < dur * 0.04) pvEv.append(h('span.pv-icon', { style: `color:${markerColor(best)}` }, '◆'), ' ', best.label);
    }
    preview.hidden = false;
    const pw = preview.offsetWidth || 220;
    preview.style.transform = `translateX(${Math.max(0, Math.min(r.width - pw, x - pw / 2))}px)`;
  }
  function hidePreview(): void {
    if (dragging || brushing) return;
    preview.hidden = true;
    hov.style.transform = 'scaleX(0)';
  }

  // --- scrub / brush
  let pendX: number | null = null, raf = 0;
  const flushMove = (): void => {
    raf = 0;
    if (pendX === null) return;
    const x = pendX; pendX = null;
    if (brushing) { const t = xToT(x); setBrush([Math.min(brushT0, t), Math.max(brushT0, t)]); }
    else if (dragging) clock.seek(xToT(x), 'scrub');
    showPreview(x);
  };
  track.addEventListener('pointerdown', (e) => {
    if (!m || e.button > 0) return;
    if ((e.target as Element).closest('.tl-brush-x')) return;
    track.setPointerCapture(e.pointerId);
    track.focus({ preventScroll: true });
    if (e.shiftKey) { brushing = true; brushT0 = xToT(e.clientX); setBrush([brushT0, brushT0]); }
    else { dragging = true; el.classList.add('dragging'); clock.beginScrub(); clock.seek(xToT(e.clientX), 'scrub'); }
    showPreview(e.clientX);
    e.preventDefault();
  });
  track.addEventListener('pointermove', (e) => {
    pendX = e.clientX;
    if (!raf) raf = requestAnimationFrame(flushMove);
  });
  const endDrag = (e: PointerEvent): void => {
    if (brushing) {
      brushing = false;
      if (range && range[1] - range[0] < dur * 0.005) setBrush(null);
      o.onRangeSelect(range);
    }
    if (dragging) {
      dragging = false;
      el.classList.remove('dragging');
      if (e.type === 'pointerup') clock.seek(xToT(e.clientX), 'scrub');
      clock.endScrub();
    }
    if (e.pointerType !== 'mouse') hidePreview();
  };
  track.addEventListener('pointerup', endDrag);
  track.addEventListener('pointercancel', endDrag);
  track.addEventListener('pointerleave', () => { if (!dragging && !brushing) hidePreview(); });
  brushX.addEventListener('click', (e) => { e.stopPropagation(); setBrush(null); o.onRangeSelect(null); });

  function setBrush(r: [number, number] | null): void {
    range = r;
    brush.hidden = !r;
    if (r) { brush.style.left = pctOf(r[0]) + '%'; brush.style.width = Math.max(0.2, pctOf(r[1]) - pctOf(r[0])) + '%'; }
  }

  track.addEventListener('keydown', (e) => {
    let d = 0;
    // Left/Right match the global ±5 s (YouTube); Up/Down and Shift+Left/Right are a fine ±1 s step.
    if (e.key === 'ArrowLeft') d = e.shiftKey ? -1000 : -5000;
    else if (e.key === 'ArrowRight') d = e.shiftKey ? 1000 : 5000;
    else if (e.key === 'ArrowDown') d = -1000;
    else if (e.key === 'ArrowUp') d = 1000;
    else if (e.key === 'PageUp') d = 10000;
    else if (e.key === 'PageDown') d = -10000;
    if (d) { e.preventDefault(); e.stopPropagation(); clock.seekBy(d, 'key'); }
  });

  // --- buttons
  bPlay.addEventListener('click', () => clock.toggle());
  bPrev.addEventListener('click', () => clock.stepEvent(-1, o.isEventVisible));
  bNext.addEventListener('click', () => clock.stepEvent(1, o.isEventVisible));
  bNarr.addEventListener('click', () => o.onToggle('narration'));
  bCC.addEventListener('click', () => o.onToggle('captions'));
  bLabels.addEventListener('click', () => o.onToggle('labels'));
  bMode.addEventListener('click', () => o.onToggle('mode'));
  bReset.addEventListener('click', () => o.onToggle('resetView'));
  bFilters.addEventListener('click', () => o.onToggle('filters'));
  bFull.addEventListener('click', () => o.onToggle('fullscreen'));
  bFeed.addEventListener('click', () => o.onFeed());

  // --- speed menu
  const items = (): HTMLButtonElement[] => [...rateItems, skipItem];
  function openMenu(focusFirst: boolean): void {
    menuOpen = true; menu.hidden = false; bSpeed.setAttribute('aria-expanded', 'true');
    el.classList.add('menu-open');
    const cur = rateItems.find((b) => b.getAttribute('aria-checked') === 'true') ?? rateItems[3]!;
    if (focusFirst) cur.focus();
  }
  function closeMenu(): boolean {
    if (!menuOpen) return false;
    menuOpen = false; menu.hidden = true; bSpeed.setAttribute('aria-expanded', 'false');
    el.classList.remove('menu-open');
    return true;
  }
  bSpeed.addEventListener('click', (e) => { if (narrating) return; if (menuOpen) closeMenu(); else openMenu(e.detail === 0); });
  bSpeed.addEventListener('keydown', (e) => { if (narrating) return; if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); openMenu(true); } });
  menu.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>('.mi');
    if (!b || narrating) return;
    if (b === skipItem) clock.setSkipQuiet(!clock.skipQuiet);
    else { clock.setRate(Number(b.dataset['rate']) as PlaybackRate); closeMenu(); bSpeed.focus(); }
  });
  menu.addEventListener('keydown', (e) => {
    const list = items(), i = list.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation();
      list[(i + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length]!.focus();
    } else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); bSpeed.focus(); }
    else if (e.key === 'Tab') closeMenu();
    else if (e.key === ' ' || e.key === 'Enter') e.stopPropagation();
  });
  document.addEventListener('pointerdown', (e) => { if (menuOpen && !(e.target as Element).closest('.tl-speedwrap')) closeMenu(); });

  clock.on('state', (s) => {
    const ended = !s.playing && m !== null && clock.t >= dur;
    bPlay.innerHTML = s.playing ? ICON.pause : ended ? ICON.replay : ICON.play;
    const lab = s.playing ? 'Pause (k)' : ended ? 'Replay' : 'Play (k)';
    bPlay.setAttribute('aria-label', lab); bPlay.title = lab;
    bPlay.setAttribute('aria-pressed', String(s.playing));
    el.classList.toggle('playing', s.playing);
    text(bSpeed, s.rate === 1 ? '1×' : `${s.rate}×`);
    for (const b of rateItems) b.setAttribute('aria-checked', String(Number(b.dataset['rate']) === s.rate));
    skipItem.setAttribute('aria-checked', String(s.skipQuiet));
    skipChip.hidden = !s.skipping || narrating;
  });

  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { drawHeat(); drawActivity(); }) : null;
  ro?.observe(track);

  const api: Timeline = {
    el,
    setModel(model) {
      m = model;
      dur = Math.max(1, model.tEnd);
      track.setAttribute('aria-valuemax', String(Math.round(dur / 1000)));
      text(tDur, fmtT(dur));
      setBrush(null);
      api.setBuffered(0);
      actTicks = null;
      lastT = lastSec = lastSeg = -1;
      buildSegments();
      buildMarkers();
      requestAnimationFrame(() => { drawHeat(); drawActivity(); });
      api.render(clock.t);
    },
    setBuffered(f) { buf.style.transform = `scaleX(${Math.max(0, Math.min(1, f))})`; },
    setNodeActivity(ticks, color) { actTicks = ticks; actColor = color; el.classList.toggle('has-act', !!ticks); drawActivity(); },
    setControlsVisible(v) { el.classList.toggle('hidden-controls', !v); },
    refreshMarkers,
    render(t) {
      if (t === lastT || !m) return;
      lastT = t;
      const f = t / dur;
      prog.style.transform = `scaleX(${f})`;
      thumb.style.left = (f * 100).toFixed(3) + '%';
      const sec = Math.floor(t / 100);
      if (sec !== lastSec) {
        lastSec = sec;
        text(tCur, fmtT(t));
        track.setAttribute('aria-valuenow', String(Math.round(t / 1000)));
        track.setAttribute('aria-valuetext', `${fmtT(t, false)} of ${fmtT(dur, false)}`);
      }
      const si = segAt(t);
      if (si !== lastSeg) { lastSeg = si; text(segTitle, si >= 0 ? m.core.segments[si]!.title : ''); }
    },
    setToggle(name, on) {
      const b = toggles[name];
      if (name === 'fullscreen') { b.innerHTML = on ? ICON.unfull : ICON.full; b.setAttribute('aria-label', on ? 'Exit fullscreen (f)' : 'Fullscreen (f)'); }
      else if (name === 'mode') { b.innerHTML = on ? ICON.cube : ICON.worldline; const l = on ? 'Constellation view (v)' : 'Worldline view (v)'; b.setAttribute('aria-label', l); b.title = l; b.setAttribute('aria-pressed', String(on)); }
      else b.setAttribute('aria-pressed', String(on));
      if (name === 'filters') b.appendChild(badge);
    },
    setBadge(n) { badge.hidden = n === 0; text(badge, String(n)); bFilters.setAttribute('aria-label', n ? `Filters (${n} active)` : 'Filters'); },
    setRange(r) { setBrush(r); },
    setNarrationAvailable(v) { bNarr.hidden = !v; if (!v) api.setNarrating(false); },
    setNarrating(on) {
      narrating = on;
      bNarr.setAttribute('aria-pressed', String(on));
      if (on) closeMenu();
      const lab = on ? 'Speed is set by narration' : 'Playback speed (< >)';
      bSpeed.title = lab;
      if (on) bSpeed.setAttribute('aria-disabled', 'true'); else bSpeed.removeAttribute('aria-disabled');
      if (on) skipItem.setAttribute('aria-disabled', 'true'); else skipItem.removeAttribute('aria-disabled');
      el.classList.toggle('narrating', on);
      if (on) skipChip.hidden = true;
    },
    // Focus alone does not block auto-hide (YouTube hides regardless); focusin re-shows the controls.
    busy: () => dragging || brushing || menuOpen,
    closeMenu,
    dispose() { ro?.disconnect(); el.remove(); },
  };
  return api;
}
