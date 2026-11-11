// Shared DOM helpers for the explorer views: render context, tooltip, roving grid keyboard navigation.
import { h } from '../util/dom.js';
import type { Episode, Explorer, State } from './types.js';

export interface Ctx {
  ex: Explorer;
  state: State;
  /** episodes after the global filters (and seed restriction) */
  eps: Episode[];
  set(patch: Partial<State>, push?: boolean): void;
  openEpisode(runId: string): void;
  /** close the detail panel and return focus to whatever opened it */
  closeEpisode(): void;
  tip: Tip;
}

// ------------------------------------------------------------------------------------------- tooltip
export interface Tip {
  /** attach a tooltip (lines of plain text) to an element: shown on hover and keyboard focus */
  bind(el: Element, lines: () => string[]): void;
  hide(): void;
}

export function createTip(host: HTMLElement): Tip {
  const box = h('div.ex-tip', { role: 'tooltip', id: 'ex-tip', hidden: true });
  host.appendChild(box);
  let owner: Element | null = null;
  const place = (x: number, y: number): void => {
    const r = box.getBoundingClientRect();
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    let left = x + 14, top = y + 14;
    if (left + r.width > vw - 8) left = Math.max(8, x - r.width - 14);
    if (top + r.height > vh - 8) top = Math.max(8, y - r.height - 14);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  };
  const show = (el: Element, lines: string[], x: number, y: number): void => {
    owner = el;
    box.replaceChildren(...lines.map((l, i) => h(i === 0 ? 'strong' : 'div', null, l)));
    box.hidden = false;
    place(x, y);
  };
  const hide = (): void => { box.hidden = true; owner = null; };
  const bind = (el: Element, lines: () => string[]): void => {
    el.addEventListener('pointerenter', (e) => { const pe = e as PointerEvent; if (pe.pointerType !== 'touch') show(el, lines(), pe.clientX, pe.clientY); });
    el.addEventListener('pointermove', (e) => { if (owner === el) place((e as PointerEvent).clientX, (e as PointerEvent).clientY); });
    el.addEventListener('pointerleave', () => { if (owner === el) hide(); });
    el.addEventListener('focus', () => {
      const r = el.getBoundingClientRect();
      show(el, lines(), r.right, r.top + r.height / 2);
    });
    el.addEventListener('blur', () => { if (owner === el) hide(); });
  };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !box.hidden) hide(); }, true);
  // keep a keyboard-focus tooltip attached to its element while the page scrolls; pointer tooltips just close
  window.addEventListener('scroll', () => {
    if (owner && owner === document.activeElement) { const r = owner.getBoundingClientRect(); place(r.right, r.top + r.height / 2); } else hide();
  }, { passive: true, capture: true });
  return { bind, hide };
}

// ------------------------------------------------------------------------------------------- grid navigation
/**
 * Roving arrow-key navigation across the focusable cells of a table (elements with [data-cell]).
 * Arrow keys move by row / column, Home / End to the row ends. Only one cell is in the tab order.
 */
export function gridNav(table: HTMLTableElement): void {
  const cells = (): HTMLElement[][] => [...table.rows].map((r) => [...r.querySelectorAll<HTMLElement>('[data-cell]')]);
  const all = table.querySelectorAll<HTMLElement>('[data-cell]');
  all.forEach((c, i) => { c.tabIndex = i === 0 ? 0 : -1; });
  table.addEventListener('focusin', (e) => {
    const t = e.target as HTMLElement;
    if (!t.matches('[data-cell]')) return;
    all.forEach((c) => { c.tabIndex = c === t ? 0 : -1; });
  });
  table.addEventListener('keydown', (e) => {
    const t = e.target as HTMLElement;
    if (!t.matches('[data-cell]') || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const grid = cells().filter((r) => r.length);
    const ri = grid.findIndex((r) => r.includes(t));
    if (ri < 0) return;
    const row = grid[ri] as HTMLElement[];
    const ci = row.indexOf(t);
    // column position by geometry so rows with different cell counts still line up
    const x = t.getBoundingClientRect().left;
    const nearest = (r: HTMLElement[]): HTMLElement | undefined =>
      r.reduce<HTMLElement | undefined>((b, c) => (!b || Math.abs(c.getBoundingClientRect().left - x) < Math.abs(b.getBoundingClientRect().left - x) ? c : b), undefined);
    let next: HTMLElement | undefined;
    if (e.key === 'ArrowLeft') next = row[ci - 1];
    else if (e.key === 'ArrowRight') next = row[ci + 1];
    else if (e.key === 'Home') next = row[0];
    else if (e.key === 'End') next = row[row.length - 1];
    else if (e.key === 'ArrowUp') next = ri > 0 ? nearest(grid[ri - 1] as HTMLElement[]) : undefined;
    else next = ri < grid.length - 1 ? nearest(grid[ri + 1] as HTMLElement[]) : undefined;
    if (next) { e.preventDefault(); next.focus(); }
  });
}

// ------------------------------------------------------------------------------------------- small widgets
export function select<T extends string>(label: string, value: T, options: { v: T; label: string }[], onChange: (v: T) => void, fk: string): HTMLLabelElement {
  const sel = h('select', { 'data-fk': fk, onchange: (e: Event) => onChange((e.target as HTMLSelectElement).value as T) },
    options.map((o) => h('option', { value: o.v, selected: o.v === value }, o.label)));
  return h('label.ex-field', null, h('span', null, label), sel);
}

export function segmented<T extends string>(label: string, value: T, options: { v: T; label: string }[], onChange: (v: T) => void, fk: string): HTMLElement {
  return h('div.ex-seg', { role: 'group', 'aria-label': label },
    h('span.ex-seg-l', { 'aria-hidden': 'true' }, label),
    options.map((o) => h('button', { type: 'button', 'aria-pressed': String(o.v === value), 'data-fk': `${fk}:${o.v}`, onclick: () => onChange(o.v) }, o.label)));
}

export function legendRamp(min: string, max: string, label: string): HTMLElement {
  return h('div.ex-ramp', { 'aria-label': `${label}: colour scale from ${min} (lightest) to ${max} (darkest)` },
    h('span.ex-ramp-l', null, label), h('span', null, min),
    h('span.ex-ramp-bar', { 'aria-hidden': 'true' }, Array.from({ length: 9 }, (_v, i) => h(`i.q${i}`))),
    h('span', null, max),
    h('span.ex-ramp-empty', null, h('i', { 'aria-hidden': 'true' }), 'no data'));
}

export const short = (id: string): string => id.slice(0, 8);
export const pct = (v: number): string => `${Math.round(v * 100)}%`;
export const num = (v: number | null, d = 3): string => (v === null || !Number.isFinite(v) ? '–' : Number.isInteger(v) ? String(v) : v.toFixed(d));
