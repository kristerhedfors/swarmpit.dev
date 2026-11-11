// Explorer view state <-> URL hash (shareable). Pure; unit-tested. Unknown or malformed keys are ignored.
import { DIMS, METRICS, PIVOT_DIMS, columnById, dimDomain, emptyFilters } from './data.js';
import type { Dim, Explorer, MetricId, State, ViewId } from './types.js';

export const VIEWS: ViewId[] = ['pivot', 'landscape', 'episodes', 'lineage', 'models'];
const RE_VAL = /^[A-Za-z0-9._|-]{1,64}$/;
const RE_RUN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const RE_HEX = /^[0-9a-f]{8,64}$/;

export function defaultState(): State {
  return { view: 'pivot', filters: emptyFilters(), seed: '', rows: 'model', cols: 'goal', metric: 'wrate', cmp: 'wrate', lmode: 'models',
    sort: 'instigation_score', desc: true, page: 0, ep: '' };
}

export function encodeState(s: State): string {
  const d = defaultState();
  const p = new URLSearchParams();
  if (s.view !== d.view) p.set('v', s.view);
  for (const dim of DIMS) if (s.filters[dim].length) p.set(dim, s.filters[dim].join(','));
  if (s.seed) p.set('seed', s.seed);
  if (s.rows !== d.rows) p.set('rows', s.rows);
  if (s.cols !== d.cols) p.set('cols', s.cols);
  if (s.metric !== d.metric) p.set('m', s.metric);
  if (s.cmp !== d.cmp) p.set('cmp', s.cmp);
  if (s.lmode !== d.lmode) p.set('lm', s.lmode);
  if (s.sort !== d.sort) p.set('sort', s.sort);
  if (s.desc !== d.desc) p.set('desc', s.desc ? '1' : '0');
  if (s.page) p.set('page', String(s.page + 1));
  if (s.ep) p.set('ep', s.ep);
  // keep separators readable in shared links
  return p.toString().replace(/%2C/g, ',').replace(/%7C/g, '|');
}

export function decodeState(hash: string): State {
  const s = defaultState();
  const p = new URLSearchParams(hash.replace(/^#/, ''));
  const v = p.get('v');
  if (v && (VIEWS as string[]).includes(v)) s.view = v as ViewId;
  for (const dim of DIMS) {
    const raw = p.get(dim);
    if (raw) s.filters[dim] = [...new Set(raw.split(',').filter((x) => RE_VAL.test(x)))];
  }
  const seed = p.get('seed');
  if (seed && RE_HEX.test(seed)) s.seed = seed;
  const isPivotDim = (x: string | null): x is Dim => !!x && (PIVOT_DIMS as string[]).includes(x);
  const rows = p.get('rows'), cols = p.get('cols');
  if (isPivotDim(rows)) s.rows = rows;
  if (isPivotDim(cols)) s.cols = cols;
  const m = p.get('m');
  if (m && METRICS.some((x) => x.id === m)) s.metric = m as MetricId;
  const cmp = p.get('cmp');
  if (cmp === 'wrate' || cmp === 'pile') s.cmp = cmp;
  const lm = p.get('lm');
  if (lm === 'models' || lm === 'best') s.lmode = lm;
  const sort = p.get('sort');
  if (sort && columnById(sort)) s.sort = sort;
  const desc = p.get('desc');
  if (desc === '0' || desc === '1') s.desc = desc === '1';
  const page = Number(p.get('page'));
  if (Number.isInteger(page) && page >= 1 && page < 100000) s.page = page - 1;
  const ep = p.get('ep');
  if (ep && RE_RUN.test(ep)) s.ep = ep;
  return s;
}

/** Drop filter values / ids the loaded data does not know (a stale shared link must not hide everything). */
export function reconcile(s: State, ex: Explorer): State {
  const out: State = { ...s, filters: { ...s.filters } };
  for (const dim of DIMS) {
    if (!out.filters[dim].length) continue;
    const dom = new Set(dimDomain(ex, dim));
    out.filters[dim] = out.filters[dim].filter((v) => dom.has(v));
  }
  if (out.seed && !ex.seeds[out.seed] && !ex.episodes.some((e) => e.record_id === out.seed)) out.seed = '';
  if (out.ep && !ex.episodes.some((e) => e.run_id === out.ep)) out.ep = '';
  return out;
}
