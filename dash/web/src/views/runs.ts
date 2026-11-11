// Runs table (CONTRACT 6.2.2): sortable, filterable, live runs pinned with a pulsing status, inline
// sparkline of board steps per 30 s (lazy per visible row), j/k + Enter navigation.
import { api, filtersFrom } from '../api';
import { facetBar } from '../components/facetBar';
import { sparkline, type Spark } from '../components/sparkline';
import { SortTable, type Col } from '../components/table';
import { h, replace } from '../dom';
import { ifaceColor } from '../lib/colors';
import { dateTime, dur, noteText, num, pct } from '../lib/format';
import type { Filters, RunSummary } from '../lib/types';
import { go, hrefFor, parseHash, replaceParams, type Route } from '../router';
import { setViewKeys } from '../keys';
import type { View } from './view';

const sparkCache = new Map<string, Promise<number[]>>();
function boardSpark(r: RunSummary): Promise<number[]> {
  const k = r.run_id + ':' + r.data_version;
  let p = sparkCache.get(k);
  if (!p) {
    p = api.series({ metric: 'board_steps', x: 't', bucket: 30, run: r.run_id }).then((s) => s.xs.map((_, i) => s.series.reduce((acc, se) => acc + (se.y[i] ?? 0), 0)));
    sparkCache.set(k, p);
  }
  return p;
}

/** goal chip: 'unsolvable' is flagged (no free phase, solve rate 0 by design); 'solvable' is muted or omitted */
export function goalBadge(goal: string | null | undefined, quietDefault = false) {
  const g = goal || 'solvable';
  if (g === 'solvable') return quietDefault ? h('span', { class: 'muted' }, g) : h('b', null, g);
  return h('span', { class: 'flag goal', title: 'unsolvable goal: the flag can never be accepted, so there is no free phase' }, g);
}

export function statusPill(status: string) {
  return h('span', { class: 'pill ' + status, title: 'run status: ' + status }, h('i', { class: 'dot' }), status);
}

