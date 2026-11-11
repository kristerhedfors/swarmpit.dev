// Pivot view: any dimension x any dimension, one metric, as an accessible heatmap table with counts and margins.
import { h } from '../util/dom.js';
import { DIM_INFO, METRICS, MULTI_DIMS, PIVOT_DIMS, dimLabel, fmtAgg, fmtMetric, metricInfo, pivot, rampBin } from './data.js';
import type { Agg, Dim, MetricId } from './types.js';
import type { Ctx } from './ui.js';
import { gridNav, legendRamp, select } from './ui.js';

/** cells with fewer episodes than this get a dashed outline: their colour rests on very little data */
export const LOW_N = 5;

export function renderPivot(host: HTMLElement, ctx: Ctx): void {
  const { ex, state } = ctx;
  const { rows: rd, cols: cd, metric } = state;
  const pv = pivot(ex, ctx.eps, rd, cd, metric, state.filters.agent_code);
  const mi = metricInfo(metric);
  const dimOpts = PIVOT_DIMS.map((d) => ({ v: d, label: DIM_INFO[d].label }));
  const controls = h('div.ex-controls', null,
    select<Dim>('Rows', rd, dimOpts, (v) => ctx.set({ rows: v }), 'p:rows'),
    h('button.ex-btn.ex-swap', { type: 'button', 'data-fk': 'p:swap', 'aria-label': 'Swap rows and columns', title: 'Swap rows and columns',
      onclick: () => ctx.set({ rows: cd, cols: rd }) }, '⇄'),
    select<Dim>('Columns', cd, dimOpts, (v) => ctx.set({ cols: v }), 'p:cols'),
    select<MetricId>('Metric', metric, METRICS.map((m) => ({ v: m.id, label: m.label })), (v) => ctx.set({ metric: v }), 'p:metric'));
  const hdr = legendRamp(fmtMetric(metric, 0), fmtMetric(metric, pv.max), mi.label);
  const same = rd === cd;
  const colLabel = (c: string): string => (same ? 'All' : dimLabel(ex, cd, c));
  const drill = (r: string | null, c: string | null): void => {
    const f = { ...state.filters };
    if (r !== null) f[rd] = [r];
    if (c !== null && !same) f[cd] = [c];
    ctx.set({ filters: f, view: 'episodes', page: 0 }, true);
  };
  const label = (r: string | null, c: string | null, a: Agg): string =>
    `${r === null ? 'All' : dimLabel(ex, rd, r)}, ${c === null ? 'all' : colLabel(c)}: ${mi.label} ${fmtAgg(metric, a)}`;
  const cellBtn = (r: string | null, c: string | null, a: Agg, total: boolean): HTMLElement => {
    const bin = total || a.n === 0 ? null : rampBin(a.value, pv.max);
    const showCount = metric !== 'episodes';
    const btn = h('button', { type: 'button', 'data-cell': '', 'data-fk': `p:${r ?? '*'}:${c ?? '*'}`, 'aria-label': a.n ? `${label(r, c, a)}. Open episodes.` : `${label(r, c, a)}`,
      class: `ex-hc${bin === null ? '' : ` q${bin}`}${a.n === 0 ? ' ex-empty' : ''}${total ? ' ex-total' : ''}${!total && metric !== 'episodes' && a.n > 0 && a.n < LOW_N ? ' ex-lown' : ''}`,
      'aria-disabled': a.n === 0 ? 'true' : null, onclick: () => { if (a.n) drill(r, c); } },
    h('span.ex-hv', null, a.n === 0 ? '–' : fmtMetric(metric, a.value)),
    showCount && a.n ? h('span.ex-hn', null, mi.kind === 'rate' ? `${a.k}/${a.n}` : `n=${a.n}`) : null);
    ctx.tip.bind(btn, () => {
      const lines = [`${r === null ? 'All' : dimLabel(ex, rd, r)} · ${c === null ? 'all' : colLabel(c)}`, `${mi.label}: ${fmtMetric(metric, a.value)}`];
      if (a.lo !== null && a.hi !== null) lines.push(`${a.k} of ${a.n} episodes; Wilson 95% CI ${Math.round(a.lo * 100)}–${Math.round(a.hi * 100)}%`);
      else lines.push(`${a.n} episodes`);
      if (a.n > 0) lines.push('Click to list these episodes');
      return lines;
    });
    return btn;
  };
  const table = h('table.ex-heat', null,
    h('caption', null, `${mi.label} by ${DIM_INFO[rd].label.toLowerCase()}${same ? '' : ` and ${DIM_INFO[cd].label.toLowerCase()}`}`,
      h('span.muted', null, ` · ${ctx.eps.length} episodes`)),
    h('thead', null, h('tr', null,
      h('th', { scope: 'col', class: 'ex-corner' }, h('span', null, DIM_INFO[rd].label), h('span.muted', null, same ? '' : ` \\ ${DIM_INFO[cd].label}`)),
      pv.cols.map((c) => h('th', { scope: 'col' }, colLabel(c))),
      same ? null : h('th', { scope: 'col', class: 'ex-total-h' }, 'All'))),
    h('tbody', null,
      pv.rows.map((r, i) => h('tr', null,
        h('th', { scope: 'row' }, dimLabel(ex, rd, r)),
        pv.cols.map((c, j) => h('td', null, cellBtn(r, same ? null : c, (pv.cells[i] as Agg[])[j] as Agg, false))),
        same ? null : h('td', null, cellBtn(r, null, pv.rowTotals[i] as Agg, true)))),
      same ? null : h('tr.ex-total-r', null,
        h('th', { scope: 'row' }, 'All'),
        pv.cols.map((c, j) => h('td', null, cellBtn(null, c, pv.colTotals[j] as Agg, true))),
        h('td', null, cellBtn(null, null, pv.total, true)))));
  gridNav(table);
  host.replaceChildren(
    controls,
    h('p.ex-desc', null, h('strong', null, `${mi.label}: `), mi.desc),
    h('p.ex-desc.ex-small', null, h('strong', null, `${DIM_INFO[rd].label}: `), DIM_INFO[rd].desc,
      same ? null : [' ', h('strong', null, `${DIM_INFO[cd].label}: `), DIM_INFO[cd].desc]),
    hdr,
    pv.rows.length ? h('div.ex-scroll', { tabindex: '0', role: 'region', 'aria-label': 'Pivot table (scrollable)' }, table)
      : h('p.ex-none', null, 'No episodes match the filters.'),
    h('p.ex-note.muted', null, 'Colour shows the metric on a shared scale from 0 to the largest cell; the “All” margins are not coloured. ',
      metric === 'episodes' ? '' : `A dashed outline marks cells with fewer than ${LOW_N} episodes. `,
      MULTI_DIMS.includes(rd) || MULTI_DIMS.includes(cd) ? 'Agent trigger is per agent: an episode counts under every code its agents carry, so cells do not add up to the “All” margins. ' : '',
      'Arrow keys move between cells; Enter lists the episodes behind a cell.'));
}
