// Port of dash/server/obs/derive.py derive_run (dash/CONTRACT.md section 3.3): step flags, clock offsets,
// canonical t, classification, board linkage + read-path resolution, posts/threads, interactions, agent rows,
// params / code version / tag. One function per derive.py section (the numbered `# ---` headers there). Same order
// of float operations as the Python, so t values agree bit for bit. Not ported: 1. positional joins (step call /
// tool_idx: nothing in the bundle reads them), lab_commit_at (git lookup; stubbed to the pre-git default),
// conditions, models, detections (SSE only), token splitting per step (unused by the bundle).
import * as C from './classify.js';
import {
  type Dict, type J, addTo, bisectLeft, bisectRight, cmpTuple, isDict, median, nearest, pushTo, truthy, unquote, urlsplit,
} from './py.js';
import type { BoardRow, ModelRow, RecRow, RelayRow, StepRow } from './parse.js';
import { type AgentRow, type Ingested, type RunRow, runTag } from './ingest.js';

export const DERIVE_VERSION = 3;
export const SCHEMA_VERSION = 1;
const REPLY_WINDOW_S = 600.0;

export interface PostRow {
  uid: string; post_id: number | null; kind: 'post' | 'dm'; is_seed: boolean; thread: string | null;
  author: string | null; author_node: string | null; agent_id: number | null; ts: number | null; t: number | null;
  upvotes0: number; body: string | null; body_len: number; step: number | null; phase: J; flags: string[];
  mentions: string[]; line_off: number | null; _order: number; _cache_path?: string;
}
export interface ThreadRow { thread: string; title: string | null; is_seed: boolean; created_ts: number | null;
                             created_t: number | null; created_by: string | null }
export interface Interaction { edge_uid: string; kind: string; src: string; dst: string; ts: number | null;
                               t: number | null; weight: number; live_live: boolean; evidence: Dict }
export interface Derived { posts: PostRow[]; threads: Map<string, ThreadRow>; interactions: Interaction[];
                           agentIds: number[] }

interface PerAgent { recs: RecRow[]; mcs: ModelRow[]; steps: StepRow[]; relay: RelayRow[]; board: BoardRow[] }
/** The run's rows grouped per agent, plus what every stage reads. */
interface Ctx {
  db: Ingested; run: RunRow; iface: string | null; ctf: Dict; agentIds: number[];
  agent(aid: number): PerAgent;
}
type Rel = (ts: number | null) => number | null;
type StepKey = `${number}:${number | null}`;
const stepKey = (aid: number | null, step: number | null): StepKey => `${aid ?? -1}:${step}` as StepKey;

export function derive(db: Ingested): Derived {
  const x = groupByAgent(db);
  flagSteps(x);
  const offsets = clockOffsets(x);
  const time = canonicalTimes(x, offsets);
  const steps = classifySteps(x, time.rel);
  linkBoard(x);
  const { posts, threads, names } = replayPosts(x, time, steps);
  const interactions = deriveInteractions(x, time.rel, posts, names);
  agentRows(x, steps, offsets, names);
  runParams(x);
  x.run.t_end = time.tEnd;
  for (const p of posts) delete p._cache_path;
  return { posts, threads, interactions, agentIds: x.agentIds };
}

function groupByAgent(db: Ingested): Ctx {
  const byAgent = new Map<number, PerAgent>();
  const agent = (aid: number): PerAgent => {
    let a = byAgent.get(aid);
    if (!a) byAgent.set(aid, a = { recs: [], mcs: [], steps: [], relay: [], board: [] });
    return a;
  };
  for (const r of db.recs) agent(r.agent_id).recs.push(r);
  for (const r of db.mcs) agent(r.agent_id).mcs.push(r);
  for (const r of db.steps) agent(r.agent_id).steps.push(r);
  for (const r of db.relay) agent(r.agent_id).relay.push(r);
  for (const r of db.board) if (r.agent_id !== null) agent(r.agent_id).board.push(r);
  const agentIds = [...new Set([...db.agents.keys(), ...byAgent.keys()])].sort((a, b) => a - b);
  for (const aid of agentIds) agent(aid);
  return { db, run: db.run, iface: db.run.iface, ctf: db.run.ctf ?? {}, agentIds, agent };
}

