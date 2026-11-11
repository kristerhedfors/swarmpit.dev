// Overview / cell comparison (CONTRACT 6.2.1): facet bar + group-by picker, KPI tiles, forest-plot
// small multiples with run-bootstrap CIs and per-agent strips, a cell x metric heatmap, and the
// time-to-discovery curve per cell.
import { api, filtersFrom } from '../api';
import { ecdf } from '../components/ecdf';
import { facetBar } from '../components/facetBar';
import { forest } from '../components/forest';
import { heatmap } from '../components/heatmap';
import { h, replace } from '../dom';
import { cv, popColor, slot } from '../lib/colors';
import { compact, noteText, pct, shortModel } from '../lib/format';
import type { Cell, CellsResponse, Facets, Filters, MetricDef, RunsResponse } from '../lib/types';
import { go, hrefFor, parseHash, type Route } from '../router';
import type { View } from './view';

const DEFAULT_GROUP = ['model', 'iface', 'pop'];
// Solvable and unsolvable runs are different experiments: when both are in view the default group-by splits them
// (the server never pools them silently either; without a goal filter or group it keeps solvable runs only).
const groupFor = (p: URLSearchParams, facets: Facets) => {
  const g = p.get('group');
  if (g) return g.split(',').filter(Boolean);
  const goals = facets.dims.find((d) => d.key === 'param.goal')?.values ?? [];
  return goals.length > 1 ? [...DEFAULT_GROUP, 'param.goal'] : DEFAULT_GROUP;
};
const DEFAULT_FOREST = ['share_after_contact', 'board_steps_free', 'posts', 'discovered', 'episodes', 'secret_leak'];
const HEAT = ['discovered', 'first_contact', 'board_steps', 'board_steps_free', 'share_after_contact', 'share_free', 'posts', 'posts_free', 'episodes', 'solved', 'solve_step', 'idle_free_steps', 'tok_prompt', 'secret_leak'];

