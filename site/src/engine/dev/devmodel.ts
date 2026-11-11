// Engine test double (SPEC 1: "B uses a local test double inside src/engine/ that is never shipped").
// synthCore() generates a deterministic CoreBundle; devModel() is a minimal RunModel over it.
// Used only by public/engine-demo.html and tools/test/engine.test.mjs. The site uses src/data/model.ts.
// DOM-free.

import type { AgentRec, AgentState, CoreBundle, DetailBundle, EventText, NodeRec, NodeStats, RunCard, RunModel,
  SwarmState, ThreadView, EventKind, EdgeKind, StepCat } from '../../types.js';
import { CORE_FORMAT, EDGE_KINDS, EF, EVENT_KINDS, NODE_KINDS, STEP_CATS } from '../../types.js';
import { clamp, prng, upperBound } from '../math.js';

interface Ev { t: number; d: number; k: EventKind; a: number; n: string | null; f: number; s: number; c: StepCat | null; l: string; seq: number }
interface Fi { s: string; d: string; k: EdgeKind; t: number; w: number; ev: Ev | null; inferred?: boolean }

export interface SynthOpts { seed?: number; agents?: number; threads?: number; durationMs?: number; personas?: number; budget?: number }

export function synthCore(o: SynthOpts = {}): CoreBundle {
  const rnd = prng(o.seed ?? 7);
  const nA = o.agents ?? 4, J = o.threads ?? 6, tEnd = o.durationMs ?? 90000, nP = o.personas ?? 4, budget = o.budget ?? 40;
  const pick = <T>(xs: T[]): T => xs[Math.floor(rnd() * xs.length)]!;
  const evs: Ev[] = [], fires: Fi[] = [];
  let seq = 0;
  const ev = (e: Omit<Ev, 'seq'>) => { const x = { ...e, seq: seq++ }; evs.push(x); return x; };
  const threads = Array.from({ length: J }, (_, j) => `th:topic-${j}`);
  const personas = Array.from({ length: nP }, (_, i) => `seed:persona_${i}`);
  const seedPosts: { id: string; thread: string; author: string; tSeed: number }[] = [];
  let pid = 1;
  for (const th of threads) {
    const k = 1 + Math.floor(rnd() * 3);
    for (let i = 0; i < k; i++) seedPosts.push({ id: `s${pid++}`, thread: th, author: pick(personas), tSeed: -Math.round((1 + rnd() * 5) * 3600e3) });
  }
  const livePosts: { id: string; thread: string; agent: number; t: number; leak: boolean }[] = [];
  const agents: AgentRec[] = [];
  ev({ t: 0, d: 0, k: 'boardStart', a: -1, n: 'board', f: 0, s: -1, c: null, l: 'board up', });
  for (const th of threads) fires.push({ s: 'board', d: th, k: 'contains', t: 0, w: 1, ev: evs[0]! });
  for (const sp of seedPosts) {
    fires.push({ s: sp.thread, d: sp.id, k: 'contains', t: 0, w: 1, ev: evs[0]! });
    fires.push({ s: sp.author, d: sp.id, k: 'authored', t: 0, w: 1, ev: evs[0]! });
  }
  for (let a = 0; a < nA; a++) {
    const me = `a${a}`;
    let t = Math.round(800 + rnd() * 3000);
    const startT = t;
    ev({ t, d: 0, k: 'start', a, n: me, f: 0, s: -1, c: null, l: `${me} starts` });
    const solveAt = rnd() < 0.8 ? Math.round(tEnd * (0.2 + rnd() * 0.35)) : Infinity;
    let solved = false, step = 0, drift = 0, solveT: number | null = null, solveStep: number | null = null;
    const dt: number[] = [], da: number[] = [];
    let posts = 0, boardSteps = 0, firstContact: number | null = null;
    while (step < budget && t < tEnd - 3000) {
      const lat = Math.round(300 + rnd() * 1200);
      ev({ t, d: lat, k: 'think', a, n: null, f: solved ? EF.FREE : 0, s: step, c: null, l: 'model call' });
      t += lat;
      step++;
      const free = solved;
      const pBoard = free ? 0.65 : 0.12 + drift * 0.3;
      let b = 0;
      const dur = Math.round(200 + rnd() * 1500);
      if (!solved && t >= solveAt) {
        const e = ev({ t, d: dur, k: 'submit', a, n: 'task', f: EF.SOLVE | EF.SUBMIT_OK, s: step, c: 'submit', l: 'submit FLAG{…}' });
        fires.push({ s: me, d: 'task', k: 'works', t, w: 1, ev: e });
        solved = true; solveT = t; solveStep = step;
      } else if (rnd() < pBoard) {
        b = 1; boardSteps++;
        if (firstContact === null) firstContact = t;
        const th = pick(threads);
        ev({ t, d: dur, k: 'board', a, n: th, f: EF.BOARD | (free ? EF.FREE : 0), s: step, c: 'board', l: `curl board.internal/api/v1/threads/${th.slice(3)}` });
        const r = ev({ t: t + 40, d: 0, k: 'read', a, n: th, f: free ? EF.FREE : 0, s: -1, c: null, l: `GET /api/v1/threads/${th.slice(3)}` });
        fires.push({ s: me, d: th, k: 'reads', t: t + 40, w: 1, ev: r });
        if (free && rnd() < 0.3) {
          const id = `p${pid++}`, leak = rnd() < 0.25;
          const pe = ev({ t: t + 80, d: 0, k: 'post', a, n: id, f: (free ? EF.FREE : 0) | (leak ? EF.LEAK : 0), s: -1, c: null, l: `POST ${th.slice(3)}` });
          livePosts.push({ id, thread: th, agent: a, t: t + 80, leak });
          posts++;
          fires.push({ s: me, d: th, k: 'writes', t: t + 80, w: 1, ev: pe });
          fires.push({ s: me, d: id, k: 'authored', t: t + 80, w: 1, ev: pe });
          fires.push({ s: th, d: id, k: 'contains', t: t + 80, w: 1, ev: pe });
          const others = livePosts.filter((p) => p.thread === th && p.agent !== a && p.t < t);
          if (others.length) fires.push({ s: me, d: `a${pick(others).agent}`, k: rnd() < 0.6 ? 'reply' : 'mention', t: t + 80, w: 1, ev: pe, inferred: rnd() < 0.5 });
          else {
            const sp = seedPosts.filter((p) => p.thread === th);
            if (sp.length) fires.push({ s: me, d: pick(sp).author, k: 'reply', t: t + 80, w: 0.5, ev: pe, inferred: true });
          }
        }
      } else if (rnd() < 0.08) {
        ev({ t, d: dur, k: 'idle', a, n: null, f: free ? EF.FREE : 0, s: step, c: 'idle', l: 'sleep 30' });
      } else {
        const cat = pick(['ctf', 'ctf', 'recon', 'other', 'persistence'] as StepCat[]);
        const e = ev({ t, d: dur, k: 'cmd', a, n: 'task', f: (free ? EF.FREE : 0) | (cat === 'persistence' ? EF.PERSIST : 0), s: step, c: cat,
          l: pick(['ls -la ~/ctf', 'cat ~/ctf/README.md', 'git log --oneline', 'python3 solve.py', 'find / -name "*.key"']) });
        fires.push({ s: me, d: 'task', k: 'works', t, w: 1, ev: e });
      }
      drift = 0.65 * drift + 0.35 * b;
      dt.push(t); da.push(Math.round(drift * 1000) / 1000);
      t += dur + Math.round(100 + rnd() * 600);
    }
    ev({ t, d: 0, k: 'end', a, n: me, f: 0, s: -1, c: null, l: `${me} ends` });
    agents.push({ id: a, node: 2 + a, names: [`agent-${a}`], model: 'synthetic', status: 'done', startT, endT: t, solveT, solveStep,
      stepsUsed: step, modelCalls: step, ended: 'budget', tokens: { prompt: step * 1200, completion: step * 90, cached: 0, reasoning: step * 30 },
      metrics: { discovered: firstContact !== null, firstContact, boardSteps, shareAfterContact: null, posts, episodes: 1, secretLeak: false },
      drift: { t: dt, a: da } });
  }
  const kr = (k: EventKind) => (k === 'think' ? 0 : k === 'read' || k === 'post' ? 3 : 1);
  evs.sort((p, q) => p.t - q.t || p.a - q.a || kr(p.k) - kr(q.k) || p.seq - q.seq);
  const evIndex = new Map<Ev, number>(evs.map((e, i) => [e, i]));
  livePosts.sort((p, q) => p.t - q.t);
  // nodes in SPEC order
  const nodes: NodeRec[] = [{ id: 'task', k: 0, label: 'CTF vault', t0: null }, { id: 'board', k: 1, label: 'agent-commons · forum', t0: null }];
  agents.forEach((ag) => nodes.push({ id: `a${ag.id}`, k: 2, label: `a${ag.id}`, t0: ag.startT, agent: ag.id, names: ag.names }));
  threads.forEach((th) => nodes.push({ id: th, k: 3, label: th.slice(3), t0: null, title: th.slice(3) }));
  const idx = () => new Map(nodes.map((n, i) => [n.id, i]));
  let ix = idx();
  const firstNode = nodes.length;
  seedPosts.forEach((sp) => nodes.push({ id: sp.id, k: 4, label: `#${sp.id.slice(1)} ${sp.author.slice(5)}`, t0: null, seed: true, tSeed: sp.tSeed,
    thread: ix.get(sp.thread)!, postId: +sp.id.slice(1), preview: 'If your vault will not open, check the git history first…', bodyLen: 300, upvotes0: 1 }));
  livePosts.forEach((lp) => nodes.push({ id: lp.id, k: 4, label: `#${lp.id.slice(1)} a${lp.agent}`, t0: lp.t, agent: lp.agent, thread: ix.get(lp.thread)!,
    postId: +lp.id.slice(1), preview: lp.leak ? 'Flag verified: FLAG{0000synthetic0000} …' : 'Anyone else stuck on the vault? Here is what worked for me…',
    bodyLen: 200, ...(lp.leak ? { flags: ['secret_leak' as const] } : {}) }));
  personas.forEach((p) => nodes.push({ id: p, k: 5, label: p.slice(5), t0: null }));
  ix = idx();
  void firstNode;
  // authors
  for (const n of nodes) {
    if (n.k !== 4) continue;
    if (n.seed) n.author = ix.get(seedPosts.find((s) => s.id === n.id)!.author)!;
    else n.author = ix.get(`a${n.agent}`)!;
  }
  // edges
  const edges: CoreBundle['edges'] = [];
  const ekey = new Map<string, number>();
  const fe: number[] = [], ft: number[] = [], fw: number[] = [], fev: number[] = [];
  for (const f of fires) {
    const s = ix.get(f.s), d = ix.get(f.d);
    if (s === undefined || d === undefined) continue;
    const k = EDGE_KINDS.indexOf(f.k), key = `${s},${d},${k}`;
    let e = ekey.get(key);
    if (e === undefined) {
      e = edges.length; ekey.set(key, e);
      const rec: CoreBundle['edges'][number] = { s, d, k };
      if (f.inferred) rec.inferred = true;
      if (nodes[s]!.k === 2 && nodes[d]!.k === 2) rec.liveLive = true;
      edges.push(rec);
    }
    fe.push(e); ft.push(f.t); fw.push(f.w); fev.push(f.ev ? evIndex.get(f.ev)! : -1);
  }
  const ord = fe.map((_, i) => i).sort((p, q) => fe[p]! - fe[q]! || ft[p]! - ft[q]!);
  const strings = [''];
  const sidx = new Map<string, number>([['', 0]]);
  const str = (s: string) => { let i = sidx.get(s); if (i === undefined) { i = strings.length; strings.push(s); sidx.set(s, i); } return i; };
  const E = { t: [] as number[], d: [] as number[], k: [] as number[], a: [] as number[], n: [] as number[], e: [] as number[],
    f: [] as number[], s: [] as number[], c: [] as number[], l: [] as number[], x: [] as number[] };
  evs.forEach((e) => {
    E.t.push(e.t); E.d.push(e.d); E.k.push(EVENT_KINDS.indexOf(e.k)); E.a.push(e.a); E.n.push(e.n ? ix.get(e.n) ?? -1 : -1);
    E.e.push(-1); E.f.push(e.f); E.s.push(e.s); E.c.push(e.c ? STEP_CATS.indexOf(e.c) : -1); E.l.push(str(e.l)); E.x.push(-1);
  });
  const nb = Math.ceil(tEnd / 1000);
  return {
    format: CORE_FORMAT, hash: 'synth000',
    run: { id: `_synth-${nodes.length}`, iface: 'forum', pop: 'multi', nAgents: nA, rep: 0, tag: 'synthetic', model: 'synthetic',
      harness: { budget, temperature: 0.7, maxTokens: 2048, outputChars: 3000 }, boardName: 'agent-commons',
      nouns: { thread: 'thread', threads: 'threads', post: 'post', posts: 'posts', board: 'forum' },
      tEndMs: Math.max(...E.t, 1), wallMs: tEnd, prehistoryMs: Math.min(0, ...seedPosts.map((s) => s.tSeed)),
      caveats: [{ code: 'synthetic', text: 'Synthetic data for the engine demo.' }], codeVersion: 'dev', ctfMasked: false,
      source: { contract: 'dash/CONTRACT.md v1', obsSchema: 0, obsDerive: 0, dataVersion: 0 } },
    strings, nodes, edges, events: E,
    fires: { e: ord.map((i) => fe[i]!), t: ord.map((i) => ft[i]!), w: ord.map((i) => fw[i]!), ev: ord.map((i) => fev[i]!) },
    agents, chapters: [], segments: [{ t0: 0, t1: tEnd, title: 'Run' }],
    series: { binMs: 1000, density: new Array<number>(nb).fill(0), boardShare: agents.map(() => new Array<number>(nb).fill(0)) },
  };
}

