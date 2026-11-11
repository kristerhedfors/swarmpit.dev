// Forest plot: one row per cell, dot = mean, line = run-cluster bootstrap 95% CI, faint strip of
// per-agent points behind it. Colour carries the cell's colour dimension (pop by default); every
// row is also text-labelled so identity never rests on colour.
import { h, onResize, replace, s, tooltip } from '../dom';
import { byUnit, num } from '../lib/format';
import { ticks } from '../lib/time';
import type { Cell, MetricDef } from '../lib/types';
import { cv } from '../lib/colors';

export interface ForestOpts {
  metric: MetricDef;
  cells: Cell[];
  label: (c: Cell) => string;
  color: (c: Cell) => string;
  onRow?: (c: Cell) => void;
  /** rows are grouped under a header per value (e.g. model) */
  groupOf?: ((c: Cell) => string) | null;
  /** mean-marker shape index (0 circle, 1 square, 2 diamond, 3 triangle), e.g. per model */
  shapeOf?: ((c: Cell) => number) | null;
}

function marker(shape: number, cx: number, cy: number, r: number, attrs: Record<string, string | number>): SVGElement {
  switch (shape % 4) {
    case 1: return s('rect', { x: cx - r * 0.85, y: cy - r * 0.85, width: r * 1.7, height: r * 1.7, ...attrs });
    case 2: return s('path', { d: `M${cx},${cy - r * 1.15} L${cx + r * 1.15},${cy} L${cx},${cy + r * 1.15} L${cx - r * 1.15},${cy} Z`, ...attrs });
    case 3: return s('path', { d: `M${cx},${cy - r * 1.15} L${cx + r},${cy + r * 0.75} L${cx - r},${cy + r * 0.75} Z`, ...attrs });
    default: return s('circle', { cx, cy, r, ...attrs });
  }
}

