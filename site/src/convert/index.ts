// Raw run dir -> player bundle, in the browser or node (docs/browser-swarm.md §5, module C).
//
// A zero-dependency port of the observatory ingest + derive (dash/server/obs/{parse,ingest,derive,classify}.py, the
// v_agent_metrics / v_run_metrics views) and the site exporter (site/tools/export.py RunBuilder, chapters,
// segments, series, make_card, normalize_numbers, the core hash). Input is the orchestrate `runs/<id>/` layout
// or the browser swarm zip (§3); output satisfies src/data/loader.ts assertCore / assertDetail and src/types.ts.
//
// Not ported: tools/redact.py (only the --mask-ctf rule, opts.maskCtf), the git lab_commit lookup (stubbed),
// findings / index assembly. Never throws on malformed input lines: they are skipped and counted in a caveat.
import type { CoreBundle, DetailBundle, RunCard } from '../types.js';
import { EF, EVENT_KINDS } from '../types.js';
import { buildRun, HIDDEN_BY_DEFAULT } from './build.js';
import { ctfMask, Masker } from './ctf.js';
import { derive } from './derive.js';
import { findRunId, ingest } from './ingest.js';
import type { RunFiles } from './ingest.js';
import { agentMetrics, runMetrics, type RunMetrics } from './metrics.js';
import { type Dict, type J, isDict, mmss, ms, roundN } from './py.js';

export type { RunFiles } from './ingest.js';
export interface ConvertOpts {
  maskCtf?: boolean;
  live?: boolean;     // partial run: tolerate missing end files, unfinished agents read 'running'
  runId?: string;     // [C, appended] when no run.json / manifest / start record names the run
  hidden?: boolean;   // [C, appended] RunCard.hidden (default false: a visitor's own traces are listed)
}
export interface Converted { card: RunCard; core: CoreBundle; detail: DetailBundle }

const RUN_FILE_RE = /^(run\.json|manifest\.json|seed\.json|board_events\.jsonl|board_events\.content\.chatter\.json|board\.out|cfg-a\d+\.json)$/;
const AGENT_DIR_RE = /^a\d+$/;
const PREFIX = Symbol('runIdPrefix');

/** Zip / folder entries -> RunFiles. Strips the `<run_id>/` (or `runs/<run_id>/`) prefix shared by every entry,
 *  drops directory entries and OS litter. The stripped prefix is kept as a run id fallback. */
export function runFilesFromEntries(entries: { name: string; data: Uint8Array }[]): RunFiles {
  let list = entries
    .map((e) => ({ name: e.name.replace(/\\/g, '/').replace(/^(\.\/)+/, '').replace(/^\/+/, ''), data: e.data }))
    .filter((e) => e.name && !e.name.endsWith('/') && !/(^|\/)(__MACOSX|\.DS_Store)(\/|$)/.test(e.name));
  let prefix = '';
  for (;;) {
    if (!list.length || list.some((e) => !e.name.includes('/') || RUN_FILE_RE.test(e.name))) break;
    const seg = list[0]!.name.split('/')[0]!;
    if (AGENT_DIR_RE.test(seg) || list.some((e) => e.name.split('/')[0] !== seg)) break;
    prefix = seg;
    list = list.map((e) => ({ name: e.name.slice(seg.length + 1), data: e.data }));
  }
  const m: RunFiles = new Map(list.map((e) => [e.name, e.data] as const));
  if (prefix) Object.defineProperty(m, PREFIX, { value: prefix, enumerable: false });
  return m;
}
/** The folder name runFilesFromEntries stripped, else ''. */
const strippedPrefix = (files: RunFiles): string => (files as RunFiles & { [PREFIX]?: string })[PREFIX] ?? '';

// ------------------------------------------------------------------------------------------------ finalize
/** export.py normalize_numbers: integral floats are ints (free in JS), others rounded to 6 decimals and never
 *  below 1e-4 (4 decimals there), so Python and JS serialise identically. Non-finite numbers become 0. */
