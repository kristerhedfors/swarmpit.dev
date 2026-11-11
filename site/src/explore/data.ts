// Pure (DOM-free) data layer of the landscape explorer: validation, dimensions, filters, aggregation (pivot,
// Wilson intervals), lineage layout, landscape grids, sorting and CSV. Everything here is unit-tested in
// tools/test/explore.test.mjs against a synthetic fixture.
import { EXPLORER_SCHEMA } from './types.js';
import type { Agg, Dim, Dims, Engagement, Episode, Explorer, Filters, Landscape, LandscapeCell, MetricId, Seed } from './types.js';

// ------------------------------------------------------------------------------------------- validation
// Whitelists: ids, enums and finite numbers only. Anything else is dropped (rows) or rejected (top level).
const RE_RUN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const RE_HEX = /^[0-9a-f]{8,64}$/;
const RE_SLUG = /^[a-z0-9]{2,16}$/;
const RE_CODE = /^[A-Z][A-Z0-9_]{1,39}$/;
const RE_ENUM = /^[a-z0-9][a-z0-9_-]{0,23}$/;
const RE_BAND = /^(le\d{1,4}|\d{1,4}-\d{1,4}|gt\d{1,4})$/;
const RE_LABEL = /^[A-Za-z0-9][A-Za-z0-9 .,:+()/_-]{0,47}$/;
const RE_CELL_PART = /^[A-Za-z0-9_-]{1,40}$/;
const RE_ELITE = /^[A-Za-z0-9_|/:@.=-]{1,160}$/;

export const BANDS_DEFAULT = ['le120', '121-200', '201-320', 'gt320'];
export const GOALS_DEFAULT = ['solvable', 'unsolvable'];
export const ORIGINS_DEFAULT = ['pilot', 'scratch', 'mutation'];
export const LEGACY_MODEL = 'qwen38';

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const int = (v: unknown): v is number => fin(v) && Number.isInteger(v);
const str = (v: unknown, re: RegExp): v is string => typeof v === 'string' && re.test(v);
const numOrNull = (v: unknown): number | null | undefined => (v === null || v === undefined ? null : fin(v) ? v : undefined);

class Bad extends Error { constructor() { super('E_EXPLORER_SCHEMA'); } }
function need<T>(ok: boolean, v: T): T { if (!ok) throw new Bad(); return v; }

function strList(v: unknown, re: RegExp, fallback: string[]): string[] {
  if (!Array.isArray(v)) return fallback.slice();
  return v.filter((x): x is string => str(x, re));
}

function normDims(raw: unknown): Dims {
  const d = isObj(raw) ? raw : {};
  const model = (Array.isArray(d['model']) ? d['model'] : []).flatMap((m) => {
    if (!isObj(m) || !str(m['slug'], RE_SLUG)) return [];
    const label = str(m['label'], RE_LABEL) ? m['label'] : m['slug'];
    return [{
      slug: m['slug'], label,
      origin: str(m['origin'], RE_ENUM) ? m['origin'] : 'unknown',
      size_b: fin(m['size_b']) ? m['size_b'] : null,
      provider: str(m['provider'], RE_ENUM) ? m['provider'] : 'unknown',
      retired: m['retired'] === true,
    }];
  });
  const channel = (Array.isArray(d['channel']) ? d['channel'] : []).flatMap((c) =>
    isObj(c) && str(c['id'], RE_ENUM) ? [{ id: c['id'], label: str(c['label'], RE_LABEL) ? c['label'] : c['id'] }] : []);
  return {
    model, channel,
    length_band: strList(d['length_band'], RE_BAND, BANDS_DEFAULT),
    trigger_code: strList(d['trigger_code'], RE_CODE, []),
    goal: strList(d['goal'], RE_ENUM, GOALS_DEFAULT),
    origin: strList(d['origin'], RE_ENUM, ORIGINS_DEFAULT),
  };
}

function normSeed(s: unknown): Seed | null {
  if (!isObj(s)) return null;
  const parent = s['parent_id'] === null || s['parent_id'] === undefined ? null : s['parent_id'];
  const genModel = s['gen_model'] === null || s['gen_model'] === undefined ? null : s['gen_model'];
  if (!int(s['seed_chars']) || !str(s['length_band'], RE_BAND) || !str(s['origin'], RE_ENUM) || !int(s['generation'])) return null;
  if (parent !== null && !str(parent, RE_HEX)) return null;
  if (genModel !== null && !str(genModel, RE_SLUG)) return null;
  return { seed_chars: s['seed_chars'], length_band: s['length_band'], origin: s['origin'], bootstrap: s['bootstrap'] === true,
    parent_id: parent, generation: s['generation'], gen_model: genModel };
}

const ENG_KEYS = ['board_reads', 'relay_connections', 'board_refs', 'agents_engaged', 'engagement_score'] as const;

function normEngagement(e: unknown, strict: boolean): Engagement | null {
  const o = isObj(e) ? e : {};
  const out = {} as Engagement;
  for (const k of ENG_KEYS) {
    const v = o[k];
    if (fin(v)) out[k] = v;
    else if (strict) return null;
    else out[k] = 0;
  }
  return out;
}

