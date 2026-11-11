// Client-side run model (CONTRACT section 6.3): the /timeline response is indexed once into
// per-lane sorted arrays plus prefix sums, so state at any cursor t is a binary search away and
// scrubbing never touches the network.
import type { Graph, Post, RunDetail, Timeline, TimelineEvent } from './types';
import type { StepClass } from './colors';
import { lastLEnum } from './time';

export interface LaneIndex {
  lane: number;
  label: string;
  names: string[];
  steps: TimelineEvent[];   // kind step|idle, sorted by t
  stepT: Float64Array;
  stepClass: StepClass[];
  stepFlag: Uint8Array;     // bit0 persistence, bit1 secret_leak
  cumBoard: Int32Array;     // prefix: board steps up to and including i
  cumEpisodes: Int32Array;
  models: TimelineEvent[];
  modelT: Float64Array;
  board: TimelineEvent[];   // board events of this agent (lane) sorted
  boardT: Float64Array;
  records: TimelineEvent[]; // start/end/crash/api_retry
  posts: Post[];            // live posts by this agent
  postT: Float64Array;
  reqT: Float64Array;       // req|irc_cmd times
  start: number | null; solve: number | null; end: number | null;
}

export interface AgentAt {
  lane: number;
  status: 'not_started' | 'running' | 'done' | 'crashed' | 'timed_out';
  phase: 'task' | 'free' | null;
  stepIdx: number;            // index into lane.steps (-1 none)
  step: TimelineEvent | null;
  model: TimelineEvent | null;
  cum: { steps: number; board_steps: number; episodes: number; posts: number; requests: number };
  share5: number | null;
}

export interface BoardPostAt extends Post { upvotes: number; isNew: boolean }
export interface ThreadAt { thread: string; title: string; is_seed: boolean; posts: BoardPostAt[]; lastT: number }

const WRITE_KINDS = new Set(['post', 'dm', 'upvote', 'delete']);
export const BOARD_KINDS = new Set(['req', 'post', 'upvote', 'delete', 'dm', 'irc_cmd', 'irc_join', 'connect', 'disconnect', 'board_start']);

export function stepClassOf(e: TimelineEvent, writeSteps: Set<string> | null): StepClass {
  if (e.kind === 'idle' || e.cat === 'idle') return 'idle';
  if (e.cat === 'submit') return 'submit';
  if (e.is_board || e.cat === 'board') {
    if (writeSteps && e.step != null && writeSteps.has(e.lane + ':' + e.step)) return 'write';
    if (/-X\s*(POST|PUT|DELETE)|--data|PRIVMSG|append=|-d\s/.test(e.label)) return 'write';
    return 'read';
  }
  return 'cmd';
}

export class RunModel {
  tl: Timeline;
  detail: RunDetail;
  graph: Graph | null = null;
  lanes: LaneIndex[] = [];
  boardLane: TimelineEvent[] = [];   // every board event (all agents) for the board lane
  boardLaneT = new Float64Array();
  allT = new Float64Array();         // every event t (for next/prev event, density)
  posts: Post[] = [];                // all posts sorted by t (seed first, negative t)
  postT = new Float64Array();
  upvotes: { t: number; src: string; post_uid: string }[] = [];
  tMin = 0;
  tMax = 1;
  maxTokens = 1;
  maxStep = 1;
  swarmOnset: number | null = null;
  liveOnset: number | null = null;   // swarm_onset detection received over SSE (live runs)
  firstContacts: { lane: number; t: number; step: number | null }[] = [];
  edgesLL: { t: number }[] = [];
  threadTitles = new Map<string, string>();

  constructor(tl: Timeline, detail: RunDetail) {
    this.tl = tl;
    this.detail = detail;
    for (const th of detail.threads) this.threadTitles.set(th.thread, th.title || th.thread);
    this.rebuild();
  }

  get iface(): string { return this.detail.run.iface || 'forum'; }
  get live(): boolean { return this.tl.status === 'live' || this.tl.status === 'collecting'; }

