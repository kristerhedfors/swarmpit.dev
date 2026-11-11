import { EDGE_KINDS, EF, EVENT_KINDS, NODE_KINDS, STEP_CATS } from '../types.js';
import type { AgentState, Detail, NodeKind, RunModel } from '../types.js';
import { ext, upperBound, KI } from '../data/model.js';
import { h, agentVar } from '../util/dom.js';
import { explain } from './glossary.js';
import { fmtDur, fmtNum, fmtT, oneLine, pct, seedAgo, trunc } from '../util/format.js';

export const kindOf = (m: RunModel, n: number): NodeKind => NODE_KINDS[m.nodeKind[n]!] ?? 'post';
export const detailOf = (m: RunModel, ev: number): Detail | undefined => {
  const x = ext(m).evX[ev]!;
  return x >= 0 ? m.detail?.items[x] : undefined;
};

/** Text with [start,end) ranges wrapped in <mark class="leak">. */
export function withSpans(body: string, spans: [number, number][], max = Infinity): (Node | string)[] {
  const out: (Node | string)[] = [];
  let i = 0;
  const lim = Math.min(body.length, max);
  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0])) {
    if (a >= lim) break;
    if (a > i) out.push(body.slice(i, a));
    out.push(h('mark.leak', null, body.slice(Math.max(a, i), Math.min(b, lim))));
    i = Math.max(i, Math.min(b, lim));
  }
  if (i < lim) out.push(body.slice(i, lim));
  if (lim < body.length) out.push('…');
  return out;
}

const FLAG_RE = /FLAG\{[^}\s]{1,64}\}?/g;
/** Leak spans for a post (detail when loaded, else a FLAG{…} heuristic over the preview). */
export function postSpans(m: RunModel, node: number, text: string): [number, number][] {
  const d = m.detail?.posts[m.core.nodes[node]?.id ?? ''];
  if (d && text === d.body) return d.leakSpans;
  if (!(m.core.nodes[node]?.flags ?? []).includes('secret_leak')) return [];
  const out: [number, number][] = [];
  for (const mm of text.matchAll(FLAG_RE)) out.push([mm.index ?? 0, (mm.index ?? 0) + mm[0].length]);
  return out;
}

export const chip = (txt: string, cls = '', style = ''): HTMLElement => h('span.ichip', { class: `ichip ${cls}`, style: style || null }, txt);
export const agentChip = (a: number, label = `a${a}`): HTMLElement => chip(label, 'agent', `--chip:${agentVar(a)}`);
/** Key/value row; the key gets an ⓘ explanation when the glossary has one (`xk` overrides the lookup key). */
export const row = (k: string, ...v: (Node | string | null)[]): HTMLElement => xrow(k, k, ...v);
export const xrow = (k: string, xk: string, ...v: (Node | string | null)[]): HTMLElement =>
  h('div.irow', null, h('span.ik', null, k, explain(xk, k)), h('span.iv', null, ...v));

export function nodeAuthor(m: RunModel, n: number): HTMLElement | string {
  const nd = m.core.nodes[n];
  if (!nd || nd.author === undefined) return '';
  const au = m.core.nodes[nd.author];
  if (!au) return '';
  const a = au.agent ?? (kindOf(m, nd.author) === 'agent' ? Number(au.id.slice(1)) : -1);
  return a >= 0 ? agentChip(a, au.label) : chip(au.label, 'persona');
}

export const flagChips = (flags: string[] | undefined): HTMLElement[] =>
  (flags ?? []).flatMap((f) => {
    const l = f === 'secret_leak' ? 'LEAK' : f.replace('_', ' ');
    return [chip(l, f === 'secret_leak' ? 'leakchip' : ''), explain(l)].filter((x): x is HTMLElement => x !== null);
  });

export function stepText(m: RunModel, ev: number): { cmd: string; out: string; exit: number | null; via?: string } {
  const d = detailOf(m, ev);
  if (d && d.t === 'step') return { cmd: d.cmd, out: d.out, exit: d.exit, via: d.via };
  return { cmd: m.label(ev), out: '', exit: m.evF[ev]! & EF.ERROR ? 1 : null };
}
export function thinkText(m: RunModel, ev: number): string {
  const d = detailOf(m, ev);
  if (d && d.t === 'think') return oneLine(d.reason || d.content);
  return m.label(ev);
}
export const catOf = (m: RunModel, ev: number): string => STEP_CATS[m.core.events.c[ev] ?? -1] ?? EVENT_KINDS[m.evK[ev]!] ?? '';