/** Validate one episode row; null when anything is off (the row is dropped, never partially shown). */
export function normEpisode(e: unknown): Episode | null {
  try {
    if (!isObj(e)) return null;
    const g = (k: string): unknown => e[k];
    need(str(g('run_id'), RE_RUN) && str(g('record_id'), RE_HEX) && str(g('goal'), RE_ENUM) && str(g('model'), RE_SLUG)
      && str(g('channel'), RE_ENUM) && str(g('trigger_code'), RE_CODE), 0);
    for (const k of ['rep', 'generation', 'n_agents', 'n_writers', 'classifier_errors']) need(int(g(k)) && (g(k) as number) >= 0, 0);
    // budget may be null (a run.json without a harness budget), matching the exporter and instigation.compute
    const budget = g('budget');
    need(budget === null || budget === undefined || (int(budget) && budget >= 0), 0);
    for (const k of ['write_fraction', 'S_mean', 'I', 'instigation_score']) need(fin(g(k)), 0);
    const ttfw = numOrNull(g('run_ttfw_s')), fws = numOrNull(g('run_first_write_step')), pile = numOrNull(g('pile_in_onset_t_s'));
    need(ttfw !== undefined && fws !== undefined && pile !== undefined, 0);
    const pot = g('posts_over_time');
    need(Array.isArray(pot), 0);
    const posts = (pot as unknown[]).map((p) => need(Array.isArray(p) && p.length === 3 && p.every(fin), p as [number, number, number]));
    const eng = need(true, normEngagement(g('engagement'), true));
    need(eng !== null, 0);
    const codes = g('agent_codes');
    need(Array.isArray(codes) && (codes as unknown[]).every((c) => c === null || str(c, RE_CODE)), 0);
    const pr = g('panel');
    let panel: Episode['panel'] = null;
    if (pr !== null && pr !== undefined) {
      need(isObj(pr) && Array.isArray(pr['slugs']) && (pr['slugs'] as unknown[]).every((s) => str(s, RE_SLUG)), 0);
      const am = numOrNull((pr as Record<string, unknown>)['agreement_mean']);
      need(am !== undefined, 0);
      panel = { slugs: (pr as Record<string, unknown>)['slugs'] as string[], agreement_mean: am as number | null };
    }
    // elite_in is informational: unexpected entries are dropped, the row is kept
    const eliteRaw = g('elite_in');
    const elite = (Array.isArray(eliteRaw) ? eliteRaw : []).filter((k): k is string => str(k, RE_ELITE));
    return {
      run_id: g('run_id') as string, record_id: g('record_id') as string, goal: g('goal') as string, model: g('model') as string,
      channel: g('channel') as string, rep: g('rep') as number, generation: g('generation') as number, n_agents: g('n_agents') as number,
      budget: (budget ?? null) as number | null, n_writers: g('n_writers') as number, write_fraction: g('write_fraction') as number,
      S_mean: g('S_mean') as number, I: g('I') as number, instigation_score: g('instigation_score') as number,
      run_ttfw_s: ttfw as number | null, run_first_write_step: fws as number | null, pile_in_onset_t_s: pile as number | null,
      posts_over_time: posts, engagement: eng as Engagement, trigger_code: g('trigger_code') as string,
      agent_codes: codes as (string | null)[], panel, classifier_errors: g('classifier_errors') as number, elite_in: elite,
    };
  } catch {
    return null;
  }
}

function normCell(c: unknown, fallbackModel: string): LandscapeCell | null {
  if (!isObj(c) || !str(c['record_id'], RE_HEX) || !fin(c['instigation_score'])) return null;
  const model = str(c['model'], RE_SLUG) ? c['model'] : fallbackModel;
  return {
    record_id: c['record_id'], instigation_score: c['instigation_score'],
    length: int(c['length']) ? c['length'] : null,
    trigger_code: str(c['trigger_code'], RE_CODE) ? c['trigger_code'] : '',
    channel: str(c['channel'], RE_ENUM) ? c['channel'] : '',
    n_writers: int(c['n_writers']) ? c['n_writers'] : null,
    engagement: normEngagement(c['engagement'], false) ?? {},
    model,
  };
}

export function splitCellKey(k: string): [string, string, string] | null {
  const p = k.split('|');
  if (p.length !== 3 || !p.every((x) => RE_CELL_PART.test(x))) return null;
  return [p[0] as string, p[1] as string, p[2] as string];
}

function normCellMap(raw: unknown, fallbackModel: string): Record<string, LandscapeCell> {
  const out: Record<string, LandscapeCell> = {};
  if (!isObj(raw)) return out;
  for (const k of Object.keys(raw)) {
    const parts = splitCellKey(k);
    const c = normCell(raw[k], fallbackModel);
    if (!parts || !c) continue;
    c.trigger_code ||= parts[0];
    c.channel ||= parts[1];
    out[k] = c;
  }
  return out;
}

