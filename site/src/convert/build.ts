// Port of site/tools/export.py RunBuilder.build (SPEC section 4): obs rows -> CoreBundle + DetailBundle
// (src/types.ts). One function per `# ---` section of build(), called in the same order (node, edge, event and
// string indices are positional). Chapters are chapters.ts, the graph primitives graph.ts. Redaction
// (tools/redact.py) is not ported; `red()` only applies the --mask-ctf mask (ctf.ts).
import { CORE_FORMAT, DETAIL_FORMAT, EDGE_KINDS, EF, EVENT_KINDS, STEP_CATS } from '../types.js';
import type { CaveatCode, Detail, PostDetail, SegmentRec } from '../types.js';
import { buildChapters, type Onset } from './chapters.js';
import { ctfAllow, leakSpans, type Masker } from './ctf.js';
import type { Derived, Interaction, PostRow } from './derive.js';
import { DERIVE_VERSION, SCHEMA_VERSION } from './derive.js';
import { type EdgeKindS, type Ev, type EventKindS, type EvOpts, Graph, TRUNC } from './graph.js';
import type { AgentRow, Ingested, RunRow } from './ingest.js';
import type { AgentMetrics } from './metrics.js';
import type { BoardRow, ModelRow, RecRow, StepRow } from './parse.js';
import {
  type Dict, type J, bisectRight, cmpNullsLast, cmpStr, cmpTuple, headTail, int0, isDict, ms, oneLine, or, pushTo,
  pyStr, pyStrip, roundN, stripChars, toFloat, toInt, trunc, truthy,
} from './py.js';

const SC = Object.fromEntries(STEP_CATS.map((k, i) => [k, i])) as Record<string, number>;
const POST_FLAGS = new Set(['secret_leak', 'self_id', 'injection', 'memory_note']);
export const HIDDEN_BY_DEFAULT = new Set(['think', 'idle', 'proto', 'connect', 'disconnect']);
export type Nouns = { thread: string; threads: string; post: string; posts: string; board: string };
const NOUNS: Record<string, Nouns> = {
  forum: { thread: 'thread', threads: 'threads', post: 'post', posts: 'posts', board: 'forum' },
  wiki: { thread: 'page', threads: 'pages', post: 'edit', posts: 'edits', board: 'wiki' },
  cache: { thread: 'directory', threads: 'directories', post: 'artifact', posts: 'artifacts', board: 'artifact cache' },
  irc: { thread: 'channel', threads: 'channels', post: 'message', posts: 'messages', board: 'IRC server' },
};
const CAVEAT_TEXT: Record<string, string> = {
  irc_inbound_broken: 'IRC inbound was broken in this run: agent bytes never reached the board (pilot bug).',
  tagged: 'Calibration / smoke run, not part of the pilot cells.',
  old_harness: 'Recorded with an older harness version (voluntary_stop endings).',
  final_missing: 'Final copy-out missing; reconstructed from the live tap.',
  heuristic_edges: 'Reply and exposure edges are inferred; see edge tooltips.',
  synthetic: 'Synthetic stress-test data, not a real run.',
  unsolvable: 'Unsolvable goal: the flag can never be found, so no agent solves and there is no free time.',
  // converter-only codes (docs/browser-swarm.md §5), appended after the export.py ones
  preview: 'Browser-lab preview run (CheerpX, weak-uid isolation): the agent can obtain root in its VM, so the solve '
    + 'is not verified and the run is not comparable with the isolated pilot.',
  live: 'Live run in progress: agents may still be working; the trace ends at the last recorded event.',
  incomplete: 'Incomplete trace: the final run record is missing, so end times and outcomes may be absent.',
};
const malformedText = (n: number): string =>
  `${n} log line${n === 1 ? '' : 's'} could not be parsed and ${n === 1 ? 'was' : 'were'} skipped.`;

export interface BuildOut { core: Dict; detail: Dict }

/** What every stage reads: the run, its rows in export.py's query order, and the graph being built. */
interface Ctx {
  db: Ingested; dv: Derived; g: Graph; red: (s: string | null | undefined) => string;
  r: RunRow; iface: string; nouns: Nouns; boardName: string; tOrigin: number; tEnd: number; nAgents: number;
  amap: Map<number, AgentRow>;
  steps: StepRow[]; calls: ModelRow[]; recs: RecRow[];   // ORDER BY agent_id, seq
  bevs: BoardRow[];                                      // ORDER BY seq
  inter: Interaction[];                                  // ORDER BY t NULLS LAST, edge_uid
  stepsByAgent: Map<number, StepRow[]>;
}

