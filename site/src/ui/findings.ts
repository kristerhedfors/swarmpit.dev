import type { Findings, Goal, Iface, IndexBundle } from '../types.js';
import { h, s } from '../util/dom.js';
import { pct } from '../util/format.js';
import { GOAL_LABEL, goalOf, modelLabel, primaryModel } from '../data/runs.js';
import { findingsGoals, findingsView, goalComparison, headlineRows, metricOf, solveRate, weighted } from '../data/findingsData.js';
import type { MetricKey } from '../data/findingsData.js';

const IF_SLOT: Record<Iface, number> = { forum: 1, wiki: 2, cache: 3, irc: 4 };
const IF_LABEL: Record<Iface, string> = { forum: 'forum', wiki: 'wiki', cache: 'cache', irc: 'IRC' };
type Metric = { k: MetricKey; title: string; unit: 'rate' | 'count'; sub: string };
// Charts per goal. The unsolvable goal has no free time (n/a), so its third chart is the whole-run board share.
const METRICS_BY_GOAL: Record<Goal, Metric[]> = {
  solvable: [
    { k: 'contacted', title: 'Found the board', unit: 'rate', sub: 'share of agents with any board contact' },
    { k: 'posted', title: 'Posted', unit: 'rate', sub: 'share of agents that wrote at least one post' },
    { k: 'board_steps_free', title: 'Board steps in free time', unit: 'count', sub: 'mean per agent, after solving' },
  ],
  unsolvable: [
    { k: 'contacted', title: 'Found the board', unit: 'rate', sub: 'share of agents with any board contact' },
    { k: 'posted', title: 'Posted', unit: 'rate', sub: 'share of agents that wrote at least one post' },
    { k: 'share_board', title: 'Board share of the run', unit: 'rate', sub: 'share of steps on the board; no free time exists' },
  ],
};

function fmtV(v: number, unit: 'rate' | 'count'): string { return unit === 'rate' ? pct(v) : v.toFixed(2); }

