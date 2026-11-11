import type { Captions, ChapterRec, PlaybackClock, SceneEngine } from '../types.js';

/** Caption-driven camera nudges on p1 story beats (SPEC 12.4). Any camera interaction disables them. */
export function createTour(engine: SceneEngine, captions: Captions, clock: PlaybackClock, reducedMotion: boolean): { dispose(): void } {
  let disabled = reducedMotion;
  let lastTouch = -Infinity;
  const off = engine.on('cameraInteract', () => { lastTouch = performance.now(); disabled = true; });
  captions.onBeat((ch: ChapterRec) => {
    if (disabled || !clock.playing || ch.node === undefined || ch.node < 0) return;
    if (performance.now() - lastTouch < 5000) return;
    engine.focusNode(ch.node, { nudge: 0.35, ms: 900 });
  });
  return { dispose: off };
}
