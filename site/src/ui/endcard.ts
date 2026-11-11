import type { EndCard, RunCard } from '../types.js';
import { h } from '../util/dom.js';
import { ICON } from './icons.js';

export interface EndCardEl extends EndCard {
  readonly el: HTMLElement; readonly visible: boolean;
  /** Show the "See the numbers" action (the page has a #findings section); on by default (PlayerOpts.findings). */
  setFindings(on: boolean): void;
}

export function createEndCard(): EndCardEl {
  let cb: (a: 'replay' | 'next' | 'sibling' | 'findings') => void = () => undefined;
  const bReplay = h('button.ec-btn.primary', { type: 'button', html: ICON.replay + '<span>Replay</span>' });
  const bSib = h('button.ec-btn', { type: 'button' });
  const bNext = h('button.ec-btn', { type: 'button' });
  const bFind = h('button.ec-btn', { type: 'button' }, 'See the numbers ↓');
  const el = h('div.endcard', { role: 'dialog', 'aria-label': 'End of run', hidden: true },
    h('div.ec-inner', null, h('div.ec-ttl', null, 'End of run'), h('div.ec-row', null, bReplay, bSib, bNext, bFind)));
  bReplay.addEventListener('click', () => cb('replay'));
  bSib.addEventListener('click', () => cb('sibling'));
  bNext.addEventListener('click', () => cb('next'));
  bFind.addEventListener('click', () => cb('findings'));
  return {
    el,
    get visible() { return !el.hidden; },
    show(next, sibling) {
      bSib.hidden = !sibling;
      if (sibling) bSib.textContent = `Same swarm on a ${sibling.iface} →`;
      bNext.hidden = !next;
      if (next) bNext.textContent = `Next swarm: ${next.id} →`;
      el.hidden = false;
      bReplay.focus({ preventScroll: true });
    },
    hide() { el.hidden = true; },
    setFindings(on) { bFind.hidden = !on; },
    onAction(f) { cb = f; },
  };
}
export type { RunCard };
