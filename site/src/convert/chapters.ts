// Port of export.py RunBuilder.chapters: story beats over the packed events, clustered and captioned.
import { CHAPTER_KINDS, EF, EVENT_KINDS } from '../types.js';
import type { ChapterKind, ChapterRec } from '../types.js';
import type { Ev, Graph } from './graph.js';
import { type Dict, cmpTuple, pyRound, trunc } from './py.js';

/** Which beat of a cluster names it (lower = more important). */
const IMPORTANCE: ChapterKind[] = ['leak', 'onset', 'solve', 'firstContact', 'dm', 'post', 'crash', 'contact', 'upvote',
  'persistence', 'end', 'phaseFree', 'boot'];
/** Same-priority, same-kind beats closer than this cluster into one chapter. */
const CLUSTER_MS = 1500;

type Chap = Omit<ChapterRec, 'agent' | 'node' | 'ev'> & { agent?: number | null; node?: number | null; ev?: number | null };
/** The first live-live reply / mention / DM: [t, src node, dst node, kind, its board event]. */
export type Onset = [number, number, number, string, Ev | null];

export function buildChapters(g: Graph, nouns: { post: string; board: string }, onset: Onset | null,
                              red: (s: string) => string, running: boolean): ChapterRec[] {
  const beats = collectBeats(g, nouns, onset, running).map(dropEmpty);
  const ci = (k: string): number => (CHAPTER_KINDS as readonly string[]).indexOf(k);
  beats.sort((x, y) => cmpTuple([x.t, x.p, ci(x.k), x.agent ?? -1], [y.t, y.p, ci(y.k), y.agent ?? -1]));
  const out = cluster(beats);
  for (const c of out) {
    c.label = red(c.label);
    c.caption = red(c.caption);
  }
  out.sort((x, y) => x.t - y.t || x.p - y.p);
  return out as ChapterRec[];
}

function collectBeats(g: Graph, nouns: { post: string; board: string }, onset: Onset | null, running: boolean): Chap[] {
  const { nodes, events: evs } = g;
  const ch: Chap[] = [];
  const beat = (v: Ev, k: ChapterKind, node: number | null, p: Chap['p'], label: string, caption: string): void => {
    ch.push({ t: v.t, k, agent: v.a, node, ev: v.i, p, label, caption });
  };
  const agentNode = (a: number): number | null => g.id(`a${a}`) ?? null;
  const K = (v: Ev): string => EVENT_KINDS[v.k]!;
  const bn = nouns.board;
  const firstContact = new Map<number, Ev>();
  const firstPersist = new Map<number, Ev>();
  let firstPost: Ev | null = null, firstUpvote: Ev | null = null;
  for (const v of evs) {
    const k = K(v), a = v.a;
    if (a >= 0 && ['board', 'read', 'list', 'doc'].includes(k) && !firstContact.has(a)) firstContact.set(a, v);
    if (a >= 0 && v.f & EF.PERSIST && !firstPersist.has(a)) firstPersist.set(a, v);
    if (k === 'post' && firstPost === null) firstPost = v;
    if (k === 'upvote' && firstUpvote === null) firstUpvote = v;
  }
  [...firstContact.values()].sort((x, y) => x.i - y.i).forEach((v, j) => {
    const phase = v.f & EF.FREE ? 'during free time' : 'still on its task';
    if (j === 0) beat(v, 'firstContact', 1, 1, `a${v.a} finds the board`,
                      `a${v.a} wanders onto the ${bn} ${pyRound(v.t / 1000)} s in, ${phase}.`);
    else beat(v, 'contact', 1, 2, `a${v.a} finds the board`, `a${v.a} finds the ${bn} too, ${phase}.`);
  });
  const threadLabel = (n: number): string => {
    const tn = n >= 0 ? nodes[n]!.thread : undefined;
    return tn !== undefined && tn !== null ? nodes[tn]!.label : bn;
  };
  for (const v of evs) {
    const k = K(v), a = v.a;
    if (v.f & EF.SOLVE) beat(v, 'solve', 0, 1, `a${a} solves the vault`, `a${a} cracks the vault at step ${v.s} and enters free time.`);
    if (k === 'post') {
      const tl = threadLabel(v.n);
      if (v.f & EF.LEAK) beat(v, 'leak', v.n, 1, `a${a} posts the flag`, `a${a} posts the secret flag publicly in ${tl}.`);
      else if (v === firstPost) beat(v, 'post', v.n, 1, `a${a} posts in ${tl}`, `a${a} writes its first ${nouns.post} in ${tl}.`);
      else beat(v, 'post', v.n, 3, `a${a} posts in ${tl}`, `a${a} posts in ${tl}.`);
    }
    if (k === 'upvote') {
      const au = v.n >= 0 ? nodes[v.n]!.author : undefined;
      const who = au !== undefined && au !== null ? nodes[au]!.label : 'someone';
      beat(v, 'upvote', v.n, v === firstUpvote ? 2 : 3, `a${a} upvotes ${who}`, `a${a} upvotes a ${nouns.post} by ${who}.`);
    }
    if (k === 'dm') {
      const ll = Boolean(v.f & EF.LIVE_LIVE);
      beat(v, 'dm', v.n, ll ? 1 : 2, `a${a} sends a DM`, `a${a} sends a direct message` + (ll ? ' to another agent.' : '.'));
    }
    if (k === 'crash') beat(v, 'crash', agentNode(a), 1, `a${a} crashes`, `a${a} crashes.`);
  }
  for (const v of firstPersist.values())
    beat(v, 'persistence', agentNode(v.a), 2, `a${v.a} tries to persist`, `a${v.a} tries to keep something running after its session.`);
  const ends = evs.filter((v) => K(v) === 'end' || K(v) === 'crash');
  if (ends.length && !running) {
    const v = ends[ends.length - 1]!;
    beat(v, 'end', agentNode(v.a), 2, 'last agent finishes', `The last agent (a${v.a}) finishes; the run winds down.`);
  }
  if (onset !== null) {
    const [t, s, d, kind, ev] = onset;
    const verb = ({ reply: 'replies to', mention: 'mentions', dm: 'messages' } as Record<string, string>)[kind]!;
    const c: Chap = { t, k: 'onset', agent: (nodes[s]!.agent as number | undefined) ?? null, node: d, p: 1, label: 'Agents start talking',
                      caption: `Agents start talking to each other: ${nodes[s]!.label} ${verb} ${nodes[d]!.label}.` };
    if (ev !== null) c.ev = ev.i;
    ch.push(c);
  }
  return ch;
}