export function buildRun(db: Ingested, dv: Derived, am: AgentMetrics[], M: Masker, maskCtf: boolean): BuildOut {
  const x = context(db, dv, M);
  agentNodes(x);
  const threads = threadNodes(x);
  const posts = postNodes(x);
  applyDeletes(x);
  const phase = phaseLookup(x);
  recordEvents(x);
  modelEvents(x);
  stepEvents(x);
  const board = boardEvents(x, posts, phase.at);
  fireSeedStructure(x, threads, posts.seeds, board);
  const onset = socialEdges(x, posts.authorIndex, board.lineEv);
  const packed = x.g.pack();
  const agentRecs = agentRecords(x, am, phase.solveMs);
  const running = db.live && agentRecs.some((a) => a.status === 'running');
  const chapters = buildChapters(x.g, x.nouns, onset, x.red, running);
  const segments = buildSegments(agentRecs, x.tEnd, running);
  const series = buildSeries(x);
  const cav = runCaveats(x);
  const core: Dict = {
    format: CORE_FORMAT, run: runMeta(x, cav, maskCtf), strings: x.g.strings.list, nodes: x.g.nodes, edges: x.g.edges,
    events: packed.events, fires: packed.fires, agents: agentRecs, chapters, segments, series,
  };
  const needles = maskCtf ? [...M.mask.values()] : ctfAllow(isDict(db.run.ctf) ? db.run.ctf : {});
  const postDetails: Record<string, PostDetail> = {};
  for (const [uid, body] of posts.body) {
    const b = trunc(body, TRUNC.body);
    postDetails[uid] = { body: b, leakSpans: leakSpans(b, needles), mentions: posts.mentions.get(uid) ?? [] };
  }
  const detail: Dict = { format: DETAIL_FORMAT, items: packed.items, posts: postDetails };
  return { core, detail };
}

function context(db: Ingested, dv: Derived, M: Masker): Ctx {
  const red = (s: string | null | undefined): string => M.red(s ?? '');
  const r = db.run;
  const iface = r.iface ?? 'forum';
  const agents = [...db.agents.values()].sort((a, b) => a.agent_id - b.agent_id);
  const bySeq = <T extends { agent_id: number; seq: number }>(xs: T[]): T[] =>
    [...xs].sort((a, b) => a.agent_id - b.agent_id || a.seq - b.seq);
  const steps = bySeq(db.steps);
  const stepsByAgent = new Map<number, StepRow[]>();
  for (const st of steps) pushTo(stepsByAgent, st.agent_id, st);
  const maxAid = agents.reduce((m, a) => Math.max(m, a.agent_id), -1);
  return {
    db, dv, g: new Graph(red), red, r, iface, nouns: NOUNS[iface] ?? NOUNS.forum!, boardName: db.boardName || 'agent-commons',
    tOrigin: r.t_origin ?? 0, tEnd: ms(r.t_end) || 0, nAgents: Math.max(int0(r.n_agents), agents.length, maxAid + 1),
    amap: new Map(agents.map((a) => [a.agent_id, a] as const)),
    steps, calls: bySeq(db.mcs), recs: bySeq(db.recs), bevs: [...db.board].sort((a, b) => a.seq - b.seq),
    inter: [...dv.interactions].sort((a, b) => cmpNullsLast(a.t, b.t) || cmpStr(a.edge_uid, b.edge_uid)),
    stepsByAgent,
  };
}
const relMs = (x: Ctx, ts: number | null | undefined): number | null =>
  (ts !== null && ts !== undefined ? ms(ts - x.tOrigin) : null);
const agentNode = (x: Ctx, a: number): number => x.g.id(`a${a}`) ?? -1;

// ------------------------------------------------------------------------------------------------ nodes: task, board, agents
function agentNodes(x: Ctx): void {
  x.g.node('task', 'task', 'CTF vault', null);
  x.g.node('board', 'board', `${x.boardName} · ${x.nouns.board}`, null);
  for (let i = 0; i < x.nAgents; i++) {
    const a = x.amap.get(i);
    x.g.node(`a${i}`, 'agent', `a${i}`, relMs(x, a?.start_ts), { agent: i, names: (a?.display_names ?? []).map(x.red) });
  }
}

// ------------------------------------------------------------------------------------------------ threads
interface Th { slug: string; title: string; seed: boolean; t0: number | null }
/** Thread nodes: derived threads plus every slug a read, post or DM names (earliest sighting = birth). */
function threadNodes(x: Ctx): Th[] {
  const th = new Map<string, Th>();
  for (const t of x.dv.threads.values())
    th.set(t.thread, { slug: t.thread, title: t.title || t.thread, seed: t.is_seed, t0: t.is_seed ? null : ms(t.created_t) });
  const ensure = (slug: string, tMs: number | null, title: string | null = null): void => {
    const h = th.get(slug);
    if (!h) th.set(slug, { slug, title: title || slug, seed: false, t0: tMs });
    else if (!h.seed && h.t0 !== null && tMs !== null && tMs < h.t0) h.t0 = tMs;
  };
  for (const b of x.bevs) {
    if (b.kind !== 'req' || !b.target) continue;
    if (b.target_kind === 'thread' || (b.target_kind === 'listing' && x.iface === 'cache')) ensure(b.target, ms(b.t));
  }
  for (const p of x.dv.posts) {
    if (p.kind === 'dm') ensure(`dm:${pyStr(p.thread)}`, ms(p.t), `DM ${pyStr(p.thread)}`);
    else if (p.thread) ensure(p.thread, ms(p.t));
  }
  const key = (h: Th): (number | string)[] => [h.seed ? 0 : 1, h.t0 !== null ? h.t0 : -1, h.slug];
  const sorted = [...th.values()].sort((a, b) => cmpTuple(key(a), key(b)));
  for (const h of sorted) {
    const label = x.iface === 'irc' && !h.slug.startsWith('dm:') ? '#' + h.slug : h.slug;
    x.g.node(`th:${h.slug}`, 'thread', label, h.seed ? null : h.t0, { title: trunc(x.red(h.title), TRUNC.title) });
  }
  return sorted;
}

