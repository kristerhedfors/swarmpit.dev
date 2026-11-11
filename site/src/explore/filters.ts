// Global filter bar: one multi-select chip group per dimension. A chip's count is the number of episodes it
// would match given every OTHER active filter, so empty combinations are visible before you click.
import { h } from '../util/dom.js';
import { DIMS, DIM_INFO, applyFilters, dimDomain, dimLabel, dimValues, filtersActive, modelInfo } from './data.js';
import type { Ctx } from './ui.js';
import { short } from './ui.js';

export function renderFilters(host: HTMLElement, ctx: Ctx): void {
  const { ex, state } = ctx;
  const groups = DIMS.map((dim) => {
    const sel = state.filters[dim];
    const others = applyFilters(ex, state.filters, state.seed, dim);
    const counts = new Map<string, number>();
    for (const e of others) for (const v of dimValues(ex, e, dim)) counts.set(v, (counts.get(v) ?? 0) + 1);
    const values = dimDomain(ex, dim);
    const toggle = (v: string): void => {
      const next = sel.includes(v) ? sel.filter((x) => x !== v) : [...sel, v];
      ctx.set({ filters: { ...state.filters, [dim]: next }, page: 0 });
    };
    return h('fieldset.ex-fg', null,
      h('legend', { title: DIM_INFO[dim].desc }, DIM_INFO[dim].label),
      h('button.ex-chip.ex-all', { type: 'button', 'aria-pressed': String(!sel.length), 'data-fk': `f:${dim}:*`,
        'aria-label': `${DIM_INFO[dim].label}: all`, onclick: () => ctx.set({ filters: { ...state.filters, [dim]: [] }, page: 0 }) }, 'All'),
      values.map((v) => {
        const n = counts.get(v) ?? 0;
        const retired = dim === 'model' && modelInfo(ex, v)?.retired;
        const label = dimLabel(ex, dim, v);
        return h('button.ex-chip', { type: 'button', 'aria-pressed': String(sel.includes(v)), 'data-fk': `f:${dim}:${v}`,
          class: `ex-chip${n ? '' : ' ex-zero'}${retired ? ' ex-retired' : ''}`,
          'aria-label': `${DIM_INFO[dim].label} ${label}${retired ? ' (retired)' : ''}: ${n} episodes`, onclick: () => toggle(v) },
        label, retired ? h('span.ex-tag', null, 'retired') : null, h('span.ex-n', { 'aria-hidden': 'true' }, String(n)));
      }));
  });
  const active = filtersActive(state.filters, state.seed);
  host.replaceChildren(
    h('div.ex-fgs', null, groups),
    h('div.ex-fbar', null,
      h('span.ex-count', { 'aria-live': 'polite' }, h('strong', null, String(ctx.eps.length)), ` of ${ex.episodes.length} episodes`),
      state.seed ? h('button.ex-chip.ex-seedchip', { type: 'button', 'aria-pressed': 'true', 'data-fk': 'f:seed',
        'aria-label': `Seed ${state.seed}: remove`, onclick: () => ctx.set({ seed: '', page: 0 }) }, `seed ${short(state.seed)} ×`) : null,
      active ? h('button.ex-btn', { type: 'button', 'data-fk': 'f:reset', onclick: () => ctx.set({ filters: { ...emptyAll(ctx) }, seed: '', page: 0 }) }, 'Clear all filters') : null));
}

function emptyAll(ctx: Ctx) {
  const f = { ...ctx.state.filters };
  for (const d of DIMS) f[d] = [];
  return f;
}