function normalizeNumbers(o: J): J {
  if (typeof o === 'number') {
    if (!Number.isFinite(o)) return 0;
    if (Number.isInteger(o)) return o;
    let x = roundN(o, 6);
    if (Math.abs(x) < 1e-4) x = roundN(x, 4);
    return x;
  }
  if (Array.isArray(o)) return o.map(normalizeNumbers);
  if (isDict(o)) {
    const out: Dict = {};
    for (const [k, v] of Object.entries(o)) out[k] = normalizeNumbers(v);
    return out;
  }
  return o;
}
function maskTree(o: J, M: Masker): J {
  if (typeof o === 'string') return M.red(o);
  if (Array.isArray(o)) return o.map((v) => maskTree(v, M));
  if (isDict(o)) {
    const out: Dict = {};
    // export.py redact_tree also masks keys; every bundle key here is a fixed field name or a node id
    // (detail.posts), never content, and masking a key would corrupt the bundle shape (`t`, `events`, ...)
    for (const [k, v] of Object.entries(o)) out[k] = maskTree(v, M);
    return out;
  }
  return o;
}
async function sha256Hex(text: string): Promise<string> {
  const buf = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
/** export.py finalize(): hash = sha256(core JSON without hash)[:8]; both bundles carry it after `format`. */
async function finalize(core: Dict, detail: Dict, M: Masker | null): Promise<[Dict, Dict, string, string]> {
  let c = normalizeNumbers(M ? maskTree(core, M) : core) as Dict;
  let d = normalizeNumbers(M ? maskTree(detail, M) : detail) as Dict;
  const h = (await sha256Hex(JSON.stringify(c))).slice(0, 8);
  const { format: cf, ...cr } = c;
  const { format: df, ...dr } = d;
  c = { format: cf, hash: h, ...cr };
  d = { format: df, hash: h, ...dr };
  return [c, d, JSON.stringify(c), JSON.stringify(d)];
}

// ------------------------------------------------------------------------------------------------ card
const BOARD_KINDS = new Set(['boardStart', 'read', 'list', 'doc', 'miss', 'post', 'upvote', 'delete', 'dm', 'join', 'connect',
  'disconnect', 'proto']);
export function density64(core: CoreBundle): number[] {
  const tEnd = Math.max(1, core.run.tEndMs);
  const out = new Array<number>(64).fill(0);
  const ev = core.events;
  for (let i = 0; i < ev.t.length; i++) {
    if (HIDDEN_BY_DEFAULT.has(EVENT_KINDS[ev.k[i]!]!)) continue;
    out[Math.min(63, Math.max(0, Math.trunc((ev.t[i]! * 64) / tEnd)))]! += 1;
  }
  return out;
}
/** export.py make_card (runm = the v_run_metrics row). */
export function makeCard(core: CoreBundle, coreBytes: number, detailBytes: number, hidden: boolean, runm: RunMetrics | null): RunCard {
  const run = core.run, A = core.agents, ev = core.events;
  const K = (i: number): string => EVENT_KINDS[ev.k[i]!]!;
  const discovered = A.filter((a) => a.metrics.discovered).length;
  const solved = A.filter((a) => a.solveT !== null).length;
  let posts = 0, boardEvents = 0;
  for (let i = 0; i < ev.t.length; i++) {
    const k = K(i);
    if (k === 'post' || k === 'dm') posts++;
    if (BOARD_KINDS.has(k)) boardEvents++;
  }
  const leak = A.some((a) => a.metrics.secretLeak);
  const liveEdges = runm ? runm.live_live_edges : core.edges.filter((e) => e.liveLive).length;
  const onset = runm ? runm.swarm_onset_t : null;
  const onsetMs = onset !== null ? ms(onset) : (core.chapters.find((c) => c.k === 'onset')?.t ?? null);
  const score = 3 * liveEdges + 2 * posts + 2 * Number(leak) + discovered + (onsetMs !== null ? 2 : 0);
  const n = run.nAgents;
  const parts = [`${n} agent${n !== 1 ? 's' : ''}`];
  if (run.goal === 'unsolvable') parts.push('unsolvable goal');
  parts.push(n > 1 ? `${discovered} found the board` : discovered ? 'found the board' : 'never found the board');
  if (leak) {
    let lt: number | null = null;
    for (let i = 0; i < ev.t.length; i++) if (K(i) === 'post' && ev.f[i]! & EF.LEAK) { lt = ev.t[i]!; break; }
    parts.push(lt !== null ? `flag posted publicly at ${mmss(lt)}` : 'flag posted publicly');
  } else if (posts) parts.push(`${posts} ${posts !== 1 ? run.nouns.posts : run.nouns.post}`);
  if (onsetMs !== null) parts.push(`agents talk at ${mmss(onsetMs)}`);
  const h = core.hash;
  return {
    id: run.id, core: `runs/${run.id}.${h}.core.json`, detail: `runs/${run.id}.${h}.detail.json`, coreBytes, detailBytes,
    hash: h, iface: run.iface, pop: run.pop, nAgents: n, rep: run.rep, tag: run.tag, model: run.model,
    ...(run.goal === 'unsolvable' ? { goal: 'unsolvable' as const } : {}), durationMs: run.tEndMs, hidden,
    caveats: run.caveats.map((c) => c.code),
    stats: { discovered, solved, posts, liveEdges, leak, boardEvents, events: ev.t.length, nodes: core.nodes.length,
             swarmOnsetMs: onsetMs },
    density: density64(core), score, blurb: parts.join(' · '),
  };
}

// ------------------------------------------------------------------------------------------------ entry point
export async function convertRun(files: RunFiles, opts: ConvertOpts = {}): Promise<Converted> {
  const runId = findRunId(files, opts.runId) || strippedPrefix(files) || 'run';
  const db = ingest(files, runId, Boolean(opts.live));
  const dv = derive(db);
  const am = agentMetrics(db, dv);
  const rm = runMetrics(db, dv, am);
  const M = new Masker(opts.maskCtf ? ctfMask(db.run.ctf ?? {}) : new Map());
  const { core, detail } = buildRun(db, dv, am, M, Boolean(opts.maskCtf));
  const [c, d, cj, dj] = await finalize(core, detail, opts.maskCtf ? M : null);
  const enc = new TextEncoder();
  const coreB = c as unknown as CoreBundle, detailB = d as unknown as DetailBundle;
  const card = makeCard(coreB, enc.encode(cj).length, enc.encode(dj).length, Boolean(opts.hidden), rm);
  return { card, core: coreB, detail: detailB };
}
