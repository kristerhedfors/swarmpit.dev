import { NODE_KINDS } from '../types.js';
import type { FilterMask, NodeList, RunModel } from '../types.js';
import { h } from '../util/dom.js';

export interface NodeListEl extends NodeList {
  readonly el: HTMLElement;
  onHighlight(cb: (node: number | null) => void): void;
  onFocusNode(cb: (node: number) => void): void;
}

const ORDER = ['agent', 'task', 'board', 'thread', 'post', 'persona', 'handle'];

export function createNodeList(): NodeListEl {
  let m: RunModel | null = null, nodes: number[] = [], active = -1, sig = '', lastUpd = 0;
  let pick: (n: number) => void = () => undefined, hl: (n: number | null) => void = () => undefined, foc: (n: number) => void = () => undefined;
  const el = h('div.nodelist.sr-only-focusable', { role: 'listbox', tabindex: '0', 'aria-label': 'Nodes in the 3D view' });
  const optId = (n: number): string => `nl-${n}`;

  function setActive(j: number): void {
    if (!nodes.length) return;
    active = Math.max(0, Math.min(nodes.length - 1, j));
    const n = nodes[active]!;
    el.setAttribute('aria-activedescendant', optId(n));
    for (const o of el.children) o.setAttribute('aria-selected', String(o.id === optId(n)));
    el.querySelector(`#${optId(n)}`)?.scrollIntoView({ block: 'nearest' });
    hl(n);
  }
  el.addEventListener('keydown', (e) => {
    let j = active;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') j++;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') j--;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = nodes.length - 1;
    else if (e.key === 'Enter' || e.key === ' ') { if (active >= 0) pick(nodes[active]!); e.preventDefault(); e.stopPropagation(); return; }
    else if (e.key === 'f') { if (active >= 0) foc(nodes[active]!); e.preventDefault(); e.stopPropagation(); return; }
    else return;
    e.preventDefault(); e.stopPropagation();
    setActive(j);
  });
  el.addEventListener('focus', () => { if (active < 0) setActive(0); else setActive(active); });
  el.addEventListener('blur', () => hl(null));
  el.addEventListener('click', (e) => {
    const o = (e.target as Element).closest('[role=option]');
    if (o) pick(Number(o.id.slice(3)));
  });

  return {
    el,
    setModel(model) { m = model; sig = ''; active = -1; el.textContent = ''; nodes = []; },
    update(mask: FilterMask, t: number) {
      if (!m) return;
      const now = performance.now();
      if (now - lastUpd < 500) return;
      lastUpd = now;
      const vis: number[] = [];
      for (let i = 0; i < m.nNodes; i++) if (m.nodeT0[i]! <= t && t < m.nodeT1[i]! && (mask.node[i] ?? 2) > 0) vis.push(i);
      const s = vis.join(',');
      if (s === sig) return;
      sig = s;
      const cur = active >= 0 ? nodes[active] : undefined;
      nodes = vis.sort((a, b) => ORDER.indexOf(NODE_KINDS[m!.nodeKind[a]!]!) - ORDER.indexOf(NODE_KINDS[m!.nodeKind[b]!]!)
        || m!.core.nodes[a]!.label.localeCompare(m!.core.nodes[b]!.label));
      el.replaceChildren(...nodes.map((n) => h('div', { role: 'option', id: optId(n), 'aria-selected': 'false' },
        `${NODE_KINDS[m!.nodeKind[n]!]}: ${m!.core.nodes[n]!.label}${(mask.node[n] ?? 2) === 1 ? ' (filtered)' : ''}`)));
      active = cur !== undefined ? nodes.indexOf(cur) : -1;
      if (document.activeElement === el && active >= 0) setActive(active);
    },
    onPick(cb) { pick = cb; },
    onHighlight(cb) { hl = cb; },
    onFocusNode(cb) { foc = cb; },
  };
}
