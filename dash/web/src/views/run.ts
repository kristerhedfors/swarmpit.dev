// Run detail (CONTRACT 6.2.3): trajectory lanes + time slider driving the board-state panel,
// interaction graph, agent status strip, cumulative charts and the step inspector. All scrubbing is
// client-side over the indexed /timeline response; live runs append SSE events incrementally.
import { api } from '../api';
import { BoardState } from '../components/boardState';
import { GraphView } from '../components/graph';
import { Lanes, type XMode } from '../components/lanes';
import { Slider } from '../components/slider';
import { CumChart, type CumSeries } from '../components/sparkline';
import { StepInspector } from '../components/stepInspector';
import { h, replace } from '../dom';
import { RunModel, type LaneIndex } from '../lib/board';
import { agentColor, agentDash, ARC_DASH, cv, setAgentFocus, STEP_CLASSES, stepColor } from '../lib/colors';
import { clockT, compact, dateTime, dur, noteText, num, pct, trunc } from '../lib/format';
import { clamp, lastLEnum } from '../lib/time';
import type { LiveMessage, TimelineEvent } from '../lib/types';
import { setViewKeys } from '../keys';
import { live } from '../sse';
import { hrefFor, replaceParams, type Route } from '../router';
import { goalBadge, statusPill } from './runs';
import type { View } from './view';

type Panel = 'board' | 'graph' | 'inspector';

