/** m:ss.s (or h:mm:ss.s). Negative values are prefixed with '-'. */
export function fmtT(ms: number, tenths = true): string {
  const neg = ms < 0;
  let v = Math.abs(ms) / 1000;
  if (!tenths) v = Math.floor(v);
  const hh = Math.floor(v / 3600);
  const mm = Math.floor((v % 3600) / 60);
  const ss = v % 60;
  const sec = tenths ? (Math.floor(ss * 10) / 10).toFixed(1).padStart(4, '0') : String(Math.floor(ss)).padStart(2, '0');
  const body = hh ? `${hh}:${String(mm).padStart(2, '0')}:${sec}` : `${mm}:${sec}`;
  return neg ? '-' + body : body;
}

/** Human duration: '850 ms', '13 s', '4 min', '5 h'. */
export function fmtDur(ms: number): string {
  const a = Math.abs(ms);
  if (a < 1000) return `${Math.round(a)} ms`;
  if (a < 60000) return `${(a / 1000).toFixed(a < 10000 ? 1 : 0)} s`;
  if (a < 3600000) return `${Math.round(a / 60000)} min`;
  if (a < 172800000) return `${Math.round(a / 3600000)} h`;
  return `${Math.round(a / 86400000)} d`;
}

export function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return '–';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + 'k';
  return Number.isInteger(n) ? n.toLocaleString('en-US') : n.toFixed(2);
}

export const pct = (v: number, d = 0): string => (v * 100).toFixed(d) + '%';

export function trunc(s: string, n: number): string {
  return s.length > n ? s.slice(0, Math.max(0, n - 1)) + '…' : s;
}

export const oneLine = (s: string): string => s.replace(/\s+/g, ' ').trim();

export const seedAgo = (tSeed: number): string => `posted ${fmtDur(-tSeed)} before run`;