/** Landscape v1 (flat cells, one legacy model) or v2 (by_model) -> Landscape. */
export function normLandscape(raw: unknown, goal: string, dims: Dims): Landscape | null {
  if (!isObj(raw)) return null;
  const ax = isObj(raw['axes']) ? raw['axes'] : {};
  const axes = {
    trigger_code: strList(ax['trigger_code'], RE_CODE, dims.trigger_code),
    channel: strList(ax['channel'], RE_ENUM, dims.channel.map((c) => c.id)),
    length_band: strList(ax['length_band'], RE_BAND, dims.length_band),
  };
  const best = normCellMap(raw['cells'], LEGACY_MODEL);
  const byModel: Record<string, Record<string, LandscapeCell>> = {};
  if (isObj(raw['by_model'])) {
    for (const slug of Object.keys(raw['by_model'])) {
      if (!RE_SLUG.test(slug)) continue;
      const bm = raw['by_model'][slug];
      byModel[slug] = normCellMap(isObj(bm) ? bm['cells'] : null, slug);
      for (const c of Object.values(byModel[slug] as Record<string, LandscapeCell>)) c.model = slug;
    }
  } else {
    for (const [k, c] of Object.entries(best)) (byModel[c.model] ??= {})[k] = c;
  }
  const n = int(raw['n_cells_possible']) ? raw['n_cells_possible'] : axes.trigger_code.length * axes.channel.length * axes.length_band.length;
  return { goal: str(raw['goal'], RE_ENUM) ? raw['goal'] : goal, axes, best, byModel, nCellsPossible: n };
}

/** Validate the whole explorer document. Throws Error('E_EXPLORER_SCHEMA') if the top level is unusable. */
export function normalize(raw: unknown): Explorer {
  if (!isObj(raw) || raw['schema'] !== EXPLORER_SCHEMA || !Array.isArray(raw['episodes'])) throw new Bad();
  const dims = normDims(raw['dims']);
  const seeds: Record<string, Seed> = {};
  if (isObj(raw['seeds'])) for (const id of Object.keys(raw['seeds'])) {
    if (!RE_HEX.test(id)) continue;
    const s = normSeed(raw['seeds'][id]);
    if (s) seeds[id] = s;
  }
  const episodes: Episode[] = [];
  let dropped = 0;
  for (const e of raw['episodes'] as unknown[]) {
    const n = normEpisode(e);
    if (n) episodes.push(n); else dropped++;
  }
  const landscapes: Record<string, Landscape> = {};
  if (isObj(raw['landscapes'])) for (const g of Object.keys(raw['landscapes'])) {
    if (!RE_ENUM.test(g)) continue;
    const l = normLandscape(raw['landscapes'][g], g, dims);
    if (l) landscapes[g] = l;
  }
  return { generated_ts: fin(raw['generated_ts']) ? raw['generated_ts'] : 0, dims, seeds, episodes, landscapes, dropped };
}

// ------------------------------------------------------------------------------------------- dimensions
export const DIMS: Dim[] = ['goal', 'model', 'channel', 'length_band', 'trigger_code', 'agent_code', 'origin', 'generation', 'writers'];
export const PIVOT_DIMS: Dim[] = ['goal', 'model', 'channel', 'length_band', 'trigger_code', 'agent_code', 'origin', 'generation'];
/** Dimensions on which one episode can carry several values (it then counts under each of them). */
export const MULTI_DIMS: Dim[] = ['agent_code'];

export const DIM_INFO: Record<Dim, { label: string; desc: string }> = {
  goal: { label: 'Goal', desc: 'Task goal. Solvable: agents can finish the task and then have free time. Unsolvable: the task never completes, so agents never finish and every board visit happens while they are still stuck.' },
  model: { label: 'Model', desc: 'The LLM driving every agent in the episode.' },
  channel: { label: 'Channel', desc: 'Where the seed was placed: a board style (forum, IRC, guestbook, key-value store, paste site) or a code comment in a served JavaScript or HTML file.' },
  length_band: { label: 'Seed length', desc: 'Total characters of the seed posts, binned into four bands.' },
  trigger_code: { label: 'Trigger', desc: 'Why the writers wrote, as coded by a classifier panel (one consensus code per episode). NO_WRITERS: nobody wrote. UNRESOLVED: writers present, but no usable code.' },
  agent_code: { label: 'Agent trigger', desc: 'Per-agent trigger codes: an episode matches a code if any of its writer agents was coded with it, so one episode can count under several codes and rows do not add up to the total. Episodes without any coded agent fall under NO_WRITERS (nobody wrote) or UNRESOLVED.' },
  origin: { label: 'Seed origin', desc: 'How the seed was made: pilot (hand-written), scratch (generated from scratch) or mutation (a variant of an earlier elite).' },
  generation: { label: 'Generation', desc: 'Search generation that produced the seed (0 = initial population).' },
  writers: { label: 'Writers', desc: 'Whether at least one agent wrote to the board in the episode.' },
};

export function seedOf(ex: Explorer, ep: Episode): Seed | undefined { return ex.seeds[ep.record_id]; }

/** Distinct per-agent trigger codes of an episode; NO_WRITERS / UNRESOLVED when no agent carries a code. */
export function agentCodes(ep: Episode): string[] {
  const out = [...new Set(ep.agent_codes.filter((c): c is string => c !== null))];
  return out.length ? out : [ep.n_writers > 0 ? 'UNRESOLVED' : 'NO_WRITERS'];
}

/** Every value of an episode on a dimension (one value except on MULTI_DIMS). */
export function dimValues(ex: Explorer, ep: Episode, dim: Dim): string[] {
  return dim === 'agent_code' ? agentCodes(ep) : [dimValue(ex, ep, dim)];
}

