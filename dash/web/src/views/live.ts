// Live feed (CONTRACT 6.2.4): SSE stream (auto-reconnect with Last-Event-ID) seeded from
// /api/live/recent; per-run cards with a 1 s-binned activity strip per agent (last 10 min, Canvas)
// and the current command per agent; kind/run filter chips, pause and auto-scroll toggles.
import { api, recentItems } from '../api';
import { FEED_KINDS, feedItem, feedKind, HIGHLIGHT, kindColor } from '../components/feed';
import { fitCanvas, h, onResize, replace } from '../dom';
import { agentColor, cv, onThemeChange, stepColor } from '../lib/colors';
import { clockT, trunc } from '../lib/format';
import type { LiveMessage, RunSummary, TimelineEvent } from '../lib/types';
import { go, hrefFor, type Route } from '../router';
import { live } from '../sse';
import { app } from '../store';
import { goalBadge, statusPill } from './runs';
import type { View } from './view';

const WINDOW_S = 600;
interface RunCard {
  run_id: string; iface: string; n: number; status: string; head: number;
  marks: Map<number, { t: number; cls: 'cmd' | 'read' | 'write' | 'submit' | 'idle' | 'model' }[]>;
  cmd: Map<number, { t: number; label: string }>;
  /** per-agent lifecycle from end / crash / solve events */
  state: Map<number, { ended?: 'done' | 'crashed'; solved?: boolean }>;
  el: HTMLElement; canvas: HTMLCanvasElement; rows: HTMLElement; pill: HTMLElement; W: number;
}

