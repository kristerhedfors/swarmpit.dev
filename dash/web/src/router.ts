// Hash router: '#/<view>[/<arg>]?<query>'. Filters serialise exactly like API params.
export interface Route { view: string; arg: string | null; params: URLSearchParams }

export function parseHash(hash = location.hash): Route {
  const raw = hash.replace(/^#/, '') || '/';
  const qi = raw.indexOf('?');
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const params = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : '');
  const parts = path.split('/').filter(Boolean);
  const view = parts[0] || 'overview';
  const arg = parts.length > 1 ? decodeURIComponent(parts.slice(1).join('/')) : null;
  return { view, arg, params };
}

export function hrefFor(view: string, arg?: string | null, params?: URLSearchParams | Record<string, string | number | null | undefined>): string {
  let p: URLSearchParams;
  if (params instanceof URLSearchParams) p = params;
  else {
    p = new URLSearchParams();
    if (params) for (const [k, v] of Object.entries(params)) if (v != null && v !== '') p.set(k, String(v));
  }
  const q = p.toString();
  return `#/${view}${arg ? '/' + encodeURIComponent(arg) : ''}${q ? '?' + q : ''}`;
}

export function go(view: string, arg?: string | null, params?: URLSearchParams | Record<string, string | number | null | undefined>) {
  location.hash = hrefFor(view, arg, params);
}

/** Update query params without a history entry or re-render (scrubbing). */
export function replaceParams(patch: Record<string, string | number | null | undefined>) {
  const r = parseHash();
  for (const [k, v] of Object.entries(patch)) {
    if (v == null || v === '') r.params.delete(k); else r.params.set(k, String(v));
  }
  const url = location.pathname + location.search + hrefFor(r.view, r.arg, r.params);
  history.replaceState(history.state, '', url);
}

/** Navigate to new params for the same view (filters): creates a history entry. */
export function setParams(patch: Record<string, string | number | null | undefined>) {
  const r = parseHash();
  for (const [k, v] of Object.entries(patch)) {
    if (v == null || v === '') r.params.delete(k); else r.params.set(k, String(v));
  }
  location.hash = hrefFor(r.view, r.arg, r.params);
}
