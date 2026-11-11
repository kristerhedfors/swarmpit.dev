// Interaction graph (CONTRACT 6.2.3). d3-force layout computed once on the full-run graph (so
// nodes do not jump while scrubbing); edges fade in by first_t <= cursor. Seed personas are squares,
// agents circles, unresolved handles diamonds; node size = in-weight. > 20 nodes -> adjacency matrix.
import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationNodeDatum } from 'd3-force';
import { fitCanvas, h, onResize, replace, s, tooltip } from '../dom';
import { agentColor, cv, EDGE_KINDS, edgeColor, onThemeChange, seq } from '../lib/colors';
import { clockT, num } from '../lib/format';
import type { Graph, GraphEdge, GraphNode } from '../lib/types';

interface N extends SimulationNodeDatum { id: string; node: GraphNode; r: number }

export class GraphView {
  el = h('div', { class: 'graph' });
  private body = h('div');
  private g: Graph | null = null;
  private nodes: N[] = [];
  private kinds = new Set<string>(['reply', 'mention', 'dm', 'upvote', 'exposure']);
  private showSeed = true;
  private t = Infinity;
  private W = 380;
  private svg: SVGSVGElement | null = null;
  private edgeEls: { e: GraphEdge; el: SVGPathElement }[] = [];
  private nodeEls: { n: N; el: SVGGElement }[] = [];
  private tip = tooltip();
  private summaryEl = h('div', { class: 'muted', style: { fontSize: '11px', marginTop: '6px' } });
  private matrix: HTMLCanvasElement | null = null;
  private cleanup: (() => void)[] = [];

  constructor() {
    const ctl = h('div', { class: 'legend', style: { marginBottom: '6px' } },
      ...EDGE_KINDS.map((k) => h('label', { class: 'it' },
        h('input', { type: 'checkbox', checked: this.kinds.has(k), onchange: (e: Event) => { (e.target as HTMLInputElement).checked ? this.kinds.add(k) : this.kinds.delete(k); this.render(); } }),
        h('i', { class: 'ln', style: { background: edgeColor(k), ...(k === 'upvote' ? { backgroundImage: 'repeating-linear-gradient(90deg, transparent 0 3px, var(--surface-1) 3px 5px)' } : {}) } }), k)),
      h('label', { class: 'it' }, h('input', { type: 'checkbox', checked: true, onchange: (e: Event) => { this.showSeed = (e.target as HTMLInputElement).checked; this.layout(); this.render(); } }), 'seed personas'));
    const legend = h('div', { class: 'legend', style: { marginBottom: '4px' } },
      h('span', { class: 'it' }, '● agent'), h('span', { class: 'it' }, '■ seed persona'), h('span', { class: 'it' }, '◆ unresolved handle'),
      h('span', { class: 'it muted' }, 'arrow: src attends to dst · size = in-weight'));
    this.el.append(ctl, legend, this.body, this.summaryEl);
    this.cleanup.push(onResize(this.el, (w) => { if (Math.abs(w - this.W) > 20) { this.W = Math.max(240, w); this.layout(); this.render(); } }));
    this.cleanup.push(onThemeChange(() => { this.layout(); this.render(); }));
  }
  destroy() { this.cleanup.forEach((f) => f()); this.tip.hide(); }

  setGraph(g: Graph) { this.g = g; this.layout(); this.render(); }

  setT(t: number) {
    if (this.t === t) return;
    this.t = t;
    this.applyT();
  }

  private visibleNodes(): GraphNode[] {
    if (!this.g) return [];
    return this.g.nodes.filter((n) => this.showSeed || n.kind === 'agent');
  }

  private layout() {
    const g = this.g; if (!g) return;
    const ns = this.visibleNodes();
    if (ns.length > 20) { this.layoutMatrix(ns); return; }
    this.matrix = null;
    const H = Math.max(260, Math.min(420, this.W * 0.85));
    const maxIn = Math.max(1, ...ns.map((n) => n.in_w));
    this.nodes = ns.map((n, i) => ({ id: n.id, node: n, r: 5 + 8 * Math.sqrt(n.in_w / maxIn), x: this.W / 2 + Math.cos(i * 2.4) * (40 + i * 6), y: H / 2 + Math.sin(i * 2.4) * (40 + i * 6) }));
    const ids = new Set(this.nodes.map((n) => n.id));
    const links = g.edges.filter((e) => ids.has(e.src) && ids.has(e.dst) && e.src !== e.dst).map((e) => ({ source: e.src, target: e.dst, w: e.w, k: e.kind }));
    const sim = forceSimulation(this.nodes)
      .force('link', forceLink<N, { source: string; target: string; w: number; k: string }>(links).id((d) => d.id).distance((l) => (l.k === 'exposure' ? 130 : 90)).strength((l) => (l.k === 'exposure' ? 0.02 : 0.15)))
      .force('charge', forceManyBody().strength(-420))
      .force('x', forceX<N>(this.W / 2).strength((d) => (d.node.kind === 'agent' ? 0.06 : 0.03)))
      .force('y', forceY<N>(H / 2).strength(0.05))
      .force('collide', forceCollide<N>().radius((d) => d.r + 16))
      .stop();
    for (let i = 0; i < 300; i++) sim.tick();
    for (const n of this.nodes) { n.x = Math.max(n.r + 4, Math.min(this.W - n.r - 4, n.x ?? 0)); n.y = Math.max(n.r + 4, Math.min(H - n.r - 14, n.y ?? 0)); }
    this.svgH = H;
  }
  private svgH = 300;

