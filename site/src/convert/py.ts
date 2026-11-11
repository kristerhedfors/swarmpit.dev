// Python-compatibility helpers for the converter (docs/browser-swarm.md §5).
//
// The converter is a line-by-line port of dash/server/obs/{parse,ingest,derive,classify}.py and
// site/tools/export.py. These helpers reproduce the Python semantics the port depends on: round() (half to
// even), str.strip() whitespace, code-point lengths and slicing (len / s[:n]), f-string formatting of
// None/True/float, bisect, statistics.median, the obs parse.py coercions (_f/_i/_s) and urllib.parse.
// Nothing here throws on bad input: every coercion returns null instead (CONTRACT: never throw on a line).

/** A decoded JSON value (records from steps.jsonl, board_events.jsonl, run.json, ...). */
export type J = any;
export type Dict = Record<string, J>;

export const isDict = (v: unknown): v is Dict => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Python truthiness of a decoded JSON value. */
export function truthy(v: J): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === '') return false;
  if (Array.isArray(v)) return v.length > 0;
  if (isDict(v)) return Object.keys(v).length > 0;
  return !(typeof v === 'number' && Number.isNaN(v));
}
/** Python `a or b`. */
export const or = <A, B>(a: A, b: B): A | B => (truthy(a) ? a : b);

/** round(x) for floats: half to even. */
export function pyRound(x: number): number {
  if (Math.abs(x % 1) === 0.5) return 2 * Math.round(x / 2);
  return Math.round(x);
}
/** round(x, n). Ties are decided on the exact binary value like Python except exact decimal ties (rare). */
export function roundN(x: number, n: number): number {
  if (!Number.isFinite(x)) return x;
  if (Number.isInteger(x)) return x;
  const r = Number(x.toFixed(n));
  return Object.is(r, -0) ? 0 : r;
}
/** export.py ms(): seconds -> integer ms, None -> null. */
export function ms(t: number | null | undefined): number | null {
  return t === null || t === undefined || !Number.isFinite(t) ? null : pyRound(t * 1000);
}
/** int(x or 0) over a DB value. */
export function int0(v: J): number {
  const n = toInt(v);
  return n ?? 0;
}

// ------------------------------------------------------------------------------------------------ strings
const PY_WS = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const RE_STRIP = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, 'g');
const RE_ONE_LINE = new RegExp(`[${PY_WS}]*\\n[${PY_WS}]*`, 'g');
/** str.strip() with Python's whitespace set. */
export const pyStrip = (s: string): string => s.replace(RE_STRIP, '');
/** str.strip(chars) for a literal char set. */
export function stripChars(s: string, chars: string): string {
  let a = 0, b = s.length;
  while (a < b && chars.includes(s[a]!)) a++;
  while (b > a && chars.includes(s[b - 1]!)) b--;
  return s.slice(a, b);
}

const SURR = /[\uD800-\uDFFF]/;
/** len(s) in code points (Python), not UTF-16 units. */
export function cpLen(s: string): number {
  if (!SURR.test(s)) return s.length;
  let n = 0;
  for (const _ of s) n++;
  return n;
}
/** A code-point view of s: plain UTF-16 when there are no surrogates (the common case). */
function cps(s: string): { n: number; at(i: number): string; slice(a: number, b?: number): string } {
  if (!SURR.test(s)) return { n: s.length, at: (i) => s[i]!, slice: (a, b) => s.slice(a, b) };
  const arr = Array.from(s);
  return { n: arr.length, at: (i) => arr[i]!, slice: (a, b) => arr.slice(a, b).join('') };
}

/** export.py trunc(): n chars incl. the ellipsis, preferring a whitespace cut in the last 24 chars. */
export function trunc(s0: string | null | undefined, n: number): string {
  const s = s0 ?? '';
  const v = cps(s);
  if (v.n <= n) return s;
  let cut = Math.max(0, n - 1);
  let ws = -1;
  for (let i = cut - 1; i >= 0; i--) {
    const c = v.at(i);
    if (c === ' ' || c === '\n' || c === '\t') { ws = i; break; }
  }
  if (ws >= cut - 24 && ws > 0) cut = ws;
  return v.slice(0, cut) + '…';
}

/** export.py one_line(). */
export const oneLine = (s: string | null | undefined): string => pyStrip(s ?? '').replace(RE_ONE_LINE, ' ⏎ ');

