// Keyboard shortcuts (CONTRACT 6.4). One global listener; the active view installs a handler.
import { h } from './dom';

export type KeyHandler = (e: KeyboardEvent) => boolean | void;
let viewHandler: KeyHandler | null = null;
export function setViewKeys(fn: KeyHandler | null) { viewHandler = fn; }

export const SHORTCUTS: [string, string][] = [
  ['← / →', 'scrub −1 s / +1 s (Shift ±10 s, Alt ±0.1 s)'],
  [', / .', 'previous / next event (any lane)'],
  ['[ / ]', 'previous / next step of the selected agent'],
  ['{ / }', 'previous / next board event (post/read)'],
  ['Space', 'play / pause'],
  ['- / =', 'playback speed down / up'],
  ['Home / End', 'jump to run start / end (live: follow head)'],
  ['1 – 9', 'select agent lane a0 – a8 (0 = board lane)'],
  ['x', 'toggle x axis time ↔ step'],
  ['a', 'toggle reply / mention arcs'],
  ['g / b / i', 'focus graph / board / inspector panel'],
  ['f', 'toggle follow-live (live runs)'],
  ['/', 'focus filter / search'],
  ['j / k, Enter', 'move / open in tables'],
  ['Esc', 'clear selection, close tooltip'],
  ['?', 'shortcut help'],
];

let modal: HTMLElement | null = null;
export function toggleHelp() {
  if (modal) { modal.remove(); modal = null; return; }
  modal = h('div', { class: 'modal-bg', onclick: (e: Event) => { if (e.target === modal) toggleHelp(); } },
    h('div', { class: 'modal', role: 'dialog', 'aria-label': 'Keyboard shortcuts' },
      h('h2', { style: { marginBottom: '8px' } }, 'Keyboard shortcuts'),
      h('table', null, ...SHORTCUTS.map(([k, d]) => h('tr', null, h('td', { style: { paddingRight: '14px' } }, ...k.split(' ').map((p) => (p === '/' && k !== '/' ? ' / ' : p === '–' ? ' – ' : p === 'Shift' ? p : h('kbd', null, p))).flatMap((x) => [x, ' '])), h('td', { class: 'secondary' }, d)))),
      h('p', { class: 'muted' }, 'Esc closes.')));
  document.body.appendChild(modal);
}

document.addEventListener('keydown', (e) => {
  const tgt = e.target as HTMLElement | null;
  const typing = tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.tagName === 'SELECT' || tgt.isContentEditable);
  if (e.key === 'Escape') {
    if (modal) { toggleHelp(); e.preventDefault(); return; }
    if (typing) { tgt!.blur(); return; }
  }
  if (typing || e.metaKey || e.ctrlKey) return;
  if (e.key === '?') { toggleHelp(); e.preventDefault(); return; }
  if (e.key === '/') {
    const s = document.querySelector<HTMLInputElement>('input[type="search"]');
    if (s) { s.focus(); e.preventDefault(); return; }
  }
  if (viewHandler && viewHandler(e)) e.preventDefault();
});
