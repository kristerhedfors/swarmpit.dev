import type { ChapterKind } from './types.js';
import { createLoader } from './data/loader.js';
import { detectEnv } from './util/env.js';
import { readUrl } from './player/url.js';
import { createPlayer } from './player/player.js';
import type { SitePlayer } from './player/player.js';
import { renderFindings } from './ui/findings.js';
import { h } from './util/dom.js';

async function boot(): Promise<void> {
  const env = detectEnv();
  const html = document.documentElement;
  html.classList.add('js');
  html.classList.toggle('reduced-motion', env.reducedMotion);
  html.classList.toggle('coarse', env.coarse);

  // Theme toggle (dark-first; persisted).
  const themeBtn = document.getElementById('theme-toggle');
  const applyTheme = (t: string | null): void => {
    if (t === 'light' || t === 'dark') html.dataset['theme'] = t; else delete html.dataset['theme'];
    themeBtn?.setAttribute('aria-pressed', String(html.dataset['theme'] === 'light'));
  };
  let stored: string | null = null;
  try { stored = localStorage.getItem('swarmpit-theme'); } catch { /* private mode */ }
  applyTheme(stored ?? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : null));
  themeBtn?.addEventListener('click', () => {
    const next = html.dataset['theme'] === 'light' ? 'dark' : 'light';
    applyTheme(next);
    try { localStorage.setItem('swarmpit-theme', next); } catch { /* ignore */ }
    window.dispatchEvent(new Event('themechange'));
  });

  const root = document.getElementById('player');
  if (!root) return;
  const loader = createLoader(new URL('data/', document.baseURI));
  let index;
  try {
    index = await loader.index();
  } catch (e) {
    root.querySelector('.stage')?.appendChild(h('div.pl-error', { role: 'alert' }, h('p', null, `Could not load the swarm index (${(e as Error).message}).`),
      h('button.ec-btn.primary', { type: 'button', onclick: () => location.reload() }, 'Retry')));
    return;
  }

  const fh = document.getElementById('findings-host');
  if (fh) renderFindings(fh, index);
  const gen = document.getElementById('gen-info');
  if (gen) gen.textContent = `Data generated ${index.generated.slice(0, 10)} · ${index.runs.filter((r) => !r.hidden).length} runs · code ${index.generator.codeVersions.slice(-1)[0] ?? ''}`;
  const red = document.getElementById('redaction-info');
  if (red) {
    const rules = Object.entries(index.generator.redactions).filter(([k]) => k !== 'ctf_mask' || index.generator.ctf === 'mask');
    red.textContent = rules.map(([k, n]) => `${k.replace('_', ' ')} (${n})`).join(' · ');
  }

  const url = readUrl();
  const id = url.run && index.runs.some((r) => r.id === url.run) ? url.run : index.featured || index.runs.find((r) => !r.hidden)?.id;
  let player: SitePlayer;
  try {
    player = createPlayer({ root, index, loader, env, url });
  } catch (e) {
    console.error(e);
    root.classList.add('no-gl');
    return;
  }
  if (id) await player.load(id);

  // Autoplay once the player is >= 50% visible (never under reduced motion / save-data).
  if (typeof IntersectionObserver === 'function') {
    const io = new IntersectionObserver((es) => {
      for (const e of es) if (e.intersectionRatio >= 0.5) { player.autoplay(); io.disconnect(); }
    }, { threshold: [0.5] });
    io.observe(root);
  }

  document.querySelectorAll<HTMLButtonElement>('[data-showme]').forEach((b) => {
    b.addEventListener('click', () => player.showMe((b.dataset['showme'] ?? '').split(',') as ChapterKind[]));
  });
  (window as unknown as { swarmpit: SitePlayer }).swarmpit = player;
}

void boot();
