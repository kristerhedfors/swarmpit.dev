import { BundleError, EF, EVENT_KINDS, EVENT_FAMILY, NODE_KINDS } from '../types.js';
import type {
  AgentState, CoreBundle, DetailBundle, EventKind, EventText, NodeStats, RunCard, RunModel, SwarmState, ThreadView,
} from '../types.js';

/** First index i in [lo, hi) with a[i] > x. */
export function upperBound(a: ArrayLike<number>, x: number, lo = 0, hi = a.length): number {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if ((a[m] as number) <= x) lo = m + 1; else hi = m;
  }
  return lo;
}
/** First index i in [lo, hi) with a[i] >= x. */
export function lowerBound(a: ArrayLike<number>, x: number, lo = 0, hi = a.length): number {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if ((a[m] as number) < x) lo = m + 1; else hi = m;
  }
  return lo;
}

const K = (name: EventKind): number => EVENT_KINDS.indexOf(name);
export const KI = {
  start: K('start'), end: K('end'), crash: K('crash'), think: K('think'), cmd: K('cmd'), board: K('board'),
  submit: K('submit'), idle: K('idle'), read: K('read'), list: K('list'), doc: K('doc'), miss: K('miss'),
  post: K('post'), upvote: K('upvote'), del: K('delete'), dm: K('dm'),
};
const NK = { agent: NODE_KINDS.indexOf('agent'), thread: NODE_KINDS.indexOf('thread'), post: NODE_KINDS.indexOf('post') };
const isStep = (k: number): boolean => k === KI.cmd || k === KI.board || k === KI.submit || k === KI.idle;
const isRead = (k: number): boolean => k === KI.read || k === KI.list || k === KI.doc;

export const FAMILY_ICON: Record<string, string> = {
  lifecycle: '●', think: '…', task: '$', boardStep: '◆', idle: 'z', read: '→', write: '✎', social: '♥', irc: '#',
};
const VERB: Partial<Record<EventKind, string>> = {
  boardStart: 'board opens', start: 'starts', end: 'ends', crash: 'CRASH', retry: 'API retry', think: 'thinks',
  cmd: '$', board: '$', submit: 'SUBMIT', idle: 'idles', read: 'reads', list: 'lists', doc: 'reads docs', miss: 'miss',
  post: 'POST', upvote: '▲', delete: 'DELETE', dm: 'DM', join: 'JOIN', connect: 'connects', disconnect: 'disconnects', proto: 'IRC',
};

interface AgentIdx {
  ev: Uint32Array; evT: Float64Array;
  stepEv: Uint32Array; stepT: Float64Array; boardCum: Uint16Array;
  thinkT: Float64Array; tokCum: Float64Array; // 3 per think (prompt, completion, reasoning), cumulative
  postT: Float64Array;
}

