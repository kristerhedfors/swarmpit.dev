// Time-to-discovery curve per cell: cumulative share of agents that touched the board by step
// (or seconds). Undiscovered agents are right-censored at their last step. With one shared censoring
// horizon this equals Kaplan-Meier 1-S(t).
//
// Colour comes from the caller (the overview's colorOf, so solo/multi mean the same thing here as in the
// forest plots above); cells that share a colour inside a panel are told apart by dash pattern, and every
// line is listed in the legend with its own colour + dash sample. With several models (or another first
// group dim with several values) the chart becomes small multiples, one panel per value, so no model is
// ever dropped.
import { h } from '../dom';
import type { Cell } from '../lib/types';
import { CumChart } from './sparkline';

const DASHES: number[][] = [[], [6, 3], [2, 2], [9, 3, 2, 3], [4, 4], [1, 3]];
const MAX_PER_PANEL = 16;

export interface EcdfOpts {
  color: (c: Cell) => string;
  /** split into one panel per distinct value (e.g. model); null = one panel */
  panelOf?: ((c: Cell) => string) | null;
}

function dashSample(color: string, dash: number[]): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', '22'); svg.setAttribute('height', '8'); svg.setAttribute('aria-hidden', 'true');
  const ln = document.createElementNS(ns, 'line');
  ln.setAttribute('x1', '0'); ln.setAttribute('x2', '22'); ln.setAttribute('y1', '4'); ln.setAttribute('y2', '4');
  ln.setAttribute('stroke', color); ln.setAttribute('stroke-width', '2');
  if (dash.length) ln.setAttribute('stroke-dasharray', dash.join(' '));
  svg.appendChild(ln);
  return svg;
}

function panel(title: string, cells: Cell[], label: (c: Cell) => string, mode: 'step' | 's', o: EcdfOpts): HTMLElement {
  const key = mode === 'step' ? 'first_contact' : 'time_to_discovery_s';
  const shown = cells.slice(0, MAX_PER_PANEL);
  const chart = new CumChart({ title, unit: 'share of agents', height: 180,
    fmt: (v) => Math.round(v * 100) + '%', xFmt: (v) => (mode === 'step' ? String(Math.round(v)) : Math.round(v) + ' s') });
  const allX = new Set<number>([0]);
  const per = shown.map((c) => {
    const pts = (c.points ?? []).map((p) => (typeof p[key] === 'number' ? (p[key] as number) : null));
    pts.forEach((v) => { if (v != null) allX.add(v); });
    return pts;
  });
  allX.add(Math.max(1, ...allX));
  const xs = [...allX].sort((a, b) => a - b);
  const usedByColor = new Map<string, number>();
  const series = shown.map((c, i) => {
    const color = o.color(c);
    const k = usedByColor.get(color) ?? 0;
    usedByColor.set(color, k + 1);
    const pts = per[i]; const n = pts.length || 1;
    return { label: label(c), color, dash: DASHES[k % DASHES.length], width: 2, step: true,
      y: xs.map((x) => pts.filter((v) => v != null && v <= x).length / n) };
  });
  chart.setData(xs, series);
  chart.el.appendChild(h('div', { class: 'legend', style: { margin: '4px 0 0' } },
    ...series.map((s2) => h('span', { class: 'it' }, dashSample(s2.color, s2.dash), s2.label)),
    cells.length > MAX_PER_PANEL ? h('span', { class: 'muted' }, `+${cells.length - MAX_PER_PANEL} more cells in this panel (filter to compare)`) : null));
  return chart.el;
}

export function ecdf(cells: Cell[], label: (c: Cell) => string, mode: 'step' | 's', o: EcdfOpts): HTMLElement {
  const base = `Time to discovery (${mode === 'step' ? 'step' : 'seconds'})`;
  const groups = new Map<string, Cell[]>();
  for (const c of cells) { const k = o.panelOf ? o.panelOf(c) : ''; const a = groups.get(k) ?? []; a.push(c); groups.set(k, a); }
  const note = h('div', { class: 'muted', style: { fontSize: '11px', marginTop: '4px' } }, 'agents that never touched the board are censored at their last step · colour = same encoding as the forest plots · dash = distinguishes cells of the same colour');
  if (groups.size <= 1) {
    const el = panel(base, cells, label, mode, o);
    el.appendChild(note);
    return el;
  }
  return h('div', null,
    h('div', { class: 'grid', style: { gridTemplateColumns: `repeat(auto-fit, minmax(${groups.size > 2 ? 320 : 380}px, 1fr))`, gap: '12px' } },
      ...[...groups].map(([k, cs]) => panel(`${base} · ${k}`, cs, label, mode, o))),
    note);
}
