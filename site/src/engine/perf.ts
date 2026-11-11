// ?perf=1 overlay (SPEC 7.6): frame ms p50/p95 over 120 frames, draw calls, counts, backend, DPR.

export interface PerfOverlay {
  record(ms: number, info: { draws: number; nodes: number; edges: number; trail: number; pulses: number; backend: string; dpr: number }): void;
  dispose(): void;
}

export function createPerf(host: HTMLElement): PerfOverlay {
  const el = document.createElement('div');
  el.className = 'perf';
  el.style.cssText = 'position:absolute;right:8px;top:8px;z-index:5;font:11px/1.35 ui-monospace,Menlo,monospace;' +
    'background:rgba(0,0,0,.72);color:#e8e6dc;padding:6px 8px;border-radius:6px;pointer-events:none;white-space:pre';
  host.appendChild(el);
  const ring = new Float32Array(120), sorted = new Float32Array(120);
  let n = 0, k = 0, last = 0;
  return {
    record(ms, i) {
      ring[k] = ms; k = (k + 1) % 120; if (n < 120) n++;
      const now = performance.now();
      if (now - last < 250) return;
      last = now;
      sorted.set(ring); const s = sorted.subarray(0, n).sort();
      const p = (q: number) => s[Math.min(n - 1, Math.floor(q * n))]!.toFixed(2);
      el.textContent = `frame p50 ${p(0.5)} ms  p95 ${p(0.95)} ms\ndraws ${i.draws}  ${i.backend} @${i.dpr}x\n` +
        `nodes ${i.nodes}  edges ${i.edges}\ntrail ${i.trail}  pulses ${i.pulses}`;
    },
    dispose() { el.remove(); },
  };
}