export function runView(): View {
  const root = h('div');
  let runId = '';
  let model: RunModel | null = null;
  let t = 0;
  let selLane: number | null = null;
  let unsubLive: (() => void) | null = null;
  let destroyed = false;
  let urlTimer = 0;
  let liveTimer = 0;
  let lastGraphFetch = 0;
  let pendingLive: TimelineEvent[] = [];
  let following = false;
  let loading = false;
  let digitBuf = '';
  let digitTimer = 0;
  let paintLegend: (() => void) | null = null;
  const cleanups: (() => void)[] = [];

  // components (created per mount)
  let lanes: Lanes, slider: Slider, board: BoardState, graph: GraphView, insp: StepInspector;
  let charts: { share: CumChart; posts: CumChart; tokens: CumChart; edges: CumChart } | null = null;
  const stripEl = h('div', { class: 'agents-strip' });
  const panelBody = h('div', { class: 'panel' });
  const tabs = h('div', { class: 'tabs', role: 'tablist' });
  const totalsEl = h('div', { class: 'muted', style: { fontSize: '12px' } });
  let stripSig = '';

  function setT(nt: number, fromUser = true) {
    if (!model) return;
    t = clamp(nt, model.tMin, model.tMax);
    if (fromUser && following && t < model.tMax - 0.5) { following = false; slider.setFollow(false); }
    lanes.setCursor(t);
    slider.setT(t);
    board.update(t);
    graph.setT(t);
    renderStrip();
    if (charts) for (const c of Object.values(charts)) c.setCursor(t);
    if (selLane != null) syncInspector();
    const tot = model.totalsAt(t);
    totalsEl.textContent = `at ${clockT(t)}: ${tot.started}/${model.lanes.length} started · ${tot.discovered} discovered · ${tot.solved} solved · ${tot.posts} posts · ${tot.requests} requests · ${tot.live_live_edges} live-live edges`;
    clearTimeout(urlTimer);
    urlTimer = window.setTimeout(() => replaceParams({ t: t.toFixed(1) }), 250);
  }

  let inspKey = '';
  function syncInspector() {
    if (!model || selLane == null) return;
    const l = model.lane(selLane); if (!l) return;
    const idx = lastLEnum(l.stepT, t);
    const key = `${selLane}:${idx}`;
    if (key === inspKey) return;
    inspKey = key;
    lanes.setSelected(selLane, idx >= 0 ? idx : null);
    void insp.show(selLane, idx >= 0 ? idx : null);
    replaceParams({ agent: selLane, step: idx >= 0 ? (l.steps[idx].step ?? idx + 1) : null });
  }

  function selectAgent(lane: number | null, stepIdx: number | null = null) {
    selLane = lane;
    inspKey = '';
    if (model) { setAgentFocus(model.lanes.length, lane); if (model.lanes.length > 8 && charts) buildCharts(); }
    if (lane == null) { lanes.setSelected(null, null); replaceParams({ agent: null, step: null }); renderStrip(true); return; }
    const l = model?.lane(lane);
    if (l && stepIdx != null && l.steps[stepIdx]) {
      const s = l.steps[stepIdx];
      setPanel('inspector');
      setT(s.t_end != null && s.t_end > s.t ? s.t + (s.t_end - s.t) * 0.999 : s.t + 1e-6);
    } else syncInspector();
    renderStrip(true);
  }

  function setPanel(p: Panel) {
    for (const b of tabs.querySelectorAll('button')) b.classList.toggle('on', b.dataset.p === p);
    for (const b of tabs.querySelectorAll('button')) b.setAttribute('aria-selected', String(b.dataset.p === p));
    replace(panelBody, p === 'board' ? board.el : p === 'graph' ? graph.el : insp.el);
    replaceParams({ panel: p === 'board' ? null : p });
  }

  function renderStrip(force = false) {
    if (!model) return;
    const ats = model.lanes.map((l) => model!.agentAt(l, t));
    const sig = ats.map((a) => `${a.lane}:${a.status}:${a.phase}:${a.stepIdx}:${a.cum.posts}:${a.cum.requests}`).join('|') + selLane;
    if (sig === stripSig && !force) return;
    stripSig = sig;
    const budget = model.detail.run.harness?.budget;
    replace(stripEl, ...ats.map((a) => {
      const l = model!.lane(a.lane)!;
      return h('div', { class: 'agent-card' + (selLane === a.lane ? ' sel' : ''), onclick: () => selectAgent(selLane === a.lane ? null : a.lane), title: 'Select agent (digit keys by lane position; type two digits quickly for lanes 10+)' },
        h('div', { class: 'hd' }, h('i', { class: 'sw', style: { background: agentColor(a.lane) } }), h('b', null, l.label), h('span', { class: 'muted' }, trunc(l.names[0] ?? '', 18)), h('span', { class: 'grow' }),
          h('span', { class: 'pill ' + (a.status === 'running' ? 'live' : a.status === 'done' ? 'done' : a.status === 'not_started' ? 'pending' : 'errored') }, h('i', { class: 'dot' }), a.status.replace('_', ' ')),
          a.phase ? h('span', { class: 'flag' + (a.phase === 'free' ? ' correct' : '') }, a.phase) : null),
        h('div', { class: 'cmd', title: a.step?.label ?? '' }, a.step ? `#${a.step.step ?? a.stepIdx + 1}${budget ? '/' + budget : ''} $ ${a.step.label || '(idle)'}` : '—'),
        h('div', { class: 'ctrs' }, h('span', null, 'board ', h('b', null, String(a.cum.board_steps))), h('span', null, 'ep ', h('b', null, String(a.cum.episodes))), h('span', null, 'posts ', h('b', null, String(a.cum.posts))), h('span', null, 'req ', h('b', null, String(a.cum.requests))), h('span', null, 'share₅ ', h('b', null, a.share5 == null ? '–' : pct(a.share5)))));
    }));
  }

  // ------------------------------------------------------------------ charts
  function buildCharts() {
    if (!model) return;
    const xr = [model.tMin, model.tMax];
    const onClickX = (x: number) => setT(x);
    if (!charts) {
      charts = {
        share: new CumChart({ title: 'Board share, rolling 5 steps', unit: 'share', fmt: (v) => Math.round(v * 100) + '%', sync: 'run', onClickX }),
        posts: new CumChart({ title: 'Cumulative posts', unit: 'count', fmt: intFmt, sync: 'run', onClickX }),
        tokens: new CumChart({ title: 'Cumulative tokens', unit: 'prompt + completion', fmt: (v) => compact(v), sync: 'run', onClickX }),
        edges: new CumChart({ title: 'Live-live edges', unit: 'count', fmt: intFmt, sync: 'run', onClickX }),
      };
    }
    // every run chart shares the run's x window (the token series comes from the server on its own grid)
    for (const c of Object.values(charts)) c.setXRange(xr[0], xr[1]);
    // board share: forward-filled per agent at union of step times
    const xs = uniq([xr[0], ...model.lanes.flatMap((l) => Array.from(l.stepT)), xr[1]]);
    charts.share.setData(xs, model.lanes.map((l) => ({ label: l.label, color: agentColor(l.lane), dash: agentDash(l.lane), step: true, y: ffill(xs, l, (i) => share5(l, i)) })));
    const xp = uniq([xr[0], ...model.lanes.flatMap((l) => Array.from(l.postT)), xr[1]]);
    charts.posts.setData(xp, model.lanes.map((l) => ({ label: l.label, color: agentColor(l.lane), dash: agentDash(l.lane), step: true, y: xp.map((x) => (l.start != null && x < l.start ? null : lastLEnum(l.postT, x) + 1)) })));
    const xe = uniq([xr[0], ...model.edgesLL.map((e) => e.t), xr[1]]);
    charts.edges.setData(xe, [{ label: 'live-live edges', color: cv('--text-secondary'), step: true, y: xe.map((x) => { let n = 0; for (const e of model!.edgesLL) { if (e.t > x) break; n++; } return n; }) }]);
    // tokens via /api/metrics/series (x=t)
    const bucket = Math.max(1, Math.round((model.tMax - model.tMin) / 150));
    api.series({ metric: 'cum_tokens', x: 't', bucket, run: runId }).then((s) => {
      if (!charts || destroyed) return;
      const series: CumSeries[] = s.series.map((se) => { const id = Number((se.key as Record<string, unknown>).agent_id); return { label: se.label, color: agentColor(Number.isFinite(id) ? id : -1), dash: agentDash(Number.isFinite(id) ? id : 0), step: true, y: se.y }; });
      charts.tokens.setData(s.xs, series);
      charts.tokens.setCursor(t);
    }).catch(() => { charts?.tokens.el.replaceChildren(h('h3', null, 'Cumulative tokens'), h('div', { class: 'empty' }, 'series unavailable')); });
    for (const c of Object.values(charts)) c.setCursor(t);
  }

  // ------------------------------------------------------------------ load
  async function load(route: Route, keepState = false) {
    runId = route.arg ?? '';
    const p = route.params;
    // Subscribe BEFORE fetching: events published between the server's /timeline snapshot and the end of this
    // load are buffered in pendingLive and applied to the new model (append() dedupes on ref|kind).
    if (!unsubLive) subscribeLive();
    loading = true;
    let detail, tl;
    try {
      [detail, tl] = await Promise.all([api.run(runId), api.timeline(runId)]);
    } catch (e) {
      loading = false;
      replace(root, h('div', { class: 'err' }, `Failed to load run ${runId}: ${String(e)}`), h('a', { href: hrefFor('runs') }, '← runs'));
      return;
    }
    loading = false;
    if (destroyed) return;
    const firstLoad = !model || !keepState;
    model = new RunModel(tl, detail);
    setAgentFocus(model.lanes.length, selLane);
    if (firstLoad) {
      mountLayout();
      const xm = p.get('x') === 'step' ? 'step' : 'time';
      (root as HTMLElement & { _setX?: (x: XMode) => void })._setX?.(xm as XMode);
    }
    lanes.setModel(model, !firstLoad);
    slider.setModel(model);
    board.setModel(model);
    insp.setRun(runId, detail.run.harness?.output_chars, tl.data_version);
    renderHeader();
    api.graph(runId).then((g) => { if (!model || destroyed) return; model.setGraph(g); graph.setGraph(g); lanes.redraw(); slider.draw(); buildCharts(); renderHeader(); setT(t, false); }).catch(() => graph.el.replaceChildren(h('div', { class: 'empty' }, 'graph unavailable')));
    buildCharts();
    if (firstLoad) {
      const tq = p.get('t');
      if (model.live && tq == null) { following = true; slider.setFollow(true); }
      const t0 = tq != null ? Number(tq) : model.tMax;
      setPanel((p.get('panel') as Panel) || 'board');
      const ag = p.get('agent');
      if (ag != null && ag !== '') {
        selLane = Number(ag);
        const st = p.get('step');
        const l = model.lane(selLane);
        if (st && l) { const k = l.steps.findIndex((s) => String(s.step) === st); if (k >= 0) { selectAgent(selLane, k); if (tq != null) setT(t0, false); } else setT(t0, false); }
        else setT(t0, false);
      } else setT(t0, false);
    } else setT(following ? model.tMax : t, false);
    if (!model.live) { unsubLive?.(); unsubLive = null; pendingLive = []; }
    else if (pendingLive.length) flushLive();
  }

  function subscribeLive() {
    unsubLive?.();
    unsubLive = live.subscribe((m) => onLiveMsg(m), [runId]);
  }

  function onLiveMsg(m: LiveMessage) {
    const d = m.data as Record<string, unknown>;
    if (m.event === 'reset' || m.event === 'reconnected') {
      // the server could not replay what we missed (restart / overflow): refetch the timeline
      if (model && !loading) void load({ view: 'run', arg: runId, params: new URLSearchParams() }, true);
      return;
    }
    if (!model || loading) {
      if (m.event === 'event') pendingLive.push(d as unknown as TimelineEvent);
      return;
    }
    if (m.event === 'event') {
      pendingLive.push(d as unknown as TimelineEvent);
      if (!liveTimer) liveTimer = window.setTimeout(flushLive, 800);
    } else if (m.event === 'metrics_update' && d.metrics && typeof d.metrics === 'object') {
      // keep the header (discovery/solved/posts) current for live runs
      Object.assign(model.detail.run.metrics, d.metrics as Record<string, unknown>);
      renderHeader();
    } else if (m.event === 'run_finished' || (m.event === 'run_updated' && typeof d.t_origin === 'number' && model.tl.t_origin != null && Math.abs((d.t_origin as number) - model.tl.t_origin) > 0.01)) {
      void load({ view: 'run', arg: runId, params: new URLSearchParams() }, true);
    } else if (m.event === 'run_updated' && typeof d.data_version === 'number' && d.data_version > model.tl.data_version + 500) {
      void load({ view: 'run', arg: runId, params: new URLSearchParams() }, true);
    }
  }

  function flushLive() {
    liveTimer = 0;
    if (!model || destroyed) return;
    const evs = pendingLive; pendingLive = [];
    if (!model.append(evs)) return;
    // interactions are derived server-side: refresh the graph (edges, live-live counts) at most every 5 s
    if (evs.some((e) => ['post', 'dm', 'upvote', 'req', 'irc_cmd', 'swarm_onset'].includes(e.kind)) && Date.now() - lastGraphFetch > 5000) {
      lastGraphFetch = Date.now();
      api.graph(runId).then((g) => { if (!model || destroyed) return; model.setGraph(g); graph.setGraph(g); buildCharts(); setT(following ? model.tMax : t, false); }).catch(() => undefined);
    }
    lanes.setModel(model, true);
    if (following) lanes.followHead();
    slider.setModel(model);
    board.setModel(model);
    buildCharts();
    renderHeader();
    setT(following ? model.tMax : t, false);
  }

  // ------------------------------------------------------------------ layout
  const headerEl = h('div');
  function mountLayout() {
    lanes = new Lanes({
      onCursor: (nt) => setT(nt),
      onSelect: (lane, idx) => { if (idx == null) selectAgent(lane); else selectAgent(lane, idx); },
      onView: (a, b) => { if (lanes.getXMode() === 'time') slider?.setView(a, b); },
    });
    slider = new Slider((nt) => setT(nt), (on) => { following = on; if (on && model) { setT(model.tMax, false); lanes.followHead(); } });
    board = new BoardState();
    graph = new GraphView();
    insp = new StepInspector((agent, idx) => selectAgent(agent, idx));
    cleanups.push(() => lanes.destroy(), () => slider.destroy(), () => graph.destroy());
    replace(tabs, ...(['board', 'graph', 'inspector'] as Panel[]).map((p) => h('button', { role: 'tab', dataset: { p }, onclick: () => setPanel(p), title: `Show ${p} (${p[0]})` }, p === 'board' ? 'Board state' : p === 'graph' ? 'Interaction graph' : 'Step inspector')));
    const xSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'x axis' },
      h('button', { class: 'on', dataset: { x: 'time' }, onclick: () => setX('time') }, 'time'),
      h('button', { dataset: { x: 'step' }, onclick: () => setX('step') }, 'step'));
    const arcsBtn = h('button', { class: 'btn', 'aria-pressed': 'false', title: 'Show reply / mention / DM arcs between lanes (a)', onclick: () => toggleArcs() }, 'arcs');
    const dashSvg = (d: number[]) => { const ns = 'http://www.w3.org/2000/svg'; const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('width', '18'); svg.setAttribute('height', '8'); const ln = document.createElementNS(ns, 'line'); ln.setAttribute('x1', '0'); ln.setAttribute('x2', '18'); ln.setAttribute('y1', '4'); ln.setAttribute('y2', '4'); ln.setAttribute('stroke', 'currentColor'); ln.setAttribute('stroke-width', '1.5'); if (d.length) ln.setAttribute('stroke-dasharray', d.join(' ')); svg.appendChild(ln); return svg; };
    const arcsLegend = h('span', { class: 'it', style: { display: 'none' }, title: 'Arcs = live-live interaction events from the interaction graph (from = the agent who attended, dot = the agent attended to)' },
      'arcs:', ...(['reply', 'mention', 'dm'] as const).map((k2) => h('span', { class: 'it' }, dashSvg(ARC_DASH[k2]), k2)));
    function toggleArcs() { const on = arcsBtn.getAttribute('aria-pressed') !== 'true'; arcsBtn.setAttribute('aria-pressed', String(on)); arcsBtn.classList.toggle('on', on); arcsLegend.style.display = on ? '' : 'none'; lanes.setArcs(on); }
    (root as HTMLElement & { _toggleArcs?: () => void })._toggleArcs = toggleArcs;
    const legend = h('div', { class: 'legend' });
    paintLegend = () => replace(legend,
      ...STEP_CLASSES.map((c) => h('span', { class: 'it' }, c.id === 'idle' ? h('i', { class: 'hollow' }) : h('i', { class: 'sw', style: { background: stepColor(c.id) } }), c.label)),
      h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: 'transparent', outline: `1.5px solid ${cv('--status-critical')}` } }), 'persistence / leak'),
      h('span', { class: 'it muted' }, 'board: ○ read ● post ▲ upvote ◆ dm · ticks = model calls (height = tokens) · shaded = free time'),
      arcsLegend);
    paintLegend();
    const lanesCard = h('div', { class: 'card lanes-card' },
      h('div', { class: 'lanes-toolbar' }, h('span', { class: 'secondary' }, 'x'), xSeg, arcsBtn, h('button', { class: 'btn', title: 'Reset zoom (double-click lanes)', onclick: () => lanes.resetView() }, 'reset zoom'), h('span', { class: 'grow' }), legend),
      lanes.el, slider.el);
    replace(root, headerEl,
      h('div', { class: 'run-layout' },
        h('div', { style: { minWidth: '0' } }, lanesCard, h('div', { class: 'row', style: { marginTop: '8px' } }, totalsEl), stripEl, h('div', { class: 'charts', id: 'charts' })),
        h('div', { class: 'side' }, h('div', { class: 'card' }, tabs, panelBody))));
    function setX(x: XMode) {
      lanes.setXMode(x);
      for (const b of xSeg.querySelectorAll('button')) b.classList.toggle('on', (b as HTMLElement).dataset.x === x);
      replaceParams({ x: x === 'time' ? null : x });
      lanes.setCursor(t);
      if (x === 'step') slider.setView(model?.tMin ?? 0, model?.tMax ?? 1); else { const [a, b] = lanes.getView(); slider.setView(a, b); }
    }
    (root as HTMLElement & { _setX?: (x: XMode) => void })._setX = setX;
    queueMicrotask(() => {
      const ch = root.querySelector('#charts');
      if (ch && charts) ch.replaceChildren(charts.share.el, charts.posts.el, charts.tokens.el, charts.edges.el);
    });
  }

  function renderHeader() {
    if (!model) return;
    const r = model.detail.run;
    const warn: Node[] = [];
    if (r.params?.irc_inbound_broken) warn.push(h('span', { class: 'warnbadge', title: 'IRC inbound broken: an agent sent bytes to the board but the board parsed no IRC command from them (pre-6034d34 LF bug)' }, 'irc_inbound_broken'));
    if (r.final_missing) warn.push(h('span', { class: 'warnbadge' }, 'final copy missing (live mirror)'));
    if (!r.t_origin_final) warn.push(h('span', { class: 'warnbadge', title: 't=0 is provisional until run.json lands' }, 'provisional t₀'));
    for (const n of r.notes ?? []) warn.push(h('span', { class: 'flag', title: noteText(n) }, trunc(noteText(n), 48)));
    const ctf = r.ctf as Record<string, unknown> | null | undefined;
    const ctfEl = ctf ? h('details', { style: { display: 'inline-block' } }, h('summary', { class: 'muted' }, 'ctf'), h('code', null, `flag ${ctf.flag ?? '–'} · passphrase ${ctf.passphrase ?? '–'} · vault ${ctf.vault_id ?? '–'}${ctf.seal_key_hex ? ` · sealed under ${ctf.seal_key_hex} (never in the VM)` : ''}`)) : null;
    replace(headerEl, h('div', { class: 'run-head' },
      h('a', { href: hrefFor('runs'), class: 'muted' }, '← runs'),
      h('h1', null, r.run_id), statusPill(r.status),
      h('span', { class: 'secondary' }, model.detail.condition?.label ?? r.condition_label ?? ''),
      h('span', { class: 'meta' },
        h('span', null, 'started ', h('b', { title: r.started == null && model.tl.t_origin != null ? 'provisional: run.json not written yet' : '' }, r.started != null ? dateTime(r.started) : model.tl.t_origin != null ? '≈ ' + dateTime(model.tl.t_origin) : '–')),
        h('span', null, 'duration ', h('b', null, r.duration_s != null ? dur(r.duration_s) : dur(model.tMax) + ' so far')),
        h('span', null, 'goal ', goalBadge(r.goal)),
        h('span', null, 'budget ', h('b', null, String(r.harness?.budget ?? '–'))),
        h('span', null, 'code ', h('b', null, String(r.params?.code_version ?? '–'))),
        h('span', null, 'discovery ', h('b', null, pct(r.metrics.discovery_rate as number))),
        h('span', null, 'solved ', h('b', null, pct(r.metrics.solve_rate as number))),
        h('span', null, 'posts ', h('b', null, num(r.metrics.total_posts as number))),
        h('span', null, 'swarm onset ', h('b', null, model.swarmOnset != null ? clockT(model.swarmOnset) : '–'))),
      ctfEl, ...warn));
  }

  // ------------------------------------------------------------------ keys
  function keys(e: KeyboardEvent): boolean {
    if (!model) return false;
    const k = e.key;
    if (k === 'ArrowLeft' || k === 'ArrowRight') { const d = e.shiftKey ? 10 : e.altKey ? 0.1 : 1; setT(t + (k === 'ArrowLeft' ? -d : d)); return true; }
    if (k === ',' || k === '.') { const nt = model.nextEventT(t, k === '.' ? 1 : -1); if (nt != null) setT(nt); return true; }
    if (k === '{' || k === '}') { const nt = model.nextEventT(t, k === '}' ? 1 : -1, (ev) => ev.kind === 'post' || ev.kind === 'req' || ev.kind === 'dm' || ev.kind === 'upvote'); if (nt != null) setT(nt); return true; }
    if (k === '[' || k === ']') {
      const lane = selLane ?? model.lanes[0]?.lane; if (lane == null) return true;
      const l = model.lane(lane)!; const i = lastLEnum(l.stepT, t);
      const j = clamp(k === ']' ? i + 1 : (i > 0 && t > (l.steps[i].t_end ?? l.steps[i].t) + 0.01 ? i : i - 1), 0, l.steps.length - 1);
      if (selLane == null) selLane = lane;
      selectAgent(lane, j);
      return true;
    }
    if (k === ' ') { slider.toggle(); return true; }
    if (k === '-' || k === '_') { slider.speedStep(-1); return true; }
    if (k === '=' || k === '+') { slider.speedStep(1); return true; }
    if (k === 'Home') { setT(model.tMin); return true; }
    if (k === 'End') { setT(model.tMax); if (model.live) { following = true; slider.setFollow(true); } return true; }
    if (/^[0-9]$/.test(k)) {
      // digits select lanes by position (1 = first lane); type several quickly for lanes beyond 9 (e.g. 1 2)
      digitBuf += k;
      clearTimeout(digitTimer);
      const apply = () => { const n = Number(digitBuf); digitBuf = ''; if (n === 0) selectAgent(null); else { const l = model?.lanes[n - 1]; if (l) selectAgent(l.lane); } };
      if (Number(digitBuf) * 10 > model.lanes.length) apply(); else digitTimer = window.setTimeout(apply, 600);
      return true;
    }
    if (k === 'x') { (root as HTMLElement & { _setX?: (x: XMode) => void })._setX?.(lanes.getXMode() === 'time' ? 'step' : 'time'); return true; }
    if (k === 'a') { (root as HTMLElement & { _toggleArcs?: () => void })._toggleArcs?.(); return true; }
    if (k === 'g') { setPanel('graph'); return true; }
    if (k === 'b') { setPanel('board'); return true; }
    if (k === 'i') { setPanel('inspector'); return true; }
    if (k === 'f' && model.live) { const on = !following; following = on; slider.setFollow(on); return true; }
    if (k === 'Escape') { selectAgent(null); return true; }
    return false;
  }

  return {
    el: root,
    mount: (r) => { destroyed = false; replace(root, h('div', { class: 'empty' }, 'Loading run…')); void load(r); setViewKeys(keys); },
    onTheme: () => {
      if (!model) return;
      inspKey = '';
      board.setModel(model); board.update(t);
      buildCharts(); renderHeader(); renderStrip(true); paintLegend?.();
      if (selLane != null) syncInspector();
    },
    update: (r) => {
      // same run: apply externally driven params (pasted / edited URLs, links, Back/Forward). The view's own
      // URL writes use history.replaceState, which fires no hashchange, so they never come through here.
      if (r.arg !== runId) return false;
      if (!model) return true;
      const p = r.params;
      const xq = p.get('x') === 'step' ? 'step' : 'time';
      if (xq !== lanes.getXMode()) (root as HTMLElement & { _setX?: (x: XMode) => void })._setX?.(xq);
      const pq = (p.get('panel') as Panel) || 'board';
      setPanel(pq);
      const ag = p.get('agent');
      const lane = ag != null && ag !== '' && Number.isFinite(Number(ag)) ? Number(ag) : null;
      const st = p.get('step');
      const l = lane != null ? model.lane(lane) : undefined;
      const k = l && st ? l.steps.findIndex((s2) => String(s2.step) === st) : -1;
      if (lane !== selLane || k >= 0) selectAgent(lane, k >= 0 ? k : null);
      const tq = p.get('t');
      if (tq != null && Number.isFinite(Number(tq))) {
        if (model.live) { following = false; slider.setFollow(false); }
        setT(Number(tq), false);
      }
      return true;
    },
    destroy: () => { destroyed = true; unsubLive?.(); unsubLive = null; setAgentFocus(0, null); clearTimeout(urlTimer); clearTimeout(liveTimer); clearTimeout(digitTimer); cleanups.forEach((f) => f()); charts && Object.values(charts).forEach((c) => c.destroy()); setViewKeys(null); },
  };
}

function uniq(a: number[]): number[] { const s = [...new Set(a.filter((x) => Number.isFinite(x)))].sort((x, y) => x - y); return s; }
function share5(l: LaneIndex, i: number) { const lo = Math.max(0, i - 4); return (l.cumBoard[i] - (lo > 0 ? l.cumBoard[lo - 1] : 0)) / (i - lo + 1); }
function ffill(xs: number[], l: LaneIndex, val: (i: number) => number): (number | null)[] {
  return xs.map((x) => { const i = lastLEnum(l.stepT, x); return i < 0 ? null : val(i); });
}

function intFmt(v: number) { return Number.isInteger(v) ? num(v) : ''; }
