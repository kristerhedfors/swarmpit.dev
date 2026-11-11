// Port of the file half of dash/server/obs/ingest.py (Ingester._ingest_run_dir and the apply_* helpers) over an
// in-memory run directory (docs/browser-swarm.md §3 zip layout = orchestrate runs/<id>/ + browser extras).
//
// The result is the set of obs table rows for one run (runs, agents, seed_posts, steps, model_calls,
// agent_records, board_events, relay_events) in DuckDB ORDER BY order, ready for derive.ts. Malformed lines are
// skipped and counted (never thrown), like ingest.py's "parse error" / "record error" notes.
import { type Dict, type J, isDict, toFloat, toInt, truthy } from './py.js';
import {
  type BoardRow, type ModelRow, type RecRow, type RelayRow, type StepRow, DEFAULT_GOAL, isBlank, normBoard, normModel,
  normRecord, normRelay, normStep, parseBrowserRunId, parseJsonBytes, parseRunId, type RunIdInfo, runGoal, splitLines,
  vmAgentId,
} from './parse.js';

export type RunFiles = Map<string, Uint8Array>;

const IMAGE = 'python:3.12-bookworm';
const PREVIEW_TAG = 'preview';
const AGENT_FILE_RE = /^a(\d+)\/(.+)$/;
const CFG_RE = /^cfg-a(\d+)\.json$/;
const AGENT_META_RE = /^a(\d+)\/agent-meta\.json$/;
/** Upper bound on agent ids / n_agents (swarms are 1..4, orchestrate cells <= 16): an uploaded zip naming
 *  `a999999999/` or `n_agents: 1e300` must not make build() allocate a node per agent (hang / OOM). */
export const MAX_AGENTS = 64;
const agentIdOk = (i: number): boolean => Number.isInteger(i) && i >= 0 && i < MAX_AGENTS;
const clampN = (n: number | null): number | null => (n === null ? null : Math.min(Math.max(n, 0), MAX_AGENTS));

/** derive.py is_browser_lab / run_tag. */
export function isBrowserLab(runId: string | null, params: Dict | null): boolean {
  const p = isDict(params) ? params : {};
  return p.preview === true || p['env'] === 'cheerpx' || p.topology === 'board-co-resident' || String(runId ?? '').startsWith('browser-');
}
export const runTag = (runId: string | null, params: Dict | null, tag: string | null): string =>
  tag || (isBrowserLab(runId, params) ? PREVIEW_TAG : '');

export interface RunRow {
  run_id: string; iface: string | null; pop: string | null; n_agents: number | null; rep: number | null; tag: string;
  model: string | null; harness: Dict | null; ctf: Dict | null; params: Dict; started: number | null;
  finished: number | null; timed_out: number[]; status: string;
  // derived
  t_origin: number | null; t_end: number | null; code_version: string | null; board_offset: number;
}
export interface AgentRow {
  agent_id: number; model: string | null; cfg: Dict | null; summary: J;
  // derived (derive.py agent_rows)
  status?: string; start_ts?: number | null; end_ts?: number | null; solved?: boolean | null; solve_step?: number | null;
  solve_ts?: number | null; steps_used?: J; model_calls?: J; ended?: J; crashed?: J; tok_prompt?: J;
  tok_completion?: J; tok_cached?: J; tok_reasoning?: number; wall_s?: J; clock_offset_s?: number;
  display_names?: string[];
}
export interface SeedPost { post_id: number; thread: string | null; thread_title: string | null; author: string | null;
                            ago_min: number; upvotes0: number; body: string | null }
export interface Ingested {
  run: RunRow; agents: Map<number, AgentRow>; seedPosts: SeedPost[]; boardName: string | null; hasSeed: boolean;
  steps: StepRow[]; mcs: ModelRow[]; recs: RecRow[]; board: BoardRow[]; relay: RelayRow[];
  malformed: number;        // non-blank JSONL lines / JSON files that failed to parse or normalise
  partial: boolean;         // live / partial run (opts.live, no run.json, or run.json without `finished`)
  live: boolean;            // opts.live: a run still in progress
  hasRunJson: boolean;
  sinkJoined: number;       // posts whose content came from board_events.content.chatter.json
}

const parseObj = (b: Uint8Array | undefined): Dict | null | undefined => {
  if (!b) return undefined;
  const v = parseJsonBytes(b);
  return isDict(v) ? v : null;   // null = present but unparseable / not an object
};
/** A run.json epoch (orchestrate writes floats); ISO strings are accepted as an extension. */
function epoch(v: J): number | null {
  const f = toFloat(v);
  if (f !== null) return f;
  if (typeof v === 'string') {
    const t = Date.parse(v);
    return Number.isFinite(t) ? t / 1000 : null;
  }
  return null;
}

