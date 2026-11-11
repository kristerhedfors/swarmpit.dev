import { RATES } from '../types.js';
import type { ClockDriver, ClockEvents, PlaybackClock, PlaybackRate, RunModel, SeekCause } from '../types.js';
import { Emitter } from '../util/bus.js';
import { HIDDEN_EVENT_KINDS } from '../data/filters.js';
import { EVENT_KINDS } from '../types.js';

export interface Clock extends PlaybackClock {
  /** Which events count as "visible" for skip-quiet gaps (defaults to the default-visible kinds). */
  setVisible(pass: ((i: number) => boolean) | null): void;
  readonly scrubbing: boolean;
}

const QUIET_MS = 1500, EASE_MS = 400, MAX_MULT = 12;

/** Skip-quiet multiplier for the gap between t and the next visible event (pure). */
export function skipMult(gapPrev: number, gapNext: number): number {
  if (!(gapPrev + gapNext > QUIET_MS) || !Number.isFinite(gapNext)) return 1;
  return Math.max(1, Math.min(MAX_MULT, gapNext / EASE_MS));
}

export function createClock(): Clock {
  const bus = new Emitter<ClockEvents>();
  let m: RunModel | null = null;
  let t = 0, dur = 0, playing = false, rate: PlaybackRate = 1, skipQuiet = false, skipping = false;
  let scrubbing = false, wasPlaying = false;
  let pass: ((i: number) => boolean) | null = null;
  let driver: ClockDriver | null = null;
  let visT = new Float64Array(0);
  let chapT = new Float64Array(0);

  const hiddenK = new Set(HIDDEN_EVENT_KINDS.map((k) => EVENT_KINDS.indexOf(k)));
  function rebuildVisible(): void {
    if (!m) { visT = new Float64Array(0); return; }
    const out: number[] = [];
    for (let i = 0; i < m.nEvents; i++) if (pass ? pass(i) : !hiddenK.has(m.evK[i]!)) out.push(m.evT[i]!);
    visT = Float64Array.from(out);
  }
  const ub = (a: Float64Array, x: number): number => { let lo = 0, hi = a.length; while (lo < hi) { const md = (lo + hi) >>> 1; if (a[md]! <= x) lo = md + 1; else hi = md; } return lo; };

  const state = (): void => bus.emit('state', { playing, rate, skipQuiet, skipping });
  function setT(nt: number, cause: SeekCause | null, dtWall = 0): void {
    nt = Math.max(0, Math.min(dur, nt));
    const from = t;
    t = nt;
    if (cause) bus.emit('seek', { t, from, cause });
    bus.emit('tick', { t, dtWall });
  }
  const crossChapters = (from: number): void => {
    for (let k = ub(chapT, from); k < chapT.length && chapT[k]! <= t; k++) if (chapT[k]! > from) bus.emit('chapter', { index: k });
  };
  /** Narration: t follows the driver (audio master); rate and skip-quiet are ignored; the driver decides the end. */
  function driveStep(dtWall: number): boolean {
    const d = driver!;
    if (skipping) { skipping = false; state(); }
    const from = t;
    setT(d.t(), null, dtWall);
    crossChapters(from);
    if (d.done()) {
      playing = false; state();
      bus.emit('ended', {});
    }
    return t !== from;
  }

  const c: Clock = {
    on: bus.on.bind(bus), emit: bus.emit.bind(bus),
    get duration() { return dur; }, get t() { return t; }, get playing() { return playing; },
    get rate() { return rate; }, get skipQuiet() { return skipQuiet; }, get skipping() { return skipping; },
    get scrubbing() { return scrubbing; },
    get driven() { return driver !== null; },
    setModel(model) {
      m = model; dur = model.tEnd; playing = false; skipping = false; scrubbing = false;
      chapT = Float64Array.from(model.core.chapters.map((ch) => ch.t));
      rebuildVisible();
      setT(0, 'api');
      state();
    },
    setVisible(p) { pass = p; rebuildVisible(); },
    play() {
      if (!m) return;
      if (driver ? driver.done() : t >= dur) setT(0, driver ? 'api' : 'end');
      if (!playing) { playing = true; state(); }
    },
    pause() { if (playing || skipping) { playing = false; skipping = false; state(); } },
    toggle() { if (playing) c.pause(); else c.play(); },
    seek(nt, cause) { setT(nt, cause); },
    seekBy(dt, cause) { setT(t + dt, cause); },
    stepEvent(dir, p) {
      if (!m) return;
      c.pause();
      const i = m.nextEvent(t, dir, p);
      if (i >= 0) setT(m.evT[i]!, 'key');
      else setT(dir > 0 ? dur : 0, 'key');
    },
    setRate(r) { if ((RATES as readonly number[]).includes(r) && r !== rate) { rate = r; state(); } },
    setSkipQuiet(on) { if (on !== skipQuiet) { skipQuiet = on; if (!on) skipping = false; state(); } },
    beginScrub() { if (!scrubbing) { scrubbing = true; wasPlaying = playing; if (playing) { playing = false; state(); } } },
    endScrub() { if (scrubbing) { scrubbing = false; if (wasPlaying && (driver ? !driver.done() : t < dur)) { playing = true; state(); } } },
    setDriver(d) { driver = d; if (d && skipping) { skipping = false; state(); } },
    syncDriver() { if (driver && playing && !scrubbing && m) driveStep(0); },
    advance(dtWall) {
      if (!playing || scrubbing || !m) return false;
      if (driver) return driveStep(dtWall);
      let mult = 1, cap = Infinity;
      if (skipQuiet) {
        const j = ub(visT, t);
        const next = j < visT.length ? visT[j]! : dur;
        const prev = j > 0 ? visT[j - 1]! : 0;
        mult = skipMult(t - prev, next - t);
        if (mult > 1) cap = next;
      }
      const sk = mult > 1.05;
      if (sk !== skipping) { skipping = sk; state(); }
      const from = t;
      let nt = Math.min(t + dtWall * rate * mult, cap);
      if (nt >= dur) nt = dur;
      setT(nt, null, dtWall);
      crossChapters(from);
      if (t >= dur) {
        playing = false; skipping = false; state();
        bus.emit('ended', {});
      }
      return t !== from;
    },
  };
  return c;
}