export function overviewView(): View {
  const root = h('div');
  let seq = 0;
  let repaint: (() => void) | null = null;

  async function render(route: Route) {
    const my = ++seq;
    const p = route.params;
    const filters = filtersFrom(p);
    const metricsSel = (p.get('metrics') || DEFAULT_FOREST.join(',')).split(',').filter(Boolean);
    const inclBroken = p.get('irc_broken') === '1';
    root.classList.add('loading');
    let facets: Facets, runs: RunsResponse, cells: CellsResponse;
    try {
      const need = [...new Set([...metricsSel, ...HEAT, 'first_contact', 'time_to_discovery_s'])];
      [facets, runs] = await Promise.all([api.facets(filters), api.runs({ limit: 10000 }, filters)]);
      if (my !== seq) return;
      const group = groupFor(p, facets);
      // irc_inbound_broken runs (evidence of a broken IRC pipe, see help) are excluded server-side by default
      cells = await api.cells({ group: group.join(','), metrics: need.join(','), points: 1, ci: 'bootstrap', ...(inclBroken ? {} : { exclude_broken: 1 }) }, filters);
    } catch (e) {
      if (my === seq) replace(root, h('div', { class: 'err' }, 'Failed to load overview: ' + String(e)));
      return;
    }
    if (my !== seq) return;
    root.classList.remove('loading');
    repaint = () => paint(route, facets, runs, cells);
    repaint();
  }

  function paint(route: Route, facets: Facets, runs: RunsResponse, cells: CellsResponse) {
    const p = route.params;
    const filters = filtersFrom(p);
    const group = groupFor(p, facets);
    const metricsSel = (p.get('metrics') || DEFAULT_FOREST.join(',')).split(',').filter(Boolean);
    const ecdfMode = p.get('ttd') === 's' ? 's' : 'step';
    const inclBroken = p.get('irc_broken') === '1';
    // without a goal filter or group-by the server keeps solvable runs only (say so when unsolvable ones exist)
    const goalDim = facets.dims.find((d) => d.key === 'param.goal')?.values ?? [];
    const goalNote = goalDim.some((v) => v.value === 'unsolvable') && !group.some((g) => g === 'param.goal' || g === 'goal') && !filters['param.goal'] && !filters.goal ? ' · unsolvable-goal runs excluded' : '';
    const defs = new Map(facets.metrics.map((m) => [m.metric_id, m]));
    const md = (id: string): MetricDef => defs.get(id) ?? { metric_id: id, label: id, unit: '', grain: 'agent', agg: 'mean', higher_is: 'neutral', description: '' };
    const modelVals = [...new Set(cells.cells.map((c) => String(c.key.model ?? '')))];
    // models are shown without their org prefix ('mistralai/…'), then with any shared prefix cut
    const cut = modelVals.length > 1 ? commonPrefix(modelVals.map(shortModel)).replace(/[^-_.]*$/, '') : '';
    const fmtV = (g: string, v: unknown) => (g === 'model' && v != null ? (cut ? '…' + shortModel(v).slice(cut.length) : shortModel(v)) : v === '' ? '(untagged)' : String(v ?? '–'));
    // full label (heatmap, tooltips) and a compact one for forest rows: group dims with a single value in view
    // are stated once in the subtitle, and with several models the model becomes a row-group header + marker
    // shape instead of a truncated label prefix
    const label = (c: Cell) => group.map((g) => fmtV(g, c.key[g])).join(' · ');
    const distinct = (g: string) => new Set(cells.cells.map((c) => JSON.stringify(c.key[g] ?? null))).size;
    const constDims = cells.cells.length > 1 ? group.filter((g) => distinct(g) === 1) : [];
    const multiModel = group.includes('model') && modelVals.length > 1;
    const rowDims = group.filter((g) => !constDims.includes(g) && !(multiModel && g === 'model'));
    const rowLabel = (c: Cell) => (rowDims.length ? rowDims.map((g) => fmtV(g, c.key[g])).join(' · ') : label(c));
    const models = [...new Set(cells.cells.map((c) => String(c.key.model ?? '')))];
    const groupOf = multiModel ? (c: Cell) => (c.key.model == null ? '–' : shortModel(c.key.model)) : null;
    const shapeOf = multiModel ? (c: Cell) => models.indexOf(String(c.key.model ?? '')) : null;
    const constNote = constDims.length ? constDims.map((g) => `${g} = ${String(cells.cells[0].key[g] ?? '–')}`).join(' · ') : '';
    const colorOf = (c: Cell) => group.includes('pop') ? popColor(String(c.key.pop)) : group.includes('model') ? slot(models.indexOf(String(c.key.model))) : slot(0);
    const openCell = (c: Cell) => {
      const q = new URLSearchParams(p);
      for (const g of group) { const v = c.key[g]; if (v != null) q.set(g === 'run_status' ? 'status' : g, String(v)); }
      ['group', 'metrics', 'ttd', 'irc_broken'].forEach((k) => q.delete(k));
      go('runs', null, q);
    };
    const setP = (patch: Record<string, string | null>) => { const q = new URLSearchParams(p); for (const [k, v] of Object.entries(patch)) v == null ? q.delete(k) : q.set(k, v); go('overview', null, q); };
    const onFilters = (f: Filters) => { const q = new URLSearchParams(); for (const k of ['group', 'metrics', 'ttd', 'irc_broken']) if (p.get(k)) q.set(k, p.get(k)!); for (const [k, v] of Object.entries(f)) q.set(k, v.join(',')); go('overview', null, q); };

    // KPI tiles
    const rs = runs.runs;
    const agentsN = rs.reduce((s, r) => s + (r.n_agents ?? 0), 0);
    const tokens = rs.reduce((s, r) => s + (r.metrics.tokens_prompt ?? 0), 0);
    const leak = rs.length ? rs.filter((r) => r.metrics.any_leak).length / rs.length : null;
    const budgets = facets.dims.find((d) => d.key === 'harness.budget')?.values.map((v) => v.value).filter((v) => v != null && v !== '') ?? [];
    const live = rs.filter((r) => r.status === 'live' || r.status === 'collecting').length;
    const broken = rs.filter((r) => r.params?.irc_inbound_broken === true);
    const noteRuns = rs.filter((r) => r.notes?.length);
    const tiles = h('div', { class: 'tiles' },
      tile('Runs', String(rs.length), live ? `${live} live` : `${new Set(rs.map((r) => r.condition_id)).size} conditions`),
      tile('Agent sessions', String(agentsN), `${cells.cells.length} cells in view`),
      tile('Prompt tokens', compact(tokens), 'sum over runs in view'),
      tile('Step budget', budgets.length ? budgets.join(' / ') : '–', 'harness.budget'),
      tile('Runs with a secret leak', pct(leak), 'flag / passphrase posted to the board'),
      h('div', { class: 'tile' }, h('div', { class: 'label' }, 'Warnings'),
        h('div', { class: 'value' }, String(broken.length + noteRuns.length)),
        h('div', { class: 'hint' }, broken.length ? h('span', { class: 'warnbadge' }, `irc_inbound_broken × ${broken.length}`) : 'none',
          noteRuns.length ? h('span', { title: noteRuns.map((r) => r.run_id + ': ' + r.notes.map(noteText).join('; ')).join('\n') }, ` · ${noteRuns.length} runs with notes`) : null)));

    // group-by + metric pickers (same row, above charts)
    const groupDims = ['model', 'iface', 'pop', 'n_agents', 'rep', 'tag', 'condition_id', ...facets.dims.filter((d) => (d.key.startsWith('harness.') || d.key.startsWith('param.')) && ((d.values.length >= 2 && d.values.length <= 12) || group.includes(d.key))).map((d) => d.key)];
    const groupPicker = h('div', { class: 'facet' }, h('span', { class: 'k' }, 'group by'),
      ...[...new Set(groupDims)].map((g) => h('button', { class: 'chip' + (group.includes(g) ? ' on' : ''), 'aria-pressed': String(group.includes(g)),
        onclick: () => { const ng = group.includes(g) ? group.filter((x) => x !== g) : [...group, g]; setP({ group: ng.length ? ng.join(',') : null }); } }, g)));
    const metricPicker = h('div', { class: 'facet' }, h('span', { class: 'k' }, 'metrics'),
      ...facets.metrics.map((m) => h('button', { class: 'chip' + (metricsSel.includes(m.metric_id) ? ' on' : ''), title: m.description, 'aria-pressed': String(metricsSel.includes(m.metric_id)),
        onclick: () => { const ns = metricsSel.includes(m.metric_id) ? metricsSel.filter((x) => x !== m.metric_id) : [...metricsSel, m.metric_id]; setP({ metrics: ns.join(',') }); } }, m.label)));
    const brokenToggle = broken.length ? h('label', { class: 'facet', title: 'IRC runs where an agent sent bytes but the board parsed no IRC command (pre-6034d34 LF bug) are excluded from cell comparisons by default' },
      h('input', { type: 'checkbox', checked: inclBroken, onchange: () => setP({ irc_broken: inclBroken ? null : '1' }) }), h('span', { class: 'secondary' }, 'include irc_inbound_broken runs')) : null;

    const colorLegend = group.includes('pop')
      ? h('div', { class: 'legend' }, h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: popColor('solo') } }), 'solo'), h('span', { class: 'it' }, h('i', { class: 'sw', style: { background: popColor('multi') } }), 'multi'), h('span', { class: 'muted' }, 'dot = mean · line = 95% run-bootstrap CI · faint = agents'))
      : group.includes('model') ? h('div', { class: 'legend' }, ...models.map((m, i) => h('span', { class: 'it', title: m }, h('i', { class: 'sw', style: { background: slot(i) } }), shortModel(m))), h('span', { class: 'muted' }, 'dot = mean · line = 95% CI · faint = agents'))
      : h('div', { class: 'legend' }, h('span', { class: 'muted' }, 'dot = mean · line = 95% run-bootstrap CI · faint = agents'));
    if (multiModel) colorLegend.appendChild(h('span', { class: 'muted' }, ' · rows grouped by model; mean marker shape = model (' + models.map((m, i) => `${['●', '■', '◆', '▲'][i % 4]} ${shortModel(m)}`).join(', ') + ')'));
    if (constNote) colorLegend.appendChild(h('span', { class: 'muted' }, ' · all cells: ' + constNote));

    const forests = h('div', { class: 'ov-grid' }, ...metricsSel.filter((m) => defs.has(m) || cells.metrics.includes(m)).map((m) => forest({ metric: md(m), cells: cells.cells, label: rowLabel, color: colorOf, onRow: openCell, groupOf, shapeOf })));
    const heatMetrics = HEAT.filter((m) => cells.metrics.includes(m)).map(md);
    const ttdSeg = h('div', { class: 'seg' },
      h('button', { class: ecdfMode === 'step' ? 'on' : '', onclick: () => setP({ ttd: null }) }, 'steps'),
      h('button', { class: ecdfMode === 's' ? 'on' : '', onclick: () => setP({ ttd: 's' }) }, 'seconds'));

    replace(root,
      h('div', { class: 'view-title' }, h('h1', null, 'Cell comparison'), h('span', { class: 'muted' }, `${cells.cells.length} cells · ${cells.ci} CI, B=${cells.B} · tagged runs ${filters.include_tagged ? 'included' : 'excluded'}${goalNote}`),
        h('span', { class: 'grow' }), h('a', { href: hrefFor('runs', null, new URLSearchParams([...p.entries()].filter(([k]) => !['group', 'metrics', 'ttd', 'irc_broken'].includes(k)))) }, 'runs table →')),
      facetBar(facets, filters, onFilters, { showStatus: true, extra: brokenToggle ? [brokenToggle] : [] }),
      tiles,
      h('div', { class: 'facetbar' }, groupPicker),
      h('div', { class: 'facetbar' }, metricPicker),
      cells.cells.length ? h('div', null,
        h('div', { class: 'row', style: { margin: '4px 0 8px' } }, colorLegend),
        forests,
        h('div', { class: 'card', style: { marginTop: '12px' } }, h('header', null, h('h2', null, 'Cell × metric'), h('span', { class: 'sub' }, 'colour normalised per column (stronger contrast with the surface = higher); raw values shown; click a row to see its runs')),
          heatmap(cells.cells, heatMetrics, label, openCell)),
        h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0,1fr)', marginTop: '12px' } },
          h('div', null, h('div', { class: 'row', style: { marginBottom: '6px' } }, h('span', { class: 'secondary' }, 'time-to-discovery x-axis'), ttdSeg), ecdf(cells.cells, rowLabel, ecdfMode, { color: colorOf, panelOf: groupOf }))))
        : h('div', { class: 'empty card' }, 'No agent data matches these filters yet.'),
    );
    void cv;
  }

  return {
    el: root,
    mount: (r) => { void render(r); },
    update: (r) => { void render(r); return true; },
    destroy: () => { seq++; repaint = null; },
    onTheme: () => repaint?.(),
    onLive: (m) => { if (m.event === 'run_finished') void render(parseHash()); },
  };
}

function commonPrefix(a: string[]) { let p = a[0] ?? ''; for (const s of a) while (!s.startsWith(p)) p = p.slice(0, -1); return p; }

function tile(label: string, value: string, hint: string) {
  return h('div', { class: 'tile' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), h('div', { class: 'hint' }, hint));
}
