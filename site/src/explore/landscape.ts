// Landscape view: the MAP-Elites archive as one grid per goal x model (rows: trigger code; columns: channel x
// seed-length band), each filled cell coloured by its elite's instigation score.
import { h } from '../util/dom.js';
import { BAND_LABEL, dimLabel, eliteEpisode, landscapeGrids, landscapeMax, modelInfo, rampBin } from './data.js';
import type { LGrid } from './data.js';
import type { LandscapeCell } from './types.js';
import type { Ctx } from './ui.js';
import { gridNav, legendRamp, num, segmented, short } from './ui.js';

export function renderLandscape(host: HTMLElement, ctx: Ctx): void {
  const { ex, state } = ctx;
  const max = landscapeMax(ex);
  const grids = landscapeGrids(ex, state.filters, state.lmode);
  const controls = h('div.ex-controls', null,
    segmented('Grids', state.lmode, [{ v: 'models', label: 'One per model' }, { v: 'best', label: 'Best across models' }],
      (v) => ctx.set({ lmode: v }), 'l:mode'));
  const intro = h('p.ex-desc', null,
    'Each grid is a MAP-Elites archive: for every combination of trigger code, channel and seed-length band it keeps the single best seed ',
    '(the elite) found so far. Colour follows the elite’s score on the scale below; blank cells hold no elite. Each goal shows only the channels its ',
    'search actually ran. Goal, model, channel, length and trigger filters narrow the grids; the other filters do not apply here. In “Best across models” ',
    'with a model filter, each cell shows the best elite among the selected models. Click a cell to open the episode behind its elite.');
  if (!Object.keys(ex.landscapes).length) {
    host.replaceChildren(controls, intro, h('p.ex-none', null, 'No landscape archive is published yet.'));
    return;
  }
  host.replaceChildren(controls, intro, legendRamp('0', max.toFixed(2), 'Elite instigation score'),
    grids.length ? h('div.ex-grids', null, grids.map((g) => grid(g, max, ctx))) : h('p.ex-none', null, 'No grid matches the goal / model filters.'));
}

function grid(g: LGrid, max: number, ctx: Ctx): HTMLElement {
  const { ex } = ctx;
  const mi = g.model ? modelInfo(ex, g.model) : undefined;
  const title = `${g.goal[0]?.toUpperCase() ?? ''}${g.goal.slice(1)} · ${g.model ? (mi?.label ?? g.model) : 'best across models'}`;
  const channels = [...new Set(g.cols.map((c) => c.channel))];
  const bands = [...new Set(g.cols.map((c) => c.band))];
  const cell = (trig: string, channel: string, band: string): HTMLElement => {
    const key = `${trig}|${channel}|${band}`;
    const c = g.cells[key];
    if (!c) return h('td.ex-lc.ex-empty', null, h('span.sr-only', null, 'empty'));
    const bin = rampBin(c.instigation_score, max);
    const ep = eliteEpisode(ex, g.goal, c, key);
    const desc = `${trig}, ${dimLabel(ex, 'channel', channel)}, ${BAND_LABEL[band] ?? band} chars: elite ${short(c.record_id)} score ${c.instigation_score.toFixed(3)}`
      + `${c.n_writers === null ? '' : `, ${c.n_writers} writers`}${g.model ? '' : `, ${dimLabel(ex, 'model', c.model)}`}`;
    const btn = h('button', { type: 'button', class: `ex-lb q${bin ?? 0}`, 'data-cell': '', 'data-fk': `l:${g.goal}:${g.model}:${key}`,
      'aria-label': ep ? `${desc}. Open episode.` : desc, onclick: () => { if (ep) ctx.openEpisode(ep.run_id); } },
    h('span', { 'aria-hidden': 'true' }, c.instigation_score.toFixed(2).replace(/^0/, '')));
    ctx.tip.bind(btn, () => eliteLines(ctx, g, key, c, ep !== undefined));
    return h('td.ex-lc', null, btn);
  };
  if (!g.rows.length || !g.cols.length) {
    return h('figure.ex-fig', null, h('figcaption', null, h('strong', null, title), mi?.retired ? h('span.ex-tag', null, 'retired') : null),
      h('p.ex-none', null, 'No cells to show: this goal’s search never ran on the selected channels, lengths or triggers.'));
  }
  const table = h('table.ex-land', null,
    h('caption', null, title, mi?.retired ? h('span.ex-tag', null, 'retired') : null,
      h('span.muted', null, ` · ${g.filled} of ${g.nCellsPossible} cells shown filled`)),
    h('thead', null,
      h('tr', null, h('th', { scope: 'col', rowspan: '2', class: 'ex-corner' }, 'Trigger \\ channel'),
        channels.map((ch) => h('th', { scope: 'colgroup', colspan: String(bands.length), class: 'ex-chh' }, dimLabel(ex, 'channel', ch)))),
      h('tr', null, channels.flatMap((ch) => bands.map((b) => h('th', { scope: 'col', class: 'ex-bh', title: `${b} seed chars`,
        'aria-label': `${dimLabel(ex, 'channel', ch)} ${BAND_LABEL[b] ?? b} chars` }, BAND_LABEL[b] ?? b))))),
    h('tbody', null, g.rows.map((t) => h('tr', null, h('th', { scope: 'row' }, t),
      channels.flatMap((ch) => bands.map((b) => cell(t, ch, b)))))));
  gridNav(table);
  return h('figure.ex-fig', null, h('div.ex-scroll', { tabindex: '-1' }, table));
}

function eliteLines(ctx: Ctx, g: LGrid, key: string, c: LandscapeCell, hasEp: boolean): string[] {
  const { ex } = ctx;
  const [trig, ch, band] = key.split('|') as [string, string, string];
  const e = c.engagement;
  return [
    `${trig} · ${dimLabel(ex, 'channel', ch)} · ${BAND_LABEL[band] ?? band} chars`,
    `Elite seed ${c.record_id}`,
    `Score ${c.instigation_score.toFixed(3)} · writers ${num(c.n_writers)} · length ${num(c.length)}`,
    `Engagement ${num(e.engagement_score ?? null)} (reads ${num(e.board_reads ?? null)}, relay ${num(e.relay_connections ?? null)}, refs ${num(e.board_refs ?? null)})`,
    `Model ${dimLabel(ex, 'model', c.model)} · goal ${g.goal}`,
    hasEp ? 'Click to open the episode' : 'Episode not in the published set',
  ];
}
