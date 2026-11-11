import type { PickHit, RunModel, Tooltip } from '../types.js';
import { h } from '../util/dom.js';
import { agentBlock, boardBlock, edgeBlock, kindOf, postBlock, stepBlock, taskBlock, threadBlock } from './info.js';

export interface Tip extends Tooltip { setModel(m: RunModel): void; readonly el: HTMLElement }

export function hitContent(m: RunModel, hit: PickHit, t: number, full: boolean): HTMLElement[] {
  if (hit.kind === 'step') return stepBlock(m, hit.event, full);
  if (hit.kind === 'edge') return edgeBlock(m, hit.edge, t);
  const n = hit.node;
  switch (kindOf(m, n)) {
    case 'agent': { const a = m.core.nodes[n]?.agent ?? m.core.agents.findIndex((r) => r.node === n); return a >= 0 ? agentBlock(m, a, t, full) : []; }
    case 'thread': return threadBlock(m, n, t);
    case 'post': return postBlock(m, n, t, full);
    case 'task': return taskBlock(m, t);
    case 'board': return boardBlock(m, t);
    default: {
      const nd = m.core.nodes[n]!, st = m.nodeStats(n, t);
      return [h('div.ihead', null, h('span.ichip.kind', null, kindOf(m, n) === 'persona' ? 'bait persona' : 'handle'), h('span.iname', null, nd.label)),
        h('div.irow', null, `${st.posts} ${m.core.run.nouns.posts} by now`, st.posts && !full ? h('span.muted', null, ' · click to list them') : null)];
    }
  }
}

export function createTooltip(host: HTMLElement): Tip {
  let m: RunModel | null = null;
  const el = h('div.tooltip', { role: 'tooltip', hidden: true });
  host.appendChild(el);
  let key = '';
  return {
    el,
    setModel(model) { m = model; el.hidden = true; key = ''; },
    show(hit, x, y, t) {
      if (!m) return;
      const k = JSON.stringify(hit) + '@' + Math.floor(t / 250);
      if (k !== key) { key = k; el.replaceChildren(...hitContent(m, hit, t, false)); }
      el.hidden = false;
      const hw = host.clientWidth, hh = host.clientHeight;
      const w = el.offsetWidth, ht = el.offsetHeight;
      let px = x + 16, py = y + 16;
      if (px + w > hw - 8) px = Math.max(8, x - w - 16);
      if (py + ht > hh - 8) py = Math.max(8, y - ht - 16);
      el.style.transform = `translate3d(${Math.round(px)}px,${Math.round(py)}px,0)`;
    },
    hide() { el.hidden = true; key = ''; },
  };
}
