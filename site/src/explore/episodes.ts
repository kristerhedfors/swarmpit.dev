// Episodes view: every filtered episode in a sortable, paginated table; a row opens the detail panel.
import { h } from '../util/dom.js';
import { COLUMNS, modelInfo, paginate, sortEpisodes, toCSV } from './data.js';
import type { Ctx } from './ui.js';

export const PAGE_SIZE = 50;

export function renderEpisodes(host: HTMLElement, ctx: Ctx): void {
  const { ex, state } = ctx;
  const sorted = sortEpisodes(ex, ctx.eps, state.sort, state.desc);
  const pg = paginate(sorted, state.page, PAGE_SIZE);
  const sortBy = (id: string): void => ctx.set({ sort: id, desc: state.sort === id ? !state.desc : COLUMNS.find((c) => c.id === id)?.num ?? false, page: 0 });
  const pager = (where: string): HTMLElement => h('nav.ex-pager', { 'aria-label': `Pages (${where})` },
    h('button.ex-btn', { type: 'button', 'data-fk': `e:prev:${where}`, 'aria-disabled': pg.page === 0 ? 'true' : null,
      onclick: () => { if (pg.page > 0) ctx.set({ page: pg.page - 1 }); } }, '‹ Prev'),
    h('span', null, `Page ${pg.page + 1} of ${pg.pages}`),
    h('button.ex-btn', { type: 'button', 'data-fk': `e:next:${where}`, 'aria-disabled': pg.page >= pg.pages - 1 ? 'true' : null,
      onclick: () => { if (pg.page < pg.pages - 1) ctx.set({ page: pg.page + 1 }); } }, 'Next ›'));
  const download = (): void => {
    const url = URL.createObjectURL(new Blob([toCSV(ex, sorted)], { type: 'text/csv' }));
    const a = h('a', { href: url, download: 'instigation-episodes.csv' });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const table = h('table.ex-eps', null,
    h('caption.sr-only', null, `Episodes, sorted by ${COLUMNS.find((c) => c.id === state.sort)?.title ?? state.sort}, ${state.desc ? 'descending' : 'ascending'}`),
    h('thead', null, h('tr', null, COLUMNS.map((c) => {
      const on = state.sort === c.id;
      return h('th', { scope: 'col', class: c.num ? 'num' : null, 'aria-sort': on ? (state.desc ? 'descending' : 'ascending') : null },
        h('button.ex-sort', { type: 'button', title: c.title, 'data-fk': `e:sort:${c.id}`, onclick: () => sortBy(c.id) },
          c.label, h('span.ex-sort-i', { 'aria-hidden': 'true' }, on ? (state.desc ? '▼' : '▲') : '')));
    }))),
    h('tbody', null, pg.items.map((e) => h('tr', { class: e.run_id === state.ep ? 'ex-sel' : null, onclick: (ev: Event) => {
      if ((ev.target as HTMLElement).closest('button')) return;
      ctx.openEpisode(e.run_id);
    } }, COLUMNS.map((c, i) => {
      const v = c.get(ex, e);
      const txt = v === null ? '–' : String(v);
      if (i === 0) return h('th', { scope: 'row' }, h('button.ex-link', { type: 'button', 'data-fk': `e:open:${e.run_id}`,
        'aria-label': `Open episode ${e.run_id}`, onclick: () => ctx.openEpisode(e.run_id) }, txt));
      const retired = c.id === 'model' && modelInfo(ex, e.model)?.retired;
      return h('td', { class: [c.num ? 'num' : '', c.id === 'record_id' ? 'mono' : '', c.id === 'n_writers' && e.n_writers > 0 ? 'ex-w' : ''].join(' ').trim() || null },
        c.id === 'record_id' ? txt.slice(0, 8) : txt, retired ? h('span.ex-tag', null, 'retired') : null);
    })))));
  host.replaceChildren(
    h('div.ex-controls', null, pager('top'),
      h('button.ex-btn', { type: 'button', 'data-fk': 'e:csv', onclick: download }, `Download CSV (${sorted.length})`)),
    sorted.length
      ? h('div.ex-scroll.ex-eps-wrap', { tabindex: '0', role: 'region', 'aria-label': 'Episodes table (scrollable)' }, table)
      : h('p.ex-none', null, 'No episodes match the filters.'),
    h('div.ex-controls', null, pager('bottom')),
    h('p.ex-note.muted', null, 'Click a column header to sort (again to reverse). Click a row or its run id to open the episode. ',
      'The CSV holds every field of the filtered episodes in the current sort order.'));
}