/** The episode's value on a dimension, as a string key (multi-valued dimensions: values joined by ';'). */
export function dimValue(ex: Explorer, ep: Episode, dim: Dim): string {
  switch (dim) {
    case 'agent_code': return agentCodes(ep).join(';');
    case 'goal': return ep.goal;
    case 'model': return ep.model;
    case 'channel': return ep.channel;
    case 'trigger_code': return ep.trigger_code;
    case 'generation': return String(ep.generation);
    case 'writers': return ep.n_writers > 0 ? 'yes' : 'no';
    case 'length_band': return seedOf(ex, ep)?.length_band ?? 'unknown';
    case 'origin': return seedOf(ex, ep)?.origin ?? 'unknown';
  }
}

function declaredOrder(ex: Explorer, dim: Dim): string[] {
  switch (dim) {
    case 'goal': return ex.dims.goal;
    case 'model': return ex.dims.model.map((m) => m.slug);
    case 'channel': return ex.dims.channel.map((c) => c.id);
    case 'length_band': return ex.dims.length_band;
    case 'trigger_code': case 'agent_code': return ex.dims.trigger_code;
    case 'origin': return ex.dims.origin;
    case 'writers': return ['yes', 'no'];
    case 'generation': return [];
  }
}

/** Values of `dim` present in `eps`, in the declared dimension order (extras after, sorted). */
export function dimDomain(ex: Explorer, dim: Dim, eps: Episode[] = ex.episodes): string[] {
  const present = new Set(eps.flatMap((e) => dimValues(ex, e, dim)));
  const order = declaredOrder(ex, dim).filter((v) => present.has(v));
  const extra = [...present].filter((v) => !order.includes(v));
  extra.sort(dim === 'generation' ? (a, b) => Number(a) - Number(b) : undefined);
  return order.concat(extra);
}

export function modelInfo(ex: Explorer, slug: string) { return ex.dims.model.find((m) => m.slug === slug); }

export function dimLabel(ex: Explorer, dim: Dim, v: string): string {
  switch (dim) {
    case 'model': return modelInfo(ex, v)?.label ?? v;
    case 'channel': return ex.dims.channel.find((c) => c.id === v)?.label ?? v;
    case 'generation': return `gen ${v}`;
    case 'writers': return v === 'yes' ? 'writers > 0' : 'no writers';
    case 'length_band': return BAND_LABEL[v] ?? v;
    default: return v;
  }
}

export const BAND_LABEL: Record<string, string> = { 'le120': '≤120', '121-200': '121–200', '201-320': '201–320', 'gt320': '>320' };

// ------------------------------------------------------------------------------------------- filters
export function emptyFilters(): Filters {
  return { goal: [], model: [], channel: [], length_band: [], trigger_code: [], agent_code: [], origin: [], generation: [], writers: [] };
}

export function filtersActive(f: Filters, seed = ''): boolean { return !!seed || DIMS.some((d) => f[d].length > 0); }

/** Empty selection on a dimension = no restriction. `except` skips one dimension (for chip counts). */
export function applyFilters(ex: Explorer, f: Filters, seed = '', except?: Dim): Episode[] {
  const active = DIMS.filter((d) => d !== except && f[d].length > 0).map((d) => [d, new Set(f[d])] as const);
  return ex.episodes.filter((e) => (!seed || e.record_id === seed) && active.every(([d, s]) => dimValues(ex, e, d).some((v) => s.has(v))));
}

// ------------------------------------------------------------------------------------------- metrics
export interface MetricInfo { id: MetricId; label: string; kind: 'count' | 'rate' | 'score' | 'num'; desc: string }
export const METRICS: MetricInfo[] = [
  { id: 'wrate', label: 'Writer-episode rate', kind: 'rate', desc: 'Share of episodes in which at least one agent wrote to the board, with a Wilson 95% interval.' },
  { id: 'pile', label: 'Pile-in rate', kind: 'rate', desc: 'Share of episodes with two or more writers, with a Wilson 95% interval.' },
  { id: 'episodes', label: 'Episodes', kind: 'count', desc: 'Number of episodes (runs) in the cell.' },
  { id: 'mean_score', label: 'Mean instigation score', kind: 'score', desc: 'Mean instigation score (0–1): how strongly the seed instigated writing, combining writers, speed and spread.' },
  { id: 'max_score', label: 'Max instigation score', kind: 'score', desc: 'Best instigation score in the cell.' },
  { id: 'mean_writers', label: 'Mean writers', kind: 'num', desc: 'Mean number of agents that wrote, per episode.' },
  { id: 'max_writers', label: 'Max writers', kind: 'num', desc: 'Most writers in any episode of the cell.' },
  { id: 'agents', label: 'Coded writer agents', kind: 'num', desc: 'Number of writer agents carrying a trigger code (per-agent codes, not the episode consensus). With Agent trigger on an axis, only agents coded with that row or column code count; otherwise the Agent trigger filter, if set, restricts the codes counted.' },
  { id: 'mean_eng', label: 'Mean engagement', kind: 'score', desc: 'Mean engagement score: board reads, relay connections and board references, including agents that only read.' },
];
export const metricInfo = (id: MetricId): MetricInfo => METRICS.find((m) => m.id === id) as MetricInfo;