  /** Append live events (SSE) and re-index. Cheap enough for live runs (< 50k events). */
  append(evs: TimelineEvent[]) {
    const seen = new Set(this.tl.events.map((e) => e.ref + '|' + e.kind));
    let added = 0;
    const phaseOf = (lane: number) => {
      let ph = this.tl.phases.find((p) => p.agent_id === lane);
      if (!ph) { ph = { agent_id: lane, start: null, solve: null, end: null }; this.tl.phases.push(ph); }
      return ph;
    };
    for (const e of evs) {
      // server-derived detections (src: 'derived') carry state, not lane marks
      if (e.kind === 'solve' && e.lane >= 0 && e.t != null) { const ph = phaseOf(e.lane); if (ph.solve == null) { ph.solve = e.t; added++; } continue; }
      if (e.kind === 'swarm_onset' && e.t != null) { if (this.liveOnset == null || e.t < this.liveOnset) { this.liveOnset = e.t; added++; } continue; }
      if (e.kind === 'model' && e.tokens == null && e.completion_tokens != null) e.tokens = e.completion_tokens;
      if (!['step', 'idle', 'model', 'start', 'end', 'crash', 'api_retry'].includes(e.kind) && !BOARD_KINDS.has(e.kind)) continue;
      const k = e.ref + '|' + e.kind;
      if (seen.has(k)) continue;
      seen.add(k);
      this.tl.events.push(e);
      if (e.kind === 'post' && e.uid && !this.tl.posts.some((p) => p.uid === e.uid)) {
        const m = e.label.match(/^(.*?) @(\S+): ([\s\S]*)$/);
        this.tl.posts.push({ uid: e.uid, post_id: Number(e.uid.slice(1)) || null, kind: 'post', is_seed: false, thread: m?.[2] ?? null,
          author: m?.[1] ?? null, author_node: 'a' + e.lane, agent_id: e.lane, t: e.t, upvotes0: 0, body: m?.[3] ?? e.label, flags: e.flags ?? [], step: e.step ?? null });
      }
      if (e.kind === 'start' && e.lane >= 0) { const ph = phaseOf(e.lane); if (ph.start == null) ph.start = e.t; }
      if (e.kind === 'step' && e.cat === 'submit' && e.phase === 'free' && e.lane >= 0) { const ph = phaseOf(e.lane); if (ph.solve == null) ph.solve = e.t_end ?? e.t; }
      if ((e.kind === 'end' || e.kind === 'crash') && e.lane >= 0) phaseOf(e.lane).end = e.t;
      added++;
    }
    if (added) {
      this.tl.events.sort((a, b) => a.t - b.t);
      this.tl.t_end = Math.max(this.tl.t_end ?? 0, this.tl.events.length ? this.tl.events[this.tl.events.length - 1].t : 0);
      this.rebuild();
    }
    return added;
  }

