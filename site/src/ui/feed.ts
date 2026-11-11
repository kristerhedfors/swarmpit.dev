import type { Feed, FilterMask, RunModel } from '../types.js';
import { h, text, agentVar } from '../util/dom.js';
import { fmtT, trunc } from '../util/format.js';
import { ICON } from './icons.js';

export interface FeedRail extends Feed {
  readonly el: HTMLElement;
  onExpand(cb: (on: boolean) => void): void;
  onHover(cb: (ev: number | null) => void): void;
}

const ROW = 30;

export function createFeed(): FeedRail {
  let m: RunModel | null = null, mask: FilterMask | null = null;
  let list = new Uint32Array(0), times = new Float64Array(0);
  let anchor = -1, t = 0, userScrollAt = -Infinity, programmatic = false, expanded = false;
  let pick: (i: number) => void = () => undefined, expCb: (on: boolean) => void = () => undefined;
  let hoverCb: (ev: number | null) => void = () => undefined;
  const pool: HTMLButtonElement[] = [];

  const count = h('span.feed-count');
  const bText = h('button.feed-text', { type: 'button', 'aria-pressed': 'false' }, 'Text view');
  const spacer = h('div.feed-spacer');
  // One tab stop for the whole feed (roving tabindex); rows stay real buttons (no list roles overriding them).
  const scroller = h('div.feed-list', { tabindex: '-1', role: 'group', 'aria-label': 'Event feed (arrow keys move between events)' }, spacer);
  const el = h('aside.feed', { 'aria-label': 'Events' },
    h('div.feed-head', null, h('span.feed-ttl', { html: ICON.feed }), h('strong', null, 'Events'), count, h('span.tl-spacer'), bText), scroller);

  bText.addEventListener('click', () => { api.setExpanded(!expanded); expCb(expanded); });
  scroller.addEventListener('scroll', () => { if (!programmatic) userScrollAt = performance.now(); programmatic = false; draw(); }, { passive: true });
  scroller.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>('.feed-row');
    if (b) pick(Number(b.dataset['ev']));
  });
  scroller.addEventListener('pointerover', (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>('.feed-row');
    hoverCb(b ? Number(b.dataset['ev']) : null);
  });
  scroller.addEventListener('pointerleave', () => hoverCb(null));
  let focusJ = -1; // row index that owns focus (or last had it while focus stayed in the feed)
  scroller.addEventListener('focusin', (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>('.feed-row');
    if (b) focusJ = Number(b.dataset['j']);
  });
  scroller.addEventListener('focusout', (e) => {
    if (!scroller.contains(e.relatedTarget as Node | null)) { focusJ = -1; draw(); } // tab back in lands on the current event
  });
  scroller.addEventListener('keydown', (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>('.feed-row');
    if (!b) return;
    const cur = Number(b.dataset['j']);
    let j: number;
    if (e.key === 'ArrowDown') j = cur + 1; else if (e.key === 'ArrowUp') j = cur - 1;
    else if (e.key === 'Home') j = 0; else if (e.key === 'End') j = list.length - 1;
    else return;
    e.preventDefault(); e.stopPropagation();
    if (j < 0 || j >= list.length) return;
    focusJ = j;
    scrollTo(j, false);
    draw();
    scroller.querySelector<HTMLButtonElement>(`.feed-row[data-j="${j}"]`)?.focus();
  });

  function rebuild(): void {
    if (!m) { list = new Uint32Array(0); times = new Float64Array(0); return; }
    const out: number[] = [];
    for (let i = 0; i < m.nEvents; i++) if (!mask || (mask.event[i] ?? 2) >= 2) out.push(i);
    list = Uint32Array.from(out);
    times = Float64Array.from(out, (i) => m!.evT[i]!);
    spacer.style.height = list.length * ROW + 'px';
    text(count, String(list.length));
    anchor = -2;
    api.setTime(t);
    draw();
  }

  function rowFor(k: number): HTMLButtonElement {
    let b = pool[k];
    if (!b) {
      b = h('button.feed-row', { type: 'button', tabindex: '-1' }, h('span.fr-t'), h('span.fr-dot'), h('span.fr-who'), h('span.fr-txt'));
      pool[k] = b;
      scroller.appendChild(b);
    }
    return b;
  }

  function draw(): void {
    if (!m) return;
    const top = scroller.scrollTop, hh = scroller.clientHeight || 400;
    const j0 = Math.max(0, Math.floor(top / ROW) - 4), j1 = Math.min(list.length, Math.ceil((top + hh) / ROW) + 4);
    // A focused row keeps its element and its event (never recycled under the user).
    const ae = document.activeElement;
    const fk = ae instanceof HTMLButtonElement ? pool.indexOf(ae) : -1;
    const fj = fk >= 0 ? Number((ae as HTMLButtonElement).dataset['j']) : -1;
    // the single tab stop: focused/last-focused row, else the current event, clamped into the drawn window
    let tabJ = focusJ >= 0 ? focusJ : Math.max(0, anchor);
    if (tabJ < j0 || tabJ >= j1) tabJ = Math.min(Math.max(tabJ, j0), j1 - 1);
    let k = 0;
    const order: number[] = [];
    for (let j = j0; j < j1; j++) order.push(j);
    if (fj >= 0 && (fj < j0 || fj >= j1) && fj < list.length) order.push(fj);
    for (const j of order) {
      let slot: number;
      if (j === fj) slot = fk;
      else { if (k === fk) k++; slot = k++; }
      const i = list[j]!, b = rowFor(slot);
      b.tabIndex = j === tabJ ? 0 : -1;
      b.hidden = false;
      if (b.dataset['ev'] !== String(i)) {
        const d = m.describe(i);
        b.dataset['ev'] = String(i);
        const [tt, dot, who, txt] = b.children as unknown as HTMLElement[];
        tt!.textContent = fmtT(m.evT[i]!);
        dot!.textContent = d.icon;
        dot!.style.color = agentVar(d.agent);
        who!.textContent = d.agent >= 0 ? `a${d.agent}` : '';
        who!.style.setProperty('--chip', agentVar(d.agent));
        const body = d.short.replace(/^(a\d+|board) /, '');
        txt!.textContent = trunc(body, 140);
        b.title = d.long;
        b.setAttribute('aria-label', `${fmtT(m.evT[i]!)} ${d.long}`);
        b.classList.toggle('hit', (mask?.event[i] ?? 0) === 3);
      }
      b.dataset['j'] = String(j);
      b.style.transform = `translateY(${j * ROW}px)`;
      b.classList.toggle('anchor', j === anchor);
      b.classList.toggle('future', j > anchor);
    }
    if (k === fk) k++;
    for (; k < pool.length; k++) if (k !== fk || fj < 0) pool[k]!.hidden = true;
  }

  function scrollTo(j: number, center: boolean): void {
    const hh = scroller.clientHeight || 400;
    const y = j * ROW;
    let target = scroller.scrollTop;
    if (center) target = y - hh * 0.6;
    else if (y < target) target = y;
    else if (y + ROW > target + hh) target = y + ROW - hh;
    target = Math.max(0, target);
    if (Math.abs(target - scroller.scrollTop) > 1) { programmatic = true; scroller.scrollTop = target; }
  }

  const api: FeedRail = {
    el,
    setModel(model) { m = model; for (const b of pool) b.dataset['ev'] = ''; scroller.scrollTop = 0; rebuild(); },
    setMask(mk) { mask = mk; for (const b of pool) b.dataset['ev'] = ''; rebuild(); },
    setTime(nt) {
      t = nt;
      let lo = 0, hi = times.length;
      while (lo < hi) { const md = (lo + hi) >>> 1; if (times[md]! <= t) lo = md + 1; else hi = md; }
      const a = lo - 1;
      if (a === anchor) return;
      anchor = a;
      if (performance.now() - userScrollAt > 3000 && el.offsetParent !== null) {
        const hh = scroller.clientHeight || 400, y = Math.max(0, a) * ROW;
        if (y < scroller.scrollTop + ROW || y > scroller.scrollTop + hh - 2 * ROW) scrollTo(Math.max(0, a), true);
      }
      draw();
    },
    onPick(cb) { pick = cb; },
    setExpanded(on) { expanded = on; bText.setAttribute('aria-pressed', String(on)); requestAnimationFrame(draw); },
    onExpand(cb) { expCb = cb; },
    onHover(cb) { hoverCb = cb; },
  };
  return api;
}
