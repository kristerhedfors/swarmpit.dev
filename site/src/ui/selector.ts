import type { Goal, IndexBundle, Iface, ModelInfo, Pop, RunCard, RunQuery, SwarmSelector } from '../types.js';
import { h, s, text } from '../util/dom.js';
import { fmtT } from '../util/format.js';
import { explain } from './glossary.js';
import { chooseGoalRun, chooseRun, GOAL_LABEL, goalChips, goalOf, listedModels, modelChips, modelLabel,
  repLabel } from '../data/runs.js';

const IFACES: Iface[] = ['forum', 'wiki', 'cache', 'irc'];
const IFACE_LABEL: Record<Iface, string> = { forum: 'Forum', wiki: 'Wiki', cache: 'Artifact cache', irc: 'IRC' };

export interface Selector extends SwarmSelector {
  readonly active: string | null;
  /** Re-read idx.runs (the player's addRun/updateRun/removeRun mutate it) and re-render. */
  refresh(): void;
}

/** Host actions shown as chips at the start of the selector row (docs/browser-swarm.md §7). Each chip is rendered only
 * when its callback is given; download/delete get the active run id and are disabled while nothing is selected. */
export interface SelectorActions {
  onNew?: () => void;
  onUpload?: () => void;
  onDownload?: (id: string) => void;
  onDelete?: (id: string) => void;
}
/** The 'Local' lane: cards for which match() is true (traces made or uploaded in this browser) leave the model / goal /
 * iface / pop / rep lanes and are listed as one flat group, newest first. Without it nothing changes. */
export interface LocalLane { match(c: RunCard): boolean; label?(c: RunCard): string; title?: string }
export interface SelectorOpts {
  onPick: (id: string) => void;
  onPrefetch: (c: RunCard) => void;
  actions?: SelectorActions;
  local?: LocalLane;
}

/** UTC 'YYYYMMDDHHMMSS' inside a run id (browser-<iface>-<pop>-<stamp>-<hex>) as 'MM-DD HH:MM', else ''. */
export function idStamp(id: string): string {
  const m = /(?:^|-)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:-|$)/.exec(id);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : '';
}
/** Default Local-lane chip label: 'forum · 4 · 10-24 14:02' (iface, agents, start time from the id). */
export function localLabel(c: RunCard): string {
  const st = idStamp(c.id);
  return [c.iface, c.pop === 'solo' ? 'solo' : String(c.nAgents), st || c.id.slice(-8)].join(' · ');
}
/** Local cards newest first: by the id's time stamp, then id. */
export function localOrder(L: RunCard[]): RunCard[] {
  const key = (c: RunCard): string => (/(\d{14})/.exec(c.id)?.[1] ?? '') + c.id;
  return [...L].sort((a, b) => (key(a) < key(b) ? 1 : key(a) > key(b) ? -1 : 0));
}

export function sparkline(d: number[], w = 120, hh = 24): SVGSVGElement {
  const max = Math.max(1, ...d);
  const pts = d.map((v, i) => `${((i + 0.5) / Math.max(1, d.length)) * w},${hh - 1 - (v / max) * (hh - 3)}`).join(' ');
  return s('svg', { class: 'spark', viewBox: `0 0 ${w} ${hh}`, width: w, height: hh, 'aria-hidden': 'true' },
    s('polyline', { points: `0,${hh - 1} ${pts} ${w},${hh - 1}`, class: 'spark-area' }),
    s('polyline', { points: pts, class: 'spark-line' }));
}

