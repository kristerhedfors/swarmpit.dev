import { h } from '../util/dom.js';
import { ICON } from './icons.js';

const KEYS: [string, string][] = [
  ['Space / k', 'Play / pause'], ['j / l', 'Back / forward 10 s'], ['← / →', 'Back / forward 5 s (Shift or ↑ / ↓ on the timeline: 1 s)'],
  [', / .', 'Previous / next event (filtered)'], ['< / >', 'Slower / faster'], ['0 – 9', 'Seek to 0–90 %'], ['Home / End', 'Start / end'],
  ['f', 'Fullscreen'], ['v', 'Constellation ↔ worldline view'], ['c', 'Captions'], ['n', 'Narration (voice + sound)'], ['Shift + c', 'Follow now (worldline)'],
  ['t', 'Labels on / off'], ['r', 'Reset view'], ['/', 'Search'], ['?', 'This help'], ['Esc', 'Unpin · close panel · release the 3D view · clear range'],
  ['drag · right-drag · wheel', 'Orbit · pan · zoom (pinch on touch / trackpad)'], ['double-click', 'Focus a node'],
  ['W A S D / Q E', 'Orbit / zoom with the 3D view focused'], ['Tab into the view', 'Node list: arrows move, Enter pins, f focuses'],
];

export interface Help { readonly el: HTMLElement; open(): void; close(): boolean; toggle(): void }

export function createHelp(): Help {
  const bClose = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Close help', html: ICON.close });
  const el = h('div.help', { role: 'dialog', 'aria-modal': 'false', 'aria-label': 'Keyboard shortcuts', hidden: true, tabindex: '-1' },
    h('div.help-head', null, h('strong', null, 'Keyboard & gestures'), h('span.tl-spacer'), bClose),
    h('dl.help-keys', null, ...KEYS.flatMap(([k, v]) => [h('dt', null, h('kbd', null, k)), h('dd', null, v)])),
    h('p.muted.small', null, 'Like YouTube, ← / → jump 5 s. Plain scrolling moves the page until you click the 3D view; ctrl + scroll or two fingers always zoom.'));
  let prev: HTMLElement | null = null;
  const api: Help = {
    el,
    open() { prev = document.activeElement as HTMLElement | null; el.hidden = false; el.focus(); },
    close() { if (el.hidden) return false; el.hidden = true; prev?.focus?.(); return true; },
    toggle() { if (el.hidden) api.open(); else api.close(); },
  };
  bClose.addEventListener('click', () => api.close());
  el.addEventListener('keydown', (e) => { if (e.key === 'Escape' || e.key === '?') { e.preventDefault(); e.stopPropagation(); api.close(); } });
  return api;
}