/** The run id: opts, run.json, manifest.json, a start record (`run`), summary.json (`run`), else ''. */
export function findRunId(files: RunFiles, hint?: string): string {
  if (hint) return hint;
  for (const name of ['run.json', 'manifest.json']) {
    const o = parseObj(files.get(name));
    if (o && typeof o.run_id === 'string' && o.run_id) return o.run_id;
  }
  for (const [k, b] of files) {
    const m = AGENT_FILE_RE.exec(k);
    if (!m) continue;
    if (m[2] === 'summary.json') {
      const o = parseObj(b);
      if (o && typeof o.run === 'string' && o.run) return o.run;
    } else if (m[2] === 'steps.jsonl') {
      for (const ln of splitLines(b.subarray(0, Math.min(b.length, 65536)))) {
        const r = parseJsonBytes(ln.bytes);
        if (isDict(r) && r.kind === 'start' && typeof r.run === 'string' && r.run) return r.run;
      }
    }
  }
  return '';
}

/** Malformed-input tally (ingest.py's "parse error" / "record error" notes). */
interface Tally { malformed: number }
type RunIds = { p: RunIdInfo; bp: { iface: string; n_agents: number } | null };
const ensureAgent = (agents: Map<number, AgentRow>, aid: number): AgentRow => {
  let a = agents.get(aid);
  if (!a) agents.set(aid, a = { agent_id: aid, model: null, cfg: null, summary: null });
  return a;
};
const popOf = (n: number | null): string | null => (n === null ? null : n === 1 ? 'solo' : 'multi');

export function ingest(files: RunFiles, runId: string, live: boolean): Ingested {
  const bad: Tally = { malformed: 0 };
  const ids: RunIds = { p: parseRunId(runId), bp: parseBrowserRunId(runId) };
  const run = ensureRun(runId, ids);
  const agents = new Map<number, AgentRow>();
  const seed = snapshotSeed(files, run, bad);
  applyCfgs(files, run, agents, bad);
  const rj = parseObj(files.get('run.json'));
  if (rj === null) bad.malformed++;
  if (rj) applyRunJson(run, rj, runId, ids);
  else applyAgentMetas(files, run);
  // partial: no run.json, or a run spec without `finished` (a live mirror). ingest.py: run.json present = done,
  // else errored (stale dir) or collecting (fresh); opts.live = collecting, so unfinished agents read 'running'.
  const partial = live || !rj || run.finished === null;
  run.status = live ? 'collecting' : rj ? 'done' : 'errored';
  const eachRecord = recordReader(partial, bad);
  const { board, sinkJoined } = readBoard(files, readSink(files, bad), seed.threadSlugs, eachRecord);
  const flag = typeof run.ctf?.flag === 'string' ? run.ctf.flag : null;
  const streams = readAgentDirs(files, agents, flag, eachRecord, bad);
  return { run, agents, seedPosts: seed.posts, boardName: seed.boardName, hasSeed: seed.hasSeed, ...streams, board,
           malformed: bad.malformed, partial, live, hasRunJson: Boolean(rj), sinkJoined };
}

/** ensure_run: the row the run id grammar implies. */
function ensureRun(runId: string, { p, bp }: RunIds): RunRow {
  const run: RunRow = {
    run_id: runId, iface: p.iface ?? bp?.iface ?? null, pop: p.pop, n_agents: clampN(p.n_agents ?? bp?.n_agents ?? null),
    rep: p.rep, tag: runTag(runId, null, p.tag), model: null, harness: null, ctf: null,
    params: { image: IMAGE, ...(p.goal !== null && p.goal !== DEFAULT_GOAL ? { goal: p.goal } : {}) },
    started: null, finished: null, timed_out: [], status: 'pending', t_origin: null, t_end: null, code_version: null,
    board_offset: 0,
  };
  if (bp && run.n_agents !== null) run.pop = popOf(run.n_agents);
  return run;
}

interface Seed { posts: SeedPost[]; boardName: string | null; hasSeed: boolean; threadSlugs: (string | null)[] }
/** snapshot_seed (no inferred fallback). threadSlugs: lab/board/store.py _thread_seq, first-seen order, a repeated
 *  slug keeps its first seq. */