export function createSelector(idx: IndexBundle, o: SelectorOpts): Selector {
  let query: RunQuery = {};
  let active: string | null = null;
  const local = o.local;
  const isLocal = (r: RunCard): boolean => !!local && local.match(r);
  const listed = (): RunCard[] => idx.runs.filter((r) => !isLocal(r) && (query.includeHidden || !r.hidden)
    && (!query.model || query.model.includes(r.model)) && (!query.goal || query.goal.includes(goalOf(r)))
    && (!query.iface || query.iface.includes(r.iface)) && (!query.pop || query.pop.includes(r.pop)));
  // idx.runs can change (player addRun/updateRun/removeRun): reindex() rebuilds what is derived from it
  let byId = new Map<string, RunCard>();
  let models: ModelInfo[] = [];
  let primary = '';
  function reindex(): void {
    byId = new Map(idx.runs.map((r) => [r.id, r]));
    models = listedModels(local ? { ...idx, runs: idx.runs.filter((r) => !isLocal(r)) } : idx);
    primary = models[0]?.id ?? '';
  }
  reindex();

  const gModel = h('div.sel-group', { role: 'radiogroup', 'aria-label': 'Model' });
  const gGoal = h('div.sel-group', { role: 'radiogroup', 'aria-label': 'Goal' });
  const gIface = h('div.sel-group', { role: 'radiogroup', 'aria-label': 'Board interface' });
  const gPop = h('div.sel-group', { role: 'radiogroup', 'aria-label': 'Population' });
  const gRep = h('div.sel-group', { role: 'radiogroup', 'aria-label': 'Repetition' });
  const caveat = h('a.sel-caveat', { href: '#findings', hidden: true });
  const infoTitle = h('span.sel-title');
  const infoBlurb = h('span.sel-blurb');
  const infoSpark = h('span.sel-spark');
  const infoStats = h('span.sel-stats');
  const gLocal = h('div.sel-group.sel-local', { role: 'radiogroup', 'aria-label': local?.title ?? 'Local traces', hidden: true });
  const info = h('div.sel-info', { 'aria-live': 'polite' }, infoTitle, infoSpark, infoBlurb, infoStats);
  // host action chips: plain buttons (not radios), so the radiogroup arrow-key handling below never reaches them
  const acts = o.actions ?? {};
  const actBtn = (act: string, label: string, aria: string, cb: (() => void) | undefined): HTMLButtonElement | null => {
    if (!cb) return null;
    const b = h('button.chip.sel-act', { type: 'button', 'data-act': act, 'aria-label': aria, title: aria }, label);
    b.addEventListener('click', () => cb());
    return b;
  };
  /** A callback on the active run id (a no-op while nothing is selected; the chip is disabled then too). */
  const onActive = (f: ((id: string) => void) | undefined): (() => void) | undefined =>
    f && (() => { if (active) f(active); });
  const bNew = actBtn('new', '+', 'Run a new swarm', acts.onNew);
  const bUp = actBtn('upload', 'upload', 'Upload a trace (zip or folder)', acts.onUpload);
  const bDown = actBtn('download', 'download', 'Download the selected trace (zip)', onActive(acts.onDownload));
  const bDel = actBtn('delete', 'delete', 'Delete the selected trace', onActive(acts.onDelete));
  const actRow = [bNew, bUp, bDown, bDel].filter((b): b is HTMLButtonElement => !!b);
  const gActs = actRow.length ? h('div.sel-group.sel-acts', { role: 'group', 'aria-label': 'Trace actions' }, actRow) : null;
  const el = h('div.selector', { role: 'group', 'aria-label': 'Choose a recorded swarm' },
    h('div.sel-row', null, h('span.sel-lab', null, 'Swarm', explain('Swarm')), gActs, gLocal, gModel, gGoal, gIface, gPop, gRep, caveat), info);

  const cur = (): RunCard | undefined => (active ? byId.get(active) : undefined);

  const choose = (model: string, goal: Goal, iface: Iface, pop: Pop, rep: number | null, anyIface = false): RunCard | undefined =>
    chooseRun(listed(), model, iface, pop, rep, anyIface, goal);

  function showInfo(r: RunCard | undefined): void {
    if (!r) { text(infoTitle, ''); text(infoBlurb, ''); infoSpark.textContent = ''; text(infoStats, ''); return; }
    text(infoTitle, r.id + (r.hidden ? ' (unlisted)' : ''));
    text(infoBlurb, r.blurb);
    infoSpark.replaceChildren(sparkline(r.density));
    text(infoStats, (models.length > 1 || isLocal(r) ? `${modelLabel(idx, r.model)} · ` : '')
      + (goalOf(r) === 'unsolvable' ? 'unsolvable goal · ' : '')
      + `${fmtT(r.durationMs, false)} · ${r.stats.events} events · ${r.stats.nodes} nodes`);
  }

  function chip(group: HTMLElement, label: string, checked: boolean, card: RunCard | undefined, extra = ''): HTMLButtonElement {
    const b = h('button.chip', { type: 'button', role: 'radio', 'aria-checked': String(checked), tabindex: checked ? '0' : '-1', disabled: !card }, label);
    if (extra) b.classList.add(extra);
    const hover = (): void => { if (card) { o.onPrefetch(card); showInfo(card); } };
    b.addEventListener('pointerenter', hover);
    b.addEventListener('focus', hover);
    b.addEventListener('pointerleave', () => showInfo(cur()));
    b.addEventListener('blur', () => showInfo(cur()));
    b.addEventListener('click', () => { if (card && card.id !== active) o.onPick(card.id); });
    group.appendChild(b);
    return b;
  }

  function render(): void {
    const c = cur();
    renderActions();
    renderLocal();
    // a local run is active: the site lanes render as if nothing were selected there
    renderSiteLanes(c && isLocal(c) ? undefined : c);
    showInfo(c);
  }

  function renderActions(): void {
    if (bDown) bDown.disabled = !active;
    if (bDel) bDel.disabled = !active;
  }

  function renderLocal(): void {
    const runs = local ? localOrder(idx.runs.filter(isLocal)) : [];
    gLocal.textContent = '';
    gLocal.hidden = !runs.length;
    for (const r of runs) chip(gLocal, local?.label?.(r) ?? localLabel(r), r.id === active, r, 'local');
  }

  /** The model / goal / iface / pop / rep lanes and the caveat link, around c (undefined: the defaults). */
  function renderSiteLanes(c: RunCard | undefined): void {
    const all = listed();
    // the current run's model; a run of an unlisted model (synthetic) shows the primary model's chips
    const model = c && all.some((r) => r.model === c.model) ? c.model
      : all.some((r) => r.model === primary) ? primary : all[0]?.model ?? primary;
    // the current run's goal within that model (a hidden run of a goal without listed runs: the model's first goal)
    const ofModel = all.filter((r) => r.model === model);
    const goal: Goal = c && ofModel.some((r) => goalOf(r) === goalOf(c)) ? goalOf(c) : goalOf(ofModel[0] ?? {});
    const L = ofModel.filter((r) => goalOf(r) === goal);
    const iface = c?.iface ?? L[0]?.iface ?? 'forum', pop = c?.pop ?? 'multi', rep = c?.rep ?? 0;
    gModel.textContent = ''; gGoal.textContent = ''; gIface.textContent = ''; gPop.textContent = ''; gRep.textContent = '';
    // model chips only when more than one model has listed runs; a chip keeps goal, iface, pop and rep where it can
    const shown = modelChips(models, all);
    gModel.hidden = !shown.length;
    for (const mi of shown) chip(gModel, mi.label, mi.id === model, choose(mi.id, goal, iface, pop, rep, true), 'model');
    // goal chips only when both goals have listed runs; a chip keeps model, iface, pop and rep where it can
    const goals = goalChips(all);
    gGoal.hidden = !goals.length;
    for (const g of goals) chip(gGoal, GOAL_LABEL[g], g === goal, chooseGoalRun(all, g, model, iface, pop, rep), 'goal');
    for (const f of IFACES) {
      const card = choose(model, goal, f, pop, rep);
      if (!L.some((r) => r.iface === f)) continue;
      chip(gIface, IFACE_LABEL[f], f === iface, card, 'if-' + f);
    }
    const pops: Pop[] = ['solo', 'multi'];
    for (const p of pops) {
      const ofP = L.filter((r) => r.iface === iface && r.pop === p);
      if (!ofP.length) continue;
      const n = Math.max(...ofP.map((r) => r.nAgents));
      chip(gPop, p === 'solo' ? 'Solo' : `Swarm · ${n}`, p === pop, ofP.find((r) => r.rep === rep) ?? ofP[0]);
    }
    const reps = L.filter((r) => r.iface === iface && r.pop === pop).sort((a, b) => a.rep - b.rep);
    for (const r of reps) chip(gRep, repLabel(r), r.id === active, r, 'rep');
    const cv = c?.caveats.includes('irc_inbound_broken');
    caveat.hidden = !cv;
    if (cv) { caveat.textContent = '⚠ IRC inbound broken'; caveat.title = c?.caveats.join(', ') ?? ''; }
  }

  // Roving tabindex + arrow keys inside each radio group.
  el.addEventListener('keydown', (e) => {
    const t = e.target as HTMLElement;
    const g = t.closest('[role=radiogroup]');
    if (!g || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
    const list = [...g.querySelectorAll<HTMLButtonElement>('[role=radio]:not([disabled])')];
    const i = list.indexOf(t as HTMLButtonElement);
    if (i < 0) return;
    e.preventDefault(); e.stopPropagation();
    const n = list[(i + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length]!;
    n.focus();
    n.click();
  });

  return {
    el,
    get active() { return active; },
    setActive(id) {
      const hadFocus = el.contains(document.activeElement) ? (document.activeElement as HTMLElement).closest('[role=radiogroup]')?.getAttribute('aria-label') : null;
      active = id;
      render();
      if (hadFocus) el.querySelector<HTMLElement>(`[aria-label="${hadFocus}"] [aria-checked=true]`)?.focus();
    },
    setQuery(q) { query = q; render(); },
    refresh() {
      reindex();
      if (active && !byId.has(active)) active = null;
      render();
    },
  };
}