  rebuild() {
    const tl = this.tl;
    const evs = tl.events.filter((e) => e.t != null && Number.isFinite(e.t)).sort((a, b) => a.t - b.t);
    for (const e of evs) if (e.kind === 'model' && e.tokens == null && e.completion_tokens != null) e.tokens = e.completion_tokens;
    this.allT = Float64Array.from(evs.map((e) => e.t));
    // writes per (lane, step): board events of kind post/dm/upvote/delete or target_kind write
    const writeSteps = new Set<string>();
    const leakSteps = new Set<string>();
    for (const e of evs) {
      if (!BOARD_KINDS.has(e.kind) || e.step == null) continue;
      if (WRITE_KINDS.has(e.kind) || e.cat === 'write') writeSteps.add(e.lane + ':' + e.step);
      if (e.flags?.includes('secret_leak')) leakSteps.add(e.lane + ':' + e.step);
    }
    const laneIds = new Set<number>(tl.lanes.filter((l) => l.lane >= 0).map((l) => l.lane));
    for (const a of this.detail.agents) laneIds.add(a.agent_id);
    for (const e of evs) if (e.lane >= 0) laneIds.add(e.lane);
    const ids = [...laneIds].sort((a, b) => a - b);
    const postsSorted = [...tl.posts].sort((a, b) => (a.t ?? -Infinity) - (b.t ?? -Infinity));
    this.posts = postsSorted;
    this.postT = Float64Array.from(postsSorted.map((p) => p.t ?? -Infinity));
    this.upvotes = [...tl.upvotes].sort((a, b) => a.t - b.t);
    this.lanes = ids.map((id) => {
      const meta = tl.lanes.find((l) => l.lane === id);
      const mine = evs.filter((e) => e.lane === id);
      const steps = mine.filter((e) => e.kind === 'step' || e.kind === 'idle');
      const models = mine.filter((e) => e.kind === 'model');
      const board = mine.filter((e) => BOARD_KINDS.has(e.kind));
      const records = mine.filter((e) => ['start', 'end', 'crash', 'api_retry'].includes(e.kind));
      const cls = steps.map((s) => stepClassOf(s, writeSteps));
      const flag = new Uint8Array(steps.length);
      const cumBoard = new Int32Array(steps.length), cumEp = new Int32Array(steps.length);
      let cb = 0, ce = 0, prev = false;
      steps.forEach((s, i) => {
        const isB = !!s.is_board || s.cat === 'board';
        if (isB) cb++;
        if (isB && !prev) ce++;
        prev = isB;
        cumBoard[i] = cb; cumEp[i] = ce;
        if (/nohup|\bwhile\s+true|crontab|\bat\s+now|systemd-run|(^|[^&])&\s*$/.test(s.label) || s.cat === 'persistence') flag[i] |= 1;
        if (s.step != null && leakSteps.has(id + ':' + s.step)) flag[i] |= 2;
      });
      const ph = tl.phases.find((p) => p.agent_id === id);
      const ag = this.detail.agents.find((a) => a.agent_id === id);
      const myPosts = postsSorted.filter((p) => !p.is_seed && p.agent_id === id);
      return {
        lane: id, label: meta?.label ?? 'a' + id, names: meta?.names ?? ag?.display_names ?? [],
        steps, stepT: Float64Array.from(steps.map((s) => s.t)), stepClass: cls, stepFlag: flag, cumBoard, cumEpisodes: cumEp,
        models, modelT: Float64Array.from(models.map((m) => m.t)), board, boardT: Float64Array.from(board.map((b) => b.t)), records,
        posts: myPosts, postT: Float64Array.from(myPosts.map((p) => p.t ?? 0)),
        reqT: Float64Array.from(board.filter((b) => b.kind === 'req' || b.kind === 'irc_cmd').map((b) => b.t)),
        start: ph?.start ?? ag?.start_t ?? (steps[0]?.t ?? null), solve: ph?.solve ?? ag?.solve_t ?? null, end: ph?.end ?? ag?.end_t ?? null,
      } as LaneIndex;
    });
    this.boardLane = evs.filter((e) => BOARD_KINDS.has(e.kind) && e.kind !== 'connect' && e.kind !== 'disconnect');
    this.boardLaneT = Float64Array.from(this.boardLane.map((e) => e.t));
    const ts = evs.map((e) => e.t);
    this.tMin = Math.min(0, ...this.lanes.map((l) => l.start ?? 0));
    this.tMax = Math.max(this.tMin + 1, tl.t_end ?? 0, ts.length ? ts[ts.length - 1] : 1);
    let mt = 1; for (const e of evs) if (e.kind === 'model' && (e.tokens ?? 0) > mt) mt = e.tokens!;  // no spread: large runs overflow the arg stack
    this.maxTokens = mt;
    this.maxStep = Math.max(1, ...this.lanes.map((l) => l.steps.length ? (l.steps[l.steps.length - 1].step ?? l.steps.length) : 1));
    this.firstContacts = this.lanes.flatMap((l) => {
      const i = l.steps.findIndex((s) => s.is_board || s.cat === 'board');
      return i < 0 ? [] : [{ lane: l.lane, t: l.steps[i].t, step: l.steps[i].step ?? null }];
    });
    const onset = this.detail.run.metrics?.swarm_onset_t;
    this.swarmOnset = typeof onset === 'number' ? onset : this.liveOnset ?? this.computeOnset();
    this.rebuildEdges();
  }