function snapshotSeed(files: RunFiles, run: RunRow, bad: Tally): Seed {
  const out: Seed = { posts: [], boardName: null, hasSeed: false, threadSlugs: [] };
  const seed = parseObj(files.get('seed.json'));
  if (seed === null) bad.malformed++;
  if (!seed) return out;
  out.hasSeed = true;
  out.boardName = typeof seed.board_name === 'string' ? seed.board_name : null;
  let pid = 0;
  for (const t of Array.isArray(seed.threads) ? seed.threads : []) {
    if (!isDict(t)) continue;
    const slug = typeof t.slug === 'string' ? t.slug : null;
    if (slug === null || !out.threadSlugs.includes(slug)) out.threadSlugs.push(slug);
    for (const sp of Array.isArray(t.posts) ? t.posts : []) {
      if (!isDict(sp)) continue;
      pid++;
      const ago = toInt(truthy(sp.ago_min) ? sp.ago_min : 0) ?? 0;
      out.posts.push({ post_id: pid, thread: slug, thread_title: typeof t.title === 'string' ? t.title : null,
                       author: typeof sp.author === 'string' ? sp.author : null, ago_min: ago,
                       upvotes0: Math.max(1, 12 - Math.floor(ago / 40)),
                       body: typeof sp.body === 'string' ? sp.body : null });
    }
  }
  run.params.seed_hash = 'seed';
  run.params.seed_inferred = false;
  return out;
}

/** apply_cfg for every cfg-a<i>.json, in agent order: the first cfg supplies harness, ctf and model. */
function applyCfgs(files: RunFiles, run: RunRow, agents: Map<number, AgentRow>, bad: Tally): void {
  const cfgs = [...files.keys()].map((k) => [k, CFG_RE.exec(k)] as const).filter((x) => x[1])
    .map(([k, m]) => [Number(m![1]), k] as const).filter(([aid]) => agentIdOk(aid)).sort((a, b) => a[0] - b[0]);
  for (const [aid, k] of cfgs) {
    const c = parseObj(files.get(k));
    if (!c) { bad.malformed++; continue; }
    const harness = isDict(c.harness) ? c.harness : {};
    const model = typeof harness.model === 'string' ? harness.model : null;
    const a = ensureAgent(agents, aid);
    a.cfg = c;
    a.model = a.model ?? model;
    for (const key of ['board_url', 'board_port']) if (key in c) run.params[key] = c[key];
    run.harness = run.harness ?? harness;
    run.ctf = run.ctf ?? (isDict(c.ctf) ? c.ctf : {});
    run.model = run.model ?? model;
    run.n_agents = Math.max(run.n_agents ?? 0, agents.size);
  }
}

/** apply_run_json. */
function applyRunJson(run: RunRow, rj: Dict, runId: string, { p, bp }: RunIds): void {
  const extra = isDict(rj.params) ? rj.params : {};
  Object.assign(run.params, extra);
  const goal = runGoal(extra);
  if (goal === DEFAULT_GOAL) delete run.params.goal; else run.params.goal = goal;
  if (Object.keys(extra).length)
    run.params._explicit = [...new Set([...(Array.isArray(run.params._explicit) ? run.params._explicit : []), ...Object.keys(extra)])].sort();
  const n = clampN(toInt(truthy(rj.n_agents) ? rj.n_agents : null) ?? p.n_agents ?? bp?.n_agents ?? null);
  const harness = isDict(rj.harness) ? rj.harness : {};
  const timed: number[] = [];
  for (const v of Array.isArray(rj.timed_out) ? rj.timed_out : []) {
    const aid = typeof v === 'string' ? vmAgentId(v) : toInt(v);
    if (aid !== null) timed.push(aid);
  }
  const tag = 'tag' in rj ? rj.tag : p.tag;
  run.iface = (typeof rj.iface === 'string' && rj.iface) ? rj.iface : run.iface;
  run.n_agents = n;
  run.pop = popOf(n);
  run.rep = 'rep' in rj ? toInt(rj.rep) : p.rep;
  run.tag = runTag(runId, run.params, truthy(tag) ? String(tag) : '');
  run.model = (typeof harness.model === 'string' && harness.model) ? harness.model
    : (typeof rj.model === 'string' && rj.model ? rj.model : null);
  run.harness = harness;
  run.ctf = isDict(rj.ctf) ? rj.ctf : {};
  run.started = epoch(rj.started);
  run.finished = epoch(rj.finished);
  run.timed_out = timed;
}

/** No run.json: the board never exported (an `-incomplete` swarm zip, PROTOCOL.md §9.2). The agents' own metas
 *  still carry labctl's start epoch and the timeout flag, so the clock origin is the earliest agent start. */
function applyAgentMetas(files: RunFiles, run: RunRow): void {
  const metas = [...files.keys()].map((k) => [k, AGENT_META_RE.exec(k)] as const).filter((x) => x[1])
    .filter(([, m]) => agentIdOk(Number(m![1]))).map(([k, m]) => [Number(m![1]), parseObj(files.get(k))] as const);
  const starts = metas.map(([, o]) => (o ? epoch(o.started) : null)).filter((t): t is number => t !== null);
  if (starts.length) run.started = starts.reduce((a, b) => (b < a ? b : a));
  run.timed_out = metas.filter(([, o]) => o && o.timed_out === true).map(([i]) => i).sort((a, b) => a - b);
}