/** The agent's steps.jsonl rows of the given streams in file order (seq), each tagged with its stream. */
function inFileOrder<T extends string>(streams: [T, { seq: number }[]][]): [T, { seq: number }][] {
  const merged: [T, { seq: number }][] = streams.flatMap(([tag, rows]) => rows.map((r) => [tag, r] as [T, { seq: number }]));
  return merged.sort((a, b) => a[1].seq - b[1].seq);
}
const dataUp = (r: RelayRow): boolean => r.ev === 'data' && r.dir === 'up';
const chanSlug = (chan: string): string => chan.replace(/^#+/, '');

// ------------------------------------------------------------------------------------------------ 2. step flags
/** Board steps by relay overlap or file taint (lab/analyze.py @ fc5a3be), then relay events -> step. */
function flagSteps(x: Ctx): void {
  for (const aid of x.agentIds) {
    const ag = x.agent(aid);
    const relTs = ag.relay.filter((r) => r.ev === 'open' || dataUp(r)).map((r) => r.ts_raw)
      .filter((t): t is number => t !== null).sort((a, b) => a - b);
    const boardFiles = new Set<string>();
    for (const s of ag.steps) {
      const cmd = s.command || '';
      let via: string | null = null;
      if (!s.idle) {
        if (s.t0_raw !== null && s.t1_raw !== null) {
          const i = bisectLeft(relTs, s.t0_raw);
          if (i < relTs.length && relTs[i]! <= s.t1_raw) via = 'relay';
        }
        if (via === null) for (const f of boardFiles) if (cmd.includes(f)) { via = 'taint'; break; }
      }
      s.is_board = via !== null;
      s.board_via = via ?? (C.BOARD_RE.test(cmd) ? 'mention' : null);
      if (via && cmd) for (const t of C.taintTargets(cmd)) boardFiles.add(t);
    }
    const wins = ag.steps.filter((s) => !s.idle && s.t0_raw !== null && s.t1_raw !== null)
      .map((s) => [s.t0_raw!, s.t1_raw!, s.step] as [number, number, number | null]).sort(cmpTuple);
    const starts = wins.map((w) => w[0]);
    for (const r of ag.relay) {
      let st: number | null = null;
      if (r.ts_raw !== null) {
        const i = bisectRight(starts, r.ts_raw) - 1;
        if (i >= 0 && wins[i]![1] >= r.ts_raw) st = wins[i]![2];
      }
      r.step = st;
    }
  }
}

// ------------------------------------------------------------------------------------------------ 3. clock offsets
interface Offsets { perAgent: Map<number, number>; board: number; of(aid: number | null): number }
/** Per-agent board clock skew: median distance of board requests to the nearest data-up relay event. */
function clockOffsets(x: Ctx): Offsets {
  const perAgent = new Map<number, number>();
  for (const aid of x.agentIds) {
    const ag = x.agent(aid);
    const ups = ag.relay.filter((r) => dataUp(r) && r.ts_raw !== null).map((r) => r.ts_raw!).sort((a, b) => a - b);
    const diffs: number[] = [];
    if (ups.length) {
      for (const e of ag.board)
        if ((e.kind === 'req' || e.kind === 'irc_cmd') && e.ts_raw !== null) diffs.push(e.ts_raw - ups[nearest(ups, e.ts_raw)]!);
    }
    let off = median(diffs) || 0.0;
    if (Math.abs(off) > 5) off = 0.0;
    perAgent.set(aid, off);
  }
  const board = median([...perAgent.values()].filter((o) => o)) || 0.0;
  x.run.board_offset = board;
  return { perAgent, board, of: (aid) => (aid !== null ? (perAgent.get(aid) ?? board) : board) };
}

// ------------------------------------------------------------------------------------------------ 4. canonical ts / t
interface Times { tOrigin: number; rel: Rel; tEnd: number | null; boardStartRaw: number | null }
function canonicalTimes(x: Ctx, off: Offsets): Times {
  const { db, run } = x;
  const tOrigin = run.started !== null ? run.started : inferOrigin(db, off);
  run.t_origin = tOrigin;
  const rel: Rel = (ts) => (ts === null ? null : ts - tOrigin);
  const allT: number[] = [];
  for (const aid of x.agentIds) {
    const ag = x.agent(aid);
    let prevTs: number | null = null;
    for (const [typ, r0] of inFileOrder([['r', ag.recs], ['m', ag.mcs], ['s', ag.steps]])) {
      let ts: number | null;
      if (typ === 'r') {
        const r = r0 as RecRow;
        // an api_retry record carries no time of its own: it happened at the previous record
        ts = r.kind !== 'api_retry' ? r.ts_raw : prevTs;
        if (r.kind !== 'api_retry' && ts === null) ts = prevTs;
        r.ts = ts; r.t = rel(ts);
        if (ts !== null) prevTs = ts;
      } else if (typ === 'm') {
        const r = r0 as ModelRow;
        ts = r.ts_raw;
        r.latency_s = ts !== null && prevTs !== null ? ts - prevTs : null;
        r.ts = ts; r.t = rel(ts);
        if (ts !== null) prevTs = ts;
      } else {
        const r = r0 as StepRow;
        ts = r.t0_raw;
        if (r.t1_raw !== null) prevTs = r.t1_raw;
      }
      if (ts !== null) allT.push(ts - tOrigin);
    }
    for (const r of ag.relay) if (r.ts_raw !== null) allT.push(r.ts_raw - tOrigin);
  }
  for (const e of db.board) {
    e.ts = e.ts_raw === null ? null : e.ts_raw - off.of(e.agent_id);
    e.t = rel(e.ts);
    if (e.ts !== null) allT.push(e.ts - tOrigin);
  }
  for (const s of db.steps) if (s.t1_raw !== null) allT.push(s.t1_raw - tOrigin);
  return { tOrigin, rel, tEnd: allT.length ? allT.reduce((m, t) => (t > m ? t : m), -Infinity) : null,
           boardStartRaw: db.board.find((e) => e.kind === 'board_start')?.ts_raw ?? null };
}
/** derive.py: min over start/end records and step t0 (pilot markers and cfg mtimes do not exist in a zip). A live
 *  or run.json-less trace (a browser spec run.json has no `started`) has board events before the first agent
 *  record, so there the board and model timestamps count too and t stays >= 0. */
function inferOrigin(db: Ingested, off: Offsets): number {
  let cands = [...db.recs.map((r) => r.ts_raw), ...db.steps.map((s) => s.t0_raw)].filter((t): t is number => truthy(t));
  if (db.live || !db.hasRunJson) {
    const boardTs = db.board.map((e) => (e.ts_raw === null ? null : e.ts_raw - off.of(e.agent_id)));
    cands = cands.concat([...boardTs, ...db.mcs.map((m) => m.ts_raw)].filter((t): t is number => truthy(t)));
  }
  return cands.length ? cands.reduce((m, t) => (t < m ? t : m)) : 0;
}

// ------------------------------------------------------------------------------------------------ 5/6. classification
interface StepIndex { byKey: Map<StepKey, StepRow>; solveTs: Map<number, number | null> }
function classifySteps(x: Ctx, rel: Rel): StepIndex {
  const byKey = new Map<StepKey, StepRow>();
  const solveTs = new Map<number, number | null>();
  const flag = typeof x.ctf.flag === 'string' && x.ctf.flag ? x.ctf.flag : null;
  for (const aid of x.agentIds) {
    for (const s of x.agent(aid).steps) {
      [s.cat, s.persistence] = C.categorize(Boolean(s.idle), s.is_board, s.command);
      s.submissions = s.submissions.map((sub) => ({ ...sub, correct: Boolean(flag) && String(sub.value ?? '').includes(flag!) }));
      s.submit_correct = s.submissions.length ? s.submissions.some((sub) => sub.correct) : null;
      s.t = rel(s.t0_raw); s.t_end = rel(s.t1_raw);
      byKey.set(stepKey(aid, s.step), s);
      if (s.is_solve && !solveTs.has(aid)) solveTs.set(aid, s.t1_raw);
    }
  }
  return { byKey, solveTs };
}

// ------------------------------------------------------------------------------------------------ 7. board linkage
/** Board event -> (step, attribution): requests via their relay event, writes via the request they accompany. */
function linkBoard(x: Ctx): void {
  for (const aid of x.agentIds) {
    const ag = x.agent(aid);
    const stepAt = stepLocator(ag.steps);
    const opens = ag.relay.filter((r) => r.ev === 'open' && r.ts_raw !== null)
      .sort((a, b) => cmpTuple([a.conn ?? 0, a.seq], [b.conn ?? 0, b.seq]));
    const ups = ag.relay.filter((r) => dataUp(r) && r.ts_raw !== null).sort((a, b) => a.seq - b.seq);
    const relayOf = matchRelay(ag.board.filter((e) => e.kind === 'req'), opens);
    for (const [k, v] of matchRelay(ag.board.filter((e) => e.kind === 'irc_cmd'), ups)) relayOf.set(k, v);
    for (const e of ag.board) {
      e.step = null; e.attribution = 'none';
      if (e.kind === 'req' || e.kind === 'irc_cmd') {
        const r = relayOf.get(e.line_off);
        if (r !== undefined && r.step !== null) { e.step = r.step; e.attribution = 'step'; }
        else [e.step, e.attribution] = stepAt(e.ts);
      }
    }
    const bl = ag.board;
    // derive.py's next(x for x in bl[i+1:] ...) / reversed(bl[:i]) scans, as O(n) index tables (an IRC flood
    // of k posts made the per-post slice copies O(k^2): seconds per live re-conversion)
    const nextReq = new Array<number>(bl.length + 1).fill(-1), prevIrc = new Array<number>(bl.length).fill(-1);
    for (let j = bl.length - 1; j >= 0; j--)
      nextReq[j] = bl[j]!.kind === 'req' || bl[j]!.kind === 'irc_cmd' ? j : nextReq[j + 1]!;
    for (let j = 0, last = -1; j < bl.length; j++) { prevIrc[j] = last; if (bl[j]!.kind === 'irc_cmd') last = j; }
    const at = (j: number): BoardRow | null => (j >= 0 ? bl[j]! : null);
    bl.forEach((e, i) => {
      let via: BoardRow | null;
      if (e.kind === 'post' || e.kind === 'upvote' || e.kind === 'delete') {
        via = e.kind === 'post' && x.iface === 'irc' ? at(prevIrc[i]!) : at(nextReq[i + 1]!);
        if (via) e._req = via;
      } else if (e.kind === 'dm' || e.kind === 'irc_join') via = at(prevIrc[i]!);
      else if (e.kind === 'connect' || e.kind === 'disconnect') via = null;
      else return;
      if (via) { e.step = via.step; e.attribution = via.attribution; }
      else [e.step, e.attribution] = stepAt(e.ts);
    });
  }
}
/** ts -> (step whose window contains it | the last step started before it, 'step' | 'background' | 'none'). */
function stepLocator(steps: StepRow[]): (ts: number | null) => [number | null, string] {
  const w = steps.filter((s) => s.t0_raw !== null)
    .map((s) => [s.t0_raw!, s.t1_raw ?? s.t0_raw!, s.step] as [number, number, number | null]).sort(cmpTuple);
  const starts = w.map((v) => v[0]);
  return (ts) => {
    if (ts === null) return [null, 'none'];
    const i = bisectRight(starts, ts) - 1;
    if (i < 0) return [null, 'none'];
    return [w[i]![2], w[i]![1] >= ts ? 'step' : 'background'];
  };
}
/** Board events -> relay events: positional when the counts agree, else nearest in time. */
function matchRelay(evs: BoardRow[], rels: RelayRow[]): Map<number, RelayRow> {
  const out = new Map<number, RelayRow>();
  if (!evs.length || !rels.length) return out;
  if (evs.length === rels.length) {
    evs.forEach((e, i) => out.set(e.line_off, rels[i]!));
    return out;
  }
  const rts = rels.map((r) => r.ts_raw!);
  for (const e of evs) if (e.ts !== null) out.set(e.line_off, rels[nearest(rts, e.ts)]!);
  return out;
}

// ------------------------------------------------------------------------------------------------ 8. posts and threads
interface Replay { posts: PostRow[]; threads: Map<string, ThreadRow>; names: Map<number, Set<string>> }
/** Seed + live posts and threads, agent display names, then every board event's (target_kind, target). */
function replayPosts(x: Ctx, time: Times, steps: StepIndex): Replay {
  const posts: PostRow[] = [];
  const threads = new Map<string, ThreadRow>();
  const cachePaths = seedPosts(x, time, posts, threads);
  livePosts(x, time.rel, steps, posts, threads);
  const names = displayNames(x.db.board);
  resolveTargets(x, posts, threads, cachePaths);
  return { posts, threads, names };
}

/** Seed posts and threads; returns the cache paths of the seed artifacts (path -> uid). */
function seedPosts(x: Ctx, time: Times, posts: PostRow[], threads: Map<string, ThreadRow>): Map<string, string> {
  const { rel } = time;
  const bo = x.run.board_offset;
  const seedBase = time.boardStartRaw !== null ? time.boardStartRaw : time.tOrigin + bo;
  for (const sp of x.db.seedPosts) {
    const ts = seedBase - (sp.ago_min || 0) * 60 - bo;
    const by = `seed:${sp.author ?? 'None'}`;
    posts.push({ uid: `s${sp.post_id}`, post_id: sp.post_id, kind: 'post', is_seed: true, thread: sp.thread,
                 author: sp.author, author_node: by, agent_id: null, ts, t: rel(ts),
                 upvotes0: sp.upvotes0, body: sp.body, body_len: [...(sp.body ?? '')].length, step: null, phase: null,
                 flags: C.postFlags(sp.body, null), mentions: [], line_off: null, _order: -1 });
    const key = sp.thread ?? 'None';
    const th = threads.get(key);
    if (!th) threads.set(key, { thread: key, title: sp.thread_title, is_seed: true, created_ts: ts, created_t: rel(ts),
                                created_by: by });
    else if (th.created_ts !== null && ts < th.created_ts) Object.assign(th, { created_ts: ts, created_t: rel(ts), created_by: by });
  }
  const cachePaths = new Map<string, string>();
  const perThread = new Map<string | null, number>();
  for (const p of posts) {
    const n = (perThread.get(p.thread) ?? 0) + 1;
    perThread.set(p.thread, n);
    p._cache_path = `${p.thread ?? 'None'}/${String(n).padStart(4, '0')}-${C.slugify(p.author ?? '')}.txt`;
    cachePaths.set(p._cache_path, p.uid);
  }
  return cachePaths;
}

/** len(e.body or ""); a structural-only event (sink-format post with no sink / not mirrored live, or a new-format
 *  DM, whose text never reaches any file) has no body but carries body_len_chars (lab/board/store.py add_post). */
function bodyLen(e: BoardRow): number {
  if (e.body !== null) return [...e.body].length;
  const n = isDict(e.data) ? e.data.body_len_chars : null;
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : 0;
}
/** The artifact path a cache upload was written to (its PUT path), else <thread>/<author>. */
function cachePath(e: BoardRow): string | null {
  const req = e._req;
  if (req && req.path) {
    const pp = unquote(urlsplit(req.path).path);
    if (pp.startsWith(C.CACHE_PREFIX + '/')) {
      const path = pp.slice(C.CACHE_PREFIX.length + 1);
      if (path) return path;
    }
  }
  return e.thread !== '(root)' ? `${e.thread ?? 'None'}/${e.author ?? 'None'}` : e.author;
}

function livePosts(x: Ctx, rel: Rel, steps: StepIndex, posts: PostRow[], threads: Map<string, ThreadRow>): void {
  const row = (e: BoardRow, kind: 'post' | 'dm', uid: string, thread: string | null, author: string | null,
               body: string | null): PostRow => ({
    uid, post_id: kind === 'post' ? e.post_id : null, kind, is_seed: false, thread, author,
    author_node: e.agent_id !== null ? `a${e.agent_id}` : null, agent_id: e.agent_id, ts: e.ts, t: rel(e.ts), upvotes0: 0,
    body, body_len: bodyLen(e), step: e.step,
    phase: (e.step !== null ? steps.byKey.get(stepKey(e.agent_id, e.step))?.phase : undefined) ?? null,
    flags: C.postFlags(e.body || '', x.ctf), mentions: [], line_off: e.line_off, _order: e.seq,
  });
  const newThread = (slug: string, e: BoardRow): void => {
    if (!threads.has(slug))
      threads.set(slug, { thread: slug, title: slug, is_seed: false, created_ts: e.ts, created_t: rel(e.ts),
                          created_by: e.agent_id !== null ? `a${e.agent_id}` : null });
  };
  for (const e of x.db.board) {
    if (e.kind === 'post') {
      const iface = e.iface || x.iface;
      const p = row(e, 'post', e.post_id !== null ? `p${e.post_id}` : `d${e.line_off}`, e.thread, e.author,
                    C.displayBody(e.body || '', iface));
      if (iface === 'cache') {
        const cp = cachePath(e);
        if (cp) p._cache_path = cp;
      }
      posts.push(p);
      if (e.thread) newThread(e.thread, e);
    } else if (e.kind === 'dm') posts.push(row(e, 'dm', `d${e.line_off}`, e.to_nick, e.nick, e.body));
    else if (e.kind === 'irc_join' && e.chan) newThread(chanSlug(e.chan), e);
  }
}

/** Display names per agent (insertion order of first qualifying event; sorted where they are used). */
function displayNames(board: BoardRow[]): Map<number, Set<string>> {
  const names = new Map<number, Set<string>>();
  for (const e of board) {
    if (e.agent_id === null) continue;
    if (e.kind === 'post' && e.author) addTo(names, e.agent_id, e.author);
    if (e.nick) addTo(names, e.agent_id, e.nick);
  }
  return names;
}

/** Resolve targets chronologically (the threads / cache paths known at that time). */
function resolveTargets(x: Ctx, posts: PostRow[], threads: Map<string, ThreadRow>, cachePaths: Map<string, string>): void {
  const known = new Set([...threads.values()].filter((t) => t.is_seed).map((t) => t.thread));
  const live = posts.filter((p) => !p.is_seed && p.kind === 'post').sort((a, b) => a._order - b._order);
  let li = 0;
  for (const e of x.db.board) {
    for (; li < live.length && live[li]!._order < e.seq; li++) {
      const lp = live[li]!;
      if (lp.thread !== null) known.add(lp.thread);
      if (lp._cache_path) cachePaths.set(lp._cache_path, lp.uid);
    }
    let [tk, tg] = C.resolveTarget(e.iface || x.iface, e.kind, e.method, e.path, e.status, e.line, e.cmd, known, cachePaths);
    if (['post', 'upvote', 'delete', 'dm'].includes(e.kind)) {
      tk = 'write';
      tg = e.kind === 'post' ? e.thread : tg;
    }
    if (e.kind === 'irc_join') { tk = 'thread'; tg = chanSlug(e.chan || '') || null; }
    e.target_kind = tk; e.target = tg;
    if (e.attribution === null) e.attribution = e.agent_id === null ? 'background' : 'none';
  }
}

// ------------------------------------------------------------------------------------------------ 9. interactions
type AddEdge = (kind: string, key: string | number, src: string | null, dst: string | null, ts: number | null, w: number,
                evidence: Dict) => void;
/** src+thread -> times src saw that thread (reads, channel scrollback / membership). */
type Exposures = Map<string, number[]>;
const expKey = (src: string, th: string | null): string => `${src}\u0000${th}`;

function deriveInteractions(x: Ctx, rel: Rel, posts: PostRow[], names: Map<number, Set<string>>): Interaction[] {
  const edges = new Map<string, Interaction>();
  const addEdge: AddEdge = (kind, key, src, dst, ts, w, evidence) => {
    if (!src || !dst || src === dst) return;
    const uid = `${kind}:${key}:${dst}`;
    if (edges.has(uid)) return;
    edges.set(uid, { edge_uid: uid, kind, src, dst, ts, t: rel(ts), weight: w,
                     live_live: src.startsWith('a') && dst.startsWith('a') && src !== dst, evidence });
  };
  const exposures: Exposures = new Map();
  readExposures(x, posts, addEdge, exposures);
  if (x.iface === 'irc') channelExposures(x, posts, addEdge, exposures);
  for (const l of exposures.values()) l.sort((a, b) => a - b);
  postEdges(x, posts, names, addEdge, exposures);
  const postById = new Map<number, PostRow>();
  for (const p of posts) if (p.post_id !== null) postById.set(p.post_id, p);
  for (const e of x.db.board) {
    const q = e.kind === 'upvote' && e.agent_id !== null && e.post_id !== null ? postById.get(e.post_id) : undefined;
    if (q) addEdge('upvote', e.line_off, `a${e.agent_id}`, q.author_node, e.ts, 1.0, { line_off: e.line_off, post_uid: q.uid });
  }
  return [...edges.values()];
}

/** Exposure edges from thread / post / listing reads and IRC joins: the reader saw every earlier author there. */
function readExposures(x: Ctx, posts: PostRow[], addEdge: AddEdge, exposures: Exposures): void {
  const postByUid = new Map(posts.map((p) => [p.uid, p] as const));
  // per target: post times (sorted) and each author's first post index, so "authors before ts" is a bisect
  const firsts = new Map<string, { ts: number[]; first: [number, string, string][] }>();
  const authorsBefore = (key: string, pred: (p: PostRow) => boolean, ts: number): [string, string][] => {
    let f = firsts.get(key);
    if (!f) {
      const ps = posts.filter((p) => p.kind === 'post' && p.ts !== null && p.author_node && pred(p)).sort((a, b) => a.ts! - b.ts!);
      const seen = new Set<string>();
      const first: [number, string, string][] = [];
      ps.forEach((p, i) => { if (!seen.has(p.author_node!)) { seen.add(p.author_node!); first.push([i, p.author_node!, p.uid]); } });
      firsts.set(key, f = { ts: ps.map((p) => p.ts!), first });
    }
    const k = bisectLeft(f.ts, ts);
    const out: [string, string][] = [];
    for (const [i, node, uid] of f.first) { if (i >= k) break; out.push([node, uid]); }
    return out;
  };
  for (const e of x.db.board) {
    if (e.agent_id === null || e.ts === null) continue;
    const src = `a${e.agent_id}`;
    const tk = e.target_kind, tg = e.target;
    if ((e.kind === 'req' && tk === 'thread' && tg) || (e.kind === 'irc_join' && tg)) {
      const extra = e.kind === 'irc_join' ? { scrollback: true } : {};
      for (const [node, uid] of authorsBefore(`thread\u0000${tg}`, (p) => p.thread === tg, e.ts))
        addEdge('exposure', e.line_off, src, node, e.ts, 1.0,
                { line_off: e.line_off, post_uid: uid, path: e.path || e.chan, listing: false, ...extra });
      pushTo(exposures, expKey(src, tg), e.ts);
    } else if (e.kind === 'req' && tk === 'post' && tg !== null && postByUid.has(tg)) {
      const p = postByUid.get(tg)!;
      addEdge('exposure', e.line_off, src, p.author_node, e.ts, 1.0,
              { line_off: e.line_off, post_uid: p.uid, path: e.path, listing: false });
      pushTo(exposures, expKey(src, p.thread), e.ts);
    } else if (e.kind === 'req' && tk === 'listing') {
      const pred = (p: PostRow): boolean => !tg || p.thread === tg || (p._cache_path || '').startsWith(tg + '/');
      for (const [node] of authorsBefore(`listing\u0000${tg}`, pred, e.ts))
        addEdge('exposure', e.line_off, src, node, e.ts, 0.25, { line_off: e.line_off, path: e.path, listing: true });
    }
  }
}

/** IRC channel membership (join .. disconnect): a member sees every post made in the channel meanwhile. */
function channelExposures(x: Ctx, posts: PostRow[], addEdge: AddEdge, exposures: Exposures): void {
  const member = new Map<string, [number, number, number][]>();   // channel -> [agent, joined, left]
  const open = new Map<string, [number, string, number]>();
  for (const e of x.db.board) {
    if (e.agent_id === null || e.ts === null) continue;
    if (e.kind === 'irc_join' && e.chan) {
      const ch = chanSlug(e.chan);
      const k = `${e.agent_id}\u0000${ch}`;
      if (!open.has(k)) open.set(k, [e.agent_id, ch, e.ts]);
    } else if (e.kind === 'disconnect') {
      for (const [k, [aid, ch, tj]] of [...open]) {
        if (aid === e.agent_id) { pushTo(member, ch, [aid, tj, e.ts]); open.delete(k); }
      }
    }
  }
  for (const [aid, ch, tj] of open.values()) pushTo(member, ch, [aid, tj, Infinity]);
  for (const p of posts) {
    if (p.is_seed || p.kind !== 'post' || p.ts === null) continue;
    for (const [aid, tj, tl] of member.get(p.thread ?? 'None') ?? []) {
      if (tj <= p.ts && p.ts <= tl && `a${aid}` !== p.author_node) {
        addEdge('exposure', `irc${p.line_off}`, `a${aid}`, p.author_node, p.ts, 1.0,
                { line_off: p.line_off, post_uid: p.uid, path: '#' + (p.thread ?? 'None'), listing: false, channel: true });
        pushTo(exposures, expKey(`a${aid}`, p.thread), p.ts);
      }
    }
  }
}

/** Reply, mention and DM edges of every live post; sets p.mentions. */
function postEdges(x: Ctx, posts: PostRow[], names: Map<number, Set<string>>, addEdge: AddEdge, exposures: Exposures): void {
  const owners = handleOwners(x, names);
  const handleRx = new Map([...owners.keys()].map((h) => [h, C.handleRegex(h)] as const));
  const priorPost = priorPostIndex(posts);
  const seenWithin = (src: string, th: string | null, lo: number, hi: number): boolean => {
    const s = exposures.get(expKey(src, th));
    if (!s) return false;
    const i = bisectLeft(s, lo);
    return i < s.length && s[i]! <= hi;
  };
  for (const p of posts) {
    if (p.is_seed || p.ts === null || !p.author_node) continue;
    const src = p.author_node;
    if (p.kind === 'post') {
      const q = priorPost(p.thread, p.ts, src);
      if (q) {
        const seen = seenWithin(src, p.thread, p.ts - REPLY_WINDOW_S, p.ts);
        addEdge('reply', p.uid, src, q.author_node, p.ts, seen ? 1.0 : 0.5,
                { line_off: p.line_off, post_uid: p.uid, reply_to: q.uid, ...(seen ? {} : { blind: true }) });
      }
    }
    const mine = new Set([...(names.get(p.agent_id ?? -1) ?? [])].map((n) => n.toLowerCase()));
    const ments: string[] = [];
    const body = p.body || '';
    for (const [h, rx] of handleRx) {
      if (mine.has(h) || !rx.test(body)) continue;
      const dsts = [...owners.get(h)!].filter((d) => d !== src).sort();
      for (const d of dsts) {
        addEdge('mention', p.uid, src, d, p.ts, 1.0 / dsts.length,
                { line_off: p.line_off, post_uid: p.uid, handle: h, ambiguous: dsts.length > 1 });
        ments.push(d);
      }
    }
    p.mentions = [...new Set(ments)].sort();
    if (p.kind === 'dm' && p.thread) {
      let dsts = [...(owners.get(p.thread.toLowerCase()) ?? [])].filter((d) => d !== src).sort();
      if (!dsts.length) dsts = [`name:${p.thread}`];
      for (const d of dsts)
        addEdge('dm', p.uid, src, d, p.ts, 1.0 / dsts.length,
                { line_off: p.line_off, post_uid: p.uid, to_nick: p.thread, ambiguous: dsts.length > 1 });
    }
  }
}

/** Lower-cased handle -> the nodes that used it (seed authors, agents' display names). */
function handleOwners(x: Ctx, names: Map<number, Set<string>>): Map<string, Set<string>> {
  const owners = new Map<string, Set<string>>();
  for (const sp of x.db.seedPosts) if (C.handleOk(sp.author)) addTo(owners, sp.author!.toLowerCase(), `seed:${sp.author}`);
  for (const [aid, ns] of names) for (const n of [...ns].sort()) if (C.handleOk(n)) addTo(owners, n.toLowerCase(), `a${aid}`);
  return owners;
}

/** derive.py's `prior = [q for q in posts_by_thread[thread] if q.ts < p.ts and q.author_node not in (None, src)]`
 *  then prior[-1], in O(log n): the per-thread list is sorted by (ts ?? 0), so the q.ts < p.ts candidates are a
 *  prefix; lastOk[j] is the last candidate (ts set, author_node set) at or before j and lastOther[j] the last
 *  one before it whose author differs from lastOk[j]'s, so "last candidate not by src" is one of the two.
 *  (The filter form was O(posts^2) for one busy thread: an IRC flood.) */
function priorPostIndex(posts: PostRow[]): (th: string | null, ts: number, src: string) => PostRow | null {
  const byThread = new Map<string | null, PostRow[]>();
  for (const p of [...posts].sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))) if (p.kind === 'post') pushTo(byThread, p.thread, p);
  const index = new Map<string | null, { keys: number[]; lastOk: number[]; lastOther: number[]; list: PostRow[] }>();
  for (const [th, list] of byThread) {
    const keys = list.map((q) => q.ts ?? 0), lastOk: number[] = [], lastOther: number[] = [];
    let ok = -1, other = -1;
    list.forEach((q, j) => {
      if (q.ts !== null && q.author_node) {
        if (ok >= 0 && list[ok]!.author_node !== q.author_node) other = ok;   // same author: lastOther carries over
        ok = j;
      }
      lastOk.push(ok); lastOther.push(other);
    });
    index.set(th, { keys, lastOk, lastOther, list });
  }
  return (th, ts, src) => {
    const ix = index.get(th);
    if (!ix) return null;
    let end = bisectLeft(ix.keys, ts) - 1;
    // keys are (ts ?? 0): null-ts rows sort as 0 but are never candidates; real ts equal to ts are excluded too
    while (end >= 0 && ix.list[end]!.ts !== null && ix.list[end]!.ts! >= ts) end--;
    if (end < 0) return null;
    const v = ix.lastOk[end]!;
    if (v < 0) return null;
    if (ix.list[v]!.author_node !== src) return ix.list[v]!;
    const o = ix.lastOther[end]!;
    return o >= 0 ? ix.list[o]! : null;
  };
}

