const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 });

export function num(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '–';
  if (digits === 0) return nf0.format(v);
  if (digits === 1) return nf1.format(v);
  return nf2.format(v);
}
export function compact(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a >= 1e9) return nf1.format(v / 1e9) + 'B';
  if (a >= 1e6) return nf1.format(v / 1e6) + 'M';
  if (a >= 1e4) return nf1.format(v / 1e3) + 'K';
  return nf0.format(v);
}
export function pct(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '–';
  return (v * 100).toFixed(digits) + '%';
}
/** Model id without its org prefix ('mistralai/Mistral-Small-3.2-24B-Instruct-2506' -> 'Mistral-Small-3.2-24B-Instruct-2506'). */
export function shortModel(m: unknown): string {
  const s = m == null ? '' : String(m);
  return s.slice(s.lastIndexOf('/') + 1);
}
/** Format a metric value by its unit (metric_defs.unit). */
export function byUnit(v: number | boolean | null | undefined, unit: string | undefined): string {
  if (v == null) return '–';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  switch (unit) {
    case 'share': case 'bool': return pct(v, 0);
    case 'tokens': return compact(v);
    case 'EUR': return '€' + (Math.abs(v) < 0.01 && v !== 0 ? v.toPrecision(2) : num(v, 2));
    case 's': return dur(v);
    case 'steps': case 'count': return Math.abs(v) < 10 && !Number.isInteger(v) ? num(v, 2) : num(v, 1);
    default: return Number.isInteger(v) ? num(v) : num(v, 2);
  }
}
export function dur(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '–';
  const neg = s < 0; let a = Math.abs(s);
  let out: string;
  if (a < 60) out = a.toFixed(a < 10 ? 1 : 0) + ' s';
  else if (a < 3600) { const m = Math.floor(a / 60); a -= m * 60; out = `${m}m ${Math.round(a).toString().padStart(2, '0')}s`; }
  else { const hh = Math.floor(a / 3600); const m = Math.round((a - hh * 3600) / 60); out = `${hh}h ${m.toString().padStart(2, '0')}m`; }
  return (neg ? '−' : '') + out;
}
export function clockT(t: number | null | undefined): string {
  if (t == null || !Number.isFinite(t)) return '–';
  const neg = t < 0; const a = Math.abs(t);
  const m = Math.floor(a / 60); const sec = a - m * 60;
  return `${neg ? '−' : ''}${m}:${sec.toFixed(1).padStart(4, '0')}`;
}
export function dateTime(ts: number | null | undefined): string {
  if (ts == null) return '–';
  const d = new Date(ts * 1000);
  const p = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function ago(ts: number | null | undefined): string {
  if (ts == null) return '–';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return Math.max(0, Math.round(s)) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
export function trunc(s: string | null | undefined, n: number): string {
  if (!s) return '';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
export function noteText(n: unknown): string {
  if (typeof n === 'string') return n;
  if (n && typeof n === 'object') {
    const o = n as Record<string, unknown>;
    return String(o.msg ?? o.note ?? o.kind ?? JSON.stringify(o));
  }
  return String(n);
}
