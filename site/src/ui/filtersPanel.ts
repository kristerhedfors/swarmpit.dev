import { EDGE_KINDS, EVENT_FAMILY, EVENT_KINDS } from '../types.js';
import type { EdgeKind, EventFamily, EventKind, FilterStore, NodeKind, Phase, RunModel } from '../types.js';
import { h, text, agentVar } from '../util/dom.js';
import { fmtT } from '../util/format.js';
import { ALL_NODE_KINDS } from '../data/filters.js';
import { ICON } from './icons.js';
import { explain } from './glossary.js';

const FAMILY_LABEL: [EventFamily, string][] = [['task', 'Task commands'], ['boardStep', 'Board steps'], ['read', 'Board reads'],
  ['write', 'Posts'], ['social', 'Votes & DMs'], ['think', 'Thinking'], ['irc', 'IRC protocol'], ['lifecycle', 'Lifecycle'], ['idle', 'Idle']];
const NODE_LABEL: Record<NodeKind | 'trail', string> = { agent: 'Agents', task: 'CTF vault', board: 'Board', thread: 'Threads',
  post: 'Posts', persona: 'Bait personas', handle: 'Handles', trail: 'Step trails' };
const EDGE_SHOWN: EdgeKind[] = ['works', 'reads', 'writes', 'reply', 'mention', 'dm', 'upvote', 'exposure'];

export interface FiltersPanel {
  readonly el: HTMLElement;
  setModel(m: RunModel): void;
  open(focusSearch?: boolean): void; close(): boolean; readonly isOpen: boolean;
  focusSearch(): void;
  onHit(cb: (ev: number) => void): void;
  onToggle(cb: (open: boolean) => void): void;
}