function dropEmpty(c: Chap): Chap {
  const o: Dict = {};
  for (const [k, v] of Object.entries(c)) if (v !== null && v !== undefined) o[k] = v;
  return o as Chap;
}

/** Merge same-priority, same-kind beats < CLUSTER_MS apart (chained) into one chapter named after its most
 *  important member (a solve never disappears into a leak marker); the other captions follow its own. */
function cluster(beats: Chap[]): Chap[] {
  const out: { head: Chap; members: Chap[] }[] = [];
  const open = new Map<string, [{ head: Chap; members: Chap[] }, number]>();
  for (const c of beats) {
    const gk = `${c.p}:${c.k}`;
    const g = open.get(gk);
    if (g && c.t - g[1] < CLUSTER_MS) {
      g[0].members.push(c);
      if (g[0].members.length === 2) g[0].head.merged = 2;   // `merged` sits after the beat's own keys
      open.set(gk, [g[0], c.t]);
      continue;
    }
    const cl = { head: { ...c }, members: [c] };
    out.push(cl);
    open.set(gk, [cl, c.t]);
  }
  const imp = (k: string): number => { const i = (IMPORTANCE as string[]).indexOf(k); return i >= 0 ? i : 99; };
  return out.map(({ head: c, members }) => {
    if (members.length === 1) return c;
    let best = members[0]!;
    for (const m of members) if (cmpTuple([imp(m.k), m.t], [imp(best.k), best.t]) < 0) best = m;
    c.merged = members.length;
    c.t = best.t;
    for (const key of ['k', 'agent', 'node', 'ev'] as const) {
      if (key in best) (c as Dict)[key] = (best as Dict)[key];
      else delete (c as Dict)[key];
    }
    const caps = members.map((m) => m.caption);
    c.caption = trunc([best.caption, ...caps.filter((x) => x !== best.caption)].join(' '), 400);
    c.label = trunc(`${best.label} (+${members.length - 1} more)`, 120);
    return c;
  });
}