// ------------------------------------------------------------------------------------------------ posts
interface Posts {
  seeds: PostRow[]; liveByUid: Map<string, PostRow>;
  body: Map<string, string>; mentions: Map<string, string[]>;   // redacted, by uid
  authorIndex(an: string | null): number | null;
}
/** Post nodes (seed, then live), then persona (seed author) and handle (unowned DM target) nodes. */
function postNodes(x: Ctx): Posts {
  const { g, red } = x;
  const posts = x.dv.posts;
  const seeds = posts.filter((p) => p.is_seed).sort((a, b) => cmpNullsLast(a.t, b.t) || ((a.post_id ?? 0) - (b.post_id ?? 0)));
  const lives = posts.filter((p) => !p.is_seed).sort((a, b) => cmpNullsLast(a.t, b.t) || cmpStr(a.uid, b.uid));
  const personas: string[] = [];
  for (const p of seeds) if (p.author_node && !personas.includes(p.author_node)) personas.push(p.author_node);
  const handles: string[] = [];
  for (const it of x.inter) {
    for (const nd of [it.src, it.dst]) {
      if (nd && nd.startsWith('seed:') && !personas.includes(nd)) personas.push(nd);
      if (nd && nd.startsWith('name:') && !handles.includes(nd)) handles.push(nd);
    }
  }
  handles.sort(cmpStr);
  // persona / handle nodes come after the posts, so a post's author index is known before its node exists
  const base = g.nodes.length + seeds.length + lives.length;
  const personaIdx = new Map(personas.map((pn, j) => [pn, base + j] as const));
  const handleIdx = new Map(handles.map((hn, j) => [hn, base + personas.length + j] as const));
  const authorIndex = (an: string | null): number | null =>
    (an === null ? null : g.id(an) ?? personaIdx.get(an) ?? handleIdx.get(an) ?? null);
  const body = new Map<string, string>();
  const mentions = new Map<string, string[]>();
  for (const p of [...seeds, ...lives]) {
    const text = red(p.body ?? '');
    body.set(p.uid, text);
    mentions.set(p.uid, p.mentions.map(red));
    const threadSlug = p.kind === 'dm' ? `dm:${pyStr(p.thread)}` : p.thread;
    const who = p.agent_id !== null ? `a${p.agent_id}` : (p.author || '?');
    const label = p.kind === 'dm' ? `dm ${who}→${pyStr(p.thread)}` : `#${pyStr(p.post_id)} ${who}`;
    const flags = p.flags.filter((f) => POST_FLAGS.has(f));
    const kw: Dict = { thread: threadSlug === null ? null : g.id(`th:${threadSlug}`) ?? null,
                       author: authorIndex(p.author_node), agent: p.agent_id, postId: p.post_id,
                       preview: trunc(oneLine(text), TRUNC.preview),
                       bodyLen: p.body_len || [...(p.body ?? '')].length };
    if (flags.length) kw.flags = flags;
    if (p.is_seed) {
      kw.seed = true; kw.tSeed = ms(p.t);
      if (p.upvotes0) kw.upvotes0 = Math.trunc(p.upvotes0);
      g.node(p.uid, 'post', label, null, kw);
    } else g.node(p.uid, 'post', label, ms(p.t), kw);
  }
  const tail = (nid: string): string => nid.split(':').slice(1).join(':');
  for (const pn of personas) g.node(pn, 'persona', tail(pn), null);
  for (const hn of handles) g.node(hn, 'handle', tail(hn), null);
  const liveByUid = new Map<string, PostRow>();
  for (const p of lives) if (!liveByUid.has(p.uid)) liveByUid.set(p.uid, p);
  return { seeds, liveByUid, body, mentions, authorIndex };
}

// ------------------------------------------------------------------------------------------------ deletes
function applyDeletes(x: Ctx): void {
  for (const b of x.bevs) {
    if (b.kind !== 'delete' || b.post_id === null) continue;
    const i = x.g.id(`p${b.post_id}`) ?? x.g.id(`s${b.post_id}`);
    if (i === undefined) continue;
    const t1 = ms(b.t);
    if (t1 !== null) x.g.nodes[i]!.t1 = t1; else delete x.g.nodes[i]!.t1;
  }
}

// ------------------------------------------------------------------------------------------------ phase lookups
type PhaseAt = (agent: number | null, step: number | null, tMs: number) => J;
/** An agent's phase at a step (its phase_before), else by time against its solve. */
function phaseLookup(x: Ctx): { solveMs: Map<number, number>; at: PhaseAt } {
  const stepPhase = new Map<string, J>();
  for (const s of x.steps) stepPhase.set(`${s.agent_id}:${s.step}`, s.phase_before);
  const solveMs = new Map<number, number>();
  for (const a of x.amap.values()) if (a.solve_ts !== null && a.solve_ts !== undefined) solveMs.set(a.agent_id, relMs(x, a.solve_ts)!);
  const at: PhaseAt = (agent, step, tMs) => {
    if (agent === null) return 'task';
    const k = `${agent}:${step}`;
    if (step !== null && stepPhase.has(k)) return stepPhase.get(k);
    const sv = solveMs.get(agent);
    return sv !== undefined && tMs >= sv ? 'free' : 'task';
  };
  return { solveMs, at };
}

