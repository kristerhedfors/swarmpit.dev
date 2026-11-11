// Model comparison: per goal, each model's writer-episode (or pile-in) rate as a dot with its Wilson 95% interval.
import { h, s } from '../util/dom.js';
import { applyFilters, compareModels, dimLabel, metricInfo, modelInfo } from './data.js';
import type { Comparison } from './data.js';
import type { Ctx } from './ui.js';
import { pct, segmented } from './ui.js';

const LW = 190, RW = 120, PW = 420, ROW = 34, TOP = 26, BOT = 26;

export function renderModels(host: HTMLElement, ctx: Ctx): void {
  const { ex, state } = ctx;
  const metric = state.cmp;
  const mi = metricInfo(metric);
  // the Writers filter would make every rate trivially 0% or 100%, so it does not apply here
  const cmp = compareModels(ex, applyFilters(ex, { ...state.filters, writers: [] }, state.seed), metric);
  const controls = h('div.ex-controls', null,
    segmented('Metric', metric, [{ v: 'wrate', label: 'Writer-episode rate' }, { v: 'pile', label: 'Pile-in rate (≥2 writers)' }],
      (v) => ctx.set({ cmp: v }), 'm:metric'));
  const intro = h('p.ex-desc', null,
    metric === 'wrate' ? 'Share of episodes in which at least one agent wrote to the board.' : 'Share of episodes in which two or more agents wrote to the board.',
    ' Bars are 95% Wilson intervals: wide bars mean few episodes, so overlapping bars are not evidence of a difference. ',
    'Models that ran only a handful of episodes (for example only in generation 0) have very wide intervals. Hollow dots mark retired models. The Writers filter does not apply to this view. Select a row to list its episodes.');
  host.replaceChildren(controls, intro,
    cmp.length ? h('div.ex-cmps', null, cmp.map((c) => panel(c, ctx))) : h('p.ex-none', null, 'No episodes match the filters.'));
}

function panel(c: Comparison, ctx: Ctx): HTMLElement {
  const { ex, state } = ctx;
  const mi = metricInfo(state.cmp);
  const W = LW + PW + RW, H = TOP + c.rows.length * ROW + BOT;
  const X = (v: number): number => LW + v * PW;
  const svg = s('svg', { class: 'ex-cmp', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
    'aria-label': `${mi.label} by model, ${c.goal} goal. Values are listed in the table below.` });
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    svg.appendChild(s('line', { class: 'ex-grid', x1: X(t), x2: X(t), y1: TOP - 6, y2: H - BOT + 4 }));
    svg.appendChild(s('text', { class: 'ex-axis', x: X(t), y: H - 8, 'text-anchor': 'middle' }, pct(t)));
  }
  c.rows.forEach((r, i) => {
    const y = TOP + i * ROW + ROW / 2;
    const info = modelInfo(ex, r.model);
    const a = r.agg;
    const g = s('g', { class: 'ex-cmp-row', tabindex: '0', role: 'button', 'data-fk': `m:${c.goal}:${r.model}`,
      'aria-label': `${dimLabel(ex, 'model', r.model)}${info?.retired ? ' (retired)' : ''}: ${pct(a.value ?? 0)}, ${a.k} of ${a.n} episodes, 95% CI ${pct(a.lo ?? 0)} to ${pct(a.hi ?? 0)}. List these episodes.` },
    s('rect', { class: 'ex-hit', x: 0, y: y - ROW / 2, width: W, height: ROW, rx: 4 }),
    s('text', { class: 'ex-lab', x: LW - 12, y: y + 4, 'text-anchor': 'end' }, dimLabel(ex, 'model', r.model), info?.retired ? s('tspan', { class: 'ex-axis', dx: 6 }, 'retired') : null),
    s('line', { class: 'ex-ci', x1: X(a.lo ?? 0), x2: X(a.hi ?? 0), y1: y, y2: y }),
    s('line', { class: 'ex-ci-cap', x1: X(a.lo ?? 0), x2: X(a.lo ?? 0), y1: y - 5, y2: y + 5 }),
    s('line', { class: 'ex-ci-cap', x1: X(a.hi ?? 0), x2: X(a.hi ?? 0), y1: y - 5, y2: y + 5 }),
    s('circle', { class: info?.retired ? 'ex-pt ex-pt-ret' : 'ex-pt', cx: X(a.value ?? 0), cy: y, r: 6 }),
    s('text', { class: 'ex-val', x: LW + PW + 14, y: y + 4 }, `${pct(a.value ?? 0)}`, s('tspan', { class: 'ex-axis', dx: 6 }, `${a.k}/${a.n}`)));
    const open = (): void => {
      ctx.set({ filters: { ...state.filters, goal: [c.goal], model: [r.model] }, view: 'episodes', page: 0 }, true);
    };
    g.addEventListener('click', open);
    g.addEventListener('keydown', (e) => { const k = (e as KeyboardEvent).key; if (k === 'Enter' || k === ' ') { e.preventDefault(); open(); } });
    ctx.tip.bind(g, () => [`${dimLabel(ex, 'model', r.model)} · ${c.goal}`, `${mi.label}: ${pct(a.value ?? 0)}`,
      `${a.k} of ${a.n} episodes · Wilson 95% CI ${pct(a.lo ?? 0)}–${pct(a.hi ?? 0)}`,
      info ? `${info.provider} · ${info.origin}${info.size_b ? ` · ${info.size_b}B params` : ''}${info.retired ? ' · retired' : ''}` : '']);
    svg.appendChild(g);
  });
  // on narrow screens the chart scrolls sideways; open the table so the numbers are readable without scrolling
  const narrow = typeof matchMedia === 'function' && matchMedia('(max-width: 640px)').matches;
  const table = h('details.ex-tv', { open: narrow }, h('summary', null, 'Table view'),
    h('table', null, h('thead', null, h('tr', null, ['Model', mi.label, 'Episodes with event', 'Episodes', '95% CI'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', null, c.rows.map((r) => h('tr', null, h('th', { scope: 'row' }, dimLabel(ex, 'model', r.model), modelInfo(ex, r.model)?.retired ? ' (retired)' : ''),
        h('td.num', null, pct(r.agg.value ?? 0)), h('td.num', null, String(r.agg.k)), h('td.num', null, String(r.agg.n)),
        h('td.num', null, `${pct(r.agg.lo ?? 0)}–${pct(r.agg.hi ?? 0)}`))))));
  return h('figure.ex-fig', null, h('figcaption', null, h('strong', null, `${c.goal[0]?.toUpperCase() ?? ''}${c.goal.slice(1)} goal`),
    h('span.muted', null, ` · ${c.rows.reduce((n, r) => n + r.agg.n, 0)} episodes`)), h('div.ex-scroll', null, svg), table);
}
