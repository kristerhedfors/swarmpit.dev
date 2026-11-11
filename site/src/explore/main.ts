// Entry point of explore.html: loads data/instigation/explorer.json, keeps the view state in the URL hash and
// re-renders the active view on every state change (cheap: a few hundred rows).
import { h } from '../util/dom.js';
import { applyFilters, normalize } from './data.js';
import { renderDetail } from './detail.js';
import { renderEpisodes } from './episodes.js';
import { renderFilters } from './filters.js';
import { renderLandscape } from './landscape.js';
import { renderLineage } from './lineage.js';
import { renderModels } from './models.js';
import { renderPivot } from './pivot.js';
import { VIEWS, decodeState, encodeState, reconcile } from './state.js';
import type { Explorer, State, ViewId } from './types.js';
import { createTip } from './ui.js';
import type { Ctx } from './ui.js';

const RENDER: Record<ViewId, (host: HTMLElement, ctx: Ctx) => void> = {
  pivot: renderPivot, landscape: renderLandscape, episodes: renderEpisodes, lineage: renderLineage, models: renderModels,
};

function theme(): void {
  const html = document.documentElement;
  const btn = document.getElementById('theme-toggle');
  const apply = (t: string | null): void => {
    if (t === 'light' || t === 'dark') html.dataset['theme'] = t; else delete html.dataset['theme'];
    btn?.setAttribute('aria-pressed', String(html.dataset['theme'] === 'light'));
  };
  let stored: string | null = null;
  try { stored = localStorage.getItem('swarmpit-theme'); } catch { /* private mode */ }
  apply(stored ?? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : null));
  btn?.addEventListener('click', () => {
    const next = html.dataset['theme'] === 'light' ? 'dark' : 'light';
    apply(next);
    try { localStorage.setItem('swarmpit-theme', next); } catch { /* ignore */ }
  });
}

async function load(): Promise<Explorer> {
  const res = await fetch(new URL('data/instigation/explorer.json', document.baseURI).href, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return normalize(await res.json());
}

function boot(): void {
  theme();
  const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;
  const filtersHost = $('ex-filters'), viewHost = $('ex-view'), detailHost = $('ex-detail'), meta = $('ex-meta');
  const tabs = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"][data-view]')];
  const tip = createTip(document.body);
  viewHost.textContent = 'Loading the explorer data…';

  load().then((ex) => {
    let state: State = reconcile(decodeState(location.hash), ex);
    const nModels = new Set(ex.episodes.map((e) => e.model)).size;
    const retired = ex.dims.model.filter((m) => m.retired).map((m) => m.label);
    meta.replaceChildren(`${ex.episodes.length} episodes · ${Object.keys(ex.seeds).length} seeds · ${nModels} models`,
      ex.generated_ts ? ` · data generated ${new Date(ex.generated_ts * 1000).toISOString().slice(0, 10)}` : '',
      retired.length ? ` · retired: ${retired.join(', ')}` : '',
      ex.dropped ? ` · ${ex.dropped} malformed rows skipped` : '');

    // data-fk of the control that opened the detail panel (focus returns there on close)
    let opener: string | null = null;
    const focusFk = (fk: string): boolean => {
      const el = document.querySelector<HTMLElement>(`[data-fk="${CSS.escape(fk)}"]`);
      if (!el) return false;
      if (el.matches('[data-cell], .ex-node')) {
        el.closest('table, svg')?.querySelectorAll('[data-cell], .ex-node').forEach((c) => c.setAttribute('tabindex', c === el ? '0' : '-1'));
      }
      el.focus({ preventScroll: true });
      return true;
    };
    const focusLost = (): boolean => { const a = document.activeElement; return !a || a === document.body || !a.isConnected; };

    const render = (focusDetail = false): void => {
      tip.hide();
      const active = document.activeElement as HTMLElement | null;
      const hadFocus = !!active && active !== document.body;
      const fk = active?.getAttribute('data-fk') ?? null;
      const eps = applyFilters(ex, state.filters, state.seed);
      const ctx: Ctx = { ex, state, eps, set, openEpisode, closeEpisode, tip };
      renderFilters(filtersHost, ctx);
      for (const t of tabs) {
        const on = t.dataset['view'] === state.view;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        if (on) viewHost.setAttribute('aria-labelledby', t.id);
      }
      RENDER[state.view](viewHost, ctx);
      const ep = state.ep ? ex.episodes.find((e) => e.run_id === state.ep) : undefined;
      if (ep) {
        renderDetail(detailHost, ctx, ep, focusDetail);
        document.body.classList.add('ex-has-detail');
      } else {
        detailHost.hidden = true;
        detailHost.replaceChildren();
        document.body.classList.remove('ex-has-detail');
      }
      if (focusDetail) return;
      if (fk) focusFk(fk);
      // the focused control went away (view switch, drill-down, Back): land on the view panel, not <body>
      if (hadFocus && focusLost()) viewHost.focus({ preventScroll: true });
    };

    function set(patch: Partial<State>, push = false): void {
      const prevEp = state.ep;
      state = { ...state, ...patch };
      const hash = '#' + encodeState(state);
      if (hash !== location.hash && !(hash === '#' && !location.hash)) {
        if (push) history.pushState(null, '', hash); else history.replaceState(null, '', hash);
      }
      render(!!state.ep && state.ep !== prevEp);
      if (patch.view && push) viewHost.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    function openEpisode(runId: string): void {
      const a = document.activeElement as HTMLElement | null;
      // opening a sibling from inside the panel keeps the original opener
      if (!(a && detailHost.contains(a))) opener = a?.getAttribute('data-fk') ?? `e:open:${runId}`;
      set({ ep: runId });
    }
    function closeEpisode(): void {
      const runId = state.ep;
      set({ ep: '' });
      if (!(opener && focusFk(opener)) && !focusFk(`e:open:${runId}`)) viewHost.focus({ preventScroll: true });
      opener = null;
    }

    for (const t of tabs) {
      t.addEventListener('click', () => set({ view: t.dataset['view'] as ViewId }, true));
      t.addEventListener('keydown', (e) => {
        const i = tabs.indexOf(t);
        const k = e.key;
        const j = k === 'ArrowRight' ? (i + 1) % tabs.length : k === 'ArrowLeft' ? (i - 1 + tabs.length) % tabs.length : k === 'Home' ? 0 : k === 'End' ? tabs.length - 1 : -1;
        if (j < 0) return;
        e.preventDefault();
        const to = tabs[j] as HTMLButtonElement;
        set({ view: to.dataset['view'] as ViewId }, true);
        to.focus();
      });
    }
    window.addEventListener('popstate', () => {
      state = reconcile(decodeState(location.hash), ex);
      render();
      if (focusLost()) viewHost.focus({ preventScroll: true });
    });
    window.addEventListener('hashchange', () => {
      if (location.hash.replace(/^#/, '') !== encodeState(state)) { state = reconcile(decodeState(location.hash), ex); render(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || detailHost.hidden) return;
      closeEpisode();
    });
    if (!VIEWS.includes(state.view)) state.view = 'pivot';
    render();
  }).catch(() => {
    viewHost.replaceChildren(h('div.ex-none', { role: 'alert' },
      h('p', null, 'The explorer data (data/instigation/explorer.json) is not available yet.'),
      h('p.muted', null, 'It is generated from the study runs by the exporter and published with the site.')));
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