  private render() {
    const g = this.g; if (!g) { replace(this.body, h('div', { class: 'empty' }, 'no graph')); return; }
    if (this.matrix) { replace(this.body, this.matrix); this.drawMatrix(); this.summary(); return; }
    const byId = new Map(this.nodes.map((n) => [n.id, n]));
    const svg = s('svg', { viewBox: `0 0 ${this.W} ${this.svgH}`, height: this.svgH, role: 'img', 'aria-label': 'Interaction graph' });
    const defs = s('defs');
    for (const k of EDGE_KINDS) defs.appendChild(s('marker', { id: 'arr-' + k, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 8, markerHeight: 8, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' }, s('path', { d: 'M0,0 L10,5 L0,10 z', fill: edgeColor(k) })));
    svg.appendChild(defs);
    const eg = s('g'), ng = s('g');
    svg.append(eg, ng);
    this.edgeEls = [];
    const pairCount = new Map<string, number>();
    for (const e of g.edges) {
      if (!this.kinds.has(e.kind)) continue;
      const a = byId.get(e.src), b = byId.get(e.dst);
      if (!a || !b || a === b) continue;
      const key = [e.src, e.dst].sort().join('|');
      const k = pairCount.get(key) ?? 0; pairCount.set(key, k + 1);
      const bend = (e.src < e.dst ? 1 : -1) * (14 + k * 10);
      const x1 = a.x!, y1 = a.y!, x2 = b.x!, y2 = b.y!;
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
      // shorten to node radius
      const ex = x2 - (dx / len) * (b.r + 3), ey = y2 - (dy / len) * (b.r + 3);
      const mx = (x1 + ex) / 2 - (dy / len) * bend, my = (y1 + ey) / 2 + (dx / len) * bend;
      const el = s('path', { class: 'edge', d: `M${x1},${y1} Q${mx},${my} ${ex},${ey}`, stroke: edgeColor(e.kind), 'stroke-width': e.kind === 'exposure' ? Math.min(2.5, 0.75 + Math.log2(1 + e.w) * 0.4) : Math.min(4, 1.25 + Math.log2(1 + e.w) * 0.8), 'stroke-opacity': e.kind === 'exposure' ? 0.55 : 1,
        'stroke-dasharray': e.kind === 'upvote' ? '4 3' : null, 'marker-end': `url(#arr-${e.kind})` });
      el.addEventListener('pointermove', (ev) => this.tip.show(ev.clientX, ev.clientY,
        h('div', null, h('span', { class: 'v' }, `${e.kind} ×${e.n}`), ' ', h('span', { class: 'k' }, `w ${num(e.w, 2)}`)),
        h('div', { class: 'k' }, `${e.src} → ${e.dst}`), h('div', { class: 'k' }, `first ${clockT(e.first_t)} · last ${clockT(e.last_t)}`)));
      el.addEventListener('pointerleave', () => this.tip.hide());
      eg.appendChild(el);
      this.edgeEls.push({ e, el });
    }
    this.nodeEls = [];
    const surf = cv('--surface-1');
    for (const n of this.nodes) {
      const nd = n.node;
      const fill = nd.kind === 'agent' ? agentColor(nd.agent_id ?? -1) : cv('--neutral-mark');
      const shape = nd.kind === 'agent' ? s('circle', { r: n.r, fill, stroke: surf, 'stroke-width': 2 })
        : nd.kind === 'seed' ? s('rect', { x: -n.r, y: -n.r, width: 2 * n.r, height: 2 * n.r, rx: 2, fill, stroke: surf, 'stroke-width': 2 })
        : s('path', { d: `M0,${-n.r} L${n.r},0 L0,${n.r} L${-n.r},0 Z`, fill, stroke: surf, 'stroke-width': 2 });
      const el = s('g', { class: 'node', transform: `translate(${n.x},${n.y})` }, shape, s('text', { y: n.r + 11, 'text-anchor': 'middle' }, nd.label));
      el.addEventListener('pointermove', (ev) => this.tip.show(ev.clientX, ev.clientY,
        h('div', null, h('span', { class: 'v' }, nd.label), ' ', h('span', { class: 'k' }, nd.kind)),
        nd.names?.length ? h('div', { class: 'k' }, 'names: ' + nd.names.join(', ')) : null,
        h('div', { class: 'k' }, `posts ${nd.posts} · in ${num(nd.in_w, 2)} · out ${num(nd.out_w, 2)}`),
        nd.solved_t != null ? h('div', { class: 'k' }, 'solved at ' + clockT(nd.solved_t)) : null));
      el.addEventListener('pointerleave', () => this.tip.hide());
      ng.appendChild(el);
      this.nodeEls.push({ n, el });
    }
    this.svg = svg;
    replace(this.body, svg);
    this.applyT();
  }

  private applyT() {
    const t = this.t;
    if (this.matrix) { this.drawMatrix(); this.summary(); return; }
    if (!this.svg) return;
    const active = new Set<string>();
    for (const { e, el } of this.edgeEls) {
      const on = e.first_t <= t;
      el.style.display = on ? '' : 'none';
      if (on) { active.add(e.src); active.add(e.dst); el.style.opacity = e.last_t >= t - 60 ? '1' : '0.5'; }
    }
    for (const { n, el } of this.nodeEls) el.style.opacity = n.node.kind === 'agent' || active.has(n.id) ? '1' : '0.3';
    this.summary();
  }

  private summary() {
    const g = this.g; if (!g) return;
    const ll = g.edges.filter((e) => e.first_t <= this.t && e.src.startsWith('a') && e.dst.startsWith('a') && e.src !== e.dst);
    const pairs = new Set(ll.map((e) => e.src + '>' + e.dst));
    const nA = g.nodes.filter((n) => n.kind === 'agent').length;
    const recip = [...pairs].filter((p) => { const [a, b] = p.split('>'); return pairs.has(b + '>' + a); }).length;
    const llN = ll.reduce((acc, e) => acc + (e.ts && e.ts.length ? e.ts.filter((x) => (x ?? e.first_t) <= this.t).length : e.n || 1), 0);
    this.summaryEl.textContent = `at cursor: ${llN} live-live interactions (${pairs.size} pairs) · density ${nA > 1 ? num(pairs.size / (nA * (nA - 1)), 2) : '–'} · reciprocity ${pairs.size ? num(recip / pairs.size, 2) : '–'} · full run: ${g.summary.live_live_edges}`;
  }

  // ------------------------------------------------------------------ adjacency matrix (> 20 nodes)
  private mNodes: GraphNode[] = [];
  private layoutMatrix(ns: GraphNode[]) {
    this.mNodes = [...ns].sort((a, b) => (a.kind === b.kind ? b.in_w - a.in_w : a.kind === 'agent' ? -1 : 1));
    this.matrix = h('canvas');
    this.matrix.addEventListener('pointermove', (ev) => {
      const r = this.matrix!.getBoundingClientRect();
      const n = this.mNodes.length, cell = (Math.min(this.W, 520) - 70) / n;
      const i = Math.floor((ev.clientY - r.top - 70) / cell), j = Math.floor((ev.clientX - r.left - 70) / cell);
      const a = this.mNodes[i], b = this.mNodes[j];
      if (!a || !b) { this.tip.hide(); return; }
      const es = this.g!.edges.filter((e) => e.src === a.id && e.dst === b.id && this.kinds.has(e.kind) && e.first_t <= this.t);
      this.tip.show(ev.clientX, ev.clientY, h('div', null, h('span', { class: 'v' }, num(es.reduce((s2, e) => s2 + e.w, 0), 2)), ' ', h('span', { class: 'k' }, `${a.label} → ${b.label}`)), ...es.map((e) => h('div', { class: 'k' }, `${e.kind} ×${e.n}`)));
    });
    this.matrix.addEventListener('pointerleave', () => this.tip.hide());
  }
  private drawMatrix() {
    const c = this.matrix; if (!c || !this.g) return;
    const n = this.mNodes.length, size = Math.min(this.W, 520), cell = (size - 70) / n;
    const ctx = fitCanvas(c, size, size);
    ctx.clearRect(0, 0, size, size);
    const idx = new Map(this.mNodes.map((nd, i) => [nd.id, i]));
    const w = new Float64Array(n * n);
    for (const e of this.g.edges) {
      if (!this.kinds.has(e.kind) || e.first_t > this.t) continue;
      const i = idx.get(e.src), j = idx.get(e.dst);
      if (i == null || j == null) continue;
      w[i * n + j] += e.w;
    }
    const mx = Math.max(1e-9, ...w);
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
      const v = w[i * n + j]; if (!v) continue;
      ctx.fillStyle = seq(0.15 + 0.85 * (v / mx));
      ctx.fillRect(70 + j * cell + 1, 70 + i * cell + 1, cell - 2, cell - 2);
    }
    ctx.fillStyle = cv('--text-secondary'); ctx.font = '9px system-ui'; ctx.textBaseline = 'middle';
    this.mNodes.forEach((nd, i) => {
      ctx.textAlign = 'right'; ctx.fillText(nd.label.slice(0, 11), 66, 70 + i * cell + cell / 2);
      ctx.save(); ctx.translate(70 + i * cell + cell / 2, 66); ctx.rotate(-Math.PI / 3); ctx.textAlign = 'left'; ctx.fillText(nd.label.slice(0, 11), 0, 0); ctx.restore();
    });
  }
}