/** Dot + CI whisker chart, one row per interface (rows where the metric is n/a are left out). */
function dotChart(f: Findings, m: Metric, tip: HTMLElement): HTMLElement {
  const rows = f.pooled.filter((r) => metricOf(r, m.k) !== null);
  const max = m.unit === 'rate' ? 1 : Math.ceil(Math.max(0.5, ...rows.map((r) => metricOf(r, m.k)!.hi ?? metricOf(r, m.k)!.v)));
  const W = 320, L = 64, R = 16, RH = 30, T = 8, H = T + rows.length * RH + 22;
  const x = (v: number): number => L + (v / max) * (W - L - R);
  const ticks = m.unit === 'rate' ? [0, 0.25, 0.5, 0.75, 1] : Array.from({ length: max + 1 }, (_, i) => i);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'fchart', role: 'img', 'aria-label': `${m.title} by interface, with 95% confidence intervals` },
    ...ticks.map((tv) => s('g', null, s('line', { x1: x(tv), x2: x(tv), y1: T, y2: H - 18, class: 'grid' }),
      s('text', { x: x(tv), y: H - 4, class: 'axis', 'text-anchor': 'middle' }, m.unit === 'rate' ? pct(tv) : String(tv)))));
  rows.forEach((r, i) => {
    const v = metricOf(r, m.k)!, y = T + i * RH + RH / 2;
    const excluded = f.cells.some((c) => c.iface === r.iface && c.excludedByDefault);
    const g = s('g', { class: excluded ? 'frow excluded' : 'frow', tabindex: '0', role: 'img',
      'aria-label': `${IF_LABEL[r.iface]}: ${fmtV(v.v, m.unit)}${v.lo !== null && v.hi !== null ? `, CI ${fmtV(v.lo, m.unit)} to ${fmtV(v.hi, m.unit)}` : ''}, n=${r.nAgents}` },
      s('rect', { x: 0, y: y - RH / 2, width: W, height: RH, class: 'hit' }),
      s('text', { x: L - 10, y: y + 4, class: 'lab', 'text-anchor': 'end' }, IF_LABEL[r.iface] + (excluded ? '*' : '')),
      v.lo !== null && v.hi !== null ? s('line', { x1: x(v.lo), x2: x(v.hi), y1: y, y2: y, class: 'ci', style: `stroke:var(--series-${IF_SLOT[r.iface]})` }) : null,
      v.lo !== null ? s('line', { x1: x(v.lo), x2: x(v.lo), y1: y - 5, y2: y + 5, class: 'ci', style: `stroke:var(--series-${IF_SLOT[r.iface]})` }) : null,
      v.hi !== null ? s('line', { x1: x(v.hi), x2: x(v.hi), y1: y - 5, y2: y + 5, class: 'ci', style: `stroke:var(--series-${IF_SLOT[r.iface]})` }) : null,
      s('circle', { cx: x(v.v), cy: y, r: 5, class: 'dot', style: `fill:var(--series-${IF_SLOT[r.iface]})` }));
    const show = (ev: Event): void => {
      tip.textContent = '';
      tip.append(h('strong', null, `${IF_LABEL[r.iface]} · ${m.title}`), h('div', { class: 'tnum' }, `${fmtV(v.v, m.unit)}`),
        v.lo !== null && v.hi !== null ? h('div.muted', null, `95% CI ${fmtV(v.lo, m.unit)} – ${fmtV(v.hi, m.unit)}`) : '', h('div.muted', null, `n = ${r.nAgents} agents`));
      tip.hidden = false;
      const box = svg.getBoundingClientRect(), host = tip.parentElement!.getBoundingClientRect();
      const px = ev instanceof MouseEvent ? ev.clientX : box.left + x(v.v) * (box.width / W);
      tip.style.left = `${Math.min(host.width - 180, Math.max(0, px - host.left + 12))}px`;
      tip.style.top = `${box.top - host.top + (y / H) * box.height - 10}px`;
    };
    g.addEventListener('pointermove', show);
    g.addEventListener('focus', show);
    g.addEventListener('pointerleave', () => { tip.hidden = true; });
    g.addEventListener('blur', () => { tip.hidden = true; });
    svg.appendChild(g);
  });
  return h('figure.fig', null, h('figcaption', null, h('strong', null, m.title), h('span.muted', null, m.sub)), svg);
}

/** Discovery rate, solo vs swarm side by side per interface (cells). */
function popChart(f: Findings): HTMLElement {
  const ifs = [...new Set(f.cells.map((c) => c.iface))];
  const W = 320, L = 64, R = 16, RH = 30, T = 8, H = T + ifs.length * RH + 22;
  const x = (v: number): number => L + v * (W - L - R);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'fchart', role: 'img', 'aria-label': 'Discovery rate, solo versus swarm, per interface' },
    ...[0, 0.5, 1].map((tv) => s('g', null, s('line', { x1: x(tv), x2: x(tv), y1: T, y2: H - 18, class: 'grid' }),
      s('text', { x: x(tv), y: H - 4, class: 'axis', 'text-anchor': 'middle' }, pct(tv)))));
  ifs.forEach((iface, i) => {
    const y = T + i * RH + RH / 2;
    const so = f.cells.find((c) => c.iface === iface && c.pop === 'solo'), mu = f.cells.find((c) => c.iface === iface && c.pop === 'multi');
    svg.appendChild(s('g', { class: 'frow', role: 'img', 'aria-label': `${IF_LABEL[iface]}: solo ${so ? pct(so.discoveryRate) : 'n/a'}, swarm ${mu ? pct(mu.discoveryRate) : 'n/a'}` },
      s('text', { x: L - 10, y: y + 4, class: 'lab', 'text-anchor': 'end' }, IF_LABEL[iface]),
      so && mu ? s('line', { x1: x(so.discoveryRate), x2: x(mu.discoveryRate), y1: y, y2: y, class: 'link' }) : null,
      so ? s('circle', { cx: x(so.discoveryRate), cy: y, r: 5, class: 'dot hollow' }, s('title', null, `solo ${pct(so.discoveryRate)} (n=${so.nAgents})`)) : null,
      mu ? s('rect', { x: x(mu.discoveryRate) - 5, y: y - 5, width: 10, height: 10, rx: 2, class: 'dot sq' }, s('title', null, `swarm ${pct(mu.discoveryRate)} (n=${mu.nAgents})`)) : null));
  });
  return h('figure.fig', null, h('figcaption', null, h('strong', null, 'Solo vs swarm'), h('span.muted', null, 'discovery rate per cell')),
    svg, h('div.flegend', null, h('span', null, h('i.lg.hollow'), 'solo'), h('span', null, h('i.lg.sq'), 'swarm (4 agents)')));
}