const STATUS: Record<AgentState['status'], string> = { not_started: 'not started', running: 'running', dozing: 'idle', done: 'done', crashed: 'crashed', timed_out: 'timed out' };

export function agentBlock(m: RunModel, a: number, t: number, full: boolean): HTMLElement[] {
  const r = m.core.agents[a]!, st = m.agentState(a, t);
  const out: HTMLElement[] = [
    h('div.ihead', null, agentChip(a), h('span.iname', null, r.names.join(', ')),
      chip(STATUS[st.status], 'st-' + st.status), explain('status:' + st.status, STATUS[st.status]),
      chip(st.phase === 'free' ? 'FREE' : 'TASK', 'ph-' + st.phase), explain('phase:' + st.phase, st.phase === 'free' ? 'FREE' : 'TASK')),
    row('model', r.model),
    row('step', `${st.step}/${m.core.run.harness.budget}`, h('span.muted', null, ` · ${st.calls} model calls`)),
    row('tokens', `${fmtNum(st.tokens[0])} in · ${fmtNum(st.tokens[1])} out · ${fmtNum(st.tokens[2])} reasoning`),
    row('board', `${st.boardSteps} steps · share ${pct(st.boardShare)} (last 5)`),
    row('solved', st.solved && r.solveT !== null ? `at ${fmtT(r.solveT)} (step ${r.solveStep ?? '?'})`
      : m.core.run.goal === 'unsolvable' ? 'never: unsolvable goal' : 'not yet'),
  ];
  const s = st.lastStep >= 0 ? stepText(m, st.lastStep) : null;
  if (s && s.cmd) {
    out.push(h('div.icode', null, h('span.muted', null, '$ '), trunc(oneLine(s.cmd), full ? 600 : 140),
      s.exit !== null ? h('span.muted', null, `  → exit ${s.exit}`) : null));
  }
  if (st.lastThink >= 0) {
    const th = thinkText(m, st.lastThink);
    if (th) out.push(h('div.ithink', null, trunc(th, full ? 900 : 160)));
  }
  return out;
}

export function threadBlock(m: RunModel, n: number, t: number): HTMLElement[] {
  const nd = m.core.nodes[n]!, st = m.nodeStats(n, t), posts = ext(m).threadPosts[n] ?? [];
  const first = posts[0];
  return [
    h('div.ihead', null, chip(m.core.run.nouns.thread, 'kind'), h('span.iname', null, nd.title ?? nd.label)),
    row('created by', first !== undefined ? nodeAuthor(m, first) : '–'),
    row(m.core.run.nouns.posts, String(st.posts)),
    row('readers', ...(st.readers.length ? st.readers.map((a) => agentChip(a)) : ['none yet']), st.reads ? h('span.muted', null, ` · ${st.reads} reads`) : null),
  ];
}

export function postBlock(m: RunModel, n: number, t: number, full: boolean): HTMLElement[] {
  const nd = m.core.nodes[n]!, st = m.nodeStats(n, t);
  const body = full ? m.detail?.posts[nd.id]?.body ?? nd.preview ?? '' : nd.preview ?? '';
  const when = nd.seed && nd.tSeed !== undefined ? seedAgo(nd.tSeed) : Number.isFinite(m.nodeT0[n]!) ? `posted at ${fmtT(m.nodeT0[n]!)}` : 'seed';
  const th = nd.thread !== undefined ? m.core.nodes[nd.thread] : undefined;
  return [
    h('div.ihead', null, nodeAuthor(m, n), h('span.muted', null, when)),
    th ? row('in', th.title ?? th.label) : h('span'),
    h('blockquote.ibody', null, ...withSpans(body, postSpans(m, n, body), full ? Infinity : 240)),
    row('', `▲ ${st.upvotes} · ${st.reads} reads · ${st.replies} replies`, explain('▲', 'upvotes · reads · replies'), ...flagChips(nd.flags)),
    m.nodeT1[n]! <= t ? row('', chip('deleted', 'muted')) : h('span'),
  ];
}

