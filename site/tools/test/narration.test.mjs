// Narration player side (NARRATION.md 8.2): time map, manifest validation, clock driver, controller sync.
// Imports compiled DOM-free modules from public/js (run `npm run build` first). Fixtures are hand-made with
// neutral placeholder text; real narration files are only checked structurally and never printed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public');
const js = (p) => import(join(root, 'js', p));
const { a2r, r2a, cueAt, assertNarration, assertNarrationIndex } = await js('player/timemap.js');
const { createClock } = await js('player/clock.js');
const { createNarration, SPEAKER, INTRO_SNAP_MS } = await js('player/narration.js');
const { keyAction } = await js('player/keys.js');
const { createPreviewGate, USER_SEEKS } = await js('player/preview.js');

// ---------- fixtures (placeholder text only)
const MAP = [[0, 0], [2000, 0], [5000, 3000], [6000, 3000], [7000, 9000], [9000, 9000]]; // hold, 1x, hold, 6x, closing hold
const manifest = (over = {}) => ({
  schema: 'swarmpit.narration/1', generator: 'narrate/1.0.0', runId: 'r1', runHash: '0123abcd',
  audio: '../../audio/narration/r1.89abcdef.mp3', audioBytes: 100, durationMs: 9000, runEndMs: 9000,
  timeMap: MAP.map((p) => [...p]),
  cues: [
    { audioMs: 0, endMs: 1800, runMs: 0, voice: 'guide', tpl: 'G.test', text: 'Placeholder one.' },
    { audioMs: 5000, endMs: 5900, runMs: 3000, voice: 'expert', tpl: 'E.test', text: 'Placeholder two.' },
    { audioMs: 7000, endMs: 8800, runMs: 9000, voice: 'expert', tpl: 'E.close.test', text: 'Placeholder three.' },
  ],
  sfx: { counts: { post: 2, read: 1 }, dropped: 3 },
  ...over,
});
const card = (id = 'r1', hash = '0123abcd') => ({ id, hash });
const nIndex = (runs) => ({ schema: 'swarmpit.narration-index/1', runs });
const fakeModel = (tEnd = 9000) => ({ tEnd, core: { chapters: [{ t: 1000 }, { t: 4000 }] }, nEvents: 0, evK: [], evT: [] });