/** export.py head_tail() (out_head 700, out_tail 300). */
export function headTail(s0: string | null | undefined, h = 700, t = 300): string {
  const s = s0 ?? '';
  const v = cps(s);
  const L = v.n;
  if (L <= h + t + 24) return s;
  let a = h;
  let nl = -1;
  for (let i = Math.min(h, L) - 1; i >= 0; i--) {
    const c = v.at(i);
    if (c === '\n' || c === ' ') { nl = i; break; }
  }
  if (nl >= h - 80 && nl > 0) a = nl;
  let b = L - t;
  let f = -1;
  for (let i = Math.max(0, b); i < L; i++) {
    const c = v.at(i);
    if (c === '\n' || c === ' ') { f = i; break; }
  }
  const nl2 = f >= 0 ? f : b;
  if (nl2 <= b + 80) b = nl2 + 1;
  return v.slice(0, a) + `\n…[${b - a} chars]…\n` + v.slice(b);
}

export function mmss(tMs: number | null): string {
  if (tMs === null) return '?';
  const s = Math.max(0, tMs) / 1000;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

/** repr(float) (Python): shortest round-trip, exponent outside [1e-4, 1e16). */
export function pyFloat(x: number): string {
  if (Number.isNaN(x)) return 'nan';
  if (!Number.isFinite(x)) return x > 0 ? 'inf' : '-inf';
  const ax = Math.abs(x);
  if (ax !== 0 && (ax < 1e-4 || ax >= 1e16)) {
    const [m, e] = x.toExponential().split('e') as [string, string];
    const ex = Number(e);
    return `${m}e${ex < 0 ? '-' : '+'}${String(Math.abs(ex)).padStart(2, '0')}`;
  }
  const s = String(x);
  return Number.isInteger(x) ? `${s}.0` : s;
}
/** f"{v}" / str(v) for a JSON value (ints and floats are indistinguishable after JSON.parse: integral
 *  numbers print as ints unless `float` says the column is a DOUBLE). */
export function pyStr(v: J, float = false): string {
  if (v === null || v === undefined) return 'None';
  if (v === true) return 'True';
  if (v === false) return 'False';
  if (typeof v === 'number') return float || !Number.isInteger(v) ? pyFloat(v) : String(v);
  if (typeof v === 'string') return v;
  return pyDumps(v);
}
/** json.dumps(v, ensure_ascii=False) with Python's default separators (', ', ': '). */
export function pyDumps(v: J): string {
  if (v === null || v === undefined) return 'null';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (typeof v === 'number') return Number.isFinite(v) ? (Number.isInteger(v) ? String(v) : pyFloat(v)) : 'NaN';
  if (typeof v === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(pyDumps).join(', ')}]`;
  if (isDict(v)) return `{${Object.keys(v).map((k) => `${JSON.stringify(k)}: ${pyDumps(v[k])}`).join(', ')}}`;
  return 'null';
}

// ------------------------------------------------------------------------------------------------ coercions
const INT_RE = /^[\t\n\v\f\r ]*[+-]?\d+(?:_\d+)*[\t\n\v\f\r ]*$/;
const FLOAT_RE = /^[+-]?(?:(?:\d+(?:_\d+)*)?\.?\d+(?:_\d+)*(?:[eE][+-]?\d+)?|\d+\.|inf(?:inity)?|nan)$/i;
/** obs parse.py _f(): float or None (bool -> None). NaN / inf become null (the DB stores them as NULL). */
export function toFloat(v: J): number | null {
  if (v === null || v === undefined || typeof v === 'boolean') return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string') {
    const s = pyStrip(v);
    if (!FLOAT_RE.test(s)) return null;
    const x = Number(s.replace(/_/g, ''));
    return Number.isFinite(x) ? x : null;
  }
  return null;
}
/** obs parse.py _i(): int(v) or None (bool -> None; float truncates; numeric strings must be integers). */
export function toInt(v: J): number | null {
  if (v === null || v === undefined || typeof v === 'boolean') return null;
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : null;
  if (typeof v === 'string') return INT_RE.test(v) ? Number(v.replace(/[_\s]/g, '')) : null;
  return null;
}
/** obs parse.py _s(): str or json.dumps(v). */
export function toStr(v: J): string | null {
  if (v === null || v === undefined) return null;
  return typeof v === 'string' ? v : pyDumps(v);
}
/** A VARCHAR column fed a JSON value (DuckDB casts scalars to text). */
export function asText(v: J): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'string') return v;
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return String(v);
  return JSON.stringify(v);
}

// ------------------------------------------------------------------------------------------------ algorithms
/** bisect.bisect_left (same probe sequence, so it matches Python even on unsorted input). */
export function bisectLeft(a: ArrayLike<number>, x: number): number {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((a[mid] as number) < x) lo = mid + 1; else hi = mid;
  }
  return lo;
}
export function bisectRight(a: ArrayLike<number>, x: number): number {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (x < (a[mid] as number)) hi = mid; else lo = mid + 1;
  }
  return lo;
}
/** Index of the element of sorted `a` nearest to x (the lower neighbour on a tie, like `min(a[i-1:i+1], key=...)`),
 *  -1 for an empty list. */
export function nearest(a: ArrayLike<number>, x: number): number {
  const i = bisectLeft(a, x);
  let best = i - 1 >= 0 ? i - 1 : i;
  if (best >= a.length) return -1;
  if (best === i - 1 && i < a.length && Math.abs(x - (a[i] as number)) < Math.abs(x - (a[best] as number))) best = i;
  return best;
}
/** defaultdict(list)[k].append(v) / defaultdict(set)[k].add(v). */
export function pushTo<K, V>(m: Map<K, V[]>, k: K, v: V): void {
  const l = m.get(k);
  if (l) l.push(v); else m.set(k, [v]);
}
export function addTo<K, V>(m: Map<K, Set<V>>, k: K, v: V): void {
  const s = m.get(k);
  if (s) s.add(v); else m.set(k, new Set([v]));
}
/** statistics.median, None for an empty list. */
export function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length, m = n >> 1;
  return n % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}
/** Tuple comparison for sort keys (numbers, strings, null = smallest like a missing value sorted first). */
export function cmpTuple(a: readonly (number | string | null)[], b: readonly (number | string | null)[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? null, y = b[i] ?? null;
    if (x === y) continue;
    if (x === null) return -1;
    if (y === null) return 1;
    if (x < y) return -1;
    if (x > y) return 1;
  }
  return 0;
}
/** DuckDB ORDER BY over numbers: NULLS LAST. */
export const cmpNullsLast = (x: number | null, y: number | null): number =>
  x === y ? 0 : x === null ? 1 : y === null ? -1 : x - y;
/** Binary (byte-order) string comparison like DuckDB's default collation for BMP text. */
export const cmpStr = (x: string, y: string): number => (x < y ? -1 : x > y ? 1 : 0);

// ------------------------------------------------------------------------------------------------ urllib
const HEXB = /^[0-9A-Fa-f]{2}$/;
const UTF8 = new TextDecoder('utf-8');
/** urllib.parse.unquote (UTF-8, errors='replace'). */
export function unquote(s: string): string {
  if (!s.includes('%')) return s;
  let out = '';
  let bytes: number[] = [];
  const flush = (): void => {
    if (bytes.length) { out += UTF8.decode(new Uint8Array(bytes)); bytes = []; }
  };
  for (let i = 0; i < s.length; i++) {
    const c = s[i]!;
    if (c === '%' && HEXB.test(s.slice(i + 1, i + 3))) {
      bytes.push(parseInt(s.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      flush();
      out += c;
    }
  }
  flush();
  return out;
}
export const unquotePlus = (s: string): string => unquote(s.replace(/\+/g, ' '));

/** urllib.parse.urlsplit -> {path, query} (scheme/netloc/fragment stripped like Python 3.12). */
export function urlsplit(u0: string): { path: string; query: string } {
  // 3.12: strip leading C0 controls and spaces, drop \t \r \n anywhere
  let u = u0.replace(/^[\x00-\x20]+/, '').replace(/[\t\r\n]/g, '');
  const m = /^([A-Za-z][A-Za-z0-9+\-.]*):/.exec(u);
  if (m) u = u.slice(m[0].length);
  if (u.startsWith('//')) {
    let j = u.length;
    for (const ch of '/?#') {
      const k = u.indexOf(ch, 2);
      if (k >= 0 && k < j) j = k;
    }
    u = u.slice(j);
  }
  const h = u.indexOf('#');
  if (h >= 0) u = u.slice(0, h);
  const q = u.indexOf('?');
  return q >= 0 ? { path: u.slice(0, q), query: u.slice(q + 1) } : { path: u, query: '' };
}
/** urllib.parse.parse_qs(query) key set with non-empty values (keep_blank_values=False). */
export function parseQsKeys(query: string): Set<string> {
  const out = new Set<string>();
  for (const nv of query.split('&')) {
    if (!nv) continue;
    const i = nv.indexOf('=');
    if (i < 0) continue;
    const v = nv.slice(i + 1);
    if (!v.length) continue;
    out.add(unquotePlus(nv.slice(0, i)));
  }
  return out;
}

/** bytes.fromhex (whitespace between bytes allowed), null on error. */
export function fromHex(s: string): Uint8Array | null {
  const t = s.replace(/[\t\n\v\f\r ]/g, '');
  if (t.length % 2 || /[^0-9A-Fa-f]/.test(t)) return null;
  const out = new Uint8Array(t.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(t.slice(2 * i, 2 * i + 2), 16);
  return out;
}
export function b64(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}