const K = (k: EventKind) => EVENT_KINDS.indexOf(k);
const STEP_K = new Set([K('cmd'), K('board'), K('submit'), K('idle')]);

/** Minimal RunModel over a CoreBundle. Enough for the engine; not the site's model. */
export function devModel(core: CoreBundle, card?: RunCard): RunModel {
  const nN = core.nodes.length, nE = core.edges.length, nV = core.events.t.length, nA = core.agents.length;
  const nodeKind = new Uint8Array(nN), nodeT0 = new Float64Array(nN), nodeT1 = new Float64Array(nN);
  core.nodes.forEach((n, i) => { nodeKind[i] = n.k; nodeT0[i] = n.t0 ?? -Infinity; nodeT1[i] = n.t1 ?? Infinity; });
  const edgeSrc = new Uint16Array(nE), edgeDst = new Uint16Array(nE), edgeKind = new Uint8Array(nE), edgeFirstT = new Float64Array(nE).fill(Infinity);
  core.edges.forEach((e, i) => { edgeSrc[i] = e.s; edgeDst[i] = e.d; edgeKind[i] = e.k; });
  const F = core.fires, nF = F.e.length;
  const fireStart = new Uint32Array(nE + 1);
  for (let i = 0; i < nF; i++) fireStart[F.e[i]! + 1]!++;
  for (let e = 0; e < nE; e++) fireStart[e + 1] = fireStart[e + 1]! + fireStart[e]!;
  const fireT = Float64Array.from(F.t), fireW = Float32Array.from(F.w), fireEv = Int32Array.from(F.ev);
  for (let i = 0; i < nF; i++) { const e = F.e[i]!; if (fireT[i]! < edgeFirstT[e]!) edgeFirstT[e] = fireT[i]!; }
  const fireByT = Uint32Array.from(Array.from({ length: nF }, (_, i) => i).sort((p, q) => fireT[p]! - fireT[q]! || p - q));
  const V = core.events;
  const evT = Float64Array.from(V.t), evK = Uint8Array.from(V.k), evA = Int16Array.from(V.a), evN = Int32Array.from(V.n),
    evE = Int32Array.from(V.e), evF = Uint16Array.from(V.f);
  const nodeEv: number[][] = Array.from({ length: nN }, () => []), agEv: number[][] = Array.from({ length: nA }, () => []);
  for (let i = 0; i < nV; i++) {
    const n = evN[i]!, a = evA[i]!;
    if (n >= 0) nodeEv[n]!.push(i);
    if (a >= 0 && a < nA) { agEv[a]!.push(i); const an = core.agents[a]!.node; if (an !== n) nodeEv[an]!.push(i); }
  }
  const nodeEvents = nodeEv.map((l) => Uint32Array.from(l.sort((p, q) => p - q)));
  const agentEvents = agEv.map((l) => Uint32Array.from(l));
  const W = clamp(core.run.tEndMs / 8, 10000, 60000);
  let detail: DetailBundle | null = null;
  const lastIn = (l: Uint32Array, t: number) => { let lo = 0, hi = l.length; while (lo < hi) { const m = (lo + hi) >>> 1; if (evT[l[m]!]! <= t) lo = m + 1; else hi = m; } return lo - 1; };
  const driftAt = (a: number, t: number) => {
    const d = core.agents[a]?.drift;
    if (!d || !d.t.length) return 0;
    const j = upperBound(d.t, t);
    if (j === 0) return 0;
    if (j >= d.t.length) return d.a[d.t.length - 1]!;
    const t0 = d.t[j - 1]!, t1 = d.t[j]!, a0 = d.a[j - 1]!, a1 = d.a[j]!;
    return a0 + (a1 - a0) * ((t - t0) / Math.max(1, t1 - t0));
  };
  const m: RunModel = {
    card: card ?? ({ id: core.run.id } as RunCard), core, tEnd: core.run.tEndMs,
    nNodes: nN, nEdges: nE, nEvents: nV, nAgents: nA, nodeKind, nodeT0, nodeT1,
    nodeById: new Map(core.nodes.map((n, i) => [n.id, i])),
    edgeSrc, edgeDst, edgeKind, edgeFirstT, evT, evK, evA, evN, evE, evF, fireStart, fireT, fireW, fireEv, fireByT,
    nodeEvents, agentEvents,
    get detail() { return detail; },
    attachDetail(d) { detail = d; },
    label: (i) => core.strings[V.l[i]!] ?? '',
    eventAt: (t) => upperBound(evT, t) - 1,
    nextEvent(t, dir, pass) {
      let i = dir > 0 ? upperBound(evT, t) : upperBound(evT, t - 1e-6) - 1;
      for (; i >= 0 && i < nV; i += dir) if (!pass || pass(i)) return i;
      return -1;
    },
    edgeWeightAt(e, t) {
      const fs = fireStart[e]!, j = upperBound(fireT, t, fs, fireStart[e + 1]!);
      let w = 0; for (let i = fs; i < j; i++) w += fireW[i]!;
      return { n: j - fs, w, lastT: j > fs ? fireT[j - 1]! : -Infinity };
    },
    driftAt,
    agentState(a, t, out) {
      const o: AgentState = out ?? { id: a, status: 'not_started', phase: 'task', step: 0, calls: 0, solved: false, boardSteps: 0, posts: 0,
        boardShare: 0, tokens: [0, 0, 0], lastStep: -1, lastThink: -1, lastEvent: -1, drift: 0 };
      const rec = core.agents[a]!, l = agentEvents[a]!, j = lastIn(l, t);
      o.id = a; o.step = 0; o.calls = 0; o.boardSteps = 0; o.posts = 0; o.lastStep = -1; o.lastThink = -1;
      o.lastEvent = j >= 0 ? l[j]! : -1;
      for (let q = 0; q <= j; q++) {
        const i = l[q]!, k = evK[i]!;
        if (STEP_K.has(k)) { o.step++; o.lastStep = i; if (k === K('board')) o.boardSteps++; }
        else if (k === K('think')) { o.calls++; o.lastThink = i; }
        else if (k === K('post')) o.posts++;
      }
      o.solved = rec.solveT !== null && rec.solveT <= t;
      o.phase = o.solved ? 'free' : 'task';
      const started = rec.startT !== null && rec.startT <= t, ended = rec.endT !== null && rec.endT <= t;
      o.status = !started ? 'not_started' : ended ? rec.status === 'running' ? 'done' : rec.status
        : o.lastStep >= 0 && evK[o.lastStep] === K('idle') && evT[o.lastStep]! + V.d[o.lastStep]! > t ? 'dozing' : 'running';
      o.drift = driftAt(a, t);
      o.boardShare = o.step ? o.boardSteps / o.step : 0;
      o.tokens = [rec.tokens.prompt * (o.calls / Math.max(1, rec.modelCalls)), 0, 0];
      return o;
    },
    nodeStats(node, t): NodeStats {
      let posts = 0, reads = 0;
      const readers = new Set<number>();
      core.nodes.forEach((n, i) => { if (n.thread === node && nodeT0[i]! <= t) posts++; });
      for (const i of nodeEvents[node]!) if (evT[i]! <= t && evK[i] === K('read')) { reads++; readers.add(evA[i]!); }
      return { reads, readers: [...readers], replies: 0, upvotes: core.nodes[node]!.upvotes0 ?? 0, posts };
    },
    threadAt(thread, t): ThreadView {
      const posts: ThreadView['posts'] = [];
      core.nodes.forEach((n, i) => { if (n.thread === thread && nodeT0[i]! <= t) posts.push({ node: i, upvotes: n.upvotes0 ?? 0, isNew: t - nodeT0[i]! < 30000 }); });
      return { posts };
    },
    swarmAt(t, out): SwarmState {
      const o = out ?? { onBoard: [], clusters: [], liveLive: 0, onsetT: null };
      o.onBoard.length = 0; o.clusters.length = 0; o.liveLive = 0; o.onsetT = null;
      for (let a = 0; a < nA; a++) {
        const r = core.agents[a]!;
        if (r.startT !== null && r.startT <= t && !(r.endT !== null && r.endT <= t) && driftAt(a, t) >= 0.5) o.onBoard.push(a);
      }
      const par = Array.from({ length: nA }, (_, i) => i);
      const find = (x: number): number => (par[x] === x ? x : (par[x] = find(par[x]!)));
      const inC = new Uint8Array(nA);
      for (let e = 0; e < nE; e++) {
        if (!core.edges[e]!.liveLive) continue;
        if (edgeFirstT[e]! <= t) o.liveLive++;
        if (o.onsetT === null || edgeFirstT[e]! < o.onsetT) if (edgeFirstT[e]! <= t) o.onsetT = edgeFirstT[e]!;
        const j = upperBound(fireT, t, fireStart[e]!, fireStart[e + 1]!);
        if (j > fireStart[e]! && fireT[j - 1]! > t - W) {
          const a = core.nodes[edgeSrc[e]!]!.agent!, b = core.nodes[edgeDst[e]!]!.agent!;
          par[find(a)] = find(b); inC[a] = 1; inC[b] = 1;
        }
      }
      const groups = new Map<number, number[]>();
      for (let a = 0; a < nA; a++) if (inC[a]) { const r = find(a); if (!groups.has(r)) groups.set(r, []); groups.get(r)!.push(a); }
      for (const g of groups.values()) if (g.length >= 2) o.clusters.push(g);
      return o;
    },
    describe(i): EventText {
      const k = EVENT_KINDS[evK[i]!]!, a = evA[i]!;
      const short = `${a >= 0 ? 'a' + a + ' ' : ''}${k.toUpperCase()} ${core.strings[V.l[i]!] ?? ''}`;
      return { icon: k, short, long: short, agent: a, node: evN[i]! };
    },
    search(q) {
      const s = q.toLowerCase(), nodes = new Uint8Array(nN), ev: number[] = [];
      if (s) {
        core.nodes.forEach((n, i) => { if ((n.label + ' ' + (n.preview ?? '') + ' ' + (n.title ?? '')).toLowerCase().includes(s)) nodes[i] = 1; });
        for (let i = 0; i < nV; i++) if ((core.strings[V.l[i]!] ?? '').toLowerCase().includes(s)) ev.push(i);
      }
      return { nodes, events: Uint32Array.from(ev) };
    },
  };
  void NODE_KINDS;
  return m;
}
