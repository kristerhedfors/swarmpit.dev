// Generic sortable table with keyboard navigation (j/k move, Enter opens).
import { h, replace } from '../dom';

export interface Col<T> {
  key: string;
  label: string;
  num?: boolean;
  title?: string;
  render: (r: T) => Node | string;
  sortVal?: (r: T) => number | string | null;
  wrap?: boolean;
}

export class SortTable<T> {
  el = h('div', { class: 'tbl-wrap' });
  private rows: T[] = [];
  private sel = -1;
  private trs: HTMLTableRowElement[] = [];
  constructor(
    private cols: Col<T>[],
    private o: { sort: string; order: 'asc' | 'desc'; onSort?: (k: string, o: 'asc' | 'desc') => void; onOpen?: (r: T) => void; rowClass?: (r: T) => string; pin?: (r: T) => boolean; serverSort?: boolean },
  ) {}

  setRows(rows: T[]) { this.rows = rows; this.render(); }

  private sorted(): T[] {
    if (this.o.serverSort) return this.rows;
    const c = this.cols.find((x) => x.key === this.o.sort);
    if (!c?.sortVal) return this.rows;
    const dir = this.o.order === 'asc' ? 1 : -1;
    return [...this.rows].sort((a, b) => {
      const pa = this.o.pin?.(a) ? 1 : 0, pb = this.o.pin?.(b) ? 1 : 0;
      if (pa !== pb) return pb - pa;
      const x = c.sortVal!(a), y = c.sortVal!(b);
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }

  private render() {
    const rows = this.sorted();
    const thead = h('thead', null, h('tr', null, ...this.cols.map((c) => h('th', {
      class: c.num ? 'num' : '', title: c.title ?? 'Sort', scope: 'col', 'aria-sort': this.o.sort === c.key ? (this.o.order === 'asc' ? 'ascending' : 'descending') : 'none',
      onclick: () => { if (!c.sortVal && !this.o.serverSort) return; const order = this.o.sort === c.key && this.o.order === 'desc' ? 'asc' : 'desc'; this.o.sort = c.key; this.o.order = order; this.o.onSort?.(c.key, order); this.render(); },
    }, c.label, this.o.sort === c.key ? h('span', { class: 'arr' }, this.o.order === 'asc' ? '▲' : '▼') : null))));
    this.trs = rows.map((r, i) => h('tr', { class: (this.o.rowClass?.(r) ?? '') + (i === this.sel ? ' sel' : ''), onclick: () => this.o.onOpen?.(r), style: { cursor: this.o.onOpen ? 'pointer' : '' } },
      ...this.cols.map((c) => h('td', { class: (c.num ? 'num' : '') + (c.wrap ? ' wrap' : '') }, c.render(r)))));
    const table = h('table', { class: 'tbl' }, thead, h('tbody', null, ...this.trs));
    replace(this.el, rows.length ? table : h('div', { class: 'empty' }, 'No rows match the filters.'));
    this.view = rows;
  }
  private view: T[] = [];

  move(d: number) {
    if (!this.trs.length) return;
    this.trs[this.sel]?.classList.remove('sel');
    this.sel = Math.max(0, Math.min(this.trs.length - 1, this.sel + d));
    this.trs[this.sel].classList.add('sel');
    this.trs[this.sel].scrollIntoView({ block: 'nearest' });
  }
  open() { const r = this.view[this.sel]; if (r) this.o.onOpen?.(r); }
}
