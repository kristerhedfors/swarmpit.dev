const P = (d: string, extra = ''): string =>
  `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false" ${extra}><path d="${d}"/></svg>`;
const L = (d: string): string =>
  `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${d}"/></svg>`;

export const ICON = {
  play: P('M8 5.5v13a1 1 0 0 0 1.5.86l10.2-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z', 'fill="currentColor"'),
  pause: P('M7 5h3.5v14H7zM13.5 5H17v14h-3.5z', 'fill="currentColor"'),
  replay: L('M3 12a9 9 0 1 0 3-6.7M3 4v5h5'),
  prev: L('M6 5v14M18 6l-8 6 8 6z'),
  next: L('M18 5v14M6 6l8 6-8 6z'),
  speed: L('M12 13l4-4M4.9 19a9 9 0 1 1 14.2 0'),
  cc: L('M3 6h18v12H3zM10 10.5a2 2 0 1 0 0 3M17 10.5a2 2 0 1 0 0 3'),
  tag: L('M3 12V4h8l10 10-8 8zM7.5 8.5h.01'),
  cube: L('M12 2l9 5v10l-9 5-9-5V7zM12 22V12M21 7l-9 5-9-5'),
  worldline: L('M5 21V3M12 21V8M19 21V5M3 16h18'),
  reset: L('M12 3v3M12 18v3M3 12h3M18 12h3M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z'),
  filter: L('M3 5h18l-7 8v6l-4 2v-8z'),
  feed: L('M4 6h16M4 12h16M4 18h10'),
  full: L('M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'),
  unfull: L('M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5'),
  close: L('M6 6l12 12M18 6L6 18'),
  search: L('M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4'),
  up: L('M6 15l6-6 6 6'),
  down: L('M6 9l6 6 6-6'),
  help: L('M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01'),
  skip: P('M4 6l8 6-8 6zM12 6l8 6-8 6z', 'fill="currentColor"'),
  focus: L('M3 8V3h5M21 8V3h-5M3 16v5h5M21 16v5h-5M12 9v6M9 12h6'),
  headphones: L('M3 18v-6a9 9 0 0 1 18 0v6M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z'),
  hood: L('M5 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM19 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM12 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM6.5 7.5l4.5 8.5M17.5 7.5l-4.5 8.5M7 6h10'),
} as const;
export type IconName = keyof typeof ICON;
