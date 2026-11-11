// Small charts: `CumChart` (uPlot line/step chart with a synced crosshair, a run-cursor rule and a
// one-tooltip-every-series readout) and `sparkline` (tiny canvas, single series, no axes).
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { fitCanvas, h, onResize, tooltip } from '../dom';
import { cv, onThemeChange } from '../lib/colors';
import { clockT } from '../lib/format';

export interface CumSeries { label: string; color: string; y: (number | null)[]; step?: boolean; width?: number; dash?: number[] }
export interface CumOpts {
  title: string;
  unit?: string;
  height?: number;
  fmt?: (v: number) => string;
  xFmt?: (v: number) => string;
  sync?: string;
  yMin0?: boolean;
  onClickX?: (x: number) => void;
}

export class CumChart {
  el: HTMLDivElement;
  private plot: uPlot | null = null;
  private box = h('div');
  private cursorX: number | null = null;
  private data: uPlot.AlignedData = [[]];
  private series: CumSeries[] = [];
  private tip = tooltip();
  private W = 260;
  private cleanup: (() => void)[] = [];
  private xr: [number, number] | null = null;

  constructor(private o: CumOpts) {
    this.el = h('div', { class: 'card chart-card' }, h('h3', null, o.title, o.unit ? h('span', { class: 'muted', style: { fontWeight: '400' } }, ` (${o.unit})`) : null), this.box);
    this.cleanup.push(onResize(this.box, (w) => { if (Math.abs(w - this.W) > 2) { this.W = Math.max(160, w); this.plot?.setSize({ width: this.W, height: this.o.height ?? 120 }); } }));
    this.cleanup.push(onThemeChange(() => this.build()));
  }
  destroy() { this.cleanup.forEach((f) => f()); this.plot?.destroy(); this.tip.hide(); }

  setData(xs: number[], series: CumSeries[]) {
    const sameShape = this.plot && series.length === this.series.length && series.every((s, i) => s.label === this.series[i].label);
    this.series = series;
    this.data = [xs, ...series.map((s) => s.y)] as uPlot.AlignedData;
    if (sameShape) this.plot!.setData(this.data); else this.build();
  }

  setCursor(x: number | null) { this.cursorX = x; this.plot?.redraw(false, false); }

  /** Pin the x scale (e.g. to the run's [tMin, tMax]) so synced charts share one window. */
  setXRange(a: number, b: number) {
    if (this.xr && this.xr[0] === a && this.xr[1] === b) return;
    this.xr = b > a ? [a, b] : null;
    if (this.plot && this.xr) this.plot.setScale('x', { min: this.xr[0], max: this.xr[1] });
  }