  setGraph(g: Graph) { this.graph = g; this.rebuildEdges(); if (this.swarmOnset == null) this.swarmOnset = this.computeOnset(); }

  private rebuildEdges() {
    const g = this.graph;
    // One entry per interaction event (matches server live_live_edges = count of live-live interaction rows).
    // Falls back to first_t repeated n times when the server omits per-event `ts`.
    this.edgesLL = g ? g.edges.filter((e) => e.src.startsWith('a') && e.dst.startsWith('a') && e.src !== e.dst)
      .flatMap((e) => (e.ts && e.ts.length ? e.ts.map((t) => ({ t: t ?? e.first_t })) : Array.from({ length: e.n || 1 }, () => ({ t: e.first_t }))))
      .sort((a, b) => a.t - b.t) : [];
  }

  private computeOnset(): number | null {
    const g = this.graph;
    if (!g) return null;
    const ts = g.edges.filter((e) => e.src.startsWith('a') && e.dst.startsWith('a') && e.src !== e.dst && ['reply', 'mention', 'dm'].includes(e.kind)).map((e) => e.first_t);
    return ts.length ? Math.min(...ts) : null;
  }

  lane(id: number): LaneIndex | undefined { return this.lanes.find((l) => l.lane === id); }

  agentAt(l: LaneIndex, t: number): AgentAt {
    const i = lastLEnum(l.stepT, t);
    const mi = lastLEnum(l.modelT, t);
    const ag = this.detail.agents.find((a) => a.agent_id === l.lane);
    let status: AgentAt['status'] = 'running';
    if (l.start == null || t < l.start) status = 'not_started';
    else if (l.end != null && t >= l.end) status = ag?.crashed ? 'crashed' : ag?.status === 'timed_out' ? 'timed_out' : 'done';
    const phase = status === 'not_started' ? null : l.solve != null && t >= l.solve ? 'free' : 'task';
    let share5: number | null = null;
    if (i >= 0) {
      const lo = Math.max(0, i - 4);
      const b = l.cumBoard[i] - (lo > 0 ? l.cumBoard[lo - 1] : 0);
      share5 = b / (i - lo + 1);
    }
    return {
      lane: l.lane, status, phase, stepIdx: i, step: i >= 0 ? l.steps[i] : null, model: mi >= 0 ? l.models[mi] : null,
      cum: { steps: i + 1, board_steps: i >= 0 ? l.cumBoard[i] : 0, episodes: i >= 0 ? l.cumEpisodes[i] : 0, posts: lastLEnum(l.postT, t) + 1, requests: lastLEnum(l.reqT, t) + 1 },
      share5,
    };
  }

  /** Board contents at t: threads with posts t <= cursor, upvotes accumulated, new = within 30 s. */
  boardAt(t: number): ThreadAt[] {
    const n = lastLEnum(this.postT, t) + 1;
    const upCount = new Map<string, number>();
    for (const u of this.upvotes) { if (u.t > t) break; upCount.set(u.post_uid, (upCount.get(u.post_uid) ?? 0) + 1); }
    const threads = new Map<string, ThreadAt>();
    // keep seed thread order stable
    for (const th of this.detail.threads) if (th.is_seed) threads.set(th.thread, { thread: th.thread, title: th.title || th.thread, is_seed: true, posts: [], lastT: -Infinity });
    for (let i = 0; i < n; i++) {
      const p = this.posts[i];
      const key = p.kind === 'dm' ? 'dm:' + (p.thread ?? '') : p.thread ?? '(none)';
      let th = threads.get(key);
      if (!th) { th = { thread: key, title: this.threadTitles.get(key) ?? (p.kind === 'dm' ? 'DM → ' + p.thread : key), is_seed: false, posts: [], lastT: -Infinity }; threads.set(key, th); }
      th.posts.push({ ...p, upvotes: p.upvotes0 + (upCount.get(p.uid) ?? 0), isNew: (p.t ?? -Infinity) > t - 30 && !p.is_seed });
      th.lastT = Math.max(th.lastT, p.t ?? -Infinity);
    }
    return [...threads.values()].filter((th) => th.posts.length || th.is_seed);
  }

