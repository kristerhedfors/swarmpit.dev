// Cell x metric heatmap, normalised per column (sequential single-hue ramp), raw values as text
// (ink picked by fill luminance) and in the tooltip.
import { h, tooltip } from '../dom';
import { seq, seqInk } from '../lib/colors';
import { byUnit, num } from '../lib/format';
import type { Cell, MetricDef } from '../lib/types';

export function heatmap(cells: Cell[], metrics: MetricDef[], label: (c: Cell) => string, onRow?: (c: Cell) => void): HTMLElement {
  const tip = tooltip();
  const ranges = metrics.map((m) => {
    const vs = cells.map((c) => c.values[m.metric_id]?.mean).filter((v): v is number => v != null && Number.isFinite(v));
    return vs.length ? [Math.min(...vs), Math.max(...vs)] : [0, 1];
  });
  const table = h('table', { class: 'tbl heat' },
    h('thead', null, h('tr', null, h('th', null, 'cell'), h('th', { class: 'num' }, 'runs'), ...metrics.map((m) => h('th', { class: 'num', title: m.description }, m.label)))),
    h('tbody', null, ...cells.map((c) => h('tr', { onclick: () => onRow?.(c), style: { cursor: onRow ? 'pointer' : '' } },
      h('td', null, label(c)), h('td', { class: 'num' }, String(c.n_runs)),
      ...metrics.map((m, j) => {
        const v = c.values[m.metric_id];
        const [lo, hi] = ranges[j];
        if (v?.na) return h('td', { class: 'cell muted', title: v.na }, 'n/a');
        if (!v || v.mean == null) return h('td', { class: 'cell muted' }, '–');
        const t = hi > lo ? (v.mean - lo) / (hi - lo) : 0.5;
        const td = h('td', { class: 'cell', style: { background: seq(t), color: seqInk(t) } }, byUnit(v.mean, m.unit));
        td.addEventListener('pointermove', (ev) => tip.show(ev.clientX, ev.clientY,
          h('div', null, h('span', { class: 'v' }, byUnit(v.mean, m.unit)), ' ', h('span', { class: 'k' }, m.label)),
          h('div', { class: 'k' }, label(c)),
          h('div', { class: 'k' }, v.lo != null ? `95% CI ${byUnit(v.lo, m.unit)} – ${byUnit(v.hi, m.unit)}` : 'no CI (< 2 runs)'),
          h('div', { class: 'k' }, `n=${v.n} · sd ${num(v.sd, 2)}`)));
        td.addEventListener('pointerleave', () => tip.hide());
        return td;
      })))));
  return h('div', { class: 'tbl-wrap' }, table);
}
