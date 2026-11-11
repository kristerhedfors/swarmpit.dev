import type { ClockDriver, NarrationIndex, NarrationManifest, NarrationVoice, RunCard } from '../types.js';
import type { Clock } from './clock.js';
import type { CaptionCue } from '../ui/captions.js';
import { a2r, assertNarration, assertNarrationIndex, cueAt, r2a } from './timemap.js';

/* Narration controller (NARRATION.md 9.4): one reused <audio> element is the master clock while narration is on.
 * The clock's t is derived from audio.currentTime through the manifest's time map; seeks invert the map.
 * Off by default; only toggle() (a user gesture) turns it on. Missing or invalid narration is a silent no-op. */

/** The subset of HTMLAudioElement the controller uses (a fake is used in tests). */
export interface AudioLike {
  src: string; preload: string; currentTime: number;
  readonly paused: boolean; readonly ended: boolean; readonly seeking?: boolean; readonly readyState?: number;
  play(): Promise<void> | void; pause(): void;
  addEventListener(type: string, cb: () => void): void;
  removeEventListener(type: string, cb: () => void): void;
}
export interface NarrationController {
  readonly available: boolean;   // current run has a valid, hash-matching manifest
  readonly on: boolean;          // the user wants narration (stays armed across run switches)
  readonly active: boolean;      // on && available: the audio drives the clock
  setRun(card: RunCard | null, signal?: AbortSignal): Promise<void>;
  toggle(): void;                // must be called synchronously from a user gesture
  dispose(): void;
}
export interface NarrationOpts {
  clock: Clock;
  captions: { setCue(c: CaptionCue | null): void };
  timeline: { setNarrating(on: boolean): void };
  base: URL;                                   // .../data/ ; the index is base/narration/index.json
  audio: AudioLike;
  onAvailability(avail: boolean): void;
  onInteract?(): void;
  /** True while playback is only the unattended preview (hero autoplay): turning narration on starts at the intro. */
  startAtTop?(): boolean;
  fetchJSON?(url: string, signal?: AbortSignal): Promise<unknown>;
  now?(): number;
  doc?: { readonly hidden: boolean; addEventListener(t: string, cb: () => void): void; removeEventListener(t: string, cb: () => void): void } | null;
}

/** Caption labels: the broadcast roles, never voice names. guide = play-by-play, expert = colour commentary. */
export const SPEAKER: Record<NarrationVoice, string> = { guide: 'Play-by-play', expert: 'Color' };
const END_SLACK_MS = 20, EXTRAPOLATE_MAX_MS = 250;
/** Turning narration on this close to the run start plays the intro duet instead of skipping it: the whole intro is
 * spoken while the run is held at 0, so r2a(t) for any t > 0 lands after it. */
export const INTRO_SNAP_MS = 2000;

async function defaultFetch(url: string, signal?: AbortSignal): Promise<unknown> {
  const res = await fetch(url, { signal, credentials: 'same-origin' });
  if (!res.ok) throw new Error('http');
  return res.json();
}
const quiet = (): void => { try { console.debug('narration unavailable'); } catch { /* ignore */ } };