// ------------------------------------------------------------------------------------------------ agents
function agentRows(x: Ctx, steps: StepIndex, off: Offsets, names: Map<number, Set<string>>): void {
  const { db, run } = x;
  const firstRec = new Map<number, Partial<Record<'start' | 'end' | 'crash', RecRow>>>();
  for (const r of db.recs) {
    if (r.kind !== 'start' && r.kind !== 'end' && r.kind !== 'crash') continue;
    let m = firstRec.get(r.agent_id);
    if (!m) firstRec.set(r.agent_id, m = {});
    m[r.kind] ??= r;
  }
  const timedOut = new Set(run.timed_out);
  for (const aid of x.agentIds) {
    let a: AgentRow | undefined = db.agents.get(aid);
    if (!a) db.agents.set(aid, a = { agent_id: aid, model: null, cfg: null, summary: null });
    const rb = firstRec.get(aid) ?? {};
    const summ: Dict = isDict(a.summary) ? a.summary : {};
    const endr: Dict = rb.end && isDict(rb.end.data) ? rb.end.data : {};
    const crash = rb.crash;
    const src: Dict = truthy(summ) ? summ : endr;     // summary.json, else the end record
    const has = truthy(src);
    const ag = x.agent(aid);
    const toks: Dict = isDict(src['tokens']) ? src['tokens'] : {};
    const hasToks = truthy(toks);
    const crashed = truthy(summ.crashed) ? summ.crashed : (crash ? (isDict(crash.data) ? crash.data.error ?? null : null) : null);
    let status: string;
    if (truthy(crashed)) status = 'crashed';
    else if (timedOut.has(aid)) status = 'timed_out';
    else if (truthy(summ) || truthy(endr)) status = 'done';
    else if (rb.start) status = run.status === 'done' || run.status === 'errored' ? 'crashed' : 'running';
    else status = 'pending';
    const nSteps = ag.steps.length, nMc = ag.mcs.length;
    const solveStep = has ? (src.solve_step ?? null) : (ag.steps.find((s) => s.is_solve)?.step ?? null);
    let solveTs = steps.solveTs.get(aid) ?? null;
    if (solveTs === null && solveStep !== null) solveTs = steps.byKey.get(stepKey(aid, solveStep))?.t1_raw ?? null;
    const sum = (f: (m: ModelRow) => number | null): number => ag.mcs.reduce((acc, m) => acc + (f(m) || 0), 0);
    Object.assign(a, {
      status, model: a.model || run.model,
      start_ts: rb.start ? rb.start.ts_raw : null,
      end_ts: (rb.end ?? crash)?.ts_raw ?? null,
      solved: has ? truthy(src.solved) : (truthy(steps.solveTs.get(aid)) ? true : null),
      solve_step: solveStep,
      solve_ts: solveTs,
      steps_used: has && 'steps_used' in src ? src.steps_used : nSteps,
      model_calls: has && 'model_calls' in src ? src.model_calls : nMc,
      ended: has ? (src.ended ?? null) : null,
      crashed: truthy(crashed) ? crashed : null,
      tok_prompt: hasToks ? toks.prompt ?? null : sum((m) => m.prompt_tokens),
      tok_completion: hasToks ? toks.completion ?? null : sum((m) => m.completion_tokens),
      tok_cached: hasToks ? toks.cached ?? null : sum((m) => m.cached_tokens),
      tok_reasoning: sum((m) => m.reasoning_tokens),
      wall_s: has ? src.wall_s ?? null : null,
      clock_offset_s: off.perAgent.get(aid) ?? 0.0,
      display_names: [...(names.get(aid) ?? [])].sort(),
    });
  }
}