// ------------------------------------------------------------------------------------------------ events: steps.jsonl records
const line = (text: string): Detail => ({ t: 'line', text: trunc(text, TRUNC.text) });

/** start / end / crash / api_retry records. */
function recordEvents(x: Ctx): void {
  const { g, red } = x;
  for (const rc of x.recs) {
    const data: Dict = isDict(rc.data) ? rc.data : {};
    const t = rc.t !== null ? ms(rc.t)! : 0;
    const o: EvOpts = { a: rc.agent_id, n: agentNode(x, rc.agent_id), rank: 1, seq: rc.seq, src: 'rec' };
    if (rc.kind === 'start') g.event(t, 'start', { ...o, label: 'start' });
    else if (rc.kind === 'end') {
      const ended = or(or(data.ended, rc.detail), 'end');
      const txt = `ended: ${pyStr(ended)} · solved ${truthy(data.solved) ? 'True' : 'False'} · `
        + `${pyStr('steps_used' in data ? data.steps_used : '?')} steps · ${pyStr('model_calls' in data ? data.model_calls : '?')} model calls`;
      g.event(t, 'end', { ...o, label: stripChars(`end · ${pyStr(or(or(data.ended, rc.detail), ''))}`, ' ·'), x: line(red(txt)) });
    } else if (rc.kind === 'crash') {
      const txt = red(pyStr(or(or(rc.detail, data.error), 'crash')));
      g.event(t, 'crash', { ...o, label: trunc(oneLine('crash: ' + txt), TRUNC.l), x: line(txt) });
    } else if (rc.kind === 'api_retry') {
      const txt = red(`API retry attempt ${pyStr(rc.attempt)}, waited ${pyStr(rc.wait, true)} s` + (rc.detail ? `: ${rc.detail}` : ''));
      g.event(t, 'retry', { ...o, label: trunc(oneLine(txt), TRUNC.l), x: line(txt) });
    }
  }
}

function modelEvents(x: Ctx): void {
  for (const mc of x.calls) {
    const reason = x.red(pyStrip(mc.reasoning || ''));
    const content = x.red(pyStrip(mc.content || ''));
    const tok: [number, number, number, number] = [int0(mc.prompt_tokens), int0(mc.completion_tokens), int0(mc.reasoning_tokens),
                                                   int0(mc.cached_tokens)];
    x.g.event(ms(mc.t) ?? 0, 'think', {
      a: mc.agent_id, n: agentNode(x, mc.agent_id), f: mc.phase === 'free' ? EF.FREE : 0, s: mc.call,
      label: trunc(oneLine(reason || content), TRUNC.l), d: ms(mc.latency_s || 0),
      x: { t: 'think', reason: trunc(reason, TRUNC.reason), content: trunc(content, TRUNC.content), nTools: int0(mc.n_tool_calls), tok },
      rank: 0, seq: mc.seq, src: 'model' });
  }
}

/** Steps: idle, board (relay / taint / mention), or cmd / submit working on the task. */
function stepEvents(x: Ctx): void {
  const { g, red } = x;
  for (const st of x.steps) {
    const a = st.agent_id;
    const t = ms(st.t) ?? 0;
    const d = st.t_end !== null && st.t !== null ? ms((truthy(st.t_end) ? st.t_end : st.t) - st.t) : 0;
    let f = st.phase_before === 'free' ? EF.FREE : 0;
    if (st.is_solve) f |= EF.SOLVE;
    const o: EvOpts = { a, f, s: st.step, d, rank: 1, seq: st.seq, src: 'step' };
    if (st.idle) {
      g.event(t, 'idle', { ...o, n: -1, c: SC.idle, label: '' });
      continue;
    }
    const cmd = red(st.command || '');
    if (st.exit !== null && st.exit !== 0) f |= EF.ERROR;
    if (st.persistence) f |= EF.PERSIST;
    const heuristic = st.board_via === 'relay' || st.board_via === 'taint';
    const subs = st.submissions.map((s) => red(isDict(s) ? (s as Dict).value : pyStr(s)));
    const det: Detail & Dict = { t: 'step', cmd: trunc(cmd, TRUNC.cmd), out: headTail(red(st.output || '')),
                                 outLen: int0(or(st.output_len, null)) || [...(st.output || '')].length, exit: st.exit };
    if (heuristic) det.via = st.board_via as 'relay' | 'taint';
    if (subs.length) det.submit = subs;
    const label = trunc(oneLine(cmd), TRUNC.l);
    if (st.is_board) {
      f |= EF.BOARD;
      if (heuristic) f |= EF.HEURISTIC;
      g.event(t, 'board', { ...o, n: 1, f, c: SC.board, label, x: det });
    } else {
      const cat = st.cat || 'other';
      const k = cat === 'submit' ? 'submit' : 'cmd';
      if (k === 'submit' && st.submit_correct) f |= EF.SUBMIT_OK;
      const e = g.edge(agentNode(x, a), 0, 'works');
      g.fire(e, t, 1, g.event(t, k, { ...o, n: 0, e, f, c: SC[cat] ?? SC.other, label, x: det }));
    }
  }
}

