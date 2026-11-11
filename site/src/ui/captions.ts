import type { Captions, ChapterRec } from '../types.js';
import { h, text } from '../util/dom.js';

/** A narration cue shown in place of the chapter caption (NARRATION.md 9.4). `speaker` is the role label shown
 * (play-by-play or colour commentator, never a voice name); `voice` is the role token used for styling. */
export interface CaptionCue { speaker: string; text: string; voice?: 'guide' | 'expert' }
export interface CaptionLayer extends Captions {
  readonly el: HTMLElement; readonly live: HTMLElement;
  /** Narration cue (null clears it). While set it replaces the chapter caption; each new cue is announced once. */
  setCue(cue: CaptionCue | null): void;
}

export function createCaptions(): CaptionLayer {
  let chs: ChapterRec[] = [], lastT = 0, cur: ChapterRec | null = null, lastAnnounce = -Infinity, pendingAnnounce: string | null = null;
  let cue: CaptionCue | null = null;
  let beat: (c: ChapterRec) => void = () => undefined;
  const spk = h('span.cap-spk', { hidden: true });
  const body = h('span');
  const txt = h('span.cap-text', null, spk, body);
  const el = h('div.caption', { 'aria-hidden': 'true', hidden: true, 'data-tag-avoid': '' }, txt);
  const live = h('div.sr-only', { 'aria-live': 'polite', role: 'status' });
  const announce = (s: string): void => {
    const now = performance.now();
    if (now - lastAnnounce < 3000) { pendingAnnounce = s; return; }
    lastAnnounce = now; pendingAnnounce = null; live.textContent = s;
  };
  const show = (): void => { el.hidden = !api.enabled || (!cue && !cur); };
  const api: CaptionLayer = {
    el, live, enabled: true,
    setChapters(c) { chs = c.filter((x) => x.p === 1).sort((a, b) => a.t - b.t); cur = null; lastT = 0; if (!cue) el.hidden = true; },
    setTime(t, playing) {
      // Beats: p1 chapters crossed forward during continuous playback.
      if (playing && t > lastT && t - lastT < 2500) for (const c of chs) if (c.t > lastT && c.t <= t) beat(c);
      lastT = t;
      let best: ChapterRec | null = null;
      for (const c of chs) { if (c.t > t) break; if (t - c.t <= 6000) best = c; }
      if (best !== cur) {
        cur = best;
        if (best && !cue) { text(body, best.caption || best.label); announce(best.caption || best.label); }
      }
      if (pendingAnnounce && performance.now() - lastAnnounce >= 3000) announce(pendingAnnounce);
      show();
    },
    setCue(c) {
      if (c === cue || (c && cue && c.speaker === cue.speaker && c.text === cue.text && c.voice === cue.voice)) return;
      cue = c;
      if (c?.voice) el.dataset['voice'] = c.voice; else delete el.dataset['voice'];
      el.classList.toggle('cap-cue', !!c);
      if (c) {
        spk.hidden = false; text(spk, c.speaker); text(body, c.text);
        // Spoken lines are announced as they start, without the chapter throttle.
        pendingAnnounce = null; lastAnnounce = performance.now(); live.textContent = `${c.speaker}: ${c.text}`;
      } else {
        spk.hidden = true;
        text(body, cur ? cur.caption || cur.label : '');
      }
      show();
    },
    onBeat(cb) { beat = cb; },
  };
  return api;
}
