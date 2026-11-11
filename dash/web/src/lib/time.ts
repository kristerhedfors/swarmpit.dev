/** Binary search: index of last element with key(a[i]) <= x, or -1. */
export function lastLE<T>(a: ArrayLike<T>, x: number, key: (v: T) => number): number {
  let lo = 0, hi = a.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(a[mid]) <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
/** Binary search on a plain numeric typed array. */
export function lastLEnum(a: ArrayLike<number>, x: number): number {
  let lo = 0, hi = a.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
export function firstGT(a: ArrayLike<number>, x: number): number {
  const i = lastLEnum(a, x);
  return i + 1;
}
export function clamp(v: number, lo: number, hi: number) { return v < lo ? lo : v > hi ? hi : v; }

/** "Nice" tick step for a span. */
export function niceStep(span: number, target = 6): number {
  if (!(span > 0)) return 1;
  const raw = span / target;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
}
export function ticks(lo: number, hi: number, target = 6): number[] {
  const st = niceStep(hi - lo, target);
  const out: number[] = [];
  for (let v = Math.ceil(lo / st) * st; v <= hi + 1e-9; v += st) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}