// ------------------------------------------------------------------------------------------------ events: board
const WRITES = ['post', 'upvote', 'delete', 'dm'];
interface Pairing { req: Map<number, BoardRow>; irc: Map<number, BoardRow>; folded: Set<number> }
/** Fold write reqs into the post/upvote/delete they accompany (the next one by the same agent before another
 *  write); on IRC fold the PRIVMSG/NOTICE before a post/dm into it. Keys and `folded` are line offsets. */
function pairBoardWrites(x: Ctx): Pairing {
  const req = new Map<number, BoardRow>(), irc = new Map<number, BoardRow>();
  const byAgent = new Map<number | null, BoardRow[]>();
  for (const b of x.bevs) pushTo(byAgent, b.agent_id, b);
  for (const lst of byAgent.values()) {
    const used = new Set<number>();
    lst.forEach((b, j) => {
      if (!WRITES.includes(b.kind)) return;
      for (let q = j + 1; q < lst.length; q++) {   // export.py `for nb in lst[j+1:]`, without the O(n) copy
        const nb = lst[q]!;
        if (nb.kind === 'req' && nb.target_kind === 'write' && !used.has(nb.line_off)) {
          used.add(nb.line_off);
          req.set(b.line_off, nb);
          break;
        }
        if (WRITES.includes(nb.kind)) break;
      }
      if (x.iface === 'irc' && (b.kind === 'post' || b.kind === 'dm')) {
        for (let q = j - 1; q >= 0; q--) {
          const pb = lst[q]!;
          if (pb.kind === 'irc_cmd' && ['PRIVMSG', 'NOTICE'].includes((pb.cmd || '').toUpperCase()) && !used.has(pb.line_off)) {
            used.add(pb.line_off);
            irc.set(b.line_off, pb);
            break;
          }
        }
      }
    });
  }
  const folded = new Set<number>();
  for (const b of x.bevs) if (b.kind === 'req' && b.target_kind === 'write' && (b.status || 0) < 400) folded.add(b.line_off);
  for (const pb of irc.values()) folded.add(pb.line_off);
  return { req, irc, folded };
}