  private build() {
    this.plot?.destroy();
    const axisStroke = cv('--text-muted'), grid = cv('--grid');
    const font = '10px system-ui, -apple-system, "Segoe UI", sans-serif';
    const fmt = this.o.fmt ?? ((v: number) => String(Math.round(v * 100) / 100));
    const opts: uPlot.Options = {
      width: this.W, height: this.o.height ?? 120,
      legend: { show: false },
      padding: [6, 16, 0, 0],
      cursor: { sync: this.o.sync ? { key: this.o.sync } : undefined, points: { size: 7, width: 2, stroke: () => cv('--surface-1') }, y: false, drag: { x: false, y: false } },
      scales: { x: { time: false, range: (_u, mn, mx) => (this.xr ? [this.xr[0], this.xr[1]] : [mn, mx]) }, y: { range: (_u, mn, mx) => [this.o.yMin0 === false ? mn : Math.min(0, mn), mx > 0 ? mx * 1.08 : 1] } },
      axes: [
        { stroke: axisStroke, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font, size: 22, values: (_u, vals) => vals.map((v) => (this.o.xFmt ?? ((x: number) => clockT(x).replace(/\.0$/, '')))(v)) },
        { stroke: axisStroke, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font, size: 38, values: (_u, vals) => vals.map((v) => fmt(v)) },
      ],
      series: [{}, ...this.series.map((s) => ({
        label: s.label, stroke: s.color, width: s.width ?? 2, points: { show: false }, dash: s.dash && s.dash.length ? s.dash : undefined,
        paths: s.step ? uPlot.paths.stepped!({ align: 1 }) : undefined, spanGaps: false,
      }))],
      hooks: {
        draw: [(u) => {
          if (this.cursorX == null) return;
          const x = u.valToPos(this.cursorX, 'x', true);
          if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) return;
          const ctx = u.ctx; ctx.save();
          ctx.strokeStyle = cv('--text-primary'); ctx.lineWidth = 1.5 * devicePixelRatio;
          ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke(); ctx.restore();
        }],
        setCursor: [(u) => {
          const i = u.cursor.idx;
          if (i == null || u.cursor.left == null || u.cursor.left < 0) { this.tip.hide(); return; }
          const r = u.over.getBoundingClientRect();
          const xs = u.data[0];
          const rows = this.series.map((s, k) => ({ s, v: (u.data[k + 1] as (number | null)[])[i] })).filter((r2) => r2.v != null).sort((a, b) => (b.v as number) - (a.v as number));
          if (!rows.length || !(u.cursor.event)) { this.tip.hide(); return; }
          this.tip.show(r.left + u.cursor.left, r.top + (u.cursor.top ?? 0),
            h('div', { class: 'k' }, (this.o.xFmt ?? clockT)(xs[i])),
            ...rows.slice(0, 10).map(({ s, v }) => h('div', null, h('i', { class: 'key', style: { background: s.color } }), h('span', { class: 'v' }, fmt(v as number)), ' ', h('span', { class: 'k' }, s.label))));
        }],
      },
    };
    this.plot = new uPlot(opts, this.data, this.box);
    this.plot.over.addEventListener('pointerleave', () => this.tip.hide());
    if (this.o.onClickX) this.plot.over.addEventListener('click', () => {
      const u = this.plot!; if (u.cursor.left == null) return;
      this.o.onClickX!(u.posToVal(u.cursor.left, 'x'));
    });
  }
}

export interface SparkOpts {
  /** shared y maximum (so rows are comparable); read at draw time */
  max?: () => number;
  /** shared number of x slots (so rows share one time scale); defaults to values.length */
  slots?: () => number;
  /** highlight the last bucket in the accent (only meaningful while the run is live) */
  live?: boolean;
}
export type Spark = HTMLCanvasElement & { redraw: () => void; dispose: () => void };

/** Tiny single-series bar sparkline. Bars are drawn on a shared scale when `max`/`slots` are given; the
 *  last bucket uses the accent only for live runs. Call dispose() when the element is discarded. */
export function sparkline(values: number[], w = 90, hgt = 18, o: SparkOpts = {}): Spark {
  const c = h('canvas', { 'aria-hidden': 'true' }) as Spark;
  const draw = () => {
    const ctx = fitCanvas(c, w, hgt);
    ctx.clearRect(0, 0, w, hgt);
    ctx.fillStyle = cv('--axis'); ctx.fillRect(0, hgt - 1, w, 1);
    if (!values.length) return;
    const mx = Math.max(1, o.max ? o.max() : 0, ...values);
    const n = Math.max(values.length, o.slots ? o.slots() : 0);
    const bw = w / n;
    const last = values.length - 1;
    values.forEach((v, i) => {
      if (!v) return;
      const bh = Math.max(1, (v / mx) * (hgt - 2));
      ctx.fillStyle = o.live && i === last ? cv('--accent') : cv('--neutral-mark');
      ctx.fillRect(i * bw, hgt - 1 - bh, Math.max(1, bw - 1), bh);
    });
  };
  draw();
  const off = onThemeChange(draw);
  c.redraw = draw;
  c.dispose = off;
  return c;
}
