// Colour roles. Values live in theme.css as CSS custom properties (light + dark steps of the
// same validated palette, see the dataviz reference palette); this module reads them so canvas
// and SVG code paints with the active theme. Call invalidateColors() after a theme change.
//
// Role assignment (fixed, never cycled):
//   agent lanes a0..a7   -> categorical slots 1..8 (identity; lanes are also text-labelled)
//   step category        -> neutral (task cmd), slot1 board read, slot2 board write, slot3 submit
//                           (first three slots validate all-pairs in both modes); idle = hollow;
//                           persistence = critical outline + glyph (secondary encoding)
//   pop solo/multi       -> slot1 / slot2
//   iface                -> slots 1..4 (always shown with a text label)
//   edge kinds           -> exposure neutral, reply slot1, mention slot2, dm slot3, upvote slot7 dashed

let cache: Record<string, string> = {};

export function cv(name: string): string {
  let v = cache[name];
  if (v === undefined) {
    v = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
    cache[name] = v;
  }
  return v;
}
export function invalidateColors() { cache = {}; listeners.forEach((f) => f()); }
const listeners = new Set<() => void>();
export function onThemeChange(f: () => void): () => void { listeners.add(f); return () => listeners.delete(f); }

export const slot = (i: number) => cv(`--series-${(((i % 8) + 8) % 8) + 1}`);

// Agent identity beyond 8 lanes: colours cycle (a8 = a0's hue) and a second encoding separates the cycles
// (agentDash: solid / dashed / dotted lines). With more than 8 lanes, selecting an agent switches to
// highlight-on-select: the selected agent keeps its colour, everyone else is muted.
let focus: { nLanes: number; sel: number | null } = { nLanes: 0, sel: null };
export function setAgentFocus(nLanes: number, sel: number | null) {
  if (focus.nLanes === nLanes && focus.sel === sel) return;
  const was = focus.nLanes > 8 && focus.sel != null;
  focus = { nLanes, sel };
  if (was || (nLanes > 8 && sel != null)) invalidateColors();
}
export const agentColor = (agentId: number) => {
  if (agentId < 0) return cv('--text-muted');
  if (focus.nLanes > 8 && focus.sel != null && agentId !== focus.sel) return cv('--neutral-mark');
  return slot(agentId);
};
/** Line dash per agent cycle of 8 (canvas/uPlot dash arrays; [] = solid). */
export const agentDash = (agentId: number): number[] => (agentId < 8 ? [] : agentId < 16 ? [6, 3] : [2, 2]);
/** Arc dash per interaction kind (lanes draw arcs in neutral ink, so kind is carried by the pattern). */
export const ARC_DASH: Record<string, number[]> = { reply: [], mention: [4, 3], dm: [1.5, 2.5] };

export type StepClass = 'cmd' | 'read' | 'write' | 'submit' | 'idle';
export const STEP_CLASSES: { id: StepClass; label: string }[] = [
  { id: 'cmd', label: 'task command' },
  { id: 'read', label: 'board read' },
  { id: 'write', label: 'board write' },
  { id: 'submit', label: 'submit' },
  { id: 'idle', label: 'idle' },
];
export function stepColor(c: StepClass): string {
  switch (c) {
    case 'read': return slot(0);
    case 'write': return slot(1);
    case 'submit': return slot(2);
    case 'idle': return cv('--text-muted');
    default: return cv('--neutral-mark');
  }
}
export const IFACES = ['forum', 'wiki', 'cache', 'irc'];
export const ifaceColor = (iface: string | null | undefined) => {
  const i = IFACES.indexOf(iface || '');
  return i < 0 ? cv('--text-muted') : slot(i);
};
export const popColor = (pop: string | null | undefined) => (pop === 'solo' ? slot(0) : pop === 'multi' ? slot(1) : cv('--text-muted'));

export const EDGE_KINDS = ['exposure', 'reply', 'mention', 'dm', 'upvote'] as const;
export function edgeColor(kind: string): string {
  switch (kind) {
    case 'reply': return slot(0);
    case 'mention': return slot(1);
    case 'dm': return slot(2);
    case 'upvote': return slot(6);
    default: return cv('--neutral-mark');
  }
}
export const critical = () => cv('--status-critical');
export const good = () => cv('--status-good');
export const warning = () => cv('--status-warning');

/** Sequential blue ramp (light->dark) for heatmaps; t in [0,1]. */
const SEQ_LIGHT = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'];
export function seq(t: number): string {
  const dark = document.documentElement.dataset.resolvedTheme === 'dark';
  const ramp = dark ? [...SEQ_LIGHT].reverse() : SEQ_LIGHT;
  const i = Math.max(0, Math.min(ramp.length - 1, Math.round(t * (ramp.length - 1))));
  return ramp[i];
}
export function seqInk(t: number): string {
  const dark = document.documentElement.dataset.resolvedTheme === 'dark';
  // pick ink by fill luminance: light fills get dark ink
  const lightFill = dark ? t < 0.45 : t < 0.55;
  return lightFill ? (dark ? '#ffffff' : '#0b0b0b') : (dark ? '#0b0b0b' : '#ffffff');
}
export function withAlpha(hex: string, a: number): string {
  if (!hex.startsWith('#') || (hex.length !== 7 && hex.length !== 4)) return hex;
  const full = hex.length === 4 ? '#' + [...hex.slice(1)].map((c) => c + c).join('') : hex;
  const n = parseInt(full.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