export function buildModel(card: RunCard, core: CoreBundle): RunModel {
  const N = core.nodes.length, E = core.edges.length, V = core.events.t.length, A = core.agents.length;
  const ev = core.events, fi = core.fires, S = core.strings;
  const tEnd = core.run.tEndMs;

  const nodeKind = new Uint8Array(N), nodeT0 = new Float64Array(N), nodeT1 = new Float64Array(N);
  const nodeById = new Map<string, number>();
  const nodeAgent = new Int16Array(N).fill(-1);
  core.nodes.forEach((n, i) => {
    nodeKind[i] = n.k;
    nodeT0[i] = n.t0 ?? -Infinity;
    nodeT1[i] = n.t1 ?? Infinity;
    nodeById.set(n.id, i);
    if (n.k === NK.agent && n.agent !== undefined) nodeAgent[i] = n.agent;
  });
  core.agents.forEach((a) => { if (a.node >= 0 && a.node < N) nodeAgent[a.node] = a.id; });

  const edgeSrc = new Uint16Array(E), edgeDst = new Uint16Array(E), edgeKind = new Uint8Array(E);
  core.edges.forEach((e, i) => { edgeSrc[i] = e.s; edgeDst[i] = e.d; edgeKind[i] = e.k; });

  const evT = Float64Array.from(ev.t), evK = Uint8Array.from(ev.k), evA = Int16Array.from(ev.a);
  const evN = Int32Array.from(ev.n), evE = Int32Array.from(ev.e), evF = Uint16Array.from(ev.f);
  const evS = Int32Array.from(ev.s), evX = Int32Array.from(ev.x), evL = Int32Array.from(ev.l), evD = Float64Array.from(ev.d);

  // Fires -> CSR by edge, sorted by t inside each slice.
  const F = fi.e.length;
  const order = Array.from({ length: F }, (_, i) => i).sort((x, y) => (fi.e[x]! - fi.e[y]!) || (fi.t[x]! - fi.t[y]!) || x - y);
  const fireStart = new Uint32Array(E + 1), fireT = new Float64Array(F), fireW = new Float32Array(F), fireEv = new Int32Array(F);
  const fireWCum = new Float64Array(F + 1);
  for (let j = 0; j < F; j++) {
    const i = order[j]!;
    fireT[j] = fi.t[i]!; fireW[j] = fi.w[i]!; fireEv[j] = fi.ev[i]!;
    fireStart[fi.e[i]! + 1]!++;
    fireWCum[j + 1] = fireWCum[j]! + fireW[j]!;
  }
  for (let e = 0; e < E; e++) fireStart[e + 1]! += fireStart[e]!;
  const fireEdge = new Uint32Array(F);
  for (let e = 0; e < E; e++) for (let j = fireStart[e]!; j < fireStart[e + 1]!; j++) fireEdge[j] = e;
  const edgeFirstT = new Float64Array(E);
  for (let e = 0; e < E; e++) edgeFirstT[e] = fireStart[e + 1]! > fireStart[e]! ? fireT[fireStart[e]!]! : Infinity;
  const fireByT = Uint32Array.from(Array.from({ length: F }, (_, i) => i).sort((x, y) => fireT[x]! - fireT[y]! || x - y));
  const fireByTT = Float64Array.from(fireByT, (j) => fireT[j]!);

  // Per node / per agent event lists.
  const nl: number[][] = Array.from({ length: N }, () => []);
  const al: number[][] = Array.from({ length: A }, () => []);
  for (let i = 0; i < V; i++) {
    const n = evN[i]!, a = evA[i]!;
    if (n >= 0 && n < N) nl[n]!.push(i);
    if (a >= 0 && a < A) {
      al[a]!.push(i);
      const an = core.agents[a]!.node;
      if (an >= 0 && an < N && an !== n) nl[an]!.push(i);
    }
  }
  const nodeEvents = nl.map((l) => Uint32Array.from(l.sort((x, y) => x - y)));
  const agentEvents = al.map((l) => Uint32Array.from(l));

  // Posts per thread in board order (node order is seed then live by t).
  const threadPosts: number[][] = Array.from({ length: N }, () => []);
  core.nodes.forEach((n, i) => { if (n.k === NK.post && n.thread !== undefined && n.thread < N) threadPosts[n.thread]!.push(i); });

  // Agent indices.
  const AI: AgentIdx[] = agentEvents.map((evs) => {
    const steps: number[] = [], thinks: number[] = [], posts: number[] = [];
    for (const i of evs) {
      const k = evK[i]!;
      if (isStep(k)) steps.push(i); else if (k === KI.think) thinks.push(i); else if (k === KI.post) posts.push(i);
    }
    const boardCum = new Uint16Array(steps.length + 1);
    steps.forEach((i, j) => { boardCum[j + 1] = boardCum[j]! + (evK[i] === KI.board || (evF[i]! & EF.BOARD) ? 1 : 0); });
    return {
      ev: evs, evT: Float64Array.from(evs, (i) => evT[i]!),
      stepEv: Uint32Array.from(steps), stepT: Float64Array.from(steps, (i) => evT[i]!), boardCum,
      thinkT: Float64Array.from(thinks, (i) => evT[i]!), tokCum: new Float64Array(thinks.length * 3 + 3),
      postT: Float64Array.from(posts, (i) => evT[i]!),
    };
  });
  const thinkEv = AI.map((x, a) => Uint32Array.from(agentEvents[a]!.filter((i) => evK[i] === KI.think)));
  function fillTokens(d: DetailBundle | null): void {
    AI.forEach((x, a) => {
      const rec = core.agents[a]!, n = x.thinkT.length, th = thinkEv[a]!;
      for (let j = 0; j < n; j++) {
        const it = d ? d.items[evX[th[j]!]!] : undefined;
        let p: number, c: number, r: number;
        if (it && it.t === 'think') { p = it.tok[0]; c = it.tok[1]; r = it.tok[2]; }
        else { const div = Math.max(1, rec.modelCalls || n); p = rec.tokens.prompt / div; c = rec.tokens.completion / div; r = rec.tokens.reasoning / div; }
        x.tokCum[(j + 1) * 3] = x.tokCum[j * 3]! + p;
        x.tokCum[(j + 1) * 3 + 1] = x.tokCum[j * 3 + 1]! + c;
        x.tokCum[(j + 1) * 3 + 2] = x.tokCum[j * 3 + 2]! + r;
      }
    });
  }
  fillTokens(null);

  // Live-live fires sorted by t (swarm clusters).
  const llEdges = core.edges.map((e, i) => (e.liveLive ? i : -1)).filter((i) => i >= 0);
  const llFires = Uint32Array.from(Array.from(fireByT).filter((j) => core.edges[fireEdge[j]!]!.liveLive));
  const llFireT = Float64Array.from(llFires, (j) => fireT[j]!);
  const W = Math.min(60000, Math.max(10000, tEnd / 8));
  const onset = card.stats?.swarmOnsetMs ?? core.chapters.find((c) => c.k === 'onset')?.t ?? null;

  let lower: string[] | null = null;
  let detailLower: string[] | null = null;
  let postLower: Map<number, string> | null = null;

  const m: RunModel & { evS: Int32Array; evX: Int32Array; evD: Float64Array; evL: Int32Array; nodeAgent: Int16Array;
    threadPosts: number[][]; fireEdge: Uint32Array; fireByTT: Float64Array } = {
    card, core, tEnd, nNodes: N, nEdges: E, nEvents: V, nAgents: A,
    nodeKind, nodeT0, nodeT1, nodeById, edgeSrc, edgeDst, edgeKind, edgeFirstT,
    evT, evK, evA, evN, evE, evF, fireStart, fireT, fireW, fireEv, fireByT, nodeEvents, agentEvents,
    evS, evX, evD, evL, nodeAgent, threadPosts, fireEdge, fireByTT,
    detail: null,
    attachDetail(d) {
      if (d.hash !== core.hash) throw new BundleError('detail hash mismatch', card.detail, 'hash');
      (m as { detail: DetailBundle | null }).detail = d;
      detailLower = null; postLower = null;
      fillTokens(d);
    },
    label: (i) => S[evL[i]!] ?? '',
    eventAt: (t) => upperBound(evT, t) - 1,
    nextEvent(t, dir, pass) {
      if (dir > 0) {
        for (let i = upperBound(evT, t); i < V; i++) if (!pass || pass(i)) return i;
      } else {
        for (let i = lowerBound(evT, t) - 1; i >= 0; i--) if (!pass || pass(i)) return i;
      }
      return -1;
    },
    edgeWeightAt(e, t) {
      const a = fireStart[e]!, b = fireStart[e + 1]!;
      const j = upperBound(fireT, t, a, b);
      return { n: j - a, w: fireWCum[j]! - fireWCum[a]!, lastT: j > a ? fireT[j - 1]! : -Infinity };
    },
    driftAt(a, t) {
      const d = core.agents[a]?.drift;
      if (!d || !d.t.length) return 0;
      const j = upperBound(d.t, t);
      if (j === 0) return 0;
      if (j >= d.t.length) return d.a[d.t.length - 1]!;
      const t0 = d.t[j - 1]!, t1 = d.t[j]!, a0 = d.a[j - 1]!, a1 = d.a[j]!;
      return t1 > t0 ? a0 + (a1 - a0) * ((t - t0) / (t1 - t0)) : a1;
    },
    agentState(a, t, out) {
      const rec = core.agents[a]!, x = AI[a]!;
      const o: AgentState = out ?? { id: a, status: 'not_started', phase: 'task', step: 0, calls: 0, solved: false, boardSteps: 0,
        posts: 0, boardShare: 0, tokens: [0, 0, 0], lastStep: -1, lastThink: -1, lastEvent: -1, drift: 0 };
      o.id = a;
      const js = upperBound(x.stepT, t), jt = upperBound(x.thinkT, t), je = upperBound(x.evT, t);
      o.lastStep = js > 0 ? x.stepEv[js - 1]! : -1;
      o.lastThink = jt > 0 ? thinkEv[a]![jt - 1]! : -1;
      o.lastEvent = je > 0 ? x.ev[je - 1]! : -1;
      o.step = o.lastStep >= 0 && evS[o.lastStep]! >= 0 ? evS[o.lastStep]! : js;
      o.calls = jt;
      o.boardSteps = x.boardCum[js]!;
      const w = Math.min(5, js);
      o.boardShare = w ? (x.boardCum[js]! - x.boardCum[js - w]!) / w : 0;
      o.posts = upperBound(x.postT, t);
      o.tokens[0] = x.tokCum[jt * 3]!; o.tokens[1] = x.tokCum[jt * 3 + 1]!; o.tokens[2] = x.tokCum[jt * 3 + 2]!;
      o.solved = rec.solveT !== null && rec.solveT <= t;
      o.phase = o.solved || (o.lastStep >= 0 && evF[o.lastStep]! & EF.FREE) ? 'free' : 'task';
      o.drift = m.driftAt(a, t);
      const started = rec.startT !== null ? rec.startT <= t : je > 0;
      if (!started) o.status = 'not_started';
      else if (rec.endT !== null && rec.endT <= t) o.status = rec.status === 'running' ? 'done' : rec.status;
      else if (o.lastStep >= 0 && evK[o.lastStep] === KI.idle) o.status = 'dozing';
      else o.status = 'running';
      return o;
    },
    nodeStats(node, t) {
      const st: NodeStats = { reads: 0, readers: [], replies: 0, upvotes: core.nodes[node]?.upvotes0 ?? 0, posts: 0 };
      const evs = nodeEvents[node]!, kind = nodeKind[node]!;
      const end = evs.length;
      const seen = new Set<number>();
      for (let j = 0; j < end; j++) {
        const i = evs[j]!;
        if (evT[i]! > t) break;
        if (evN[i] !== node) continue;
        const k = evK[i]!;
        if (isRead(k)) { st.reads++; if (evA[i]! >= 0) seen.add(evA[i]!); }
        else if (k === KI.upvote) st.upvotes++;
      }
      st.readers = [...seen].sort((p, q) => p - q);
      if (kind === NK.thread) {
        for (const p of threadPosts[node]!) if (nodeT0[p]! <= t && t < nodeT1[p]!) st.posts++;
        st.replies = Math.max(0, st.posts - 1);
      } else if (kind === NK.post) {
        const th = core.nodes[node]!.thread;
        if (th !== undefined) for (const p of threadPosts[th]!) if (p !== node && nodeT0[p]! > nodeT0[node]! && nodeT0[p]! <= t && t < nodeT1[p]!) st.replies++;
      } else if (kind === NK.agent) {
        const a = nodeAgent[node]!;
        if (a >= 0) st.posts = upperBound(AI[a]!.postT, t);
      } else {
        for (let p = 0; p < N; p++) if (core.nodes[p]!.author === node && nodeT0[p]! <= t) st.posts++;
      }
      return st;
    },
    threadAt(th, t): ThreadView {
      const posts: ThreadView['posts'] = [];
      for (const p of threadPosts[th] ?? []) {
        if (!(nodeT0[p]! <= t && t < nodeT1[p]!)) continue;
        let up = core.nodes[p]!.upvotes0 ?? 0;
        for (const i of nodeEvents[p]!) { if (evT[i]! > t) break; if (evK[i] === KI.upvote && evN[i] === p) up++; }
        posts.push({ node: p, upvotes: up, isNew: Number.isFinite(nodeT0[p]!) && t - nodeT0[p]! < 30000 });
      }
      return { posts };
    },
    swarmAt(t, out) {
      const o: SwarmState = out ?? { onBoard: [], clusters: [], liveLive: 0, onsetT: null };
      o.onBoard.length = 0; o.clusters.length = 0;
      for (let a = 0; a < A; a++) {
        const r = core.agents[a]!;
        if (r.startT !== null && r.startT > t) continue;
        if (r.endT !== null && r.endT <= t) continue;
        if (m.driftAt(a, t) >= 0.5) o.onBoard.push(a);
      }
      let ll = 0;
      for (const e of llEdges) if (edgeFirstT[e]! <= t) ll++;
      o.liveLive = ll;
      // Union-find over agents with live-live fires in (t - W, t].
      const par = new Int16Array(A).map((_, i) => i);
      const find = (x: number): number => { while (par[x] !== x) x = par[x] = par[par[x]!]!; return x; };
      const lo = upperBound(llFireT, t - W), hi = upperBound(llFireT, t);
      const touched = new Uint8Array(A);
      for (let j = lo; j < hi; j++) {
        const e = fireEdge[llFires[j]!]!;
        const a = nodeAgent[edgeSrc[e]!]!, b = nodeAgent[edgeDst[e]!]!;
        if (a < 0 || b < 0 || a === b) continue;
        touched[a] = touched[b] = 1;
        par[find(a)] = find(b);
      }
      const groups = new Map<number, number[]>();
      for (let a = 0; a < A; a++) if (touched[a]) { const r = find(a); (groups.get(r) ?? groups.set(r, []).get(r)!).push(a); }
      for (const g of groups.values()) if (g.length >= 2) o.clusters.push(g);
      o.onsetT = onset !== null && onset <= t ? onset : null;
      return o;
    },
    describe(i): EventText {
      const k = EVENT_KINDS[evK[i]!] ?? 'cmd';
      const a = evA[i]!, n = evN[i]!;
      const who = a >= 0 ? `a${a}` : 'board';
      let lab = S[evL[i]!] ?? '';
      const lw = lab.split(/[\s·]/, 1)[0]!.toLowerCase();
      if (lw && (lw === k.toLowerCase() || lw === (VERB[k] ?? '').toLowerCase())) lab = lab.slice(lw.length).replace(/^[\s·:]+/, '');
      const nodeLab = n >= 0 ? core.nodes[n]?.label ?? '' : '';
      let short: string;
      if (k === 'post' || k === 'dm') {
        const nd = core.nodes[n];
        const th = nd?.thread !== undefined ? core.nodes[nd.thread]?.label ?? '' : '';
        short = `${who} ${VERB[k]} ${th}${nd?.preview ? `: "${nd.preview.slice(0, 60)}"` : lab ? ': ' + lab : ''}`;
      } else if (k === 'upvote') short = `${who} ▲ ${nodeLab || lab}`;
      else if (k === 'cmd' || k === 'board') short = `${who} $ ${lab}`;
      else short = `${who} ${VERB[k] ?? k}${lab ? ' ' + lab : nodeLab && k !== 'start' && k !== 'end' ? ' ' + nodeLab : ''}`;
      const flags = evF[i]!;
      const tags = [flags & EF.LEAK ? 'LEAK' : '', flags & EF.SOLVE ? 'SOLVED' : '', flags & EF.ERROR ? 'error' : '',
        flags & EF.HEURISTIC ? 'inferred' : '', flags & EF.LIVE_LIVE ? 'live↔live' : ''].filter(Boolean);
      return { icon: FAMILY_ICON[EVENT_FAMILY[k]] ?? '•', short, long: short + (tags.length ? ` [${tags.join(', ')}]` : ''), agent: a, node: n };
    },
    search(q) {
      const nodes = new Uint8Array(N);
      q = q.trim().toLowerCase();
      if (!q) return { nodes, events: new Uint32Array(0) };
      const d = m.detail;
      lower ??= S.map((s) => s.toLowerCase());
      if (d && !detailLower) {
        detailLower = d.items.map((it) => (it.t === 'step' ? it.cmd + '\n' + it.out : it.t === 'think' ? it.reason + '\n' + it.content
          : it.t === 'req' ? it.method + ' ' + it.path : it.text).toLowerCase());
        postLower = new Map();
        for (const id in d.posts) { const ni = nodeById.get(id); if (ni !== undefined) postLower.set(ni, d.posts[id]!.body.toLowerCase()); }
      }
      core.nodes.forEach((n, i) => {
        if (n.label.toLowerCase().includes(q) || (n.title ?? '').toLowerCase().includes(q) || (n.preview ?? '').toLowerCase().includes(q)
          || (n.names ?? []).some((s) => s.toLowerCase().includes(q)) || n.id.toLowerCase() === q || postLower?.get(i)?.includes(q)) nodes[i] = 1;
      });
      const hits: number[] = [];
      for (let i = 0; i < V; i++) {
        const n = evN[i]!, x = evX[i]!;
        if (lower[evL[i]!]!.includes(q) || (n >= 0 && nodes[n]) || (detailLower && x >= 0 && detailLower[x]?.includes(q))) hits.push(i);
      }
      return { nodes, events: Uint32Array.from(hits) };
    },
  };
  return m;
}

/** Extra (non-interface) fields exposed by buildModel for the UI layer. */
export type Model = ReturnType<typeof buildModel> & { evS: Int32Array; evX: Int32Array; evD: Float64Array; evL: Int32Array;
  nodeAgent: Int16Array; threadPosts: number[][]; fireEdge: Uint32Array; fireByTT: Float64Array };
export const ext = (m: RunModel): Model => m as Model;