/** The content sink (board_events.content.chatter.json), keyed by post_id. */
function readSink(files: RunFiles, bad: Tally): Map<number, Dict> {
  const sink = new Map<number, Dict>();
  const sb = files.get('board_events.content.chatter.json');
  if (!sb) return sink;
  for (const ln of splitLines(sb)) {
    if (isBlank(ln.bytes)) continue;
    const v = parseJsonBytes(ln.bytes);
    let ok = false;
    for (const r of Array.isArray(v) ? v : [v]) {
      if (!isDict(r)) continue;
      const pid = toInt(r.post_id);
      if (pid !== null) { sink.set(pid, r); ok = true; }
    }
    if (!ok && !ln.tail) bad.malformed++;
  }
  return sink;
}

type EachRecord = (buf: Uint8Array, fn: (rec: Dict, off: number, seq: number) => void) => void;
/** ingest_jsonl_file: every JSON-object line of a stream; bad lines and normaliser errors are tallied. */
function recordReader(partial: boolean, bad: Tally): EachRecord {
  return (buf, fn) => {
    for (const ln of splitLines(buf)) {
      if (isBlank(ln.bytes)) continue;
      const rec = parseJsonBytes(ln.bytes);
      if (!isDict(rec)) {
        if (!(ln.tail && partial)) bad.malformed++;   // a live writer's unterminated last line is not malformed
        continue;
      }
      try { fn(rec, ln.off, ln.seq); } catch { bad.malformed++; }
    }
  };
}

/** board_events.jsonl. New runs keep post content only in the sink (lab/board/events.py): join it back by
 *  post_id, and map the opaque thread_seq to the seed slug (seed threads get 1..k in seed order,
 *  lab/board/store.py) or `t<seq>`. */
function readBoard(files: RunFiles, sink: Map<number, Dict>, seedThreadSlugs: (string | null)[],
                   eachRecord: EachRecord): { board: BoardRow[]; sinkJoined: number } {
  const board: BoardRow[] = [];
  let sinkJoined = 0;
  const bb = files.get('board_events.jsonl');
  if (bb) eachRecord(bb, (rec, off, seq) => {
    const row = normBoard(rec, off, seq);
    if (row.agent_id !== null && !agentIdOk(row.agent_id)) row.agent_id = null;   // MAX_AGENTS: no a1e300 lane
    if (row.kind === 'post' && !('body' in rec) && row.post_id !== null) {
      const c = sink.get(row.post_id);
      if (c) {
        if (!('author' in rec)) row.author = typeof c.author === 'string' ? c.author : null;
        row.body = typeof c.body === 'string' ? c.body : null;
        sinkJoined++;
      }
      if (row.thread === null) {
        const ts = row.thread_seq ?? (c ? toInt(c.thread_seq) : null);
        if (ts !== null) row.thread = seedThreadSlugs[ts - 1] ?? `t${ts}`;
      }
    }
    board.push(row);
  });
  return { board, sinkJoined };
}

/** a<i>/steps.jsonl (step / model / other records), a<i>/relay.jsonl and a<i>/summary.json, in agent order. */
function readAgentDirs(files: RunFiles, agents: Map<number, AgentRow>, flag: string | null, eachRecord: EachRecord,
                       bad: Tally): Pick<Ingested, 'steps' | 'mcs' | 'recs' | 'relay'> {
  const steps: StepRow[] = [], mcs: ModelRow[] = [], recs: RecRow[] = [], relay: RelayRow[] = [];
  const adirs = new Set<number>();
  for (const k of files.keys()) {
    const m = AGENT_FILE_RE.exec(k);
    if (m && agentIdOk(Number(m[1]))) adirs.add(Number(m[1]));
  }
  for (const aid of [...adirs].sort((a, b) => a - b)) {
    const agent = ensureAgent(agents, aid);
    const sbuf = files.get(`a${aid}/steps.jsonl`);
    if (sbuf) eachRecord(sbuf, (rec, off, seq) => {
      if (rec.kind === 'step') steps.push(normStep(rec, aid, off, seq, flag));
      else if (rec.kind === 'model') mcs.push(normModel(rec, aid, off, seq));
      else recs.push(normRecord(rec, aid, off, seq));
    });
    const rbuf = files.get(`a${aid}/relay.jsonl`);
    if (rbuf) eachRecord(rbuf, (rec, off, seq) => relay.push(normRelay(rec, aid, off, seq)));
    const sm = files.get(`a${aid}/summary.json`);
    if (sm) {
      const v = parseJsonBytes(sm);
      if (v === undefined) bad.malformed++;
      else agent.summary = v;
    }
  }
  return { steps, mcs, recs, relay };
}
