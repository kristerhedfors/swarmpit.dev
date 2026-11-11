// CTF values of a run (export.py ctf_flags / ctf_keys / ctf_seals / ctf_allow, the --mask-ctf map) and the one
// tools/redact.py rule the converter ports: masking them (Masker). leakSpans marks them in post bodies.
import { type Dict, b64, fromHex, truthy } from './py.js';

/** ctf[first], then every other key matching `match` in sorted order: distinct non-empty strings. */
function ctfValues(ctf: Dict, first: string, match: (k: string) => boolean): string[] {
  const ks = [first, ...Object.keys(ctf).filter((k) => k !== first && match(k.toLowerCase())).sort()];
  return [...new Set(ks.map((k) => ctf[k]).filter((v): v is string => typeof v === 'string' && Boolean(v)))];
}
export const ctfFlags = (ctf: Dict): string[] => ctfValues(ctf, 'flag', (k) => k.includes('flag'));
export const ctfKeys = (ctf: Dict): string[] => ctfValues(ctf, 'key_hex', (k) => k.endsWith('key_hex'));

const ENC = new TextEncoder();
/** base64(reverse(flag XOR key)) and the non-reversed variant (lab/agent/setup_vm.py). */
export function ctfSeals(ctf: Dict): string[] {
  const out: string[] = [];
  for (const fl of ctfFlags(ctf)) {
    for (const kh of ctfKeys(ctf)) {
      const k = fromHex(kh);
      if (!k || !k.length) continue;
      const f = ENC.encode(fl);
      const x = f.map((c, i) => c ^ k[i % k.length]!);
      for (const b of [x.slice().reverse(), x]) {
        const e = b64(b);
        out.push(e, e.replace(/=+$/, ''));
      }
    }
  }
  return [...new Set(out)];
}
export function ctfAllow(ctf: Dict): string[] {
  const base = [ctf.flag, ctf.passphrase, ctf.key_hex, ctf.vault_id].filter((v) => truthy(v)).map(String);
  const extra = [...ctfFlags(ctf), ...ctfKeys(ctf)].filter((v) => !base.includes(v));
  return [...base, ...extra, ...ctfSeals(ctf)];
}
/** export.py main(): the --mask-ctf map. */
export function ctfMask(ctf: Dict): Map<string, string> {
  const mask = new Map<string, string>();
  const flagMask = (fl: string): string => `FLAG{${(fl.startsWith('FLAG{') ? fl.slice(5, -1) : fl).slice(0, 4)}…}`;
  if (typeof ctf.flag === 'string' && ctf.flag) mask.set(ctf.flag, flagMask(ctf.flag));
  for (const [k, lab] of [['passphrase', '⟨passphrase⟩'], ['key_hex', '⟨key⟩'], ['vault_id', '⟨vault⟩']] as const)
    if (truthy(ctf[k])) mask.set(String(ctf[k]), lab);
  for (const fl of ctfFlags(ctf).slice(1)) if (!mask.has(fl)) mask.set(fl, flagMask(fl));
  for (const kh of ctfKeys(ctf).slice(1)) if (!mask.has(kh)) mask.set(kh, '⟨seal key⟩');
  for (const s of ctfSeals(ctf)) if (!mask.has(s)) mask.set(s, '⟨seal⟩');
  return mask;
}
const MIN_MASK = 4;
/** Redactor restricted to the ctf_mask rule (longest key first, like redact.py _r_ctf_mask). */
export class Masker {
  readonly keys: string[];
  constructor(readonly mask: Map<string, string>) {
    // real ctf values are >= 10 chars (new_ctf()); a degenerate 1..3 char value in an uploaded run.json would
    // shred every enum / id string in the bundle, so it is not masked (MIN_MASK)
    this.keys = [...mask.keys()].filter((k) => k.length >= MIN_MASK).sort((a, b) => b.length - a.length);
  }
  red(s: string | null | undefined): string {
    let t = s ?? '';
    if (!t || !this.keys.length) return t;
    for (const k of this.keys) {
      if (!t.includes(k)) continue;
      t = t.split(k).join(this.mask.get(k)!);
    }
    return t;
  }
}

/** export.py leak_spans: merged [start, end) spans of every needle occurrence. */
export function leakSpans(body: string, needles: string[]): [number, number][] {
  const spans: [number, number][] = [];
  for (const n of needles) {
    if (!n) continue;
    let i = body.indexOf(n);
    while (i >= 0) { spans.push([i, i + n.length]); i = body.indexOf(n, i + n.length); }
  }
  spans.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [];
  for (const s of spans) {
    const last = merged[merged.length - 1];
    if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1]);
    else merged.push([s[0], s[1]]);
  }
  return merged;
}
