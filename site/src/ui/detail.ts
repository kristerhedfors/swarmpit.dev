import { EDGE_KINDS, EVENT_KINDS, STEP_CATS } from '../types.js';
import type { DetailPanel, PickHit, RunModel } from '../types.js';
import { ext, upperBound, KI } from '../data/model.js';
import { h, s, agentVar } from '../util/dom.js';
import { fmtT, pct, trunc } from '../util/format.js';
import { hitContent } from './tooltip.js';
import { agentChip, chip, kindOf, postSpans, row, thinkText, withSpans, flagChips, nodeAuthor } from './info.js';
import { ICON } from './icons.js';
import { explain } from './glossary.js';

export interface Detail extends DetailPanel {
  readonly el: HTMLElement; readonly hit: PickHit | null;
  setModel(m: RunModel): void;
  onNeighbourhood(cb: (node: number) => void): void;
  /** A link inside the panel asks to pin (and focus) another node. */
  onOpenNode(cb: (node: number) => void): void;
  onClose(cb: () => void): void;
}

const lab = (txt: string, key = txt): HTMLElement => h('div.dp-lab', null, txt, explain(key, txt));

export function createDetail(): Detail {
  let m: RunModel | null = null, hit: PickHit | null = null, t = 0, lastKey = '';
  let seekCb: (t: number) => void = () => undefined, focusCb: (n: number) => void = () => undefined;
  let hoodCb: (n: number) => void = () => undefined, closeCb: () => void = () => undefined, openCb: (n: number) => void = () => undefined;
  // Hits we navigated away from via in-panel links (Back pops them); any outside pin clears it.
  let back: PickHit[] = [], navigating = false;
  const body = h('div.dp-body');
  const title = h('h3.dp-title', { id: 'dp-title' });
  const bFocus = h('button.dp-act', { type: 'button', html: ICON.focus + '<span>Focus</span>' });
  const bHood = h('button.dp-act', { type: 'button', html: ICON.hood + '<span>Only this neighbourhood</span>' });
  const bPrev = h('button.dp-act', { type: 'button', 'aria-label': 'Previous event of this node', html: ICON.prev });
  const bNext = h('button.dp-act', { type: 'button', 'aria-label': 'Next event of this node', html: ICON.next });
  const bClose = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Close panel (Esc)', html: ICON.close });
  const bBack = h('button.tl-btn.sm', { type: 'button', 'aria-label': 'Back', title: 'Back', html: ICON.prev, hidden: true });
  const el = h('aside.dpanel', { 'aria-labelledby': 'dp-title', hidden: true },
    h('div.dp-head', null, bBack, title, h('span.tl-spacer'), bClose), h('div.dp-acts', null, bFocus, bHood, bPrev, bNext), body);

  const nodeOf = (): number => {
    if (!hit || !m) return -1;
    if (hit.kind === 'node') return hit.node;
    if (hit.kind === 'step') { const a = m.evA[hit.event]!; return a >= 0 ? m.core.agents[a]!.node : -1; }
    return m.edgeDst[hit.edge]!;
  };
  bFocus.addEventListener('click', () => { const n = nodeOf(); if (n >= 0) focusCb(n); });
  bHood.addEventListener('click', () => { const n = nodeOf(); if (n >= 0) hoodCb(n); });
  const stepNode = (d: 1 | -1): void => {
    const n = nodeOf();
    if (!m || n < 0) return;
    const evs = m.nodeEvents[n]!;
    const j = d > 0 ? upperBound(Float64Array.from(evs, (i) => m!.evT[i]!), t) : upperBound(Float64Array.from(evs, (i) => m!.evT[i]!), t - 1) - 1;
    const i = evs[j];
    if (i !== undefined) seekCb(m.evT[i]!);
  };
  bPrev.addEventListener('click', () => stepNode(-1));
  bNext.addEventListener('click', () => stepNode(1));
  bClose.addEventListener('click', () => { api.pin(null); closeCb(); });
  const go = (n: number, keepHistory: boolean): void => {
    const prev = hit;
    navigating = true;
    try { openCb(n); } finally { navigating = false; }
    if (keepHistory && prev && hit !== prev) back.push(prev);
    bBack.hidden = !back.length;
  };
  bBack.addEventListener('click', () => {
    const p = back.pop();
    if (!p) return;
    const n = p.kind === 'node' ? p.node : -1;
    if (n >= 0) go(n, false);
    else { navigating = true; api.pin(p); navigating = false; }
    bBack.hidden = !back.length;
  });
  /** One line per post: seek link + a clickable preview that opens the post in this panel. */
  const postLink = (p: number): HTMLElement => {
    const mm = m!, nd = mm.core.nodes[p]!, t0 = mm.nodeT0[p]!;
    const open = h('button.dp-open', { type: 'button', title: 'Open this post', 'aria-label': `Open post: ${trunc(nd.preview ?? nd.label, 60)}` },
      trunc(nd.preview ?? nd.label, 120) || '(empty)');
    open.addEventListener('click', () => { if (mm.nodeT0[p]! > t) seekCb(mm.nodeT0[p]!); go(p, true); });
    return h('div.dp-post', { class: t0 > t ? 'dp-post future' : 'dp-post' },
      nd.seed ? h('span.muted', null, 'seed') : seekLink(t0, fmtT(t0)), ' ', open, ...flagChips(nd.flags));
  };

  const seekLink = (tt: number, label: string): HTMLButtonElement => {
    const b = h('button.seek', { type: 'button', 'aria-label': `Seek to ${fmtT(tt)}` }, label);
    b.addEventListener('click', () => seekCb(tt));
    return b;
  };

  function agentExtra(a: number): HTMLElement[] {
    const mm = m!, r = mm.core.agents[a]!, X = ext(mm);
    const out: HTMLElement[] = [];
    // Board-share sparkline with cursor.
    const bs = mm.core.series.boardShare[a] ?? [];
    if (bs.length) {
      const W = 260, H = 36, n = bs.length;
      const pts = bs.map((v, i) => `${((i + 0.5) / n) * W},${H - 2 - v * (H - 4)}`).join(' ');
      const cx = Math.min(W, (t / Math.max(1, mm.tEnd)) * W);
      out.push(h('div.dp-sec', null, lab('Board share over time'),
        s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'dp-spark', role: 'img', 'aria-label': `Board share, now ${pct(mm.agentState(a, t).boardShare)}` },
          s('polyline', { points: pts, class: 'spark-line', style: `stroke:${agentVar(a)}` }),
          s('line', { x1: cx, x2: cx, y1: 0, y2: H, class: 'spark-cursor' }))));
    }
    // Step ribbon.
    const steps = mm.agentEvents[a]!.filter((i) => { const k = mm.evK[i]!; return k === KI.cmd || k === KI.board || k === KI.submit || k === KI.idle; });
    if (steps.length) {
      const rib = h('div.ribbon', { role: 'list', 'aria-label': 'Steps' });
      for (const i of steps) {
        const cat = STEP_CATS[mm.core.events.c[i] ?? -1] ?? EVENT_KINDS[mm.evK[i]!]!;
        const b = h('button.rib', { type: 'button', role: 'listitem', style: `--chip:var(--cat-${cat})`, title: `step ${X.evS[i]} · ${cat} · ${fmtT(mm.evT[i]!)}: ${trunc(mm.label(i), 80)}`,
          'aria-label': `Step ${X.evS[i]}, ${cat}, at ${fmtT(mm.evT[i]!)}` });
        if (mm.evT[i]! > t) b.classList.add('future');
        b.addEventListener('click', () => seekCb(mm.evT[i]!));
        rib.appendChild(b);
      }
      out.push(h('div.dp-sec', null, lab('Steps (click to seek)'), rib));
    }
    const posts = mm.core.nodes.map((nd, i) => (nd.agent === a && kindOf(mm, i) === 'post' ? i : -1)).filter((i) => i >= 0);
    if (posts.length) out.push(h('div.dp-sec', null, lab(mm.core.run.nouns.posts, 'Posts by this author'), ...posts.map(postLink)));
    const mt = r.metrics;
    out.push(h('div.dp-sec', null, lab('Run metrics (whole run)'),
      row('first contact', mt.firstContact !== null ? seekLink(mt.firstContact, fmtT(mt.firstContact)) : 'never'),
      row('share after contact', mt.shareAfterContact !== null ? pct(mt.shareAfterContact) : '–'),
      row('board episodes', String(mt.episodes)), row('posts', String(mt.posts)),
      row('leaked secret', mt.secretLeak ? chip('LEAK', 'leakchip') : 'no'),
      row('ended', `${r.ended ?? r.status}${r.endT !== null ? ' at ' + fmtT(r.endT) : ''}`)));
    return out;
  }

  function threadExtra(n: number): HTMLElement[] {
    const mm = m!, view = mm.threadAt(n, t);
    if (!view.posts.length) return [h('div.muted', null, 'Empty at this time.')];
    return [h('div.dp-lab', null, `As it was at ${fmtT(t)}`), ...view.posts.map((p) => {
      const nd = mm.core.nodes[p.node]!;
      const body = mm.detail?.posts[nd.id]?.body ?? nd.preview ?? '';
      return h('article.dp-tpost', { class: p.isNew ? 'dp-tpost new' : 'dp-tpost' },
        h('div.ihead', null, nodeAuthor(mm, p.node), h('span.muted', null, nd.seed ? 'seed' : fmtT(mm.nodeT0[p.node]!)),
          p.isNew ? chip('new', 'newchip') : null, p.isNew ? explain('new') : null, h('span.muted', null, `▲ ${p.upvotes}`), ...flagChips(nd.flags)),
        h('div.ibody', null, ...withSpans(body, postSpans(mm, p.node, body), 600)));
    })];
  }

  /** Bait personas and handles: every post they wrote, each one clickable. */
  function authorExtra(n: number): HTMLElement[] {
    const mm = m!;
    const posts = mm.core.nodes.map((nd, i) => (nd.author === n ? i : -1)).filter((i) => i >= 0)
      .sort((a, b) => mm.nodeT0[a]! - mm.nodeT0[b]!);
    if (!posts.length) return [h('div.muted', null, `No ${mm.core.run.nouns.posts} by this author.`)];
    return [h('div.dp-sec', null, lab(mm.core.run.nouns.posts, 'Posts by this author'), ...posts.map(postLink))];
  }

  function postExtra(n: number): HTMLElement[] {
    const mm = m!, out: HTMLElement[] = [];
    const reads = mm.nodeEvents[n]!.filter((i) => mm.evN[i] === n && (mm.evK[i] === KI.read));
    if (reads.length) out.push(h('div.dp-sec', null, lab('Read by'),
      ...Array.from(reads, (i) => h('div', { class: mm.evT[i]! > t ? 'future' : '' }, agentChip(mm.evA[i]!), ' ', seekLink(mm.evT[i]!, fmtT(mm.evT[i]!))))));
    const edges: number[] = [];
    for (let e = 0; e < mm.nEdges; e++) if ((mm.edgeSrc[e] === n || mm.edgeDst[e] === n) && Number.isFinite(mm.edgeFirstT[e]!)) edges.push(e);
    if (edges.length) out.push(h('div.dp-sec', null, lab('Edges'), ...edges.map((e) =>
      h('div', null, chip(EDGE_KINDS[mm.edgeKind[e]!]!, 'edge', `--chip:var(--edge-${EDGE_KINDS[mm.edgeKind[e]!]})`), explain('edge:' + EDGE_KINDS[mm.edgeKind[e]!]), ' ',
        `${mm.core.nodes[mm.edgeSrc[e]!]?.label} → ${mm.core.nodes[mm.edgeDst[e]!]?.label} `, seekLink(mm.edgeFirstT[e]!, fmtT(mm.edgeFirstT[e]!))))));
    return out;
  }

  function stepExtra(ev: number): HTMLElement[] {
    const mm = m!, out: HTMLElement[] = [];
    out.push(h('div.muted.small', null, `The model saw the first ${mm.core.run.harness.outputChars} characters of the output.`));
    const a = mm.evA[ev]!;
    if (a >= 0) {
      const evs = mm.agentEvents[a]!;
      let think = -1;
      for (const i of evs) { if (mm.evT[i]! > mm.evT[ev]!) break; if (mm.evK[i] === KI.think) think = i; }
      if (think >= 0) out.push(h('div.dp-sec', null, lab('Reasoning of the linked model call'), h('div.ithink.full', null, thinkText(mm, think) || '(none)')));
    }
    out.push(row('', h('span', null, ''), (() => { const b = h('button.seek', { type: 'button' }, `Seek to ${fmtT(mm.evT[ev]!)}`); b.addEventListener('click', () => seekCb(mm.evT[ev]!)); return b; })()));
    return out;
  }

  function edgeExtra(e: number): HTMLElement[] {
    const mm = m!, a = mm.fireStart[e]!, b = mm.fireStart[e + 1]!;
    const list: HTMLElement[] = [];
    for (let j = a; j < Math.min(b, a + 40); j++) {
      const ev = mm.fireEv[j]!;
      list.push(h('div', { class: mm.fireT[j]! > t ? 'future' : '' }, seekLink(mm.fireT[j]!, fmtT(mm.fireT[j]!)), ' ', ev >= 0 ? trunc(mm.describe(ev).short, 90) : 'interaction'));
    }
    return [h('div.dp-sec', null, lab('Firings'), ...list)];
  }

  function render(): void {
    if (!m || !hit) return;
    const mm = m;
    let ttl = '', extra: HTMLElement[] = [];
    if (hit.kind === 'node') {
      const n = hit.node, k = kindOf(mm, n);
      ttl = mm.core.nodes[n]?.label ?? '';
      if (k === 'agent') { const a = mm.core.nodes[n]?.agent ?? mm.core.agents.findIndex((r) => r.node === n); if (a >= 0) extra = agentExtra(a); }
      else if (k === 'thread') extra = threadExtra(n);
      else if (k === 'post') extra = postExtra(n);
      else if (k === 'persona' || k === 'handle') extra = authorExtra(n);
    } else if (hit.kind === 'step') { ttl = `a${mm.evA[hit.event]} · step ${ext(mm).evS[hit.event]}`; extra = stepExtra(hit.event); }
    else { ttl = EDGE_KINDS[mm.edgeKind[hit.edge]!] + ' edge'; extra = edgeExtra(hit.edge); }
    title.textContent = ttl;
    const sc = body.scrollTop;
    body.replaceChildren(...hitContent(mm, hit, t, true), ...extra);
    body.scrollTop = sc;
    bHood.hidden = hit.kind === 'edge';
  }

  const api: Detail = {
    el,
    get hit() { return hit; },
    setModel(model) { m = model; api.pin(null); },
    pin(x) {
      if (!navigating) { back = []; bBack.hidden = true; }
      hit = x; lastKey = '';
      el.hidden = !x;
      if (x) { render(); lastKey = renderKey(); }
    },
    setTime(nt) {
      t = nt;
      if (!hit || el.hidden) return;
      const k = renderKey();
      if (k !== lastKey) { lastKey = k; render(); }
    },
    onSeek(cb) { seekCb = cb; }, onFocus(cb) { focusCb = cb; },
    onNeighbourhood(cb) { hoodCb = cb; }, onClose(cb) { closeCb = cb; }, onOpenNode(cb) { openCb = cb; },
  };
  // Re-render only when the set of events <= t changes (or detail attaches): cheap while playing.
  function renderKey(): string { return `${m ? m.eventAt(t) : -1}|${m?.detail ? 1 : 0}|${Math.floor(t / 30000)}`; }
  return api;
}
