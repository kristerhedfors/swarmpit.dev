// Engine demo (public/engine-demo.html): synthetic run + a minimal clock. Not part of the site bundle.

import type { ClockEvents, CoreBundle, IndexBundle, PickHit, PlaybackClock, PlaybackRate, RunModel } from '../../types.js';
import { RATES, EVENT_KINDS, NODE_KINDS, EDGE_KINDS } from '../../types.js';
import { Emitter } from '../../util/bus.js';
import { createEngine } from '../engine.js';
import { devModel, synthCore } from './devmodel.js';

function miniClock(): PlaybackClock {
  const em = new Emitter<ClockEvents>();
  let m: RunModel | null = null, t = 0, playing = false, rate: PlaybackRate = 1, wasPlaying = false;
  const state = () => em.emit('state', { playing, rate, skipQuiet: false, skipping: false });
  const c: PlaybackClock = {
    on: em.on.bind(em), emit: em.emit.bind(em),
    get duration() { return m ? m.tEnd : 0; }, get t() { return t; }, get playing() { return playing; },
    get rate() { return rate; }, skipQuiet: false, skipping: false,
    setModel(x) { m = x; t = 0; playing = false; state(); },
    play() { if (t >= c.duration) t = 0; playing = true; state(); },
    pause() { playing = false; state(); },
    toggle() { if (playing) c.pause(); else c.play(); },
    seek(x, cause) { const from = t; t = Math.max(0, Math.min(c.duration, x)); em.emit('seek', { t, from, cause }); },
    seekBy(dt, cause) { c.seek(t + dt, cause); },
    stepEvent(dir) { if (!m) return; c.pause(); const i = m.nextEvent(t, dir); if (i >= 0) c.seek(m.evT[i]!, 'key'); },
    setRate(r) { rate = r; state(); }, setSkipQuiet() { /* n/a */ },
    beginScrub() { wasPlaying = playing; playing = false; state(); }, endScrub() { if (wasPlaying) c.play(); },
    setDriver() { /* n/a: no narration in the demo */ }, driven: false, syncDriver() { /* n/a */ },
    advance(dt) {
      if (!playing || !m) return false;
      t = Math.min(c.duration, t + dt * rate);
      em.emit('tick', { t, dtWall: dt });
      if (t >= c.duration) { playing = false; state(); em.emit('ended', {}); }
      return true;
    },
  };
  return c;
}

const q = new URLSearchParams(location.search);
const agents = +(q.get('agents') ?? 6);
// ?run=<id> loads an exported bundle through the site's model; otherwise synthetic data
const runId = q.get('run');
let real: { core: CoreBundle; model: RunModel } | null = null;
if (runId) {
  const idx = await (await fetch('data/index.json')).json() as IndexBundle;
  const card = idx.runs.find((r) => r.id === runId);
  if (card) {
    const c = await (await fetch('data/' + card.core)).json() as CoreBundle;
    const { buildModel } = await import('../../data/model.js');
    real = { core: c, model: buildModel(card, c) };
  }
}
const core = real ? real.core : synthCore({ seed: +(q.get('seed') ?? 7), agents, threads: +(q.get('threads') ?? Math.max(6, agents)),
  durationMs: +(q.get('dur') ?? 120000), personas: +(q.get('personas') ?? 5), budget: +(q.get('budget') ?? 40) });
const model = real ? real.model : devModel(core);
const clock = miniClock();
const stage = document.getElementById('stage')!;
const engine = createEngine(stage, clock, { prefer: q.get('renderer') === '2d' ? 'canvas2d' : 'webgl2', perf: q.get('perf') === '1' });
clock.setModel(model);
engine.setRun(model);

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const play = $<HTMLButtonElement>('play'), slider = $<HTMLInputElement>('t'), time = $('time'), info = $('info'),
  speed = $<HTMLSelectElement>('speed'), mode = $<HTMLButtonElement>('mode'), tip = $('tip'), status = $('status');
for (const r of RATES) speed.add(new Option(r + 'x', String(r), r === 1, r === 1));
slider.max = String(model.tEnd);
const fmt = (ms: number) => `${Math.floor(ms / 60000)}:${((ms % 60000) / 1000).toFixed(1).padStart(4, '0')}`;
const sync = () => {
  slider.value = String(clock.t);
  time.textContent = `${fmt(clock.t)} / ${fmt(model.tEnd)}`;
  play.textContent = clock.playing ? 'Pause' : 'Play';
  const s = engine.swarm();
  info.textContent = `on board ${s.onBoard.length}/${model.nAgents} · live↔live ${s.liveLive} · swarms ${s.clusters.length}`;
};
clock.on('tick', sync); clock.on('state', sync); clock.on('seek', sync);
play.onclick = () => clock.toggle();
slider.oninput = () => clock.seek(+slider.value, 'scrub');
speed.onchange = () => clock.setRate(+speed.value as PlaybackRate);
mode.onclick = () => { engine.setMode(engine.mode === 'worldline' ? 'constellation' : 'worldline', true); mode.textContent = engine.mode; };
$('reset').onclick = () => engine.resetView();
$<HTMLInputElement>('labels').onchange = (e) => engine.setOptions({ labels: (e.target as HTMLInputElement).checked });
$<HTMLInputElement>('follow').onchange = (e) => engine.setFollow((e.target as HTMLInputElement).checked);
$('lose').onclick = () => {
  const gl = engine.canvas?.getContext('webgl2');
  const ext = gl?.getExtension('WEBGL_lose_context');
  if (!ext) { status.textContent = 'WEBGL_lose_context unavailable'; return; }
  ext.loseContext(); status.textContent = 'context lost; restoring in 1 s';
  setTimeout(() => { ext.restoreContext(); status.textContent = 'context restored'; }, 1000);
};
const describe = (h: PickHit): string => {
  if (h.kind === 'node') {
    const n = core.nodes[h.node]!;
    return `${NODE_KINDS[n.k]} ${n.id}${n.preview ? '\n' + n.preview : ''}`;
  }
  if (h.kind === 'step') return `${EVENT_KINDS[model.evK[h.event]!]} · ${model.label(h.event)}`;
  return `${EDGE_KINDS[model.edgeKind[h.edge]!]} ${core.nodes[model.edgeSrc[h.edge]!]!.id} → ${core.nodes[model.edgeDst[h.edge]!]!.id}`;
};
engine.on('hover', ({ hit, x, y }) => {
  if (!hit) { tip.hidden = true; return; }
  tip.hidden = false; tip.textContent = describe(hit);
  tip.style.transform = `translate(${x + 14}px, ${y + 14}px)`;
});
engine.on('select', ({ hit }) => { status.textContent = hit ? 'pinned: ' + describe(hit).split('\n')[0] : 'selection cleared'; });
engine.on('backend', ({ kind, reason }) => { status.textContent = `backend: ${kind}${reason ? ' (' + reason + ')' : ''}`; });
engine.on('seekRequest', ({ dt }) => clock.seekBy(dt, 'api'));
engine.on('engaged', ({ on }) => { status.textContent = on ? 'engaged: wheel zooms, drag orbits (Esc to release)' : 'released'; });
document.addEventListener('keydown', (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement || e.target === engine.canvas) return;
  if (e.key === ' ' || e.key === 'k') { e.preventDefault(); clock.toggle(); }
  else if (e.key === 'v') mode.click();
  else if (e.key === 'ArrowRight') clock.seekBy(5000, 'key');
  else if (e.key === 'ArrowLeft') clock.seekBy(-5000, 'key');
});
sync();
(window as unknown as { __engine: unknown }).__engine = { engine, clock, model };
