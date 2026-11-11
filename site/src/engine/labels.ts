// HTML info tags (SPEC 7.5): pooled, priority-ordered, greedy collision-free placement, depth-faded with
// the same fog as the renderers. Only changed DOM properties are written.
// Hysteresis: a tag shown last frame gets a priority bonus and tries its previous side first, and heat is
// quantised, so with a still camera tags do not flip sides or swap places as playback heat changes.

import type { AgentState, CreateLabels, FrameState, LabelLayer, ProjectedSet, RunModel } from '../types.js';
import { NB, NODE_KINDS } from '../types.js';
import { fogFactor } from './math.js';
import { KIND } from './layout.js';
import type { EngineFrame } from './frame.js';

const STATIC_PRIO = [90, 80, 100, 60, 40, 30, 25]; // by kind; seed posts get 20
const H = 18, PAD = 10;

interface Slot { el: HTMLElement; text: string; tf: string; op: string; cls: string; on: boolean }

export const createLabels: CreateLabels = (host: HTMLElement, max = 64): LabelLayer => {
  const layer = document.createElement('div');
  layer.className = 'tags';
  layer.setAttribute('aria-hidden', 'true');
  layer.style.cssText = 'position:absolute;inset:0;pointer-events:none;overflow:hidden;contain:strict';
  host.appendChild(layer);
  const slots: Slot[] = [];
  for (let i = 0; i < max; i++) {
    const el = document.createElement('div');
    el.className = 'tag';
    el.style.cssText = 'position:absolute;left:0;top:0;white-space:nowrap;will-change:transform,opacity;display:none';
    layer.appendChild(el);
    slots.push({ el, text: '', tf: '', op: '', cls: 'tag', on: false });
  }
  const meas = document.createElement('canvas').getContext('2d');
  const widthCache = new Map<string, number>();
  let font = '';
  const measure = (s: string): number => {
    let w = widthCache.get(s);
    if (w === undefined) {
      if (!font) { font = getComputedStyle(slots[0]!.el).font || '12px system-ui'; if (meas) meas.font = font; }
      w = (meas ? meas.measureText(s).width : s.length * 6.5) + PAD;
      if (widthCache.size > 4000) widthCache.clear();
      widthCache.set(s, w);
    }
    return w;
  };

  let cand = new Int32Array(0), prio = new Float32Array(0), cacheGen = new Int32Array(0);
  let cacheStr: string[] = [];
  let gen = 0, lastEv = -2, lastModel: RunModel | null = null;
  let side = new Int8Array(0), shown = new Uint8Array(0), shownNext = new Uint8Array(0);
  let hullSide = new Map<string, number>(), hullSideNext = new Map<string, number>();
  const STICKY = 25;
  const placed = new Float32Array((max + 8) * 4);
  const st: AgentState = { id: 0, status: 'not_started', phase: 'task', step: 0, calls: 0, solved: false, boardSteps: 0,
    posts: 0, boardShare: 0, tokens: [0, 0, 0], lastStep: -1, lastThink: -1, lastEvent: -1, drift: 0 };

  const content = (m: RunModel, f: FrameState, i: number): string => {
    if (cacheGen[i] === gen) return cacheStr[i]!;
    const rec = m.core.nodes[i]!, k = m.nodeKind[i]!, t = f.t;
    let s = rec.label;
    if (k === KIND.AGENT && rec.agent !== undefined) {
      m.agentState(rec.agent, t, st);
      const nm = rec.names?.[0];
      s = `${rec.label}${nm && nm !== rec.label ? ' · ' + nm : ''} · ${st.phase === 'free' ? 'FREE' : 'TASK'} · ${st.step}/${m.core.run.harness.budget}`;
    } else if (k === KIND.THREAD) {
      s = `#${rec.label} · ${m.nodeStats(i, t).posts}`;
    } else if (k === KIND.POST) {
      const au = rec.author !== undefined ? m.core.nodes[rec.author]?.label ?? '' : '';
      const pv = (rec.preview ?? '').replace(/\s+/g, ' ');
      const up = m.nodeStats(i, t).upvotes;
      s = `${au}: "${pv.length > 36 ? pv.slice(0, 35) + '…' : pv}"${up > 0 ? ' ▲' + up : ''}${rec.flags?.includes('secret_leak') ? ' LEAK' : ''}`;
    } else if (k === KIND.TASK) {
      const fr = (f as EngineFrame).aux?.[i] ?? 0;
      s = `${rec.label} · ${Math.round(fr * m.nAgents)}/${m.nAgents} solved`;
    }
    cacheGen[i] = gen; cacheStr[i] = s;
    return s;
  };

  const show = (sl: Slot, text: string, x: number, y: number, op: number, cls: string) => {
    if (!sl.on) { sl.el.style.display = ''; sl.on = true; }
    if (sl.text !== text) { sl.el.textContent = text; sl.text = text; }
    const tf = `translate3d(${x.toFixed(1)}px,${y.toFixed(1)}px,0)`;
    if (sl.tf !== tf) { sl.el.style.transform = tf; sl.tf = tf; }
    const o = op.toFixed(2);
    if (sl.op !== o) { sl.el.style.opacity = o; sl.op = o; }
    if (sl.cls !== cls) { sl.el.className = cls; sl.cls = cls; }
  };
  const hideFrom = (k: number) => {
    for (let j = k; j < slots.length; j++) { const sl = slots[j]!; if (sl.on) { sl.el.style.display = 'none'; sl.on = false; } }
  };

  return {
    update(p: ProjectedSet, f: FrameState, m: RunModel, s) {
      const n = p.nodeCount;
      if (m !== lastModel) {
        lastModel = m; cand = new Int32Array(n + 64); prio = new Float32Array(n + 64);
        cacheGen = new Int32Array(n).fill(-1); cacheStr = new Array<string>(n).fill(''); lastEv = -2;
        side = new Int8Array(n).fill(-1); shown = new Uint8Array(n); shownNext = new Uint8Array(n); hullSide.clear();
      }
      shownNext.fill(0); hullSideNext.clear();
      const ev = m.eventAt(f.t);
      if (ev !== lastEv) { gen++; lastEv = ev; }
      const W = host.clientWidth, Hh = host.clientHeight;
      const bits = f.nodes.bits;
      let nc = 0;
      for (let i = 0; i < n; i++) {
        if (!p.nodeVisible[i]) continue;
        const b = bits[i]!, k = m.nodeKind[i]!;
        const pinned = i === s.selected || (b & NB.PIN) !== 0, hov = i === s.hover || (b & NB.HOVER) !== 0, hit = (b & NB.MATCH) !== 0;
        if (!s.enabled && !pinned && !hov) continue;
        if (k === KIND.POST && p.nodeR[i]! * 2 < 14 && !pinned && !hov && !hit) continue;
        const x = p.nodeXY[i * 2]!, y = p.nodeXY[i * 2 + 1]!;
        if (x < -40 || y < -20 || x > W + 40 || y > Hh + 20) continue;
        let pr = k === KIND.POST && m.core.nodes[i]!.seed ? 20 : STATIC_PRIO[k] ?? 10;
        pr += (pinned ? 300 : 0) + (hov ? 200 : 0) + (hit ? 150 : 0) + (f.nodes.heat[i]! > 0.5 ? 40 : 0) + (shown[i] ? STICKY : 0);
        if (b & NB.DIM) pr -= 50;
        cand[nc] = i; prio[nc] = pr; nc++;
      }
      // hulls as pseudo-candidates (index -1 - h)
      for (let h = 0; h < f.hulls.length && nc < cand.length; h++) { cand[nc] = -1 - h; prio[nc] = 95; nc++; }
      const idx = cand.subarray(0, nc);
      const pmap = prio;
      // sort candidate positions by priority (stable on node index)
      const ord = Array.from({ length: nc }, (_, j) => j).sort((a, b) => (pmap[b]! - pmap[a]!) || (idx[a]! - idx[b]!));
      let used = 0, np = 0;
      const av = s.avoid, ac = av ? Math.min(s.avoidCount ?? 0, 8) : 0;
      for (let a = 0; a < ac; a++) { placed.set(av!.subarray(a * 4, a * 4 + 4), np * 4); np++; }
      const coarseMax = s.coarse ? Math.min(18, slots.length) : slots.length;
      for (const j of ord) {
        if (used >= coarseMax) break;
        const node = idx[j]!;
        let x: number, y: number, r: number, depth: number, text: string, cls: string, dim = false;
        if (node < 0) {
          const hl = f.hulls[-1 - node]!;
          const mi = hl.members.map((a) => 'a' + a).join(' ');
          text = `swarm · ${mi}`;
          const hx = (f as EngineFrame).hullXY, o = (-1 - node) * 3;
          if (!hx || hx[o + 2]! <= 0) continue;
          x = hx[o]!; y = hx[o + 1]!; r = 2; depth = hx[o + 2]!;
          cls = 'tag tag--hull';
        } else {
          x = p.nodeXY[node * 2]!; y = p.nodeXY[node * 2 + 1]!; r = p.nodeR[node]!; depth = p.nodeDepth[node]!;
          text = content(m, f, node);
          const b = bits[node]!;
          dim = (b & NB.DIM) !== 0;
          cls = 'tag tag--' + NODE_KINDS[m.nodeKind[node]!] + (b & NB.PIN ? ' is-pinned' : '') + (b & NB.HOVER ? ' is-hover' : '') +
            (b & NB.MATCH ? ' is-match' : '') + (b & NB.LEAK ? ' is-leak' : '') + (m.nodeKind[node] === KIND.AGENT && text.includes('· FREE') ? ' is-free' : '');
        }
        const w = measure(text);
        const opts = [[x + r + 4, y - H / 2], [x - w / 2, y - r - H - 2], [x - r - 4 - w, y - H / 2], [x - w / 2, y + r + 2]];
        const prev = node >= 0 ? side[node]! : (hullSide.get(text) ?? -1);
        let ok = -1;
        for (let qi = -1; qi < 4 && ok < 0; qi++) {
          const q = qi < 0 ? prev : qi;
          if (q < 0 || (qi >= 0 && q === prev)) continue;
          const rx = opts[q]![0]!, ry = opts[q]![1]!;
          if (rx < 0 || ry < 0 || rx + w > W || ry + H > Hh) continue;
          let clear = true;
          for (let z = 0; z < np && clear; z++) {
            const o = z * 4;
            if (rx < placed[o + 2]! && rx + w > placed[o]! && ry < placed[o + 3]! && ry + H > placed[o + 1]!) clear = false;
          }
          if (clear) ok = q;
        }
        if (ok < 0) continue;
        if (node >= 0) { side[node] = ok; shownNext[node] = 1; } else hullSideNext.set(text, ok);
        const rx = opts[ok]![0]!, ry = opts[ok]![1]!;
        placed[np * 4] = rx; placed[np * 4 + 1] = ry; placed[np * 4 + 2] = rx + w; placed[np * 4 + 3] = ry + H; np++;
        let op = fogFactor(depth, s.focusDepth) * (dim ? 0.25 : 1);
        if (depth < 1.4 * s.focusDepth) op = Math.max(op, 0.25);
        if (node >= 0 && (node === s.selected || node === s.hover)) op = 1;
        show(slots[used]!, text, rx, ry, op, cls);
        used++;
      }
      hideFrom(used);
      [shown, shownNext] = [shownNext, shown];
      [hullSide, hullSideNext] = [hullSideNext, hullSide];
    },
    clear() { hideFrom(0); },
    dispose() { layer.remove(); },
  };
};