  totalsAt(t: number) {
    let started = 0, discovered = 0, solved = 0, posts = 0, reqs = 0;
    for (const l of this.lanes) {
      const a = this.agentAt(l, t);
      if (a.status !== 'not_started') started++;
      if (a.cum.board_steps > 0) discovered++;
      if (l.solve != null && t >= l.solve) solved++;
      posts += a.cum.posts; reqs += a.cum.requests;
    }
    let edges = 0; for (const e of this.edgesLL) { if (e.t > t) break; edges++; }
    return { started, discovered, solved, posts, requests: reqs, live_live_edges: edges };
  }

  nextEventT(t: number, dir: 1 | -1, filter?: (e: TimelineEvent) => boolean): number | null {
    const evs = this.tl.events;
    if (filter) {
      if (dir > 0) { for (const e of evs) if (e.t > t + 1e-6 && filter(e)) return e.t; }
      else for (let i = evs.length - 1; i >= 0; i--) if (evs[i].t < t - 1e-6 && filter(evs[i])) return evs[i].t;
      return null;
    }
    const a = this.allT;
    if (dir > 0) { const i = lastLEnum(a, t + 1e-6) + 1; return i < a.length ? a[i] : null; }
    const i = lastLEnum(a, t - 1e-6); return i >= 0 ? a[i] : null;
  }

  /** Reply / mention / DM arcs between lanes: one per live-live interaction event of the server's
   *  interaction graph (same source as the graph panel and the live-live totals). Before /graph has loaded
   *  it falls back to a client-side inference from the post sequence. */
  arcs(): { from: number; to: number; t: number; kind: 'reply' | 'mention' | 'dm' }[] {
    const out: { from: number; to: number; t: number; kind: 'reply' | 'mention' | 'dm' }[] = [];
    const g = this.graph;
    if (g) {
      const aid = (n: string) => (/^a\d+$/.test(n) ? Number(n.slice(1)) : null);
      for (const e of g.edges) {
        if (e.kind !== 'reply' && e.kind !== 'mention' && e.kind !== 'dm') continue;
        const from = aid(e.src), to = aid(e.dst);
        if (from == null || to == null || from === to) continue;
        const ts = e.ts && e.ts.length ? e.ts.map((t) => t ?? e.first_t) : Array.from({ length: e.n || 1 }, () => e.first_t);
        for (const t of ts) out.push({ from, to, t, kind: e.kind });
      }
      return out.sort((a, b) => a.t - b.t);
    }
    const names = this.lanes.map((l) => ({ lane: l.lane, names: l.names.filter((n) => n.length >= 4) }));
    const lastByThread = new Map<string, Post[]>();
    for (const p of this.posts) {
      const prior = lastByThread.get(p.thread ?? '') ?? [];
      if (!p.is_seed && p.agent_id != null) {
        for (let i = prior.length - 1; i >= 0; i--) {
          const q = prior[i];
          if (q.author_node === p.author_node) continue;
          if (q.agent_id != null) out.push({ from: p.agent_id, to: q.agent_id, t: p.t ?? 0, kind: 'reply' });
          break;
        }
        const body = (p.body ?? '').toLowerCase();
        for (const n of names) if (n.lane !== p.agent_id && n.names.some((nm) => body.includes(nm.toLowerCase()))) out.push({ from: p.agent_id, to: n.lane, t: p.t ?? 0, kind: 'mention' });
      }
      prior.push(p);
      lastByThread.set(p.thread ?? '', prior);
    }
    return out;
  }

  /** Event density histogram over [tMin, tMax]. */
  density(bins: number): Uint32Array {
    const h = new Uint32Array(bins);
    const span = this.tMax - this.tMin || 1;
    for (const t of this.allT) { const b = Math.min(bins - 1, Math.max(0, Math.floor(((t - this.tMin) / span) * bins))); h[b]++; }
    return h;
  }
}