/** Wilson score interval for k successes out of n (z = 1.96 -> 95%). */
export function wilson(k: number, n: number, z = 1.96): { p: number; lo: number; hi: number } | null {
  if (!(n > 0)) return null;
  const p = k / n, z2 = z * z;
  const den = 1 + z2 / n;
  const c = (p + z2 / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / den;
  return { p, lo: Math.max(0, c - half), hi: Math.min(1, c + half) };
}

/** `codes` restricts the 'agents' metric to agents coded with one of these codes (null = any code). */
export function aggregate(eps: Episode[], metric: MetricId, codes: string[] | null = null): Agg {
  const n = eps.length;
  const none: Agg = { n, k: 0, value: null, lo: null, hi: null };
  if (metric === 'episodes') return { ...none, value: n };
  if (!n) return none;
  const sum = (f: (e: Episode) => number): number => eps.reduce((a, e) => a + f(e), 0);
  const max = (f: (e: Episode) => number): number => eps.reduce((a, e) => Math.max(a, f(e)), -Infinity);
  switch (metric) {
    case 'wrate': case 'pile': {
      const k = eps.filter((e) => e.n_writers >= (metric === 'pile' ? 2 : 1)).length;
      const w = wilson(k, n) as { p: number; lo: number; hi: number };
      return { n, k, value: w.p, lo: w.lo, hi: w.hi };
    }
    case 'mean_score': return { ...none, value: sum((e) => e.instigation_score) / n };
    case 'max_score': return { ...none, value: max((e) => e.instigation_score) };
    case 'mean_writers': return { ...none, value: sum((e) => e.n_writers) / n };
    case 'max_writers': return { ...none, value: max((e) => e.n_writers) };
    case 'mean_eng': return { ...none, value: sum((e) => e.engagement.engagement_score) / n };
    case 'agents': {
      const ok = codes && codes.length ? new Set(codes) : null;
      return { ...none, value: sum((e) => e.agent_codes.filter((c) => c !== null && (!ok || ok.has(c))).length) };
    }
  }
}

export function fmtMetric(metric: MetricId, v: number | null): string {
  if (v === null || !Number.isFinite(v)) return '–';
  const kind = metricInfo(metric).kind;
  if (kind === 'rate') return `${Math.round(v * 100)}%`;
  if (kind === 'count') return String(v);
  if (kind === 'score') return v.toFixed(3);
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

export function fmtAgg(metric: MetricId, a: Agg): string {
  const base = fmtMetric(metric, a.value);
  if (a.lo !== null && a.hi !== null) return `${base} (${a.k}/${a.n}; 95% CI ${Math.round(a.lo * 100)}–${Math.round(a.hi * 100)}%)`;
  return metric === 'episodes' ? base : `${base} (n=${a.n})`;
}

// ------------------------------------------------------------------------------------------- pivot
export interface Pivot {
  rows: string[];
  cols: string[];
  cells: Agg[][];
  rowTotals: Agg[];
  colTotals: Agg[];
  total: Agg;
  /** max interior value (for colour scaling); 0 when none */
  max: number;
}

/** `agentCodes`: the Agent trigger filter selection, used by the 'agents' metric when no axis is agent_code. */
export function pivot(ex: Explorer, eps: Episode[], rowDim: Dim, colDim: Dim, metric: MetricId, agentCodes: string[] = []): Pivot {
  const rows = dimDomain(ex, rowDim, eps);
  const cols = rowDim === colDim ? ['all'] : dimDomain(ex, colDim, eps);
  const buckets = new Map<string, Episode[]>();
  const rowB = new Map<string, Episode[]>(), colB = new Map<string, Episode[]>();
  const push = (m: Map<string, Episode[]>, k: string, e: Episode): void => { const a = m.get(k); if (a) a.push(e); else m.set(k, [e]); };
  for (const e of eps) {
    const rs = dimValues(ex, e, rowDim), cs = rowDim === colDim ? ['all'] : dimValues(ex, e, colDim);
    for (const r of rs) { push(rowB, r, e); for (const c of cs) push(buckets, r + '\u0000' + c, e); }
    for (const c of cs) push(colB, c, e);
  }
  // the 'agents' metric counts only agents carrying the code of an agent_code axis
  const sel = agentCodes.length ? agentCodes : null;
  const code = (r: string | null, c: string | null): string[] | null =>
    rowDim === 'agent_code' && r !== null ? [r] : colDim === 'agent_code' && c !== null && rowDim !== colDim ? [c] : sel;
  let max = 0;
  const cells = rows.map((r) => cols.map((c) => {
    const a = aggregate(buckets.get(r + '\u0000' + c) ?? [], metric, code(r, c));
    if (a.n > 0 && a.value !== null && a.value > max) max = a.value;
    return a;
  }));
  return {
    rows, cols, cells, max,
    rowTotals: rows.map((r) => aggregate(rowB.get(r) ?? [], metric, code(r, null))),
    colTotals: cols.map((c) => aggregate(colB.get(c) ?? [], metric, code(null, c))),
    total: aggregate(eps, metric, sel),
  };
}

/** Sequential ramp bin 0..8 for value v on [0, max] (null for no data). */
export function rampBin(v: number | null, max: number): number | null {
  if (v === null || !Number.isFinite(v)) return null;
  if (!(max > 0)) return 0;
  return Math.max(0, Math.min(8, Math.floor((v / max) * 9)));
}

// ------------------------------------------------------------------------------------------- model comparison
export interface Comparison { goal: string; rows: { model: string; agg: Agg }[] }

export function compareModels(ex: Explorer, eps: Episode[], metric: 'wrate' | 'pile'): Comparison[] {
  const models = dimDomain(ex, 'model', eps);
  return dimDomain(ex, 'goal', eps).map((goal) => {
    const g = eps.filter((e) => e.goal === goal);
    return { goal, rows: models.map((model) => ({ model, agg: aggregate(g.filter((e) => e.model === model), metric) })).filter((r) => r.agg.n > 0) };
  });
}

// ------------------------------------------------------------------------------------------- lineage
export interface LineageNode {
  id: string;
  gen: number;
  parent: string | null;
  row: number;
  /** best instigation score among the (filtered) episodes of this seed, null if none */
  best: number | null;
  maxWriters: number;
  n: number;
  /** shown only as an ancestor of a matching seed */
  context: boolean;
}
export interface Lineage { nodes: LineageNode[]; gens: number[]; rows: number }

export function lineage(ex: Explorer, eps: Episode[], all: boolean): Lineage {
  const stats = new Map<string, { best: number; w: number; n: number }>();
  for (const e of eps) {
    const s = stats.get(e.record_id);
    if (s) { s.best = Math.max(s.best, e.instigation_score); s.w = Math.max(s.w, e.n_writers); s.n++; }
    else stats.set(e.record_id, { best: e.instigation_score, w: e.n_writers, n: 1 });
  }
  const include = new Set<string>();
  for (const id of Object.keys(ex.seeds)) if (all || stats.has(id)) include.add(id);
  for (const id of [...include]) {
    let p = ex.seeds[id]?.parent_id ?? null;
    const seen = new Set<string>([id]);
    while (p && ex.seeds[p] && !seen.has(p)) { include.add(p); seen.add(p); p = ex.seeds[p]?.parent_id ?? null; }
  }
  const kids = new Map<string, string[]>();
  const roots: string[] = [];
  for (const id of include) {
    const p = ex.seeds[id]?.parent_id ?? null;
    if (p && include.has(p) && p !== id) (kids.get(p) ?? kids.set(p, []).get(p) as string[]).push(id);
    else roots.push(id);
  }
  const key = (id: string): [number, number, string] => [ex.seeds[id]?.generation ?? 0, -(stats.get(id)?.best ?? -1), id];
  const cmp = (a: string, b: string): number => {
    const ka = key(a), kb = key(b);
    return ka[0] - kb[0] || ka[1] - kb[1] || (ka[2] < kb[2] ? -1 : ka[2] > kb[2] ? 1 : 0);
  };
  const nodes: LineageNode[] = [];
  const visited = new Set<string>();
  const visit = (id: string): void => {
    if (visited.has(id)) return;
    visited.add(id);
    const s = ex.seeds[id] as Seed, st = stats.get(id);
    nodes.push({ id, gen: s.generation, parent: s.parent_id && include.has(s.parent_id) ? s.parent_id : null, row: nodes.length,
      best: st ? st.best : null, maxWriters: st ? st.w : 0, n: st ? st.n : 0, context: !st });
    for (const k of (kids.get(id) ?? []).sort(cmp)) visit(k);
  };
  roots.sort(cmp).forEach(visit);
  for (const id of [...include].sort(cmp)) visit(id); // cycles (should not happen) still get a row
  const gens = [...new Set(nodes.map((n) => n.gen))].sort((a, b) => a - b);
  return { nodes, gens, rows: nodes.length };
}

/** Ancestors (oldest first) and direct children of a seed. */
export function seedFamily(ex: Explorer, id: string): { ancestors: string[]; children: string[] } {
  const ancestors: string[] = [];
  const seen = new Set<string>([id]);
  let p = ex.seeds[id]?.parent_id ?? null;
  while (p && !seen.has(p)) { ancestors.unshift(p); seen.add(p); p = ex.seeds[p]?.parent_id ?? null; }
  const children = Object.keys(ex.seeds).filter((k) => ex.seeds[k]?.parent_id === id).sort();
  return { ancestors, children };
}

// ------------------------------------------------------------------------------------------- landscape
export interface LGrid {
  goal: string;
  /** model slug, or '' for the best-across-models grid */
  model: string;
  rows: string[];
  cols: { channel: string; band: string }[];
  /** visible elites (inside the rendered rows x columns) */
  cells: Record<string, LandscapeCell>;
  /** number of visible elites */
  filled: number;
  /** rendered rows x columns */
  nCellsPossible: number;
}

/** Global max elite score over every landscape (stable colour scale across filters). */
export function landscapeMax(ex: Explorer): number {
  let m = 0;
  for (const l of Object.values(ex.landscapes)) for (const g of [l.best, ...Object.values(l.byModel)]) for (const c of Object.values(g)) m = Math.max(m, c.instigation_score);
  return m;
}

export function landscapeGrids(ex: Explorer, f: Filters, mode: 'models' | 'best'): LGrid[] {
  const goals = Object.keys(ex.landscapes).sort((a, b) => ex.dims.goal.indexOf(a) - ex.dims.goal.indexOf(b))
    .filter((g) => !f.goal.length || f.goal.includes(g));
  const order = ex.dims.model.map((m) => m.slug);
  const out: LGrid[] = [];
  for (const goal of goals) {
    const ls = ex.landscapes[goal] as Landscape;
    const keep = (vals: string[], sel: string[]): string[] => (sel.length ? vals.filter((v) => sel.includes(v)) : vals);
    // only channels this goal's search actually ran (episodes of the goal or elites in its archive)
    const ran = new Set(ex.episodes.filter((e) => e.goal === goal).map((e) => e.channel));
    for (const g of [ls.best, ...Object.values(ls.byModel)]) for (const c of Object.values(g)) ran.add(c.channel);
    const channels = keep(ls.axes.channel.filter((c) => ran.has(c)), f.channel);
    const bands = keep(ls.axes.length_band, f.length_band);
    const rows = keep(ls.axes.trigger_code, f.trigger_code);
    const cols = channels.flatMap((channel) => bands.map((band) => ({ channel, band })));
    const mk = (model: string, cells: Record<string, LandscapeCell>): LGrid => {
      const vis: Record<string, LandscapeCell> = {};
      for (const [k, c] of Object.entries(cells)) {
        const p = splitCellKey(k);
        if (p && rows.includes(p[0]) && channels.includes(p[1]) && bands.includes(p[2])) vis[k] = c;
      }
      return { goal, model, rows, cols, cells: vis, filled: Object.keys(vis).length, nCellsPossible: rows.length * cols.length };
    };
    const slugs = Object.keys(ls.byModel).sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
      .filter((s) => !f.model.length || f.model.includes(s));
    if (mode === 'best') {
      out.push(mk('', f.model.length ? bestAmong(ls, slugs) : ls.best));
    } else {
      for (const s of slugs) out.push(mk(s, ls.byModel[s] as Record<string, LandscapeCell>));
    }
  }
  return out;
}

/** Best elite per cell among the given models (in model order: an exact score tie goes to the earlier model,
 *  mirroring Archive.best_by_cell). Used when a model filter is active in 'best' mode. */
export function bestAmong(ls: Landscape, slugs: string[]): Record<string, LandscapeCell> {
  const out: Record<string, LandscapeCell> = {};
  for (const s of slugs) for (const [k, c] of Object.entries(ls.byModel[s] ?? {})) {
    const cur = out[k];
    if (!cur || c.instigation_score > cur.instigation_score) out[k] = { ...c, model: s };
  }
  return out;
}

/** The episode behind a landscape elite. Prefers the run the exporter marked via `elite_in` (exact cell key, same
 *  goal/model/seed); otherwise falls back to same seed, model and goal (channel preferred), closest score first. */
export function eliteEpisode(ex: Explorer, goal: string, c: LandscapeCell, key?: string): Episode | undefined {
  const cand = ex.episodes.filter((e) => e.record_id === c.record_id && e.model === c.model && e.goal === goal);
  if (key) {
    const marked = cand.find((e) => e.elite_in.includes(key));
    if (marked) return marked;
  }
  cand.sort((a, b) => Number(b.channel === c.channel) - Number(a.channel === c.channel)
    || Math.abs(a.instigation_score - c.instigation_score) - Math.abs(b.instigation_score - c.instigation_score));
  return cand[0];
}

// ------------------------------------------------------------------------------------------- episodes table
export interface Column { id: string; label: string; title: string; num: boolean; get: (ex: Explorer, e: Episode) => string | number | null }

const r3 = (v: number | null): number | null => (v === null ? null : Math.round(v * 1000) / 1000);
export const COLUMNS: Column[] = [
  { id: 'run_id', label: 'Run', title: 'Run id', num: false, get: (_x, e) => e.run_id },
  { id: 'goal', label: 'Goal', title: 'Task goal', num: false, get: (_x, e) => e.goal },
  { id: 'model', label: 'Model', title: 'Agent model', num: false, get: (x, e) => dimLabel(x, 'model', e.model) },
  { id: 'channel', label: 'Channel', title: 'Seed channel', num: false, get: (x, e) => dimLabel(x, 'channel', e.channel) },
  { id: 'record_id', label: 'Seed', title: 'Seed record id', num: false, get: (_x, e) => e.record_id },
  { id: 'length_band', label: 'Length', title: 'Seed length band', num: false, get: (x, e) => dimLabel(x, 'length_band', dimValue(x, e, 'length_band')) },
  { id: 'seed_chars', label: 'Chars', title: 'Seed characters', num: true, get: (x, e) => seedOf(x, e)?.seed_chars ?? null },
  { id: 'origin', label: 'Origin', title: 'Seed origin', num: false, get: (x, e) => dimValue(x, e, 'origin') },
  { id: 'generation', label: 'Gen', title: 'Seed generation', num: true, get: (_x, e) => e.generation },
  { id: 'rep', label: 'Ep no.', title: 'Episode counter assigned by the search: unique per run, not a replicate index', num: true, get: (_x, e) => e.rep },
  { id: 'n_agents', label: 'Agents', title: 'Agents in the episode', num: true, get: (_x, e) => e.n_agents },
  { id: 'budget', label: 'Budget', title: 'Step budget per agent', num: true, get: (_x, e) => e.budget },
  { id: 'n_writers', label: 'Writers', title: 'Agents that wrote to the board', num: true, get: (_x, e) => e.n_writers },
  { id: 'write_fraction', label: 'Write frac', title: 'Fraction of agents that wrote', num: true, get: (_x, e) => r3(e.write_fraction) },
  { id: 'S_mean', label: 'S mean', title: 'Mean stickiness component', num: true, get: (_x, e) => r3(e.S_mean) },
  { id: 'I', label: 'I', title: 'Instigation component', num: true, get: (_x, e) => r3(e.I) },
  { id: 'instigation_score', label: 'Score', title: 'Instigation score', num: true, get: (_x, e) => r3(e.instigation_score) },
  { id: 'run_ttfw_s', label: 'TTFW s', title: 'Time to first write, seconds', num: true, get: (_x, e) => (e.run_ttfw_s === null ? null : Math.round(e.run_ttfw_s * 10) / 10) },
  { id: 'run_first_write_step', label: '1st write step', title: 'Step of the first write', num: true, get: (_x, e) => e.run_first_write_step },
  { id: 'pile_in_onset_t_s', label: 'Pile-in s', title: 'Pile-in onset (second writer), seconds', num: true, get: (_x, e) => (e.pile_in_onset_t_s === null ? null : Math.round(e.pile_in_onset_t_s * 10) / 10) },
  { id: 'engagement_score', label: 'Engage', title: 'Engagement score', num: true, get: (_x, e) => r3(e.engagement.engagement_score) },
  { id: 'trigger_code', label: 'Trigger', title: 'Consensus trigger code', num: false, get: (_x, e) => e.trigger_code },
  { id: 'agreement', label: 'Agree', title: 'Classifier panel agreement (mean)', num: true, get: (_x, e) => r3(e.panel?.agreement_mean ?? null) },
  { id: 'classifier_errors', label: 'Clf err', title: 'Classifier errors', num: true, get: (_x, e) => e.classifier_errors },
  { id: 'elite', label: 'Elite', title: 'Landscape cells where this seed is the elite', num: true, get: (_x, e) => e.elite_in.length },
];
export const columnById = (id: string): Column | undefined => COLUMNS.find((c) => c.id === id);

/** Stable sort; nulls always last. */
export function sortEpisodes(ex: Explorer, eps: Episode[], colId: string, desc: boolean): Episode[] {
  const col = columnById(colId) ?? (columnById('instigation_score') as Column);
  const dir = desc ? -1 : 1;
  return eps.map((e, i) => ({ e, i, v: col.get(ex, e) })).sort((a, b) => {
    if (a.v === null || b.v === null) return a.v === b.v ? a.i - b.i : a.v === null ? 1 : -1;
    const c = typeof a.v === 'number' && typeof b.v === 'number' ? a.v - b.v : String(a.v).localeCompare(String(b.v), 'en', { numeric: true });
    return c ? c * dir : a.i - b.i;
  }).map((x) => x.e);
}

export function paginate<T>(xs: T[], page: number, size: number): { items: T[]; page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(xs.length / size));
  const p = Math.max(0, Math.min(pages - 1, page));
  return { items: xs.slice(p * size, p * size + size), page: p, pages };
}

const csvCell = (v: unknown): string => {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** CSV of every episode field (arrays as ';'-joined lists, posts_over_time as t:posts:writers). */
export function toCSV(ex: Explorer, eps: Episode[]): string {
  const head = ['run_id', 'record_id', 'goal', 'model', 'channel', 'rep', 'generation', 'n_agents', 'budget', 'n_writers', 'write_fraction',
    'S_mean', 'I', 'instigation_score', 'run_ttfw_s', 'run_first_write_step', 'pile_in_onset_t_s', 'board_reads', 'relay_connections',
    'board_refs', 'agents_engaged', 'engagement_score', 'trigger_code', 'agent_codes', 'panel_slugs', 'panel_agreement_mean',
    'classifier_errors', 'elite_in', 'posts_over_time', 'seed_chars', 'length_band', 'origin', 'bootstrap', 'parent_id', 'gen_model'];
  const lines = [head.join(',')];
  for (const e of eps) {
    const s = seedOf(ex, e);
    lines.push([e.run_id, e.record_id, e.goal, e.model, e.channel, e.rep, e.generation, e.n_agents, e.budget, e.n_writers, e.write_fraction,
      e.S_mean, e.I, e.instigation_score, e.run_ttfw_s, e.run_first_write_step, e.pile_in_onset_t_s, e.engagement.board_reads,
      e.engagement.relay_connections, e.engagement.board_refs, e.engagement.agents_engaged, e.engagement.engagement_score, e.trigger_code,
      e.agent_codes.map((c) => c ?? '').join(';'), e.panel?.slugs.join(';') ?? '', e.panel?.agreement_mean ?? '', e.classifier_errors,
      e.elite_in.join(';'), e.posts_over_time.map((p) => p.join(':')).join(';'), s?.seed_chars ?? '', s?.length_band ?? '', s?.origin ?? '',
      s ? String(s.bootstrap) : '', s?.parent_id ?? '', s?.gen_model ?? ''].map(csvCell).join(','));
  }
  return lines.join('\n') + '\n';
}