/** A radiogroup of chips with arrow-key roving; `pick` fires on click. */
function radioGroup(label: string, cls: string, items: { id: string; text: string }[], pick: (id: string) => void):
    { el: HTMLElement; set(id: string): void } {
  const el = h(`div.sel-group.${cls}`, { role: 'radiogroup', 'aria-label': label, hidden: items.length < 2 });
  const chips = items.map((it) => {
    const b = h('button.chip', { type: 'button', role: 'radio', 'aria-checked': 'false', tabindex: '-1' }, it.text);
    b.addEventListener('click', () => pick(it.id));
    el.appendChild(b);
    return b;
  });
  el.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
    const i = chips.indexOf(e.target as HTMLButtonElement);
    if (i < 0) return;
    e.preventDefault();
    const n = chips[(i + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + chips.length) % chips.length]!;
    n.focus();
    n.click();
  });
  return { el, set(id) { chips.forEach((b, i) => { const on = items[i]!.id === id; b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; }); } };
}

export function renderFindings(host: HTMLElement, idx: IndexBundle): void {
  const all = idx.findings;
  host.textContent = '';
  if (!all || !all.pooled?.length) { host.append(h('p.muted', null, 'No findings in this data snapshot.')); return; }
  const primary = all.models?.[0] ?? primaryModel(idx);
  const models = all.models?.length ? all.models : [primary];
  const goals = findingsGoals(all);
  const multi = models.length > 1;
  const body = h('div.fbody');
  // model toggle (primary first, hidden for a single model) and goal toggle (hidden unless both goals have rows):
  // charts, tiles and notes follow the selected model and goal
  let model = models.includes(primary) ? primary : models[0]!;
  let goal: Goal = goals[0] ?? 'solvable';
  const mg = radioGroup('Model', 'fmodels', models.map((id) => ({ id, text: modelLabel(idx, id) })), (id) => { model = id; select(); });
  const gg = radioGroup('Goal', 'fgoals', goals.map((g) => ({ id: g, text: GOAL_LABEL[g] })), (id) => { goal = id as Goal; select(); });
  function select(): void {
    mg.set(model);
    gg.set(goal);
    body.replaceChildren(...findingsBody(idx, all, model, goal, primary, multi, goals.length > 1));
  }
  host.append(mg.el, gg.el, body);
  select();
}

