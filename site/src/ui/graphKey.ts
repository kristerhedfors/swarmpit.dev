// Closable infobox explaining every mark in the live graph. Open by default on wide screens (where the player
// floats its panels over the graph) until the visitor closes it; the choice is remembered. A "Key" button
// reopens it.

import { h } from '../util/dom.js';
import { ICON } from './icons.js';
import { GRAPH_KEY, GRAPH_KEY_FOOTNOTE, type Swatch } from './graphKeyData.js';

const STORE = 'swarmpit.graphKey';

function svg(inner: string): string {
  return `<svg viewBox="0 0 28 20" width="28" height="20" aria-hidden="true" focusable="false">${inner}</svg>`;
}

const hex = (cx: number, cy: number, r: number) =>
  Array.from({ length: 6 }, (_, k) => {
    const a = Math.PI / 6 + (k * Math.PI) / 3;
    return `${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`;
  }).join(' ');

/** SVG markup for one swatch. Exported for tests. */
export function swatchSvg(s: Swatch): string {
  const c = 14, m = 10;
  switch (s.kind) {
    case 'circle': {
      const r = s.small ? 3 : 6;
      const op = s.faded ? ' opacity=".45"' : '';
      return svg(s.hollow ? `<circle cx="${c}" cy="${m}" r="${r - 1}" fill="none" stroke="${s.color}" stroke-width="2"${op}/>`
        : `<circle cx="${c}" cy="${m}" r="${r}" fill="${s.color}"${op}/>`);
    }
    case 'hex': return svg(`<polygon points="${hex(c, m, 7.5)}" ${s.hollow ? `fill="none" stroke="${s.color}" stroke-width="2"` : `fill="${s.color}" stroke="var(--bs)" stroke-width=".8"`}/>`);
    case 'diamond': return svg(`<polygon points="${c},${m - 7} ${c + 7},${m} ${c},${m + 7} ${c - 7},${m}" ${s.hollow ? `fill="none" stroke="${s.color}" stroke-width="2"` : `fill="${s.color}"`}/>`);
    case 'square': return svg(s.hollow ? `<rect x="${c - 5}" y="${m - 5}" width="10" height="10" rx="2" fill="none" stroke="${s.color}" stroke-width="2"/>`
      : `<rect x="${c - 5.5}" y="${m - 5.5}" width="11" height="11" rx="2.2" fill="${s.color}"/>`);
    case 'ring': return svg(`<circle cx="${c}" cy="${m}" r="6.5" fill="none" stroke="${s.color}" stroke-width="2.2"/>`);
    case 'arc': {
      const node = s.around === 'hex' ? `<polygon points="${hex(c, m, 5.5)}" fill="var(--task)" stroke="var(--bs)" stroke-width=".8"/>`
        : `<circle cx="${c}" cy="${m}" r="5" fill="var(--series-1)"/>`;
      return svg(`${node}<path d="M${c} ${m - 8.2} A8.2 8.2 0 1 1 ${c - 7.1} ${m + 4.1}" fill="none" stroke="${s.color}" stroke-width="2"/>`);
    }
    case 'halo': return svg(`<circle cx="${c}" cy="${m}" r="5" fill="var(--series-1)"/><circle cx="${c}" cy="${m}" r="8.3" fill="none" stroke="${s.color}" stroke-width="2"/>`);
    case 'dashring': return svg(`<circle cx="${c}" cy="${m}" r="5" fill="var(--series-1)"/><circle cx="${c}" cy="${m}" r="7.6" fill="none" stroke="${s.color}" stroke-width="1.2" stroke-dasharray="3 3"/>`);
    case 'hull': return svg(`<circle cx="${c}" cy="${m}" r="8.8" fill="none" stroke="${s.color}" stroke-width="1.2" opacity=".7"/>` +
      `<circle cx="${c - 3.5}" cy="${m - 2}" r="2.6" fill="var(--series-1)"/><circle cx="${c + 3.5}" cy="${m - 1}" r="2.6" fill="var(--series-2)"/><circle cx="${c}" cy="${m + 3.5}" r="2.6" fill="var(--series-3)"/>`);
    case 'trail': return svg(`<circle cx="5" cy="${m}" r="1.8" fill="var(--series-1)" opacity=".35"/><circle cx="11" cy="${m}" r="1.8" fill="var(--status-critical)" opacity=".55"/>` +
      `<circle cx="17" cy="${m}" r="1.8" fill="var(--status-good)" opacity=".8"/><circle cx="23" cy="${m}" r="1.5" fill="none" stroke="var(--text-muted)" stroke-width="1"/>`);
    case 'line': return svg(`<line x1="2" y1="${m}" x2="26" y2="${m}" stroke="${s.color}" stroke-width="2"${s.dash ? ` stroke-dasharray="${s.dash}"` : ''}/>`);
    case 'pulse': return svg(`<line x1="2" y1="${m}" x2="26" y2="${m}" stroke="var(--neutral-mark)" stroke-width="1.5"/><circle cx="17" cy="${m}" r="3" fill="${s.color}"/>`);
  }
}

