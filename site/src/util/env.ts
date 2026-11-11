import type { Env } from '../types.js';

export function detectEnv(): Env {
  const mm = (q: string): boolean => typeof matchMedia === 'function' && matchMedia(q).matches;
  let webgl2 = false;
  try {
    const gl = document.createElement('canvas').getContext('webgl2');
    webgl2 = !!gl;
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
  } catch { webgl2 = false; }
  const nav = navigator as Navigator & { connection?: { saveData?: boolean } };
  return {
    webgl2,
    reducedMotion: mm('(prefers-reduced-motion: reduce)'),
    coarse: mm('(pointer: coarse)'),
    saveData: !!nav.connection?.saveData,
    dpr: Math.min(window.devicePixelRatio || 1, 2),
    touch: 'ontouchstart' in window || navigator.maxTouchPoints > 0,
  };
}
