// Typed API client for dash/CONTRACT.md section 5. In mock mode (?mock=1 in the page URL) every
// call is answered by the in-browser fixture generator (src/mock), loaded lazily so it never
// ships in the main chunk.
import type {
  CellsResponse, Facets, Filters, Graph, Health, LiveMessage, RunDetail, RunsResponse, SeriesResponse,
  StateResponse, StepsResponse, Timeline,
} from './lib/types';

export const MOCK = new URLSearchParams(location.search).get('mock') === '1';

export class ApiError extends Error {
  constructor(public status: number, public code: string, detail: string) { super(detail || code); }
}

type Q = Record<string, string | number | boolean | null | undefined | string[]>;

export function toParams(q?: Q, filters?: Filters): URLSearchParams {
  const p = new URLSearchParams();
  if (filters) for (const [k, vs] of Object.entries(filters)) if (vs && vs.length) p.set(k, vs.join(','));
  if (q) for (const [k, v] of Object.entries(q)) {
    if (v == null || v === '') continue;
    p.set(k, Array.isArray(v) ? v.join(',') : typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
  }
  return p;
}

let mockMod: Promise<typeof import('./mock/server')> | null = null;
export function mock() { return (mockMod ??= import('./mock/server')); }

// ETag cache for run-scoped responses (If-None-Match -> 304 -> cached body).
const etags = new Map<string, { etag: string; body: unknown }>();

async function get<T>(path: string, params?: URLSearchParams, signal?: AbortSignal): Promise<T> {
  const qs = params && [...params.keys()].length ? '?' + params.toString() : '';
  if (MOCK) {
    const m = await mock();
    try {
      return structuredClone(m.handle(path, params ?? new URLSearchParams())) as T;
    } catch (e) {
      const me = e as { status?: number; code?: string; message?: string };
      throw new ApiError(me.status ?? 500, me.code ?? 'mock_error', me.message ?? String(e));
    }
  }
  const url = path + qs;
  const headers: Record<string, string> = { Accept: 'application/json' };
  const cached = etags.get(url);
  if (cached) headers['If-None-Match'] = cached.etag;
  const res = await fetch(url, { headers, signal });
  if (res.status === 304 && cached) return cached.body as T;
  if (!res.ok) {
    let code = 'http_' + res.status, detail = res.statusText;
    try { const j = await res.json(); code = j.error || code; detail = j.detail || detail; } catch { /* not json */ }
    throw new ApiError(res.status, code, detail);
  }
  const body = (await res.json()) as T;
  const et = res.headers.get('ETag');
  if (et) etags.set(url, { etag: et, body });
  return body;
}

export const api = {
  health: () => get<Health>('/api/health'),
  facets: (filters?: Filters) => get<Facets>('/api/facets', toParams(undefined, filters)),
  runs: (q: Q, filters?: Filters) => get<RunsResponse>('/api/runs', toParams(q, filters)),
  run: (id: string) => get<RunDetail>(`/api/runs/${encodeURIComponent(id)}`),
  timeline: (id: string, q: Q = {}) => get<Timeline>(`/api/runs/${encodeURIComponent(id)}/timeline`, toParams({ include: 'posts,phases', ...q })),
  state: (id: string, t: number, include = 'board,agents,metrics') =>
    get<StateResponse>(`/api/runs/${encodeURIComponent(id)}/state`, toParams({ t, include })),
  graph: (id: string, q: Q = {}) => get<Graph>(`/api/runs/${encodeURIComponent(id)}/graph`, toParams(q)),
  steps: (id: string, agent: number, q: Q = {}) =>
    get<StepsResponse>(`/api/runs/${encodeURIComponent(id)}/agents/${agent}/steps`, toParams(q)),
  cells: (q: Q, filters?: Filters) => get<CellsResponse>('/api/metrics/cells', toParams(q, filters)),
  series: (q: Q, filters?: Filters) => get<SeriesResponse>('/api/metrics/series', toParams(q, filters)),
  liveRecent: (q: Q = {}) => get<{ items: LiveMessage[] } | LiveMessage[]>('/api/live/recent', toParams(q)),
};

/** Normalise /api/live/recent (the contract says "same envelope"; accept bare array or {items}). */
export function recentItems(r: { items: LiveMessage[] } | LiveMessage[]): LiveMessage[] {
  return Array.isArray(r) ? r : r.items ?? [];
}

/** Read a filter set (contract §5 multi-valued params) out of route params. */
export const FILTER_KEYS = ['model', 'iface', 'pop', 'n_agents', 'tag', 'status', 'experiment', 'condition', 'run', 'include_tagged'];
export function filtersFrom(params: URLSearchParams): Filters {
  const f: Filters = {};
  for (const [k, v] of params.entries()) {
    if (FILTER_KEYS.includes(k) || k.startsWith('harness.') || k.startsWith('param.')) {
      const vals = v.split(',').filter((x) => x !== '');
      if (vals.length) f[k] = vals;
      else if (k === 'tag') f[k] = [''];
    }
  }
  return f;
}
