import type { Goal, Iface, IndexBundle, ModelInfo, Pop, RunCard } from '../types.js';

// Run-card queries shared by the selector, the end card and the findings toggle. DOM-free (Node tests).
// Every lookup stays inside one model and one goal: a run's siblings, "next" and chip targets never cross model
// lanes or mix solvable with unsolvable runs (SPEC 4.11).

export const PRIMARY_MODEL = 'qwen-3.8-27b';
const IFACE_ORDER: readonly Iface[] = ['forum', 'wiki', 'cache', 'irc'];

export const GOALS: readonly Goal[] = ['solvable', 'unsolvable'];
export const GOAL_LABEL: Record<Goal, string> = { solvable: 'Solvable', unsolvable: 'Unsolvable' };

/** Goal of a card, run meta or findings row: no `goal` key is the historical solvable CTF. */
export const goalOf = (x: { goal?: string }): Goal => (x.goal === 'unsolvable' ? 'unsolvable' : 'solvable');

/** Rep-pill display label with the goal marker: 'r3' / 'r3u' (ids carry '-unsolvable', not 'u'). */
export const repLabel = (r: RunCard): string => `r${r.rep}${goalOf(r) === 'unsolvable' ? 'u' : ''}`;

/** Goals with a listed run (solvable first); with `model`, only that model's runs count. */
export function listedGoals(L: RunCard[], model?: string): Goal[] {
  return GOALS.filter((g) => L.some((r) => !r.hidden && goalOf(r) === g && (model === undefined || r.model === model)));
}

/** Goal chips for the selector: both goals when both have listed runs among `L`, else none (no chip row). */
export function goalChips(L: RunCard[]): Goal[] {
  const g = listedGoals(L);
  return g.length > 1 ? g : [];
}

/** Model id without its org prefix: 'zai-org/GLM-5.3-Flash' -> 'GLM-5.3-Flash'. */
export const shortModel = (id: string): string => id.slice(id.lastIndexOf('/') + 1);

/** Models with listed runs, primary first. Pre-multi-model indexes have no `models`: derive them from the cards. */
export function listedModels(idx: IndexBundle): ModelInfo[] {
  if (idx.models?.length) return idx.models;
  const n = new Map<string, number>();
  for (const r of idx.runs) if (!r.hidden && !r.caveats.includes('synthetic')) n.set(r.model, (n.get(r.model) ?? 0) + 1);
  return [...n].map(([id, nRuns]) => ({ id, slug: '', label: shortModel(id), provider: '', nRuns }));
}

/** Model chips for the selector: models with a listed run among `L`; empty (no chip row) unless there are two. */
export function modelChips(models: ModelInfo[], L: RunCard[]): ModelInfo[] {
  const shown = models.filter((mi) => L.some((r) => r.model === mi.id && !r.hidden));
  return shown.length > 1 ? shown : [];
}

export const primaryModel = (idx: IndexBundle): string => listedModels(idx)[0]?.id ?? PRIMARY_MODEL;

export function modelLabel(idx: IndexBundle, id: string): string {
  return idx.models?.find((m) => m.id === id)?.label ?? shortModel(id);
}

/**
 * The model's card for (iface, pop, rep). Falls back to that iface's first pop/rep; with `anyIface` (a model
 * chip) also to the model's cards of the same pop, then to its first card. With `goal` only that goal's cards
 * count, except that a model chip for a model without runs of `goal` falls back to the model's other goal.
 */
export function chooseRun(L: RunCard[], model: string, iface: Iface, pop: Pop, rep: number | null,
                          anyIface = false, goal?: Goal): RunCard | undefined {
  const ofModel = L.filter((r) => r.model === model);
  const ofGoal = goal === undefined ? ofModel : ofModel.filter((r) => goalOf(r) === goal);
  const m = ofGoal.length || !anyIface ? ofGoal : ofModel;
  const c = m.filter((r) => r.iface === iface);
  const p = c.filter((r) => r.pop === pop);
  const mp = m.filter((r) => r.pop === pop);
  const pool = p.length ? p : c.length ? c : !anyIface ? [] : mp.length ? mp : m;
  return pool.find((r) => r.rep === rep) ?? pool[0];
}

/**
 * A goal chip: the current model's run of `goal` at (iface, pop, rep) where possible (model-chip fallbacks), else
 * the first listed model (primary first) that has runs of `goal`.
 */
export function chooseGoalRun(L: RunCard[], goal: Goal, model: string, iface: Iface, pop: Pop,
                              rep: number | null): RunCard | undefined {
  const own = L.some((r) => r.model === model && goalOf(r) === goal);
  const m = own ? model : L.find((r) => goalOf(r) === goal)?.model;
  return m === undefined ? undefined : chooseRun(L, m, iface, pop, rep, true, goal);
}

/** Same model, goal, pop and rep on the next interface (cyclic), skipping broken-IRC runs. */
export function siblingRun(L: RunCard[], cur: RunCard): RunCard | undefined {
  return IFACE_ORDER.map((_, k) => IFACE_ORDER[(IFACE_ORDER.indexOf(cur.iface) + 1 + k) % 4]!)
    .map((f) => L.find((r) => r.iface === f && r.pop === cur.pop && r.rep === cur.rep && r.model === cur.model
      && goalOf(r) === goalOf(cur) && r.id !== cur.id && !r.caveats.includes('irc_inbound_broken')))
    .find(Boolean);
}

/** The following listed run of the same model and goal (cyclic); null when it is that lane's only run. */
export function nextRun(L: RunCard[], cur: RunCard): RunCard | null {
  const sameM = L.filter((r) => r.model === cur.model);
  const same = sameM.filter((r) => goalOf(r) === goalOf(cur));
  // a hidden run continues in its model and goal, else in its model, else (unlisted model) with the listed runs
  const S = same.length ? same : sameM.length ? sameM : L;
  const i = S.findIndex((r) => r.id === cur.id);
  const n = S.length > 1 ? S[(i + 1) % S.length] ?? null : null;
  return n && n.id !== cur.id ? n : null;
}
