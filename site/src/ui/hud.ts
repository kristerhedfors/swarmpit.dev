import type { AgentState, Hud, SwarmState } from '../types.js';
import { h, text } from '../util/dom.js';
import { fmtT } from '../util/format.js';
import { explain } from './glossary.js';

export interface HudEl extends Hud { readonly el: HTMLElement; setTotal(n: number): void }

export function createHud(): HudEl {
  let total = 0;
  const onB = h('span'), ll = h('span'), sol = h('span'), onset = h('span.hud-onset', { hidden: true });
  // The HUD is aria-hidden (it changes every frame); its ⓘ buttons are pointer-only.
  const x = (k: string, l: string): HTMLButtonElement | null => { const b = explain(k, l); b?.setAttribute('tabindex', '-1'); return b; };
  const el = h('div.hud', { 'aria-hidden': 'true', 'data-tag-avoid': '' }, h('span.hud-k', null, 'on board '), onB, x('on board', 'on board'), h('span.hud-sep', null, ' · '),
    h('span.hud-k', null, 'live↔live '), ll, x('hud:live↔live', 'live↔live'), h('span.hud-sep', null, ' · '), h('span.hud-k', null, 'solved '), sol, x('hud:solved', 'solved'),
    onset, h('span.hud-k', { hidden: true }, x('swarm onset', 'swarm onset')));
  return {
    el,
    setTotal(n) { total = n; },
    update(s: SwarmState, agents: AgentState[]) {
      text(onB, `${s.onBoard.length}/${total}`);
      text(ll, String(s.liveLive));
      text(sol, `${agents.filter((a) => a.solved).length}/${total}`);
      onset.hidden = (onset.nextSibling as HTMLElement).hidden = s.onsetT === null;
      if (s.onsetT !== null) text(onset, ` · swarm onset ${fmtT(s.onsetT, false)}`);
      el.classList.toggle('swarming', s.clusters.length > 0);
    },
  };
}