export function runsView(): View {
  const root = h('div');
  let seq = 0;
  let table: SortTable<RunSummary> | null = null;
  let refetchTimer = 0;
  let io: IntersectionObserver | null = null;
  let sparks: Spark[] = [];
  const disposeSparks = () => { sparks.forEach((sp) => sp.dispose()); sparks = []; };

  async function render(route: Route) {
    const my = ++seq;
    const p = route.params;
    const filters = filtersFrom(p);
    const q = p.get('q') ?? '';
    root.classList.add('loading');
    let facets, res;
    try {
      [facets, res] = await Promise.all([api.facets(filters), api.runs({ limit: 10000, q }, filters)]);
    } catch (e) { if (my === seq) replace(root, h('div', { class: 'err' }, String(e))); return; }
    if (my !== seq) return;
    root.classList.remove('loading');
    const onFilters = (f: Filters) => { const nq = new URLSearchParams(); for (const k of ['sort', 'order', 'q']) if (p.get(k)) nq.set(k, p.get(k)!); for (const [k, v] of Object.entries(f)) nq.set(k, v.join(',')); go('runs', null, nq); };
    const isLive = (r: RunSummary) => r.status === 'live' || r.status === 'collecting';
    io?.disconnect();
    disposeSparks();
    // one shared scale for every row: x = 30 s buckets up to the longest run in view, y = largest bucket seen
    const shared = { max: 1, slots: Math.max(1, ...res.runs.map((r) => Math.floor(Math.max(0, r.t_end ?? r.duration_s ?? 0) / 30) + 1)) };
    io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) {
        const el = e.target as HTMLElement; io!.unobserve(el);
        const r = res.runs.find((x) => x.run_id === el.dataset.run);
        if (r) boardSpark(r).then((v) => {
          if (my !== seq) return;
          const grew = Math.max(...v, 0) > shared.max;
          if (grew) shared.max = Math.max(...v);
          const sp = sparkline(v, 90, 18, { max: () => shared.max, slots: () => shared.slots, live: isLive(r) });
          sparks.push(sp);
          replace(el, sp);
          if (grew) sparks.forEach((x) => x.redraw());
        }).catch(() => replace(el, '–'));
      }
    });
    const cols: Col<RunSummary>[] = [
      { key: 'status', label: 'status', render: (r) => statusPill(r.status), sortVal: (r) => r.status },
      { key: 'run_id', label: 'run', render: (r) => h('a', { href: hrefFor('run', r.run_id), onclick: (e: Event) => e.stopPropagation() }, r.run_id), sortVal: (r) => r.run_id },
      { key: 'iface', label: 'iface', render: (r) => h('span', { class: 'row', style: { gap: '5px', flexWrap: 'nowrap' } }, h('i', { style: { width: '8px', height: '8px', borderRadius: '2px', background: ifaceColor(r.iface), display: 'inline-block' } }), r.iface ?? '–'), sortVal: (r) => r.iface },
      { key: 'condition_label', label: 'condition', render: (r) => h('span', { class: 'secondary' }, r.condition_label ?? '–'), sortVal: (r) => r.condition_label },
      { key: 'n_agents', label: 'n', num: true, render: (r) => num(r.n_agents), sortVal: (r) => r.n_agents },
      { key: 'rep', label: 'rep', num: true, render: (r) => num(r.rep), sortVal: (r) => r.rep },
      { key: 'goal', label: 'goal', title: 'CTF goal: unsolvable = the flag can never be accepted (no free phase)', render: (r) => goalBadge(r.goal, true), sortVal: (r) => r.goal },
      { key: 'tag', label: 'tag', render: (r) => r.tag ? h('span', { class: 'flag' }, r.tag) : '', sortVal: (r) => r.tag },
      { key: 'started', label: 'started', render: (r) => h('span', { class: 'tnum secondary' }, dateTime(r.started)), sortVal: (r) => r.started },
      { key: 'duration_s', label: 'duration', num: true, render: (r) => isLive(r) ? h('span', { class: 'muted' }, 'running') : dur(r.duration_s), sortVal: (r) => r.duration_s },
      { key: 'discovery_rate', label: 'discovery', num: true, title: 'share of agents that touched the board', render: (r) => pct(r.metrics.discovery_rate as number), sortVal: (r) => (r.metrics.discovery_rate as number) ?? null },
      { key: 'solve_rate', label: 'solved', num: true, render: (r) => pct(r.metrics.solve_rate as number), sortVal: (r) => (r.metrics.solve_rate as number) ?? null },
      { key: 'total_posts', label: 'posts', num: true, render: (r) => num(r.metrics.total_posts as number), sortVal: (r) => (r.metrics.total_posts as number) ?? null },
      { key: 'live_live_edges', label: 'live edges', num: true, title: 'inferred interactions between live agents', render: (r) => num(r.metrics.live_live_edges as number), sortVal: (r) => (r.metrics.live_live_edges as number) ?? null },
      { key: 'any_leak', label: 'leak', render: (r) => r.metrics.any_leak ? h('span', { class: 'flag secret_leak' }, 'leak') : '', sortVal: (r) => (r.metrics.any_leak ? 1 : 0) },
      { key: 'spark', label: 'board steps / 30 s', title: 'one shared scale for all rows: x = run time (30 s buckets), y = board steps per bucket; accent = current bucket of a live run', render: (r) => { const el = h('span', { dataset: { run: r.run_id } }, ''); io!.observe(el); return el; } },
      { key: 'notes', label: '', render: (r) => h('span', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } },
        r.params?.irc_inbound_broken ? h('span', { class: 'warnbadge', title: 'IRC inbound broken: agent bytes never reached the board' }, 'irc broken') : null,
        r.notes?.length ? h('span', { class: 'muted', title: r.notes.map(noteText).join('\n') }, `${r.notes.length} note${r.notes.length > 1 ? 's' : ''}`) : null) },
    ];
    table = new SortTable(cols, {
      sort: p.get('sort') || 'started', order: (p.get('order') as 'asc' | 'desc') || 'desc',
      onSort: (k, o) => replaceParams({ sort: k, order: o }),
      onOpen: (r) => go('run', r.run_id), pin: isLive, rowClass: (r) => (isLive(r) ? 'pinned' : ''),
    });
    table.setRows(res.runs);
    const search = h('input', { type: 'search', placeholder: 'filter run_id…  (/)', value: q, 'aria-label': 'Filter runs by id' });
    let st = 0;
    search.addEventListener('input', () => { clearTimeout(st); st = window.setTimeout(() => replaceAndRender({ q: search.value || null }), 250); });
    replace(root,
      h('div', { class: 'view-title' }, h('h1', null, 'Runs'), h('span', { class: 'muted' }, `${res.total} runs`), h('span', { class: 'grow' }), search),
      facetBar(facets, filters, onFilters, { showStatus: true }),
      table.el,
      h('p', { class: 'muted', style: { fontSize: '11px' } }, 'Click a row or press j/k then Enter. Live runs are pinned on top.'));
  }
  function replaceAndRender(patch: Record<string, string | null>) { replaceParams(patch); void render(parseHash()); }

  return {
    el: root,
    mount: (r) => {
      void render(r);
      setViewKeys((e) => {
        if (e.key === 'j') { table?.move(1); return true; }
        if (e.key === 'k') { table?.move(-1); return true; }
        if (e.key === 'Enter') { table?.open(); return true; }
        return false;
      });
    },
    update: (r) => { void render(r); return true; },
    onTheme: () => { void render(parseHash()); },
    destroy: () => { seq++; io?.disconnect(); disposeSparks(); clearTimeout(refetchTimer); setViewKeys(null); },
    onLive: (m) => {
      if (m.event === 'run_started' || m.event === 'run_finished' || m.event === 'metrics_update') {
        clearTimeout(refetchTimer);
        refetchTimer = window.setTimeout(() => void render(parseHash()), 1500);
      }
    },
  };
}