function findingsBody(idx: IndexBundle, all: Findings, model: string, goal: Goal, primary: string, multi: boolean,
                      bothGoals: boolean): HTMLElement[] {
  const f = findingsView(all, model, goal, primary);
  const METRICS = METRICS_BY_GOAL[goal];
  const incl = headlineRows(f);
  const n = incl.reduce((a, r) => a + r.nAgents, 0) || 1;
  const wavg = (k: MetricKey): number => weighted(incl, k) ?? 0;
  const runs = idx.runs.filter((r) => !r.hidden && r.model === model && goalOf(r) === goal);
  const unsolv = goal === 'unsolvable';
  const third = unsolv ? 'share_board' : 'board_steps_free';
  const top = [...incl].filter((r) => metricOf(r, third) !== null)
    .sort((a, b) => metricOf(b, third)!.v - metricOf(a, third)!.v)[0];
  const sr = solveRate(f);
  const tiles = unsolv ? [
    { v: pct(wavg('contacted')), l: 'of agents found the board', s: `while working a CTF task nobody can solve (n=${n})` },
    { v: pct(wavg('posted')), l: 'went on to post', s: 'nobody asked them to' },
    { v: top ? pct(metricOf(top, third)!.v) : '–', l: `of their steps on the board (${top ? IF_LABEL[top.iface] : ''})`,
      s: `whole run, the stickiest interface; solve rate ${sr === null ? 'n/a' : pct(sr)}` },
  ] : [
    { v: pct(wavg('contacted')), l: 'of agents found the board', s: `while working a CTF task (n=${n})` },
    { v: pct(wavg('posted')), l: 'went on to post', s: 'nobody asked them to' },
    { v: top ? metricOf(top, third)!.v.toFixed(1) : '–', l: `board steps after solving (${top ? IF_LABEL[top.iface] : ''})`, s: 'the stickiest interface, per agent' },
  ];
  const tip = h('div.ftip', { hidden: true, role: 'status' });
  // the table lists every model of this goal (model column when there are several); charts show the selected one
  const rows = multi ? all.pooled.filter((r) => goalOf(r) === goal) : f.pooled;
  const label = `${modelLabel(idx, model)}${bothGoals ? ` (${GOAL_LABEL[goal].toLowerCase()} goal)` : ''}`;
  if (!f.pooled.length) return [h('p.muted', null, `No findings for ${label} in this data snapshot.`)];
  const cell = (r: (typeof rows)[number], m: Metric): HTMLElement => {
    const v = metricOf(r, m.k);
    return h('td.tnum', null, v === null ? 'n/a' : `${fmtV(v.v, m.unit)}${v.lo !== null && v.hi !== null ? ` [${fmtV(v.lo, m.unit)}, ${fmtV(v.hi, m.unit)}]` : ''}`);
  };
  const cmp = bothGoals ? goalComparison(all, model, primary) : [];
  const note = unsolv ? all.goalNotes?.unsolvable?.[model] ?? all.note : all.notes?.[model] ?? all.note;
  return [
    h('div.tiles', null, ...tiles.map((t) => h('div.tile', null, h('div.tile-v', null, t.v), h('div.tile-l', null, t.l), h('div.tile-s.muted', null, t.s)))),
    h('div.figs', null, ...METRICS.map((m) => dotChart(f, m, tip)), popChart(f), tip),
    cmp.length ? h('div.ftable.fgoalcmp', null, h('strong', null, `Solvable vs unsolvable goal · ${modelLabel(idx, model)}`),
      h('table', null, h('thead', null, h('tr', null, h('th', null, 'metric'), h('th', null, GOAL_LABEL.solvable), h('th', null, GOAL_LABEL.unsolvable))),
        h('tbody', null, ...cmp.map((c) => h('tr', null, h('td', null, c.title),
          ...(['solvable', 'unsolvable'] as Goal[]).map((g) => h('td.tnum', null, c.v[g] === null ? 'n/a' : fmtV(c.v[g]!, c.unit)))))))) : null,
    h('details.ftable', null, h('summary', null, 'Table view'),
      h('table', null, h('thead', null, h('tr', null, multi ? h('th', null, 'model') : null, h('th', null, 'interface'), h('th', null, 'n'), ...METRICS.map((m) => h('th', null, m.title)))),
        h('tbody', null, ...rows.map((r) => h('tr', null, multi ? h('td', null, modelLabel(idx, r.model ?? primary)) : null,
          h('td', null, IF_LABEL[r.iface]), h('td.tnum', null, String(r.nAgents)),
          ...METRICS.map((m) => cell(r, m))))))),
    h('p.fnote.muted', null, `${unsolv ? 'Unsolvable-goal snapshot' : 'Pilot snapshot'} · ${runs.length} runs. ${note}`),
    h('ul.fcaveats.muted', null, ...f.caveats.map((c) => h('li', null, c)),
      f.cells.some((c) => c.excludedByDefault) ? h('li', null, '* excluded from the headline numbers (known logging caveat).') : null,
      f.mismatches.length ? h('li', null, `Numbers come from the observatory database (${f.mismatches.length} differences from analysis.json).`) : null),
  ].filter((x) => x !== null) as HTMLElement[];
}