export function forest(o: ForestOpts): HTMLElement {
  const box = h('div', { class: 'forest' });
  const tip = tooltip();
  const card = h('div', { class: 'card' },
    h('header', null, h('h2', null, o.metric.label), h('span', { class: 'sub' }, `${o.metric.unit} · ${o.metric.higher_is === 'stickier' ? 'higher = stickier' : o.metric.higher_is === 'better' ? 'higher = better' : 'neutral'}`)),
    box);
  // layout rows: optional group header rows (e.g. one per model), then the group's cells
  type Row = { head: string } | { cell: Cell };
  const rows: Row[] = [];
  if (o.groupOf) {
    const order: string[] = []; const by = new Map<string, Cell[]>();
    for (const c of o.cells) { const g = o.groupOf(c); if (!by.has(g)) { by.set(g, []); order.push(g); } by.get(g)!.push(c); }
    for (const g of order) { rows.push({ head: g }); for (const c of by.get(g)!) rows.push({ cell: c }); }
  } else for (const c of o.cells) rows.push({ cell: c });
  const draw = (W: number) => {
    const rowH = 22, headH = 18, top = 4, labelW = Math.min(170, W * 0.42), right = 12, bottom = 22;
    const Hh = top + rows.reduce((a, r) => a + ('head' in r ? headH : rowH), 0) + bottom;
    const dark = document.documentElement.dataset.resolvedTheme === 'dark';
    const stripOpacity = dark ? 0.5 : 0.3;
    const id = o.metric.metric_id;
    const vals: number[] = [];
    for (const c of o.cells) {
      const v = c.values[id]; if (!v) continue;
      for (const x of [v.mean, v.lo, v.hi]) if (x != null && Number.isFinite(x)) vals.push(x);
      for (const p of c.points ?? []) { const pv = p[id]; const n = typeof pv === 'boolean' ? +pv : pv; if (typeof n === 'number' && Number.isFinite(n)) vals.push(n); }
    }
    if (!vals.length) { replace(box, h('div', { class: 'empty' }, 'no data')); return; }
    let lo = Math.min(0, ...vals), hi = Math.max(...vals);
    if (o.metric.unit === 'share' || o.metric.unit === 'bool') { lo = 0; hi = Math.max(1, hi); }
    if (hi === lo) hi = lo + 1;
    const x = (v: number) => labelW + ((v - lo) / (hi - lo)) * (W - labelW - right);
    const svg = s('svg', { viewBox: `0 0 ${W} ${Hh}`, width: W, height: Hh, role: 'img', 'aria-label': `${o.metric.label} by cell` });
    const tk = ticks(lo, hi, Math.max(2, Math.floor((W - labelW) / 60)));
    for (const t of tk) {
      svg.appendChild(s('line', { x1: x(t), x2: x(t), y1: top, y2: Hh - bottom, stroke: cv('--grid'), 'stroke-width': 1 }));
      svg.appendChild(s('text', { x: x(t), y: Hh - 6, 'text-anchor': 'middle' }, o.metric.unit === 'share' || o.metric.unit === 'bool' ? Math.round(t * 100) + '%' : num(t, t % 1 ? 1 : 0)));
    }
    if (lo < 0 || lo === 0) svg.appendChild(s('line', { x1: x(Math.max(lo, 0)), x2: x(Math.max(lo, 0)), y1: top, y2: Hh - bottom, stroke: cv('--axis'), 'stroke-width': 1 }));
    let yCursor = top;
    rows.forEach((row) => {
      if ('head' in row) {
        svg.appendChild(s('text', { x: 4, y: yCursor + headH - 5, class: 'lbl', 'font-weight': '600' }, trunc(row.head, Math.floor((W - 8) / 6.2))));
        yCursor += headH;
        return;
      }
      const c = row.cell;
      const y = yCursor + rowH / 2;
      yCursor += rowH;
      const v = c.values[id];
      const color = o.color(c);
      const g = s('g', { style: o.onRow ? 'cursor:pointer' : '' });
      g.appendChild(s('rect', { x: 0, y: y - rowH / 2, width: W, height: rowH, fill: 'transparent' }));
      g.appendChild(s('text', { x: labelW - 8, y: y + 4, 'text-anchor': 'end', class: 'lbl' }, trunc(o.label(c), Math.floor(labelW / 6.2))));
      // strip points (faint)
      (c.points ?? []).forEach((p, k) => {
        const pv = p[id]; const n = typeof pv === 'boolean' ? +pv : pv;
        if (typeof n !== 'number' || !Number.isFinite(n)) return;
        const jitter = ((k * 7919) % 11) / 11 - 0.5;
        g.appendChild(s('circle', { cx: x(n), cy: y + jitter * 10, r: 2, fill: color, 'fill-opacity': stripOpacity }));
      });
      if (v && v.lo != null && v.hi != null) g.appendChild(s('line', { x1: x(v.lo), x2: x(v.hi), y1: y, y2: y, stroke: color, 'stroke-width': 2, 'stroke-linecap': 'round' }));
      if (v?.na) g.appendChild(s('text', { x: labelW + 4, y: y + 4, class: 'lbl', 'fill-opacity': 0.6 }, 'n/a (no free phase)'));
      if (v && v.mean != null) g.appendChild(marker(o.shapeOf ? o.shapeOf(c) : 0, x(v.mean), y, 4.5, { fill: color, stroke: cv('--surface-1'), 'stroke-width': 2 }));
      g.addEventListener('pointermove', (ev) => tip.show(ev.clientX, ev.clientY,
        h('div', null, h('span', { class: 'v' }, v?.na ? 'n/a' : byUnit(v?.mean ?? null, o.metric.unit)), ' ', h('span', { class: 'k' }, (o.groupOf ? o.groupOf(c) + ' · ' : '') + o.label(c))),
        v?.na ? h('div', { class: 'k' }, v.na) : null,
        h('div', { class: 'k' }, v && v.lo != null ? `95% CI ${byUnit(v.lo, o.metric.unit)} – ${byUnit(v.hi, o.metric.unit)} (run bootstrap)` : 'CI needs ≥ 2 runs'),
        h('div', { class: 'k' }, `median ${byUnit(v?.median ?? null, o.metric.unit)} · sd ${num(v?.sd ?? null, 2)} · n=${v?.n ?? 0} agents · ${c.n_runs} runs`)));
      g.addEventListener('pointerleave', () => tip.hide());
      if (o.onRow) g.addEventListener('click', () => o.onRow!(c));
      svg.appendChild(g);
    });
    replace(box, svg);
  };
  let lastW = 0;
  onResize(box, (w) => { if (Math.abs(w - lastW) > 4) { lastW = w; draw(w); } });
  return card;
}
function trunc(s2: string, n: number) { return s2.length > n ? s2.slice(0, n - 1) + '…' : s2; }