export function createFiltersPanel(store: FilterStore): FiltersPanel {
  let m: RunModel | null = null;
  let hitCb: (ev: number) => void = () => undefined;
  let togCb: (o: boolean) => void = () => undefined;
  let hitIdx = -1;
  let isOpen = false;

  const input = h('input.fp-search', { type: 'search', placeholder: 'Search labels, posts, commands…', 'aria-label': 'Search the swarm', spellcheck: 'false', autocomplete: 'off' });
  const count = h('span.fp-count', { 'aria-live': 'polite' });
  const bUp = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Previous match', html: ICON.up });
  const bDown = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Next match', html: ICON.down });
  const body = h('div.fp-body');
  const bReset = h('button.fp-reset', { type: 'button' }, 'Reset filters');
  const bClose = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Close filters', html: ICON.close });
  const el = h('div.fpanel', { role: 'dialog', 'aria-label': 'Filters', hidden: true },
    h('div.fp-head', null, h('strong', null, 'Filter & search'), h('span.tl-spacer'), bReset, bClose),
    h('div.fp-searchrow', null, h('span.fp-sicon', { html: ICON.search }), input, count, bUp, bDown), body);

  let deb = 0;
  input.addEventListener('input', () => {
    clearTimeout(deb);
    deb = window.setTimeout(() => { hitIdx = -1; store.set({ text: input.value }); }, 160);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === 'ArrowDown') { e.preventDefault(); cycle(e.shiftKey ? -1 : 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cycle(-1); }
    else if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      if (input.value) { input.value = ''; store.set({ text: '' }); } else close();
    }
  });
  bUp.addEventListener('click', () => cycle(-1));
  bDown.addEventListener('click', () => cycle(1));
  bReset.addEventListener('click', () => { input.value = ''; store.reset(); });
  bClose.addEventListener('click', () => close());
  el.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } });

  function cycle(d: 1 | -1): void {
    const hits = store.mask().hits;
    if (!hits.length) return;
    hitIdx = (hitIdx + d + hits.length) % hits.length;
    if (hitIdx < 0) hitIdx = 0;
    updateCount();
    hitCb(hits[hitIdx]!);
  }
  function updateCount(): void {
    const s = store.get(), hits = store.mask().hits;
    text(count, s.text.trim() ? (hits.length ? `${hitIdx >= 0 ? hitIdx + 1 + '/' : ''}${hits.length}` : 'no match') : '');
    bUp.disabled = bDown.disabled = !hits.length;
  }

  const toggleBtn = (label: string, on: boolean | 'mixed', onClick: () => void, style = ''): HTMLButtonElement => {
    const b = h('button.chip.fchip', { type: 'button', 'aria-pressed': String(on), style: style || null }, label);
    b.addEventListener('click', onClick);
    return b;
  };
  const section = (title: string, ...kids: (Node | null)[]): HTMLElement =>
    h('fieldset.fp-sec', null, h('legend', null, title, explain('filter:' + title, title)), h('div.fp-chips', null, ...kids));

  function render(): void {
    const s = store.get();
    if (document.activeElement !== input && input.value !== s.text) input.value = s.text;
    const focusedLabel = el.contains(document.activeElement) && document.activeElement !== input ? (document.activeElement as HTMLElement).textContent : null;
    body.textContent = '';
    if (!m) return;
    const flip = <T>(set: Set<T>, v: T): Set<T> => { const n = new Set(set); if (n.has(v)) n.delete(v); else n.add(v); return n; };

    body.append(section('Display',
      toggleBtn('Dim filtered', s.mode === 'dim', () => store.set({ mode: 'dim' })),
      toggleBtn('Hide filtered', s.mode === 'hide', () => store.set({ mode: 'hide' }))));

    if (s.neighbourhoodOf !== null) {
      const nd = m.core.nodes[s.neighbourhoodOf];
      body.append(section('Neighbourhood', toggleBtn(`Only ${nd?.label ?? '?'} + neighbours ✕`, true, () => store.set({ neighbourhoodOf: null }))));
    }

    body.append(section('Agents', toggleBtn('All', s.agents === null, () => store.set({ agents: null })),
      ...m.core.agents.map((a) => {
        const on = s.agents === null || s.agents.has(a.id);
        return toggleBtn(`a${a.id}`, on, () => {
          const cur = s.agents ?? new Set(m!.core.agents.map((x) => x.id));
          const n = flip(cur, a.id);
          store.set({ agents: n.size === m!.nAgents ? null : n });
        }, `--chip:${agentVar(a.id)}`);
      })));

    body.append(section('Nodes', ...ALL_NODE_KINDS.map((k) => toggleBtn(NODE_LABEL[k], s.nodeKinds.has(k), () => store.set({ nodeKinds: flip(s.nodeKinds, k) })))));

    body.append(section('Events', ...FAMILY_LABEL.map(([fam, label]) => {
      const kinds = EVENT_KINDS.filter((k) => EVENT_FAMILY[k] === fam);
      const nOn = kinds.filter((k) => s.eventKinds.has(k)).length;
      const st: boolean | 'mixed' = nOn === 0 ? false : nOn === kinds.length ? true : 'mixed';
      return toggleBtn(label, st, () => {
        const n = new Set<EventKind>(s.eventKinds);
        for (const k of kinds) if (st === true) n.delete(k); else n.add(k);
        store.set({ eventKinds: n });
      });
    })));

    body.append(section('Edges', ...EDGE_SHOWN.filter((k) => EDGE_KINDS.includes(k)).map((k) =>
      toggleBtn(k, s.edgeKinds.has(k), () => store.set({ edgeKinds: flip(s.edgeKinds, k) }), `--chip:var(--edge-${k})`))));

    body.append(section('Phase', ...(['task', 'free'] as Phase[]).map((p) =>
      toggleBtn(p === 'task' ? 'On task' : 'Free time', s.phases.has(p), () => {
        const n = flip(s.phases, p);
        store.set({ phases: n.size ? n : new Set<Phase>(['task', 'free']) });
      }))));

    body.append(section('Time',
      ...([[null, 'All history'], [30000, 'Last 30 s'], [10000, 'Last 10 s']] as [number | null, string][]).map(([v, l]) =>
        toggleBtn(l, s.recentMs === v, () => store.set({ recentMs: v }))),
      s.range ? toggleBtn(`Range ${fmtT(s.range[0])}–${fmtT(s.range[1])} ✕`, true, () => store.set({ range: null }))
        : h('span.fp-hint', null, 'Shift-drag the timeline to select a range')));

    if (focusedLabel) [...body.querySelectorAll('button')].find((b) => b.textContent === focusedLabel)?.focus();
  }

  store.on('change', () => { render(); updateCount(); });

  function open(fs = false): void {
    if (!isOpen) { isOpen = true; el.hidden = false; render(); updateCount(); togCb(true); }
    if (fs) input.focus();
    else el.querySelector<HTMLElement>('button')?.focus();
  }
  function close(): boolean {
    if (!isOpen) return false;
    isOpen = false; el.hidden = true; togCb(false);
    return true;
  }
  return {
    el,
    setModel(model) { m = model; hitIdx = -1; if (isOpen) render(); updateCount(); },
    open, close,
    get isOpen() { return isOpen; },
    focusSearch() { open(true); input.select(); },
    onHit(cb) { hitCb = cb; },
    onToggle(cb) { togCb = cb; },
  };
}