// ------------------------------------------------------------------------------------------------ params / code version
function runParams(x: Ctx): void {
  const { db, run } = x;
  const params = { ...run.params };
  const explicit = new Set<string>(Array.isArray(params._explicit) ? params._explicit : []);
  let codeVersion: string;
  if (explicit.has('code_version')) codeVersion = String(params.code_version);
  else if (preIdleHarness(x)) codeVersion = 'pre-987afa9-inferred';
  else if (db.steps.length) { codeVersion = '987afa9'; params.code_version_inferred = true; }  // lab_commit_at stubbed
  else codeVersion = 'unknown';
  params.code_version = codeVersion;
  delete params.pilot_rng_seed;   // no pilot.log markers in a run dir
  const harness = run.harness ?? {};
  params.max_model_calls = harness.max_model_calls ?? ((harness.budget || 0) + 25);
  if (ircInboundBroken(x)) params.irc_inbound_broken = true; else delete params.irc_inbound_broken;
  const liveIds = db.board.filter((e) => e.kind === 'post' && e.post_id !== null).map((e) => e.post_id!);
  if (liveIds.length && db.seedPosts.length && liveIds.reduce((m, i) => (i < m ? i : m)) !== db.seedPosts.length + 1)
    params.seed_mismatch = true;
  else delete params.seed_mismatch;
  run.params = params;
  run.code_version = codeVersion;
  run.tag = runTag(run.run_id, params, run.tag);
}
/** An older harness: voluntary_stop endings, or a free-time model call without tool calls not followed by an
 *  idle step. */
