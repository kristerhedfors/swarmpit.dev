import type { SeekCause } from '../types.js';

/* The unattended hero preview (autoplay before the user has done anything). Narration turned on over it starts
 * with the intro duet instead of joining mid-run (NARRATION.md 9.4). This is NOT the player's `interacted` flag:
 * the pointerdown that clicks the narration button, and act() itself, set `interacted` before the toggle runs,
 * so the preview state has to survive until the toggle has read it. Only things that move or replace the run
 * (seeks the user makes, loading another run, other actions) end the preview. */
export interface PreviewGate {
  readonly active: boolean;
  start(): void;                 // autoplay began
  end(): void;                   // the user took over (or the run changed)
  seek(cause: SeekCause): void;  // clock seek listener: user-caused seeks end the preview
}

/** Seek causes that come from the user. 'api' (also used by the preview loop), 'end', 'audio' and 'tour' do not. */
export const USER_SEEKS: ReadonlySet<SeekCause> = new Set<SeekCause>(['scrub', 'key', 'marker', 'feed', 'url']);

export function createPreviewGate(): PreviewGate {
  let active = false;
  return {
    get active() { return active; },
    start() { active = true; },
    end() { active = false; },
    seek(cause) { if (USER_SEEKS.has(cause)) active = false; },
  };
}