export interface GraphKey {
  readonly el: HTMLElement; readonly chip: HTMLButtonElement; readonly isOpen: boolean;
  open(): void; close(): boolean; onToggle(fn: () => void): void;
}

function remembered(): 'open' | 'closed' | null {
  try { const v = localStorage.getItem(STORE); return v === 'open' || v === 'closed' ? v : null; } catch { return null; }
}
function remember(v: 'open' | 'closed') { try { localStorage.setItem(STORE, v); } catch { /* private mode */ } }

/** initialOpen: the state when the visitor has not toggled the key before (default: open on wide screens). */
export function createGraphKey(o: { initialOpen?: boolean } = {}): GraphKey {
  const bClose = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Close the graph key', html: ICON.close });
  const body = h('div.gk-body');
  for (const sec of GRAPH_KEY) {
    const dl = h('dl.gk-list');
    for (const e of sec.entries) {
      dl.append(h('dt.gk-term', { 'data-key': e.id }, h('span.gk-sw', { html: swatchSvg(e.swatch) }), h('span', null, e.label)),
        h('dd.gk-desc', null, e.desc));
    }
    body.append(h('section.gk-sec', null, h('h3.gk-h', null, sec.title), dl));
  }
  body.append(h('p.gk-foot', null, GRAPH_KEY_FOOTNOTE));
  const el = h('div.gkey', { id: 'gk-box', role: 'region', 'aria-labelledby': 'gk-title', tabindex: '-1' },
    h('div.gk-head', null, h('strong', { id: 'gk-title' }, 'What the symbols mean'), h('span.tl-spacer'), bClose), body);
  const chip = h('button.gk-chip', { type: 'button', 'aria-controls': 'gk-box', title: 'What every symbol in the graph means' },
    h('span.gk-chip-i', { html: svg(`<polygon points="${hex(8, 10, 5)}" fill="var(--task)"/><circle cx="20" cy="10" r="4.5" fill="var(--series-1)"/>`) }),
    'Key');

  let open = false;
  const listeners: (() => void)[] = [];
  const set = (on: boolean, focus: boolean) => {
    open = on; el.hidden = !on; chip.setAttribute('aria-expanded', String(on)); for (const fn of listeners) fn();
    if (focus) (on ? el : chip).focus();
  };
  const wide = typeof matchMedia === 'function' ? matchMedia('(min-width: 961px)').matches : true;
  const r = remembered();
  set(r ? r === 'open' : o.initialOpen ?? wide, false);
  chip.hidden = false; // the button stays in the legend; it toggles the box

  const api: GraphKey = {
    el, chip,
    get isOpen() { return open; },
    open() { set(true, true); remember('open'); },
    close() { if (!open) return false; set(false, true); remember('closed'); return true; },
    onToggle(fn) { listeners.push(fn); },
  };
  bClose.addEventListener('click', () => api.close());
  chip.addEventListener('click', () => { if (open) api.close(); else api.open(); });
  el.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); api.close(); } });
  // keep pointer/wheel interactions inside the box from orbiting the 3D view underneath
  for (const ev of ['pointerdown', 'wheel', 'touchstart'] as const) el.addEventListener(ev, (e) => e.stopPropagation(), { passive: true });
  return api;
}