function preIdleHarness(x: Ctx): boolean {
  if (x.agentIds.some((aid) => x.db.agents.get(aid)?.ended === 'voluntary_stop')) return true;
  for (const aid of x.agentIds) {
    const ag = x.agent(aid);
    const rows = inFileOrder([['m', ag.mcs], ['s', ag.steps]]);
    const old = rows.some(([typ, r], i) => {
      if (typ !== 'm' || (r as ModelRow).phase !== 'free' || ((r as ModelRow).n_tool_calls || 0) !== 0) return false;
      const nxt = rows[i + 1];
      return !nxt || nxt[0] !== 's' || !(nxt[1] as StepRow).idle;
    });
    if (old) return true;
  }
  return false;
}
/** IRC pilot bug: an agent sent bytes upstream but the board never logged a command from it. */
function ircInboundBroken(x: Ctx): boolean {
  if (x.iface !== 'irc') return false;
  const cmdAgents = new Set(x.db.board.filter((e) => e.kind === 'irc_cmd' && e.agent_id !== null).map((e) => e.agent_id));
  return x.agentIds.some((aid) => {
    const up = x.agent(aid).relay.filter(dataUp).reduce((s, r) => s + (r.n || 0), 0);
    return up > 0 && !cmdAgents.has(aid);
  });
}
