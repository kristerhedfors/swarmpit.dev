import type { Findings, Goal, PooledMetric } from '../types.js';
import { goalOf } from './runs.js';

// Findings slices for the findings panel, DOM-free (Node tests). Rows without `model` belong to the primary model
// (pre-multi-model indexes); rows without `goal` are solvable (SPEC 4.11).

export type PooledRow = Findings['pooled'][number];
export type CellRow = Findings['cells'][number];
export type MetricKey = 'contacted' | 'posted' | 'posts' | 'board_steps' | 'board_steps_free' | 'share_board' | 'live_edges';

/** Goals present in the findings rows, solvable first; `findings.goals` when the exporter wrote it. */
export function findingsGoals(f: Findings): Goal[] {
  if (f.goals?.length) return f.goals;
  return (['solvable', 'unsolvable'] as Goal[]).filter((g) => f.pooled.some((p) => goalOf(p) === g)
    || f.cells.some((c) => goalOf(c) === g));
}

/** One (model, goal) slice of the findings: pooled and cells filtered, everything else shared. */
export function findingsView(f: Findings, model: string, goal: Goal, primary: string): Findings {
  const keep = <T extends { model?: string; goal?: string }>(x: T): boolean => (x.model ?? primary) === model && goalOf(x) === goal;
  return { ...f, pooled: f.pooled.filter(keep), cells: f.cells.filter(keep) };
}

/** A pooled metric of a row, or null when it is n/a for the row (e.g. free time under the unsolvable goal). */
export const metricOf = (r: PooledRow, k: MetricKey): PooledMetric | null => (r.metrics as Record<string, PooledMetric | undefined>)[k] ?? null;

/** Pooled rows that count for headline numbers: those whose cells are not excluded by default. */
export function headlineRows(f: Findings): PooledRow[] {
  return f.pooled.filter((r) => !f.cells.some((c) => c.iface === r.iface && c.excludedByDefault));
}

/** Agent-weighted mean of a pooled metric over rows that have it; null when none does (n/a). */
export function weighted(rows: PooledRow[], k: MetricKey): number | null {
  const xs = rows.filter((r) => metricOf(r, k) !== null);
  const n = xs.reduce((a, r) => a + r.nAgents, 0);
  return n ? xs.reduce((a, r) => a + metricOf(r, k)!.v * r.nAgents, 0) / n : null;
}

/** Agent-weighted solve rate over the slice's headline cells; null without cells. */
export function solveRate(f: Findings): number | null {
  const cs = f.cells.filter((c) => !c.excludedByDefault);
  const n = cs.reduce((a, c) => a + c.nAgents, 0);
  return n ? cs.reduce((a, c) => a + c.solveRate * c.nAgents, 0) / n : null;
}

export interface GoalComparisonRow { k: MetricKey | 'solve_rate'; title: string; unit: 'rate' | 'count';
  v: Record<Goal, number | null> }

/** Task-phase comparison of the two goals for one model (headline rows, agent-weighted). Free time is n/a for
 *  the unsolvable goal; the solve rate there is 0 by construction. Empty unless the model has both goals. */
export function goalComparison(f: Findings, model: string, primary: string): GoalComparisonRow[] {
  const views = { solvable: findingsView(f, model, 'solvable', primary), unsolvable: findingsView(f, model, 'unsolvable', primary) };
  if (!views.solvable.pooled.length || !views.unsolvable.pooled.length) return [];
  const defs: { k: MetricKey | 'solve_rate'; title: string; unit: 'rate' | 'count' }[] = [
    { k: 'contacted', title: 'Found the board', unit: 'rate' },
    { k: 'posted', title: 'Posted', unit: 'rate' },
    { k: 'posts', title: 'Posts per agent', unit: 'count' },
    { k: 'board_steps', title: 'Board steps per agent (whole run)', unit: 'count' },
    { k: 'solve_rate', title: 'Solve rate', unit: 'rate' },
    { k: 'board_steps_free', title: 'Board steps in free time', unit: 'count' },
  ];
  return defs.map((d) => ({ ...d, v: {
    solvable: d.k === 'solve_rate' ? solveRate(views.solvable) : weighted(headlineRows(views.solvable), d.k),
    unsolvable: d.k === 'solve_rate' ? solveRate(views.unsolvable) : weighted(headlineRows(views.unsolvable), d.k),
  } }));
}