interface BoardOut { lineEv: Map<number, Ev>; born: Set<number>; startEv: Ev | null }
/** One event per board line not folded into another, with its reads / writes / authored / contains fires. */
function boardEvents(x: Ctx, posts: Posts, phaseAt: PhaseAt): BoardOut {
  const { g, red, iface, nouns } = x;
  const pair = pairBoardWrites(x);
  const reqDetail = (q: BoardRow): Detail => ({ t: 'req', method: q.method || '', path: trunc(red(q.path || ''), TRUNC.cmd),
                                                status: int0(q.status), bytes: int0(q.bytes) });
  const out: BoardOut = { lineEv: new Map(), born: new Set(), startEv: null };
  /** The board -> thread `contains` fire at the thread's first write. */
  const birth = (tn: number, t: number, ev: Ev): void => {
    if (tn >= 0 && !out.born.has(tn) && g.nodes[tn]!.t0 !== null) {
      out.born.add(tn);
      g.fire(g.edge(1, tn, 'contains'), t, 1, ev);
    }
  };
  /** agent -> thread `writes`, fired by ev; returns the edge or -1. */
  const writes = (an: number | null, tn: number, t: number, ev: Ev): number => {
    if (an === null || tn < 0) return -1;
    const e = g.edge(an, tn, 'writes');
    g.fire(e, t, 1, ev);
    return e;
  };
  for (const b of x.bevs) {
    const kind = b.kind, a = b.agent_id, lo = b.line_off;
    const t = b.t !== null ? ms(b.t)! : 0;
    if (pair.folded.has(lo) && (kind === 'req' || kind === 'irc_cmd')) continue;
    const an = a !== null ? (g.id(`a${a}`) ?? null) : null;
    let f = 0;
    if (b.attribution === 'background') f |= EF.BACKGROUND;
    if (a !== null && phaseAt(a, b.step, t) === 'free') f |= EF.FREE;
    const on = (k: EventKindS, o: EvOpts): Ev => g.event(t, k, { a, f, s: b.step, seq: b.seq, src: 'board', ...o });
    let ev: Ev;
    if (kind === 'board_start') {
      ev = out.startEv = on('boardStart', { a: null, n: 1, f: 0, s: null, label: `${x.boardName} opens (${nouns.board})`, lane: -1 });
    } else if (kind === 'req') {
      const status = int0(b.status);
      const tk = b.target_kind, tg = b.target;
      const det = reqDetail(b);
      const lab = pyStrip(`${b.method || ''} ${red(b.path || '')}`);
      if (status >= 400 || tk === null || tk === 'none' || tk === 'write') {
        if (status >= 400) f |= EF.ERROR;
        ev = on('miss', { n: 1, f, label: trunc(`${lab} ${status}`, TRUNC.l), x: det });
      } else {
        let k: EventKindS, n: number;
        if (tk === 'thread') { k = 'read'; n = g.id(`th:${tg}`) ?? 1; }
        else if (tk === 'post') { k = 'read'; n = (tg !== null ? g.id(tg) : undefined) ?? 1; }
        else if (tk === 'listing') { k = 'list'; n = iface === 'cache' && tg ? (g.id(`th:${tg}`) ?? 1) : 1; }
        else { k = 'doc'; n = 1; }
        const e = an !== null ? g.edge(an, n, 'reads') : -1;
        ev = on(k, { n, e, label: trunc(lab, TRUNC.l), x: det });
        if (e >= 0) g.fire(e, t, 1, ev);
      }
    } else if (kind === 'post' || kind === 'dm') {
      const uid = kind === 'post' ? `p${pyStr(b.post_id)}` : `d${lo}`;
      const pn = g.id(uid) ?? -1;
      if (posts.liveByUid.get(uid)?.flags.includes('secret_leak')) f |= EF.LEAK;
      const slug = (kind === 'dm' ? `dm:${pyStr(b.to_nick)}` : b.thread) || '';
      const tn = g.id(`th:${slug}`) ?? -1;
      const body = posts.body.has(uid) ? posts.body.get(uid)! : red(b.body || '');
      const tl = tn >= 0 ? g.nodes[tn]!.label : slug;
      const lab = kind === 'post' ? `${tl}: "${oneLine(body)}"` : `DM ${b.nick || ''} → ${b.to_nick || ''}: "${oneLine(body)}"`;
      const q = pair.req.get(lo), ircLine = pair.irc.get(lo);
      const det = q ? reqDetail(q) : ircLine ? line(red(ircLine.line || '')) : null;
      ev = on(kind, { n: pn, f, label: trunc(lab, TRUNC.l), x: det });
      const e = writes(an, tn, t, ev);
      if (e >= 0 && kind === 'post') ev.e = e;
      if (an !== null && pn >= 0) g.fire(g.edge(an, pn, 'authored'), t, 1, ev);
      if (tn >= 0 && pn >= 0) g.fire(g.edge(tn, pn, 'contains'), t, 1, ev);
      birth(tn, t, ev);
    } else if (kind === 'upvote' || kind === 'delete') {
      const pid = pyStr(b.post_id);
      const pn = g.id(`p${pid}`) ?? g.id(`s${pid}`) ?? -1;
      const lab = kind === 'upvote' ? pyStrip(`▲ #${pid} ${b.author || ''}`) : `delete #${pid}`;
      const q = pair.req.get(lo);
      ev = on(kind, { n: pn, f, label: trunc(red(lab), TRUNC.l), x: q ? reqDetail(q) : null });
      const tn = kind === 'delete' && pn >= 0 ? g.nodes[pn]!.thread : undefined;
      if (tn !== undefined && tn !== null) {
        const e = writes(an, tn, t, ev);
        if (e >= 0) ev.e = e;
      }
    } else if (kind === 'irc_join') {
      const slug = (b.chan || '').replace(/^#+/, '') || (b.target || '');
      const tn = g.id(`th:${slug}`) ?? -1;
      ev = on('join', { n: tn, f, label: trunc(red(`JOIN #${slug}`), TRUNC.l) });
      const e = writes(an, tn, t, ev);
      if (e >= 0) ev.e = e;
      birth(tn, t, ev);
    } else if (kind === 'connect' || kind === 'disconnect') {
      ev = on(kind, { n: 1, f, label: trunc(red(kind + (b.nick ? ` ${b.nick}` : '')), TRUNC.l) });
    } else { // irc_cmd not covered by a post/dm, and unknown future kinds
      const text = red(b.line || kind);
      ev = on('proto', { n: 1, f, label: trunc(oneLine(text), TRUNC.l), x: line(text) });
    }
    out.lineEv.set(lo, ev);
  }
  return out;
}

/** Seed structure fires at t=0 (ev = boardStart); live threads never written to are born at their t0. */
function fireSeedStructure(x: Ctx, threads: Th[], seeds: PostRow[], board: BoardOut): void {
  const { g } = x;
  for (const h of threads) {
    const tn = g.id(`th:${h.slug}`)!;
    if (h.seed) g.fire(g.edge(1, tn, 'contains'), 0, 1, board.startEv);
    else if (!board.born.has(tn)) {
      g.fire(g.edge(1, tn, 'contains'), g.nodes[tn]!.t0 || 0, 1, null);
      board.born.add(tn);
    }
  }
  for (const p of seeds) {
    const pn = g.id(p.uid)!;
    const { thread: tn, author: au } = g.nodes[pn]!;
    if (tn !== undefined && tn !== null) g.fire(g.edge(tn, pn, 'contains'), 0, 1, board.startEv);
    if (au !== undefined && au !== null) g.fire(g.edge(au, pn, 'authored'), 0, 1, board.startEv);
  }
}

// ------------------------------------------------------------------------------------------------ interactions -> social edges
function evidenceText(kind: EdgeKindS, evd: Dict, dstLabel: string): string {
  if (kind === 'exposure') {
    let txt = truthy(evd.listing) ? 'listing seen' : truthy(evd.scrollback) ? 'channel scrollback' : `read content by ${dstLabel}`;
    if (truthy(evd.path)) txt += ` via ${pyStr(evd.path)}`;
    return txt;
  }
  if (kind === 'reply') return `posted after ${pyStr('reply_to' in evd ? evd.reply_to : '?')} in the same thread`
    + (truthy(evd.blind) ? ' (blind: no read within 600 s)' : ' (read it within 600 s)');
  if (kind === 'mention') return 'named this handle in a post' + (truthy(evd.ambiguous) ? ' (ambiguous handle)' : '');
  if (kind === 'dm') return 'direct message';
  return `upvoted ${pyStr('post_uid' in evd ? evd.post_uid : 'a post')}`;
}
/** Interaction edges, fired by the board event they came from; returns the swarm onset (first live-live
 *  reply / mention / DM). */
function socialEdges(x: Ctx, authorIndex: Posts['authorIndex'], lineEv: Map<number, Ev>): Onset | null {
  const { g } = x;
  let onset: Onset | null = null;
  for (const it of x.inter) {
    const s = authorIndex(it.src), d = authorIndex(it.dst);
    if (s === null || d === null) continue;   // export.py logs it as a parity error; nothing to draw
    const evd: Dict = isDict(it.evidence) ? it.evidence : {};
    const kind = it.kind as EdgeKindS;
    if (!(EDGE_KINDS as readonly string[]).includes(kind)) continue;
    const e = g.edge(s, d, kind, kind === 'exposure' || kind === 'reply' || truthy(evd.ambiguous),
                     evidenceText(kind, evd, g.nodes[d]!.label));
    const lo = toInt(evd.line_off);
    const ev = lo !== null ? (lineEv.get(lo) ?? null) : null;
    const t = ms(it.t) ?? 0;
    g.fire(e, t, toFloat(it.weight) ?? 1, ev);
    if (ev !== null) {
      if (it.live_live) ev.f |= EF.LIVE_LIVE;
      if (g.edges[e]!.inferred) ev.f |= EF.HEURISTIC;
      if (ev.e < 0 && (kind === 'upvote' || kind === 'dm')) ev.e = e;
    }
    if (it.live_live && (kind === 'reply' || kind === 'mention' || kind === 'dm'))
      if (onset === null || t < onset[0]) onset = [t, s, d, kind, ev];
  }
  return onset;
}

// ------------------------------------------------------------------------------------------------ agents
type AgentRec = Dict & { startT: number | null; endT: number | null; solveT: number | null; status: string };
const AGENT_STATUSES = ['done', 'crashed', 'timed_out', 'running'];

function agentRecords(x: Ctx, am: AgentMetrics[], solveMs: Map<number, number>): AgentRec[] {
  const mets = new Map(am.map((m) => [m.agent_id, m] as const));
  // steps that touched the board: board steps plus steps with a board read or write attributed to them
  const linked = new Set<string>();
  for (const b of x.bevs)
    if (b.agent_id !== null && b.step !== null && ((b.kind === 'req' && ['thread', 'post', 'listing', 'doc'].includes(b.target_kind ?? ''))
        || b.kind === 'post' || b.kind === 'dm')) linked.add(`${b.agent_id}:${b.step}`);
  const recs: AgentRec[] = [];
  for (let i = 0; i < x.nAgents; i++) {
    const a = x.amap.get(i);
    const m = mets.get(i);
    const fcTs = m?.time_to_discovery_s ?? null;
    const firstContact = fcTs !== null && a?.start_ts !== null && a?.start_ts !== undefined ? ms(a.start_ts + fcTs - x.tOrigin) : null;
    // drift: exponential moving average of "this step touched the board"
    const driftT: number[] = [], driftA: number[] = [];
    let av = 0.0;
    for (const st of x.stepsByAgent.get(i) ?? []) {
      av = 0.65 * av + 0.35 * (st.is_board || linked.has(`${i}:${st.step}`) ? 1.0 : 0.0);
      driftT.push(ms(st.t) ?? 0);
      driftA.push(roundN(av, 4));
    }
    let status = a?.status || 'done';
    if (!AGENT_STATUSES.includes(status)) status = status === 'pending' ? 'running' : 'done';
    const aNode = x.g.id(`a${i}`)!;
    recs.push({
      id: i, node: aNode, names: x.g.nodes[aNode]!.names ?? [], model: a?.model || x.r.model || '', status,
      startT: relMs(x, a?.start_ts), endT: relMs(x, a?.end_ts),
      solveT: solveMs.get(i) ?? null, solveStep: toInt(a?.solve_step ?? null),
      stepsUsed: int0(a?.steps_used), modelCalls: int0(a?.model_calls), ended: a?.ended === null || a?.ended === undefined ? null : pyStr(a.ended),
      'tokens': { prompt: int0(a?.tok_prompt), completion: int0(a?.tok_completion), cached: int0(a?.tok_cached),
                  reasoning: int0(a?.tok_reasoning) },
      metrics: { discovered: Boolean(m?.discovered), firstContact, boardSteps: m?.board_steps ?? 0,
                 shareAfterContact: m?.share_after_contact === null || m?.share_after_contact === undefined ? null : roundN(m.share_after_contact, 6),
                 posts: m?.posts ?? 0, episodes: m?.episodes ?? 0, 'secretLeak': Boolean(m?.secret_leak) },
      drift: { t: driftT, a: driftA },
    });
  }
  return recs;
}

// ------------------------------------------------------------------------------------------------ segments
/** Boot | On task | Free time | Wind-down, clipped to [0, tEnd] and contiguous. */
function buildSegments(agentRecs: AgentRec[], tEnd: number, running: boolean): SegmentRec[] {
  const known = (xs: (number | null)[]): number[] => xs.filter((v): v is number => v !== null);
  const starts = known(agentRecs.map((a) => a.startT)), ends = known(agentRecs.map((a) => a.endT));
  const solves = known(agentRecs.map((a) => a.solveT));
  const firstStart = starts.length ? Math.min(...starts) : 0;
  const lastEnd = running || !ends.length ? tEnd : Math.max(...ends);
  const firstSolve = solves.length ? Math.min(...solves) : null;
  const segs: [number, number, string][] = [[0, firstStart, 'Boot']];
  if (firstSolve !== null) segs.push([firstStart, firstSolve, 'On task'], [firstSolve, lastEnd, 'Free time']);
  else segs.push([firstStart, lastEnd, 'On task']);
  segs.push([lastEnd, tEnd, 'Wind-down']);
  let segments: SegmentRec[] = [];
  let cur = 0;
  for (const [a0r, a1r, title] of segs) {
    const a0 = Math.max(cur, Math.min(a0r, tEnd)), a1 = Math.max(cur, Math.min(a1r, tEnd));
    if (a1 > a0) { segments.push({ t0: a0, t1: a1, title }); cur = a1; }
  }
  if (!segments.length) segments = [{ t0: 0, t1: Math.max(tEnd, 1), title: 'Run' }];
  segments[0]!.t0 = 0;
  const last = segments[segments.length - 1]!;
  last.t1 = Math.max(tEnd, last.t1);
  return segments;
}

// ------------------------------------------------------------------------------------------------ series
/** ~240 bins: visible-event density, and per agent the board share of its last 5 steps at each bin end. */
function buildSeries(x: Ctx): { binMs: number; density: number[]; boardShare: number[][] } {
  const span = Math.max(x.tEnd, 1);
  const binMs = Math.max(1000, Math.ceil(span / 240 / 1000.0) * 1000);
  const nb = Math.max(1, Math.ceil(span / binMs));
  const density = new Array<number>(nb).fill(0);
  for (const v of x.g.events) {
    if (HIDDEN_BY_DEFAULT.has(EVENT_KINDS[v.k]!)) continue;
    density[Math.min(nb - 1, Math.max(0, Math.floor(v.t / binMs)))]! += 1;
  }
  const boardShare: number[][] = [];
  for (let i = 0; i < x.nAgents; i++) {
    const ts: number[] = [], vals: number[] = [];
    let win: number[] = [];
    for (const st of x.stepsByAgent.get(i) ?? []) {
      win.push(st.is_board ? 1 : 0);
      win = win.slice(-5);
      ts.push(ms(st.t) ?? 0);
      vals.push(win.reduce((p, q) => p + q, 0) / win.length);
    }
    const row: number[] = [];
    for (let bi = 0; bi < nb; bi++) {
      const j = bisectRight(ts, (bi + 1) * binMs) - 1;
      row.push(j >= 0 ? roundN(vals[j]!, 3) : 0);
    }
    boardShare.push(row);
  }
  return { binMs, density, boardShare };
}

// ------------------------------------------------------------------------------------------------ caveats + run meta
function runCaveats(x: Ctx): string[] {
  const { r, db } = x;
  const cav: string[] = [];
  if (r.params.irc_inbound_broken) cav.push('irc_inbound_broken');
  if (r.tag) cav.push('tagged');
  if (String(r.code_version || '').startsWith('pre-')) cav.push('old_harness');
  if (x.g.edges.some((e) => e.inferred)) cav.push('heuristic_edges');
  if (r.params.goal === 'unsolvable') cav.push('unsolvable');
  if (db.live) cav.push('live');
  else if (db.partial) cav.push('incomplete');
  if (db.malformed > 0) cav.push('malformed');
  return cav;
}

function runMeta(x: Ctx, cav: string[], maskCtf: boolean): Dict {
  const { r, nAgents } = x;
  const harness: Dict = isDict(r.harness) ? r.harness : {};
  const goal = r.params.goal === 'unsolvable' ? 'unsolvable' : 'solvable';
  const caveatText = (c: string): string => {
    if (c === 'tagged' && r.tag === 'preview') return CAVEAT_TEXT.preview!;
    if (c === 'malformed') return malformedText(x.db.malformed);
    return CAVEAT_TEXT[c] ?? c;
  };
  const tSeeds = x.g.nodes.map((n) => n.tSeed).filter((v): v is number => typeof v === 'number');
  return {
    id: r.run_id, iface: x.iface, pop: r.pop ?? (nAgents === 1 ? 'solo' : 'multi'), nAgents, rep: int0(r.rep), tag: r.tag || '',
    model: r.model ?? '', ...(goal === 'unsolvable' ? { goal } : {}),
    harness: { budget: int0(harness.budget), temperature: toFloat(harness.temperature) || 0,
               'maxTokens': int0(harness.max_tokens), outputChars: int0(harness.output_chars) },
    boardName: x.boardName, nouns: x.nouns, tEndMs: x.tEnd,
    wallMs: truthy(r.finished) && truthy(r.started) ? ms((r.finished || 0) - (r.started || 0)) : x.tEnd,
    prehistoryMs: Math.min(0, ...tSeeds),
    caveats: cav.map((c) => ({ code: c as CaveatCode, text: caveatText(c) })),
    codeVersion: r.code_version || 'unknown', ctfMasked: maskCtf,
    source: { contract: 'dash/CONTRACT.md v1', obsSchema: SCHEMA_VERSION, obsDerive: DERIVE_VERSION, dataVersion: 0 },
  };
}