export function taskBlock(m: RunModel, t: number): HTMLElement[] {
  const solved = m.core.agents.filter((a) => a.solveT !== null && a.solveT <= t);
  let subs = 0;
  const end = upperBound(m.evT, t);
  for (let i = 0; i < end; i++) if (m.evK[i] === KI.submit) subs++;
  const works = EDGE_KINDS.indexOf('works');
  const per = m.core.agents.map((a) => {
    let n = 0;
    for (let e = 0; e < m.nEdges; e++) if (m.edgeKind[e] === works && m.edgeSrc[e] === a.node) n += m.edgeWeightAt(e, t).n;
    return n;
  });
  return [
    h('div.ihead', null, chip('task', 'kind'), h('span.iname', null, m.core.nodes[0]?.label ?? 'CTF vault')),
    row('solved', `${solved.length}/${m.nAgents}`, ...solved.map((a) => agentChip(a.id))),
    row('submissions', String(subs)),
    row('task steps', ...per.map((n, a) => h('span.iper', null, agentChip(a), ` ${n} `))),
  ];
}

export function boardBlock(m: RunModel, t: number): HTMLElement[] {
  const c: Record<string, number> = { read: 0, list: 0, doc: 0, miss: 0, post: 0, upvote: 0 };
  const end = upperBound(m.evT, t);
  for (let i = 0; i < end; i++) { const k = EVENT_KINDS[m.evK[i]!]!; if (k in c) c[k]!++; }
  const r = m.core.run;
  return [
    h('div.ihead', null, chip(r.nouns.board, 'kind'), h('span.iname', null, `${r.boardName} · ${r.nouns.board}`)),
    row('requests', `${c['read']! + c['list']! + c['doc']! + c['miss']!} to now`),
    row('', `${c['read']} reads · ${c['list']} listings · ${c['doc']} docs · ${c['miss']} misses`),
    row('writes', `${c['post']} ${r.nouns.posts} · ${c['upvote']} upvotes`),
  ];
}

export function stepBlock(m: RunModel, ev: number, full: boolean): HTMLElement[] {
  const s = stepText(m, ev), a = m.evA[ev]!;
  const lines = s.out.split('\n');
  const outShown = full ? s.out : lines.slice(0, 6).join('\n') + (lines.length > 6 ? '\n…' : '');
  return [
    h('div.ihead', null, agentChip(a), h('span.iname', null, `step ${ext(m).evS[ev]}`), chip(catOf(m, ev), 'cat', `--chip:var(--cat-${catOf(m, ev)})`), explain('cat:' + catOf(m, ev)),
      h('span.muted', null, `${fmtT(m.evT[ev]!)} · ${fmtDur(ext(m).evD[ev]!)}`)),
    h('pre.icmd', null, '$ ' + (full ? s.cmd : trunc(s.cmd, 300))),
    outShown ? h('pre.iout', null, outShown) : h('div.muted', null, m.detail ? '(no output)' : 'output loads with the detail bundle…'),
    row('', s.exit !== null ? `exit ${s.exit}` : '', s.via ? chip(`via ${s.via}`, 'muted') : null, s.via ? explain(`via ${s.via}`) : null,
      m.evF[ev]! & EF.FREE ? chip('FREE', 'ph-free') : null, m.evF[ev]! & EF.FREE ? explain('phase:free', 'FREE') : null),
  ];
}

export function edgeBlock(m: RunModel, e: number, t: number): HTMLElement[] {
  const rec = m.core.edges[e]!, k = EDGE_KINDS[m.edgeKind[e]!]!, w = m.edgeWeightAt(e, t);
  const sl = m.core.nodes[m.edgeSrc[e]!]?.label ?? '?', dl = m.core.nodes[m.edgeDst[e]!]?.label ?? '?';
  return [
    h('div.ihead', null, chip(k, 'edge', `--chip:var(--edge-${k})`), explain('edge:' + k, `${k} edge`), h('span.iname', null, `${sl} → ${dl}`),
      rec.liveLive ? chip('live↔live', 'll') : null, rec.liveLive ? explain('live↔live') : null),
    row('fires', `${w.n} to now · Σw ${fmtNum(Math.round(w.w * 100) / 100)}`, explain('Σw', 'Σw (total weight)'),
      w.n ? h('span.muted', null, ` · last ${fmtT(w.lastT)}`) : null, w.n ? explain('last', 'last firing') : null),
    rec.inferred || rec.ev ? row('evidence', (rec.inferred ? 'inferred · ' : '') + (m.core.strings[rec.ev ?? 0] || 'heuristic')) : h('span'),
  ];
}