class FakeAudio {
  constructor() { this.src = ''; this.preload = 'auto'; this.currentTime = 0; this.paused = true; this.ended = false;
    this.readyState = 4; this.seeking = false; this.plays = 0; this.l = new Map(); this.reject = null; }
  play() { this.plays++; if (this.reject) { const e = new Error('x'); e.name = this.reject; return Promise.reject(e); } this.paused = false; this.ended = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  addEventListener(t, f) { if (!this.l.has(t)) this.l.set(t, new Set()); this.l.get(t).add(f); }
  removeEventListener(t, f) { this.l.get(t)?.delete(f); }
  emit(t) { for (const f of this.l.get(t) ?? []) f(); }
}

function rig({ index = nIndex({ r1: { hash: '0123abcd', manifest: 'r1.json', durationMs: 9000 } }), man = manifest(), tEnd = 9000, now = () => 0 } = {}) {
  const clock = createClock();
  clock.setModel(fakeModel(tEnd));
  const audio = new FakeAudio();
  const cues = [], avail = [], fetched = [];
  const tl = { narrating: false, setNarrating(on) { this.narrating = on; } };
  let interacted = false;
  const nar = createNarration({
    clock, audio, timeline: tl, base: new URL('https://example.test/site/data/'), doc: null, now,
    captions: { setCue: (c) => cues.push(c) },
    onAvailability: (v) => avail.push(v),
    onInteract: () => { interacted = true; },
    fetchJSON: async (url) => {
      fetched.push(url);
      if (url.endsWith('/narration/index.json')) { if (!index) throw new Error('404'); return index; }
      if (url.endsWith('/narration/r1.json') || url.endsWith('/narration/r2.json')) { if (!man) throw new Error('404'); return man; }
      throw new Error('unexpected');
    },
  });
  return { clock, audio, nar, tl, cues, avail, fetched, get interacted() { return interacted; } };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

// ---------- time map
test('a2r: linear interpolation, holds, clamping', () => {
  assert.equal(a2r(MAP, -5), 0);
  assert.equal(a2r(MAP, 1000), 0);          // intro hold
  assert.equal(a2r(MAP, 3500), 1500);       // 1x
  assert.equal(a2r(MAP, 5500), 3000);       // hold
  assert.equal(a2r(MAP, 6500), 6000);       // 6x fast-forward
  assert.equal(a2r(MAP, 8000), 9000);       // closing hold
  assert.equal(a2r(MAP, 1e9), 9000);
});

test('r2a: smallest audio position, hold start, clamping, round trip', () => {
  assert.equal(r2a(MAP, 0), 0);             // run start = start of the intro hold
  assert.equal(r2a(MAP, -1), 0);
  assert.equal(r2a(MAP, 1500), 3500);
  assert.equal(r2a(MAP, 3000), 5000);       // a beat replays its hold from the beginning
  assert.equal(r2a(MAP, 6000), 6500);
  assert.equal(r2a(MAP, 9000), 7000);       // end = start of the closing hold
  assert.equal(r2a(MAP, 1e9), 7000);
  for (let r = 0; r <= 9000; r += 37) assert.ok(Math.abs(a2r(MAP, r2a(MAP, r)) - r) <= 1, `round trip at ${r}`);
  assert.equal(a2r([], 5), 0); assert.equal(r2a([], 5), 0);
});

test('cueAt: index inside [audioMs, endMs), -1 in gaps and at boundaries', () => {
  const { cues } = manifest();
  assert.equal(cueAt(cues, 0), 0);
  assert.equal(cueAt(cues, 1799), 0);
  assert.equal(cueAt(cues, 1800), -1);
  assert.equal(cueAt(cues, 4999), -1);
  assert.equal(cueAt(cues, 5000), 1);
  assert.equal(cueAt(cues, 8799), 2);
  assert.equal(cueAt(cues, 8800), -1);
  assert.equal(cueAt(cues, -1), -1);
  assert.equal(cueAt([], 10), -1);
});

// ---------- manifest validation
test('assertNarration accepts a minimal synthetic manifest and ignores unknown keys', () => {
  assert.equal(assertNarration(manifest()).runId, 'r1');
  assert.equal(assertNarration({ ...manifest(), extra: 1 }).runId, 'r1');
  assert.equal(assertNarration(manifest({ cues: [] })).cues.length, 0);
});

test('assertNarration rejects contract violations with fixed messages', () => {
  const bad = {
    schema: manifest({ schema: 'swarmpit.narration/2' }),
    runId: manifest({ runId: '../x' }),
    nonMonotonic: manifest({ timeMap: [[0, 0], [5000, 3000], [4000, 3500], [9000, 9000]] }),
    runBackwards: manifest({ timeMap: [[0, 0], [5000, 3000], [6000, 2000], [9000, 9000]] }),
    slope: manifest({ timeMap: [[0, 0], [100, 900], [9000, 9000]] }),
    start: manifest({ timeMap: [[10, 0], [9000, 9000]] }),
    end: manifest({ timeMap: [[0, 0], [8000, 9000]] }),
    fractional: manifest({ timeMap: [[0, 0], [4000.5, 3000], [9000, 9000]] }),
    overlap: manifest({ cues: [{ audioMs: 0, endMs: 2500, runMs: 0, voice: 'guide', tpl: 'G.a', text: 'x' },
      { audioMs: 2000, endMs: 3000, runMs: 0, voice: 'guide', tpl: 'G.b', text: 'y' }] }),
    anchor: manifest({ cues: [{ audioMs: 3000, endMs: 3500, runMs: 0, voice: 'guide', tpl: 'G.a', text: 'x' }] }),
    voice: manifest({ cues: [{ audioMs: 0, endMs: 100, runMs: 0, voice: 'narrator', tpl: 'G.a', text: 'x' }] }),
    tpl: manifest({ cues: [{ audioMs: 0, endMs: 100, runMs: 0, voice: 'guide', tpl: 'free text', text: 'x' }] }),
    emptyText: manifest({ cues: [{ audioMs: 0, endMs: 100, runMs: 0, voice: 'guide', tpl: 'G.a', text: '' }] }),
    longText: manifest({ cues: [{ audioMs: 0, endMs: 100, runMs: 0, voice: 'guide', tpl: 'G.a', text: 'x'.repeat(241) }] }),
    cueRange: manifest({ cues: [{ audioMs: 0, endMs: 9001, runMs: 0, voice: 'guide', tpl: 'G.a', text: 'x' }] }),
    audioOther: manifest({ audio: '../../audio/narration/r2.89abcdef.mp3' }),
    audioAbs: manifest({ audio: 'https://example.test/r1.89abcdef.mp3' }),
    audioUp: manifest({ audio: '../../../audio/narration/r1.89abcdef.mp3' }),
    sfx: manifest({ sfx: { counts: { read: -1 }, dropped: 0 } }),
    notObject: [],
  };
  for (const [name, m] of Object.entries(bad)) {
    assert.throws(() => assertNarration(m), (e) => e.name === 'BundleError' && !e.message.includes('Placeholder'), name);
  }
});

test('assertNarrationIndex drops malformed entries and rejects a wrong schema', () => {
  const ix = assertNarrationIndex(nIndex({ r1: { hash: 'a', manifest: 'r1.json', durationMs: 1 },
    r2: { hash: 'b', manifest: '../evil.json', durationMs: 1 }, 'bad id': { hash: 'c', manifest: 'bad id.json', durationMs: 1 } }));
  assert.deepEqual(Object.keys(ix.runs), ['r1']);
  assert.throws(() => assertNarrationIndex({ schema: 'x', runs: {} }));
  assert.throws(() => assertNarrationIndex(null));
});

// ---------- clock driver
test('clock driver: t follows the driver, ignores rate and skip-quiet, emits chapter and ended once', () => {
  const c = createClock();
  c.setModel(fakeModel(9000));
  const seen = [];
  c.on('chapter', ({ index }) => seen.push(`ch${index}`));
  c.on('ended', () => seen.push('ended'));
  let x = 0, d = false;
  c.setRate(4); c.setSkipQuiet(true);
  c.setDriver({ t: () => x, done: () => d });
  assert.equal(c.driven, true);
  c.play();
  x = 1500; c.advance(16);
  assert.equal(c.t, 1500); assert.equal(c.skipping, false);
  x = 1500; assert.equal(c.advance(16), false);       // audio held: t holds regardless of rate
  x = 9000; c.advance(16);
  assert.equal(c.t, 9000); assert.equal(c.playing, true, 'no auto-end at duration while driven');
  d = true; c.advance(16);
  assert.equal(c.playing, false);
  c.advance(16); c.syncDriver();
  assert.deepEqual(seen, ['ch0', 'ch1', 'ended']);
  // play() when the driver is done seeks to 0 first
  const seeks = [];
  c.on('seek', (e) => seeks.push(e.cause));
  x = 0; c.play(); d = false;
  assert.deepEqual(seeks, ['api']); assert.equal(c.t, 0);
  // clearing the driver restores normal (rate-scaled) advance
  c.setDriver(null); c.setSkipQuiet(false);
  assert.equal(c.driven, false);
  c.advance(100); assert.equal(c.t, 400);
});

test('clock driver: syncDriver ends the run when the audio ended outside the frame loop', () => {
  const c = createClock();
  c.setModel(fakeModel(9000));
  let ended = 0; c.on('ended', () => ended++);
  c.setDriver({ t: () => 9000, done: () => true });
  c.syncDriver(); assert.equal(ended, 0, 'paused: nothing to sync');
  c.seek(100, 'api');
  c.setDriver({ t: () => 9000, done: () => false });
  c.play(); c.setDriver({ t: () => 9000, done: () => true });
  c.syncDriver();
  assert.equal(ended, 1); assert.equal(c.t, 9000); assert.equal(c.playing, false);
});

test('keys: n toggles narration', () => {
  assert.equal(keyAction({ key: 'n' }), 'narration');
  assert.equal(keyAction({ key: 'n', ctrlKey: true }), null);
});

// ---------- controller
test('narration: no index -> silent no-op, toggle does nothing', async () => {
  const r = rig({ index: null });
  await r.nar.setRun(card());
  assert.equal(r.nar.available, false);
  assert.deepEqual(r.avail, [false]);
  r.nar.toggle();
  assert.equal(r.nar.on, false); assert.equal(r.audio.plays, 0); assert.equal(r.audio.src, '');
  await r.nar.setRun(card('r2'));
  assert.equal(r.fetched.filter((u) => u.endsWith('index.json')).length, 1, 'index fetched once');
});

test('narration: stale hash or wrong run end -> unavailable', async () => {
  const a = rig({ index: nIndex({ r1: { hash: 'ffffffff', manifest: 'r1.json', durationMs: 9000 } }) });
  await a.nar.setRun(card());
  assert.equal(a.nar.available, false);
  assert.equal(a.fetched.length, 1, 'manifest not fetched for a stale entry');
  const b = rig({ man: manifest({ runHash: 'ffffffff' }) });
  await b.nar.setRun(card());
  assert.equal(b.nar.available, false);
  const c = rig({ tEnd: 12000 });
  await c.nar.setRun(card());
  assert.equal(c.nar.available, false);
});

test('narration: toggle on drives the clock from audio, syncs play/pause/seek, captions, end', async () => {
  const r = rig();
  await r.nar.setRun(card());
  assert.equal(r.nar.available, true); assert.deepEqual(r.avail, [true]);
  assert.equal(r.audio.src, '', 'no mp3 requested before narration is turned on');
  assert.equal(r.audio.preload, 'none');
  r.clock.seek(2500, 'key');
  r.nar.toggle();
  assert.equal(r.nar.on, true); assert.equal(r.nar.active, true); assert.equal(r.interacted, true);
  assert.equal(r.audio.src, 'https://example.test/site/audio/narration/r1.89abcdef.mp3');
  assert.equal(r.audio.currentTime, 4.5, 'audio starts at r2a(t)');
  assert.equal(r.clock.playing, true); assert.equal(r.clock.driven, true); assert.equal(r.audio.paused, false);
  assert.equal(r.tl.narrating, true);
  // audio is the master: run t = a2r(audio)
  r.audio.currentTime = 5.5; r.clock.advance(16);
  assert.equal(r.clock.t, 3000);
  assert.deepEqual(r.cues.at(-1), { speaker: SPEAKER.expert, text: 'Placeholder two.', voice: 'expert' });
  r.audio.currentTime = 6.2; r.clock.advance(16);
  assert.equal(r.cues.at(-1), null, 'cue cleared between cues');
  // user seek maps back through the inverse
  r.clock.seek(1500, 'scrub');
  assert.equal(r.audio.currentTime, 3.5);
  // pause/play follow the clock
  r.clock.pause(); assert.equal(r.audio.paused, true);
  r.clock.play(); assert.equal(r.audio.paused, false);
  // audio ends -> clock ends
  let ended = 0; r.clock.on('ended', () => ended++);
  r.audio.currentTime = 9; r.audio.ended = true; r.audio.paused = true; r.audio.emit('ended');
  assert.equal(ended, 1); assert.equal(r.clock.playing, false); assert.equal(r.clock.t, 9000);
  // replay restarts both from 0
  r.clock.play();
  assert.equal(r.clock.t, 0); assert.equal(r.audio.currentTime, 0); assert.equal(r.audio.paused, false);
  // turning off keeps t and play state; the clock advances on its own again
  r.audio.currentTime = 3.5; r.clock.advance(16);
  r.nar.toggle();
  assert.equal(r.nar.on, false); assert.equal(r.audio.paused, true); assert.equal(r.clock.driven, false);
  assert.equal(r.clock.playing, true); assert.equal(r.clock.t, 1500); assert.equal(r.cues.at(-1), null);
  assert.equal(r.tl.narrating, false);
  r.clock.advance(100); assert.equal(r.clock.t, 1600);
});

test('narration: extrapolation never jumps ahead after pause/resume, seek or a fresh start, and never goes back', async () => {
  let wall = 0;
  const r = rig({ now: () => wall });
  await r.nar.setRun(card());
  const ts = [];
  r.clock.on('tick', ({ t }) => ts.push(t));
  r.clock.seek(6000, 'key');                     // audio 6500, inside the 6x segment
  r.nar.toggle();
  assert.equal(r.audio.currentTime, 6.5);
  wall = 200; r.clock.advance(16);
  assert.equal(r.clock.t, 6000, 'fresh start: no extrapolation before currentTime moves');
  wall = 300; r.audio.currentTime = 6.51; r.clock.advance(16);
  assert.equal(r.clock.t, 6060);
  wall = 350; r.clock.advance(16);
  const peak = r.clock.t;
  assert.ok(peak > 6060 && peak <= 6060 + 6 * 250, 'extrapolates while advancing');
  r.clock.pause(); r.audio.emit('pause');
  wall = 5000; r.clock.play(); r.audio.emit('play');
  r.clock.advance(16);
  assert.equal(r.clock.t, peak, 'resume: holds, does not extrapolate across the pause');
  wall = 5016; r.audio.currentTime = 6.52; r.clock.advance(16);
  assert.equal(r.clock.t, peak, 'never snaps back');
  for (let i = 1; i < ts.length; i++) assert.ok(ts[i] >= ts[i - 1], 'monotonic without a seek');
  // a seek resets: the next reading is the raw position, no extrapolation until it moves
  r.clock.seek(1500, 'scrub');
  assert.equal(r.audio.currentTime, 3.5);
  wall = 9000; r.clock.advance(16);
  assert.equal(r.clock.t, 1500);
});

test('narration: pause/play from outside (media keys) keep the clock in sync', async () => {
  const r = rig();
  await r.nar.setRun(card());
  r.nar.toggle();
  assert.equal(r.clock.playing, true);
  // external pause -> the clock pauses (one click resumes)
  r.audio.paused = true; r.audio.emit('pause');
  assert.equal(r.clock.playing, false);
  r.clock.play(); assert.equal(r.audio.paused, false);
  // our own pause does not loop back, and a pause event arriving after a re-play is ignored
  r.clock.pause(); r.clock.play(); r.audio.emit('pause');
  assert.equal(r.clock.playing, true);
  // external play while the clock is paused -> the clock plays, and the end is reached normally
  r.clock.pause();
  r.audio.paused = false; r.audio.emit('play');
  assert.equal(r.clock.playing, true);
  let ended = 0; r.clock.on('ended', () => ended++);
  r.audio.currentTime = 9; r.audio.ended = true; r.audio.paused = true;
  r.audio.emit('pause'); r.audio.emit('ended');
  assert.equal(ended, 1); assert.equal(r.clock.t, 9000);
  // a stale play event (audio already paused again) does not start the clock
  r.clock.pause(); r.audio.emit('play');
  assert.equal(r.clock.playing, false);
});

test('narration: autoplay rejection pauses the clock but keeps narration on', async () => {
  const r = rig();
  await r.nar.setRun(card());
  r.audio.reject = 'NotAllowedError';
  r.nar.toggle();
  await tick();
  assert.equal(r.nar.on, true); assert.equal(r.clock.playing, false);
  r.audio.reject = null;
  r.clock.play();
  assert.equal(r.audio.paused, false);
});

test('narration: audio error drops narration for the run', async () => {
  const r = rig();
  await r.nar.setRun(card());
  r.nar.toggle();
  r.audio.emit('error');
  assert.equal(r.nar.on, false); assert.equal(r.nar.available, false); assert.equal(r.clock.driven, false);
  assert.deepEqual(r.avail, [true, false]);
});

test('narration: run switching unloads, stays armed when the next run is narrated, turns off otherwise', async () => {
  const r = rig({ index: nIndex({ r1: { hash: '0123abcd', manifest: 'r1.json', durationMs: 9000 },
    r2: { hash: '0123abcd', manifest: 'r2.json', durationMs: 9000 } }) });
  await r.nar.setRun(card());
  r.nar.toggle();
  // switch: unload first (driver removed, audio paused, cue cleared)
  const p = r.nar.setRun(null);
  assert.equal(r.clock.driven, false); assert.equal(r.audio.paused, true); assert.equal(r.cues.at(-1), null);
  await p;
  r.clock.setModel(fakeModel(9000));
  await r.nar.setRun(card('r2'));
  // the fixture manifest says runId r1: invalid for r2 -> narration turns off and the button hides
  assert.equal(r.nar.on, false); assert.equal(r.nar.available, false); assert.equal(r.avail.at(-1), false);
  assert.equal(r.tl.narrating, false);

  const s = rig();
  await s.nar.setRun(card());
  s.nar.toggle();
  s.clock.pause();
  await s.nar.setRun(null);
  s.clock.setModel(fakeModel(9000));
  await s.nar.setRun(card());
  assert.equal(s.nar.on, true); assert.equal(s.nar.active, true); assert.equal(s.clock.driven, true);
  assert.equal(s.clock.playing, false, 'armed, not autoplaying');
  s.clock.play(); assert.equal(s.audio.paused, false);
});

test('narration: a stale setRun (superseded or aborted) changes nothing', async () => {
  const r = rig();
  const ac = new AbortController();
  const p = r.nar.setRun(card(), ac.signal);
  ac.abort();
  await p;
  assert.equal(r.nar.available, false); assert.deepEqual(r.avail, []);
  const p1 = r.nar.setRun(card());
  const p2 = r.nar.setRun(null);
  await Promise.all([p1, p2]);
  assert.equal(r.nar.available, false); assert.deepEqual(r.avail, []);
});

test('narration: captions name the broadcast role (play-by-play vs colour), never a voice', async () => {
  assert.deepEqual(SPEAKER, { guide: 'Play-by-play', expert: 'Color' });
  const r = rig();
  await r.nar.setRun(card());
  r.nar.toggle();                                   // t = 0: the intro cue (guide) is current
  assert.deepEqual(r.cues.at(-1), { speaker: 'Play-by-play', text: 'Placeholder one.', voice: 'guide' });
  r.audio.currentTime = 5.2; r.audio.emit('timeupdate');
  assert.deepEqual(r.cues.at(-1), { speaker: 'Color', text: 'Placeholder two.', voice: 'expert' });
  r.audio.currentTime = 7.5; r.clock.advance(16);
  assert.equal(r.cues.at(-1).voice, 'expert');
  const n = r.cues.length;
  r.audio.emit('timeupdate'); r.clock.advance(16);
  assert.equal(r.cues.length, n, 'the same cue is not re-sent every frame');
});

test('narration: a seek before metadata is held and re-applied on loadedmetadata (WebKit drops it)', async () => {
  const r = rig();
  await r.nar.setRun(card());
  // An engine that ignores currentTime while it has no metadata (preload="none", first toggle).
  let ct = 0;
  r.audio.readyState = 0;
  Object.defineProperty(r.audio, 'currentTime', { get: () => ct, set: (v) => { if (r.audio.readyState >= 1) ct = v; }, configurable: true });
  r.clock.seek(2500, 'key');
  r.nar.toggle();
  assert.equal(ct, 0, 'the engine dropped the seek');
  r.clock.advance(16);
  assert.equal(r.clock.t, 2500, 'run does not snap back to 0 while the mp3 loads');
  r.clock.seek(6000, 'scrub');                      // a second seek before metadata replaces the first
  r.clock.advance(16);
  assert.equal(r.clock.t, 6000);
  r.audio.readyState = 1; r.audio.emit('loadedmetadata');
  assert.equal(ct, 6.5, 'pending position applied once metadata is known');
  r.audio.readyState = 4; r.clock.advance(16);
  assert.equal(r.clock.t, 6000);
  ct = 6.6; r.clock.advance(16);
  assert.equal(r.clock.t, 6600, 'then the audio drives the clock again');
});

test('narration: turning on near the start, or over the unattended preview, plays the intro', async () => {
  const r = rig();
  await r.nar.setRun(card());
  const seeks = []; r.clock.on('seek', (e) => seeks.push(e.cause));
  r.clock.seek(INTRO_SNAP_MS - 500, 'url');           // e.g. a shared link a moment into the run
  r.nar.toggle();
  assert.equal(r.audio.currentTime, 0); assert.equal(r.clock.t, 0);
  assert.deepEqual(r.cues.at(-1), { speaker: 'Play-by-play', text: 'Placeholder one.', voice: 'guide' });
  assert.ok(seeks.includes('api'), 'listeners see the jump back as a seek');

  // startAtTop (the player passes "not interacted yet"): the hero preview is not where the user chose to be
  let preview = true;
  const clock = createClock(); clock.setModel(fakeModel(9000));
  const audio = new FakeAudio(); const cues = [];
  const nar = createNarration({ clock, audio, timeline: { setNarrating() {} }, base: new URL('https://example.test/site/data/'), doc: null,
    captions: { setCue: (c) => cues.push(c) }, onAvailability() {}, startAtTop: () => preview, onInteract: () => { preview = false; },
    fetchJSON: async (u) => (u.endsWith('index.json') ? nIndex({ r1: { hash: '0123abcd', manifest: 'r1.json', durationMs: 9000 } }) : manifest()) });
  await nar.setRun(card());
  clock.play(); clock.seek(6000, 'api');               // the hero preview is mid-run
  nar.toggle();
  assert.equal(audio.currentTime, 0, 'preview: the intro plays'); assert.equal(clock.t, 0); assert.equal(clock.playing, true);
  nar.toggle(); clock.seek(6000, 'key');
  nar.toggle();
  assert.equal(audio.currentTime, 6.5, 'after an interaction: narration joins at the current moment');
});

test('narration over the hero preview: the player path (pointerdown + act) still plays the intro', async () => {
  // Mirrors player.ts: `interacted` is set by the root pointerdown and by act() BEFORE narration.toggle() runs, so
  // startAtTop must read the preview gate, which only autoplay starts and only user seeks / other actions end.
  const clock = createClock(); clock.setModel(fakeModel(9000));
  const audio = new FakeAudio();
  const preview = createPreviewGate();
  let interacted = false;
  clock.on('seek', (e) => preview.seek(e.cause));
  const nar = createNarration({ clock, audio, timeline: { setNarrating() {} }, base: new URL('https://example.test/site/data/'), doc: null,
    captions: { setCue() {} }, onAvailability() {}, onInteract: () => { interacted = true; }, startAtTop: () => preview.active,
    fetchJSON: async (u) => (u.endsWith('index.json') ? nIndex({ r1: { hash: '0123abcd', manifest: 'r1.json', durationMs: 9000 } }) : manifest()) });
  await nar.setRun(card());
  const act = (a) => { interacted = true; if (a !== 'narration') preview.end(); if (a === 'narration') { nar.toggle(); preview.end(); } };
  const pointerdown = () => { interacted = true; };

  preview.start(); clock.play();                       // autoplay
  clock.advance(6000);                                 // the preview loop plays on (and may restart via 'api' seeks)
  clock.seek(clock.t, 'api');
  assert.ok(clock.t > INTRO_SNAP_MS && preview.active);
  pointerdown(); act('narration');                     // click on the headphones button
  assert.equal(interacted, true);
  assert.equal(audio.currentTime, 0, 'the intro plays'); assert.equal(clock.t, 0);
  assert.equal(preview.active, false, 'narration on: the preview is over');

  act('narration');                                    // off; the user seeks; on again: joins at the moment
  clock.seek(6000, 'scrub');
  act('narration');
  assert.equal(audio.currentTime, 6.5);

  // the 'n' key path is the same act('narration'); other actions end the preview first
  act('narration'); preview.start(); act('toggle'); act('toggle'); clock.seek(6000, 'api');
  act('narration');
  assert.equal(audio.currentTime, 6.5, 'after pausing and resuming, the user owns the position');
  for (const c of ['scrub', 'key', 'marker', 'feed', 'url']) { preview.start(); preview.seek(c); assert.equal(preview.active, false, c); }
  for (const c of ['api', 'end', 'audio', 'tour']) { preview.start(); preview.seek(c); assert.equal(preview.active, true, c); }
  assert.equal(USER_SEEKS.size, 5);
});

// ---------- captions layer (tiny fake DOM; placeholder text only)
function fakeDom() {
  class El {
    constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this._text = ''; this.className = '';
      const cls = new Set(); this.classList = { toggle: (c, on) => { (on ?? !cls.has(c)) ? cls.add(c) : cls.delete(c); }, contains: (c) => cls.has(c), add: (c) => cls.add(c), remove: (c) => cls.delete(c) }; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    get hidden() { return 'hidden' in this.attrs; }
    set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
    appendChild(c) { this.children.push(c); return c; }
    addEventListener() {}
    get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
    set textContent(v) { this.children = []; this._text = String(v); }
  }
  return { createElement: (t) => new El(t), createTextNode: (t) => ({ textContent: String(t) }) };
}

test('captions: cue shows the role label, tags the voice for styling, announces once, restores the chapter', async () => {
  const prev = globalThis.document;
  globalThis.document = fakeDom();
  try {
    const { createCaptions } = await js('ui/captions.js');
    const cap = createCaptions();
    const [txt] = cap.el.children; const [spk, body] = txt.children;
    cap.setChapters([{ t: 100, p: 1, k: 'post', label: 'Chapter placeholder', caption: '' }]);
    cap.setTime(200, false);
    assert.equal(body.textContent, 'Chapter placeholder'); assert.equal(spk.hidden, true); assert.equal(cap.el.hidden, false);
    cap.setCue({ speaker: SPEAKER.guide, text: 'Placeholder one.', voice: 'guide' });
    assert.equal(spk.textContent, 'Play-by-play'); assert.equal(spk.hidden, false);
    assert.equal(body.textContent, 'Placeholder one.');
    assert.equal(cap.el.dataset.voice, 'guide'); assert.equal(cap.el.classList.contains('cap-cue'), true);
    assert.equal(cap.live.textContent, 'Play-by-play: Placeholder one.');
    cap.setTime(300, true);
    assert.equal(body.textContent, 'Placeholder one.', 'a chapter does not overwrite a spoken cue');
    cap.setCue({ speaker: SPEAKER.expert, text: 'Placeholder two.', voice: 'expert' });
    assert.equal(spk.textContent, 'Color'); assert.equal(cap.el.dataset.voice, 'expert');
    assert.equal(cap.live.textContent, 'Color: Placeholder two.', 'each new cue is announced without the throttle');
    cap.enabled = false; cap.setTime(300, false);
    assert.equal(cap.el.hidden, true, 'CC off hides cues too');
    cap.enabled = true;
    cap.setCue(null);
    assert.equal(spk.hidden, true); assert.equal(cap.el.dataset.voice, undefined); assert.equal(cap.el.classList.contains('cap-cue'), false);
    assert.equal(body.textContent, 'Chapter placeholder');
  } finally { globalThis.document = prev; }
});

// ---------- committed data (structure only; nothing is printed)
const NDIR = join(root, 'data', 'narration');
test('data: committed narration manifests validate and reference their mp3', { skip: !existsSync(join(NDIR, 'index.json')) && 'no narration built' }, () => {
  const ix = assertNarrationIndex(JSON.parse(readFileSync(join(NDIR, 'index.json'), 'utf8')));
  const runs = JSON.parse(readFileSync(join(root, 'data', 'index.json'), 'utf8')).runs;
  for (const [id, e] of Object.entries(ix.runs)) {
    const m = assertNarration(JSON.parse(readFileSync(join(NDIR, e.manifest), 'utf8')));
    assert.equal(m.runId, id);
    assert.ok(runs.some((r) => r.id === id), 'run is listed');
    const mp3 = new URL(m.audio, new URL(`file://${NDIR}/${e.manifest}`)).pathname;
    assert.ok(existsSync(mp3), 'mp3 exists');
    assert.equal(statSync(mp3).size, m.audioBytes, 'audioBytes');
  }
});
