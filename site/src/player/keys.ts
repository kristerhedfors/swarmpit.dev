import type { BindKeys, KeyAction } from '../types.js';

const typing = (el: EventTarget | null): boolean => {
  const e = el as HTMLElement | null;
  if (!e || !e.tagName) return false;
  const t = e.tagName;
  return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || e.isContentEditable;
};

/** Pure key -> action mapping (exported for tests). */
export function keyAction(k: KeyboardEvent | { key: string; shiftKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }): KeyAction | null {
  if (k.ctrlKey || k.metaKey || k.altKey) return null;
  switch (k.key) {
    case ' ': case 'k': return 'toggle';
    case 'j': return 'back10';
    case 'l': return 'fwd10';
    case 'ArrowLeft': return 'back5';
    case 'ArrowRight': return 'fwd5';
    case ',': return 'prevEvent';
    case '.': return 'nextEvent';
    case '<': return 'slower';
    case '>': return 'faster';
    case 'Home': return 'home';
    case 'End': return 'end';
    case 'f': return 'fullscreen';
    case 'v': return 'mode';
    case 'c': return 'captions';
    case 'n': return 'narration';
    case 'C': return 'follow';
    case 't': return 'labels';
    case 'r': return 'reset';
    case '/': return 'search';
    case '?': return 'help';
    case 'Escape': return 'escape';
  }
  if (/^[0-9]$/.test(k.key)) return { seekPct: Number(k.key) * 10 };
  return null;
}

/** Keys the page itself uses for scrolling: outside the player we only take them once the user has used the player. */
const SCROLL_KEYS = new Set([' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown']);

export const bindKeys: BindKeys = (scope, run, isPrimary, owned) => {
  const onKey = (e: KeyboardEvent): void => {
    if (e.defaultPrevented) return;
    const ae = document.activeElement;
    const inScope = !!ae && scope.contains(ae);
    if (!inScope && !isPrimary()) return;
    if (!inScope && SCROLL_KEYS.has(e.key) && !(owned?.() ?? true)) return;
    if (typing(e.target)) return;
    if (!inScope && ae && ae !== document.body && ae.closest('dialog,[role=dialog]')) return;
    const a = keyAction(e);
    if (!a) return;
    const tgt = e.target as HTMLElement | null;
    // The canvas owns camera keys while focused: 'r' always, arrows only while engaged (else they seek).
    if (tgt?.tagName === 'CANVAS' && (e.key === 'r' || (e.key.startsWith('Arrow') && !!tgt.closest('.engaged, .is-engaged')))) return;
    // Space/Enter on a focused button activates the button instead.
    if (e.key === ' ' && tgt && (tgt.tagName === 'BUTTON' || tgt.getAttribute('role') === 'option' || tgt.getAttribute('role')?.startsWith('menuitem'))) return;
    if (tgt?.getAttribute('role') === 'listbox' && e.key.startsWith('Arrow')) return;
    e.preventDefault();
    run(a);
  };
  document.addEventListener('keydown', onKey);
  return () => document.removeEventListener('keydown', onKey);
};