export function createNarration(o: NarrationOpts): NarrationController {
  const { clock, captions, timeline, audio } = o;
  const getJSON = o.fetchJSON ?? defaultFetch;
  const now = o.now ?? ((): number => performance.now());
  const doc = o.doc === undefined ? (typeof document !== 'undefined' ? document : null) : o.doc;
  const indexUrl = new URL('narration/index.json', o.base);
  audio.preload = 'none';

  let indexP: Promise<NarrationIndex | null> | null = null;
  let man: NarrationManifest | null = null, src = '';
  let on = false, active = false, gen = 0, cueIdx = -2;
  // currentTime is coarse in some browsers: extrapolate between updates, but only once currentTime has been seen
  // moving since the last (re)start, and from the wall time of that move. The output never goes backwards
  // unless the audio position itself jumps back (a seek), so chapters are never crossed twice.
  let lastCt = -1, lastWall = 0, lastOut = -1, advancing = false, waiting = false;
  // A seek requested before the mp3's metadata is known (preload="none": the first toggle, a new run). Some engines
  // (older WebKit) drop or reset such a seek and report currentTime 0, which would snap the run back to its start.
  // Until metadata arrives the requested position stands in for currentTime, and it is re-applied on loadedmetadata.
  let pendingA = -1;
  const curA = (): number => (pendingA >= 0 ? pendingA : audio.currentTime * 1000);

  const audioMs = (): number => {
    if (pendingA >= 0) return pendingA;
    const ct = audio.currentTime * 1000;
    const w = now();
    if (ct !== lastCt) {
      if (lastCt >= 0 && ct < lastCt) { lastOut = -1; advancing = false; }   // jumped back: a seek, not playback
      else if (lastCt >= 0) advancing = true;
      lastCt = ct; lastWall = w;
    }
    let v = ct;
    if (advancing && !audio.paused && !waiting && !audio.seeking && (audio.readyState ?? 4) >= 3) {
      v = ct + Math.min(EXTRAPOLATE_MAX_MS, Math.max(0, w - lastWall));
    }
    lastOut = Math.max(lastOut, v);
    return lastOut;
  };
  /** Playback (re)started or stopped: wait for currentTime to move again before extrapolating. */
  const holdExtrapolation = (): void => { advancing = false; lastCt = audio.currentTime * 1000; lastWall = now(); };
  /** The audio position was set: forget the previous output too. */
  const resetExtrapolation = (): void => { advancing = false; lastCt = -1; lastOut = -1; };
  const done = (): boolean => !!man && (audio.ended || curA() >= man.durationMs - END_SLACK_MS);
  const driver: ClockDriver = {
    t: () => (man ? a2r(man.timeMap, Math.min(audioMs(), man.durationMs)) : clock.t),
    done,
  };

  function setCueAt(aMs: number): void {
    const i = man && active ? cueAt(man.cues, aMs) : -1;
    if (i === cueIdx) return;
    cueIdx = i;
    const c = i >= 0 ? man!.cues[i]! : null;
    captions.setCue(c ? { speaker: SPEAKER[c.voice], text: c.text, voice: c.voice } : null);
  }
  function seekAudio(runT: number): void { if (man) seekAudioMs(r2a(man.timeMap, runT)); }
  function seekAudioMs(a: number): void {
    if (!man) return;
    try { audio.currentTime = a / 1000; } catch { /* not seekable yet: re-applied on loadedmetadata */ }
    pendingA = (audio.readyState ?? 4) >= 1 ? -1 : a;
    resetExtrapolation();
    setCueAt(curA());
  }
  function playAudio(): void {
    let p: Promise<void> | void;
    try { p = audio.play(); } catch (e) { onPlayError(e); return; }
    if (p && typeof p.catch === 'function') p.catch(onPlayError);
  }
  function onPlayError(e: unknown): void {
    if (!active) return;
    const name = (e as Error | null)?.name;
    if (name === 'AbortError') return;                      // a pause() interrupted play(): expected
    if (name === 'NotAllowedError') { clock.pause(); return; } // no gesture: stay on, the next user play retries
    fault();
  }
  /** Audio failed to load or decode: drop narration for this run (silent; button hidden). */
  function fault(): void {
    quiet();
    deactivate();
    man = null;
    on = false;
    timeline.setNarrating(false);
    o.onAvailability(false);
  }
  function syncPlay(): void {
    if (!active) return;
    if (clock.playing) { if (audio.paused) { holdExtrapolation(); playAudio(); } }
    else if (!audio.paused) { audio.pause(); holdExtrapolation(); }
  }

  function activate(): void {
    if (!man || active) return;
    const url = new URL(man.audio, new URL(`narration/${man.runId}.json`, o.base)).href;
    if (src !== url) { src = url; pendingA = -1; audio.src = url; }
    active = true;
    cueIdx = -2;
    seekAudio(clock.t);
    clock.setDriver(driver);
    timeline.setNarrating(true);
    syncPlay();
  }
  function deactivate(): void {
    if (!active) return;
    active = false;
    if (!audio.paused) audio.pause();
    clock.setDriver(null);
    cueIdx = -2;
    captions.setCue(null);
  }

  const offs: (() => void)[] = [];
  offs.push(clock.on('state', () => syncPlay()));
  offs.push(clock.on('seek', (e) => { if (active && e.cause !== 'audio') seekAudio(e.t); }));
  offs.push(clock.on('tick', () => { if (active) setCueAt(curA()); }));
  const listen = (type: string, cb: () => void): void => { audio.addEventListener(type, cb); offs.push(() => audio.removeEventListener(type, cb)); };
  listen('ended', () => { if (active) clock.syncDriver(); });
  listen('timeupdate', () => { if (active) setCueAt(curA()); });
  listen('loadedmetadata', () => {
    if (pendingA < 0) return;
    const a = pendingA;
    pendingA = -1;
    if (Math.abs(audio.currentTime * 1000 - a) > 30) { try { audio.currentTime = a / 1000; } catch { /* ignore */ } }
    resetExtrapolation();
  });
  listen('waiting', () => { waiting = true; });
  listen('stalled', () => { waiting = true; });
  listen('playing', () => { waiting = false; holdExtrapolation(); });
  listen('play', () => {
    holdExtrapolation();
    // started outside the controller (media keys, OS media controls): bring the clock along
    if (active && !audio.paused && !clock.playing) clock.play();
  });
  listen('pause', () => {
    holdExtrapolation();
    // paused outside the controller (media keys, headphones unplugged, OS interruption). Our own pauses only
    // happen when the clock is already paused, and a later play() leaves audio.paused false, so no loop.
    if (active && audio.paused && !audio.ended && clock.playing) clock.pause();
  });
  listen('seeking', () => { resetExtrapolation(); });
  listen('seeked', () => { holdExtrapolation(); });
  listen('error', () => { if (active) fault(); });
  if (doc) {
    const vis = (): void => { if (active && doc.hidden) clock.pause(); };
    doc.addEventListener('visibilitychange', vis);
    offs.push(() => doc.removeEventListener('visibilitychange', vis));
  }

  function loadIndex(): Promise<NarrationIndex | null> {
    indexP ??= getJSON(indexUrl.href).then((x) => assertNarrationIndex(x, 'narration/index.json'))
      .catch(() => { quiet(); return null; });
    return indexP;
  }

  const api: NarrationController = {
    get available() { return man !== null; },
    get on() { return on; },
    get active() { return active; },
    async setRun(card, signal) {
      const my = ++gen;
      deactivate();
      man = null;
      if (!card) return;
      const stale = (): boolean => my !== gen || !!signal?.aborted;
      let m: NarrationManifest | null = null;
      try {
        const idx = await loadIndex();
        if (stale()) return;
        const e = idx?.runs[card.id];
        if (e && e.hash === card.hash) {
          const raw = await getJSON(new URL(e.manifest, indexUrl).href, signal);
          if (stale()) return;
          const v = assertNarration(raw, 'narration manifest');
          const dur = clock.duration;
          if (v.runId === card.id && v.runHash === card.hash && (!(dur > 0) || Math.abs(v.runEndMs - dur) <= 1)) m = v;
          else quiet();
        }
      } catch (err) {
        if (stale() || (err as Error | null)?.name === 'AbortError') return;
        quiet();
      }
      if (stale()) return;
      man = m;
      o.onAvailability(!!m);
      if (!m) { if (on) { on = false; timeline.setNarrating(false); } return; }
      if (on) activate();
    },
    toggle() {
      if (on) {
        on = false;
        deactivate();
        timeline.setNarrating(false);
        return;
      }
      if (!man) return;
      on = true;
      const top = clock.t <= INTRO_SNAP_MS || o.startAtTop?.() === true;
      o.onInteract?.();
      activate();
      if (!clock.playing && clock.t >= clock.duration) clock.seek(0, 'api');
      else if (top && clock.t > 0) clock.seek(0, 'api');   // -> seek handler: audio to 0, the intro plays
      clock.play();   // emits 'state' -> audio.play() inside this gesture
      syncPlay();     // already playing: no 'state' event, start the audio here
    },
    dispose() {
      deactivate();
      gen++;
      for (const f of offs.splice(0)) f();
    },
  };
  return api;
}
