// Theme colours for canvas renderers, read from the same CSS custom properties the DOM uses
// (roles mirror dash/web/src/lib/colors.ts). Values are linear-ish 0..1 sRGB triples.

import type { Iface, ThemeColors } from '../types.js';

type RGB = [number, number, number];

const DARK: Record<string, string> = {
  '--page': '#0d0d0d', '--text-primary': '#ffffff', '--text-muted': '#898781', '--neutral-mark': '#6b6a64',
  '--accent': '#3987e5', '--status-critical': '#e66767', '--status-good': '#199e70',
  '--series-1': '#3987e5', '--series-2': '#d95926', '--series-3': '#199e70', '--series-4': '#c98500',
  '--series-5': '#d55181', '--series-6': '#008300', '--series-7': '#9085e9', '--series-8': '#e66767',
  '--live-live': '#f5c542', '--leak': '#e66767', '--task': '#e8e6dc',
};

export function parseColor(s: string, fb: RGB = [0.5, 0.5, 0.5]): RGB {
  s = s.trim();
  let m = /^#([0-9a-f]{3,8})$/i.exec(s);
  if (m) {
    let h = m[1]!;
    if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
    const n = parseInt(h.slice(0, 6), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  m = /^rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)/i.exec(s);
  if (m) return [+m[1]! / 255, +m[2]! / 255, +m[3]! / 255];
  return fb;
}

export function readTheme(el: Element | null, iface: Iface = 'forum'): ThemeColors {
  const cs = el && typeof getComputedStyle === 'function' ? getComputedStyle(el) : null;
  const get = (name: string): RGB => {
    const v = cs?.getPropertyValue(name).trim();
    return parseColor(v || DARK[name] || '#888888');
  };
  const slot = (i: number) => get(`--series-${(i % 8) + 1}`);
  const ifaceC = slot(['forum', 'wiki', 'cache', 'irc'].indexOf(iface) >>> 0 & 7);
  const neutral = get('--neutral-mark'), muted = get('--text-muted');
  return {
    bg: get('--page'), text: get('--text-primary'),
    agents: [0, 1, 2, 3, 4, 5, 6, 7].map(slot),
    kinds: { task: get('--task'), board: ifaceC, agent: get('--text-primary'), thread: ifaceC, post: muted,
             persona: muted, handle: neutral },
    edges: { contains: neutral, authored: neutral, works: neutral, reads: ifaceC, writes: ifaceC,
             exposure: neutral, reply: slot(0), mention: slot(1), dm: slot(2), upvote: slot(6) },
    cats: { idle: muted, board: ifaceC, submit: get('--status-good'), persistence: get('--status-critical'),
            ctf: [0.78, 0.77, 0.72], recon: [0.62, 0.61, 0.57], other: neutral },
    leak: get('--leak'), accent: get('--accent'), liveLive: get('--live-live'),
  };
}

export const css = (c: RGB, a = 1): string =>
  `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