export function liveView(): View {
  const root = h('div');
  const feedEl = h('div', { class: 'feed', 'aria-live': 'polite' });
  const cardsEl = h('div', { class: 'runcards' });
  const chipsEl = h('div', { class: 'facetbar' });
  const statusEl = h('span', { class: 'muted' });
  const items: LiveMessage[] = [];
  const seen = new Set<number>();
  const cards = new Map<string, RunCard>();
  let kinds = new Set<string>(FEED_KINDS.filter((k) => HIGHLIGHT.has(k) || k === 'lifecycle'));
  let runFilter: Set<string> | null = null;
  let paused = false, autoscroll = true;
  let unsub: (() => void) | null = null;
  let redrawIv = 0;
  let feedTimer = 0;
  const cleanups: (() => void)[] = [];

  function passes(m: LiveMessage) {
    const rid = String(m.data.run_id ?? '');
    if (runFilter && !runFilter.has(rid)) return false;
    return kinds.has(feedKind(m));
  }
  function open(runId: string, t: number | null) { if (runId) go('run', runId, t != null ? { t: t.toFixed(1) } : undefined); }

  /** Arrival order across runs, but run time (data.t) order within each run: each run's items are re-sorted
   *  by t into the slots that run occupies (batches from the live tap / end-of-run copy arrive out of order). */
  function runTimeOrder(list: LiveMessage[]): LiveMessage[] {
    const slots = new Map<string, number[]>();
    list.forEach((m, i) => { const r = String(m.data.run_id ?? ''); const a = slots.get(r) ?? []; a.push(i); slots.set(r, a); });
    const out = list.slice();
    for (const [r, idx] of slots) {
      if (!r) continue;
      let last = -Infinity;
      const keyed = idx.map((i, k) => { const t = list[i].data.t; const tk = typeof t === 'number' ? t : last; last = Math.max(last, tk); return { m: list[i], tk, k }; });
      keyed.sort((a, b) => a.tk - b.tk || a.k - b.k);
      idx.forEach((i, k) => { out[i] = keyed[k].m; });
    }
    return out;
  }

  function renderFeed() {
    const vis = runTimeOrder(items.filter(passes).slice(-400)).reverse();
    replace(feedEl, ...(vis.length ? vis.map((m) => feedItem(m, open)) : [h('div', { class: 'empty' }, 'Waiting for swarm events…')]));
  }

  function renderChips() {
    const runIds = [...cards.keys()];
    replace(chipsEl,
      h('div', { class: 'facet' }, h('span', { class: 'k' }, 'kinds'), ...FEED_KINDS.map((k) => h('button', { class: 'chip' + (kinds.has(k) ? ' on' : ''), 'aria-pressed': String(kinds.has(k)), onclick: () => { kinds.has(k) ? kinds.delete(k) : kinds.add(k); kinds = new Set(kinds); renderChips(); renderFeed(); } },
        h('i', { class: 'sw', style: { background: kindColor(k), borderRadius: '50%' } }), k.replace('_', ' ')))),
      runIds.length > 1 ? h('div', { class: 'facet' }, h('span', { class: 'k' }, 'runs'), ...runIds.map((r) => h('button', { class: 'chip' + (runFilter?.has(r) ? ' on' : ''), onclick: () => { runFilter ??= new Set(); runFilter.has(r) ? runFilter.delete(r) : runFilter.add(r); if (!runFilter.size) runFilter = null; renderChips(); renderFeed(); } }, r))) : null,
      h('label', { class: 'facet' }, h('input', { type: 'checkbox', checked: paused, onchange: () => { paused = !paused; if (!paused) renderFeed(); } }), h('span', { class: 'secondary' }, 'pause')),
      h('label', { class: 'facet' }, h('input', { type: 'checkbox', checked: autoscroll, onchange: () => { autoscroll = !autoscroll; } }), h('span', { class: 'secondary' }, 'auto-scroll')));
  }

  function card(runId: string, meta?: Partial<RunSummary>): RunCard {
    let c = cards.get(runId);
    if (c) return c;
    const canvas = h('canvas');
    const rows = h('div');
    const pill = h('span');
    const el = h('div', { class: 'card runcard' },
      h('header', null, h('a', { href: hrefFor('run', runId) }, h('h2', null, runId)), pill, meta?.goal === 'unsolvable' ? goalBadge(meta.goal) : null, h('span', { class: 'sub' }, `${meta?.iface ?? ''} · ${meta?.n_agents ?? '?'} agents · ${meta?.model ?? ''}`), h('span', { class: 'grow' }), h('a', { href: hrefFor('run', runId), class: 'muted' }, 'open →')),
      h('div', { class: 'lanes-mini' }, canvas), rows);
    c = { run_id: runId, iface: meta?.iface ?? '', n: meta?.n_agents ?? 0, status: meta?.status ?? 'live', head: 0, marks: new Map(), cmd: new Map(), state: new Map(), el, canvas, rows, pill, W: 600 };
    replace(pill, statusPill(c.status));
    cleanups.push(onResize(el, (w) => { c!.W = Math.max(200, w - 24); drawCard(c!); }));
    cards.set(runId, c);
    cardsEl.appendChild(el);
    renderChips();
    return c;
  }

  function addEvent(c: RunCard, e: TimelineEvent) {
    if (e.lane == null || e.lane < 0) { c.head = Math.max(c.head, e.t); return; }
    c.head = Math.max(c.head, e.t);
    let cls: 'cmd' | 'read' | 'write' | 'submit' | 'idle' | 'model' | null = null;
    if (e.kind === 'step') cls = e.cat === 'submit' ? 'submit' : e.is_board || e.cat === 'board' ? (/-X\s*(POST|PUT)|PRIVMSG|append=|-d\s/.test(e.label) ? 'write' : 'read') : 'cmd';
    else if (e.kind === 'idle') cls = 'idle';
    else if (e.kind === 'post' || e.kind === 'dm' || e.kind === 'upvote') cls = 'write';
    else if (e.kind === 'model') cls = 'model';
    if (cls) { const a = c.marks.get(e.lane) ?? []; a.push({ t: e.t, cls }); c.marks.set(e.lane, a); }
    if (e.kind === 'step' || e.kind === 'idle') c.cmd.set(e.lane, { t: e.t, label: e.label || '(idle)' });
    if (e.kind === 'end' || e.kind === 'crash' || e.kind === 'solve') {
      const st = c.state.get(e.lane) ?? {};
      if (e.kind === 'solve') st.solved = true; else st.ended = e.kind === 'crash' ? 'crashed' : 'done';
      c.state.set(e.lane, st);
    }
    c.n = Math.max(c.n, e.lane + 1);
  }

  function drawCard(c: RunCard) {
    const laneH = 12, W = c.W;
    const n = Math.max(1, c.n);
    const H = n * laneH + 14;
    const ctx = fitCanvas(c.canvas, W, H);
    ctx.clearRect(0, 0, W, H);
    const t0 = Math.max(Math.min(0, c.head), c.head - WINDOW_S), t1 = Math.max(c.head, t0 + 60), span = t1 - t0;
    const LW = 28;
    const x = (t: number) => LW + ((t - t0) / span) * (W - LW - 4);
    ctx.font = '10px system-ui'; ctx.textBaseline = 'middle';
    ctx.strokeStyle = cv('--grid'); ctx.fillStyle = cv('--text-muted');
    const gstep = span > 300 ? 60 : span > 120 ? 30 : 10;
    for (let m = Math.ceil(t0 / gstep) * gstep; m <= t1; m += gstep) { const xx = Math.round(x(m)) + 0.5; ctx.beginPath(); ctx.moveTo(xx, 0); ctx.lineTo(xx, H - 12); ctx.stroke(); ctx.textAlign = 'center'; ctx.fillText(clockT(m).replace(/\.0$/, ''), xx, H - 5); }
    for (let i = 0; i < n; i++) {
      const y = i * laneH;
      ctx.globalAlpha = c.state.get(i)?.ended ? 0.45 : 1; // finished agents are dimmed
      ctx.fillStyle = agentColor(i); ctx.beginPath(); ctx.arc(7, y + laneH / 2, 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = cv('--text-secondary'); ctx.textAlign = 'left'; ctx.fillText('a' + i, 13, y + laneH / 2);
      // 1 s bins: last mark in a bin wins
      const bins = new Map<number, string>();
      for (const mk of c.marks.get(i) ?? []) { if (mk.t < t0) continue; const b = Math.floor(mk.t); const prev = bins.get(b); if (!prev || prev === 'model' || prev === 'cmd') bins.set(b, mk.cls); }
      const bw = Math.max(1.5, (W - LW - 4) / span);
      for (const [b, cls] of bins) {
        if (cls === 'model') { ctx.fillStyle = cv('--axis'); ctx.fillRect(x(b), y + 2, 1, laneH - 4); continue; }
        if (cls === 'idle') { ctx.strokeStyle = cv('--text-muted'); ctx.strokeRect(x(b) + 0.5, y + 3.5, Math.max(2, bw), laneH - 7); continue; }
        ctx.fillStyle = stepColor(cls as 'cmd'); ctx.fillRect(x(b), y + 2, Math.max(2, bw), laneH - 4);
      }
      ctx.globalAlpha = 1;
    }
    // rows: current command per agent
    replace(c.rows, ...Array.from({ length: n }, (_, i) => {
      const cm = c.cmd.get(i);
      const st = c.state.get(i);
      const tag = st?.ended || st?.solved ? h('span', { class: 'flag' + (st.ended === 'crashed' ? ' secret_leak' : st.solved ? ' correct' : '') }, [st.solved ? 'solved' : '', st.ended ?? ''].filter(Boolean).join(' · ')) : null;
      return h('div', { class: 'agentrow', style: st?.ended ? { opacity: '0.6' } : {} }, h('span', { class: 'kindtag' }, h('i', { style: { background: agentColor(i) } }), 'a' + i), h('span', { class: 'cmd', title: cm?.label ?? '' }, tag, tag ? ' ' : null, st?.ended ? (cm ? 'last: ' + trunc(cm.label, 160) : '') : cm ? '$ ' + trunc(cm.label, 200) : '—'), h('span', { class: 'muted tnum', style: { textAlign: 'right' } }, cm ? clockT(cm.t) : ''));
    }));
  }

  function onMsg(m: LiveMessage, initial = false) {
    // reset / reconnect: ids from before may be reused or skipped, so forget them before the duplicate check
    if (m.event === 'reset') { items.length = 0; seen.clear(); renderFeed(); return; }
    if (m.event === 'reconnected') { seen.clear(); return; }
    if (m.id != null) { if (seen.has(m.id)) return; seen.add(m.id); }
    const d = m.data as Record<string, unknown>;
    const rid = typeof d.run_id === 'string' ? d.run_id : '';
    if (rid) {
      const c = card(rid, m.event === 'run_started' ? { iface: String(d.iface ?? ''), n_agents: Number(d.n_agents ?? 0), model: String(d.model ?? ''), goal: d.goal === 'unsolvable' ? 'unsolvable' : 'solvable' } : undefined);
      if (m.event === 'event') addEvent(c, d as unknown as TimelineEvent);
      if (m.event === 'run_updated' || m.event === 'run_finished') { c.status = String(d.status ?? c.status); replace(c.pill, statusPill(c.status)); }
    }
    items.push(m);
    if (items.length > 5000) items.splice(0, items.length - 5000);
    if (!initial && !paused && passes(m) && !feedTimer) {
      // re-render (sorted by run time within each run) at most every 250 ms
      feedTimer = window.setTimeout(() => { feedTimer = 0; renderFeed(); if (autoscroll) feedEl.scrollTop = 0; }, 250);
    }
  }

  async function init() {
    // live runs first (history for the mini lanes comes from their timelines)
    try {
      const rs = await api.runs({ limit: 200, status: 'live,collecting', include_tagged: 1 });
      for (const r of rs.runs) {
        const c = card(r.run_id, r);
        api.timeline(r.run_id).then((tl) => { for (const e of tl.events) addEvent(c, e); drawCard(c); }).catch(() => {});
      }
    } catch { /* ignore */ }
    try { for (const m of recentItems(await api.liveRecent({ limit: 500 }))) onMsg(m, true); } catch { /* ignore */ }
    renderFeed();
    if (!cards.size) cardsEl.appendChild(h('div', { class: 'card empty' }, 'No live runs right now. Cards appear when the live tap sees agent VMs (or when a run_started event arrives).'));
    unsub = live.subscribe((m) => onMsg(m));
  }

  return {
    el: root,
    mount: (r: Route) => {
      const rr = r.params.get('run'); if (rr) runFilter = new Set(rr.split(','));
      renderChips();
      replace(root,
        h('div', { class: 'view-title' }, h('h1', null, 'Live swarm feed'), statusEl, h('span', { class: 'grow' }), h('span', { class: 'muted', style: { fontSize: '11px' } }, 'data arrives from the live tap (when enabled, ~5 s) or at run end')),
        chipsEl,
        h('div', { class: 'live-grid' }, cardsEl, h('div', { class: 'card' }, h('header', null, h('h2', null, 'Events'), h('span', { class: 'sub' }, 'newest first · click to open the run at that moment')), feedEl)));
      const upd = () => { statusEl.textContent = app.get().liveConnected ? '● connected' : live.status === 'retrying' ? 'reconnecting…' : 'connecting…'; };
      cleanups.push(app.sub(upd)); upd();
      cleanups.push(onThemeChange(() => cards.forEach(drawCard)));
      redrawIv = window.setInterval(() => cards.forEach(drawCard), 1000);
      void init();
    },
    update: () => true,
    onTheme: () => { renderChips(); renderFeed(); cards.forEach(drawCard); },
    destroy: () => { unsub?.(); clearInterval(redrawIv); clearTimeout(feedTimer); cleanups.forEach((f) => f()); },
  };
}
