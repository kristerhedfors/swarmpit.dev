// Swarm Observatory web UI entry: shell, hash router, theme, health and the shared live connection.
import './theme.css';
import './keys';
import { api, MOCK } from './api';
import { h, replace } from './dom';
import { invalidateColors, slot } from './lib/colors';
import { ago } from './lib/format';
import type { LiveMessage } from './lib/types';
import { parseHash, type Route } from './router';
import { live } from './sse';
import { app } from './store';
import { helpView } from './views/help';
import { liveView } from './views/live';
import { overviewView } from './views/overview';
import { runsView } from './views/runs';
import { runView } from './views/run';
import type { View } from './views/view';

const VIEWS: Record<string, () => View> = { overview: overviewView, runs: runsView, run: runView, live: liveView, help: helpView };

// ---------------------------------------------------------------- theme
function applyTheme(repaint = false) {
  const pref = app.get().theme;
  const root = document.documentElement;
  if (pref === 'auto') delete root.dataset.theme; else root.dataset.theme = pref;
  const dark = pref === 'dark' || (pref === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  root.dataset.resolvedTheme = dark ? 'dark' : 'light';
  invalidateColors();
  if (repaint) repaintView();
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme(true));
const urlTheme = new URLSearchParams(location.search).get('theme');
if (urlTheme === 'light' || urlTheme === 'dark') app.set({ theme: urlTheme });
applyTheme();

// ---------------------------------------------------------------- shell
const navLinks: Record<string, HTMLAnchorElement> = {};
const healthEl = h('span', { class: 'tnum' });
const liveDot = h('span', { title: 'live connection' });
const themeBtn = h('button', { class: 'btn', title: 'Theme: auto / light / dark' });
function themeLabel() { const t = app.get().theme; themeBtn.textContent = t === 'auto' ? '◐ auto' : t === 'light' ? '☀ light' : '☾ dark'; }
themeBtn.addEventListener('click', () => {
  const order = ['auto', 'light', 'dark'] as const;
  const next = order[(order.indexOf(app.get().theme) + 1) % 3];
  app.set({ theme: next }); localStorage.setItem('obs.theme', next); applyTheme(true); themeLabel();
});
themeLabel();
const nav = h('nav', { class: 'nav' }, ...(['overview', 'runs', 'live', 'help'] as const).map((v) => (navLinks[v] = h('a', { href: '#/' + (v === 'overview' ? '' : v) }, v === 'overview' ? 'Overview' : v === 'runs' ? 'Runs' : v === 'live' ? 'Live' : 'Help'))));
const main = h('main', { id: 'main' });
const topbar = h('header', { class: 'topbar' },
  h('a', { class: 'brand', href: '#/' }, h('span', { class: 'dots' }, h('i', { style: { background: slot(0) } }), h('i', { style: { background: slot(1) } }), h('i', { style: { background: slot(2) } })), 'Swarm Observatory'),
  nav,
  h('div', { class: 'right' }, MOCK ? h('span', { class: 'badge-mock', title: 'Fixture data generated in the browser (?mock=1)' }, 'MOCK DATA') : null, liveDot, healthEl, h('span', { class: 'muted' }, 'read-only'), themeBtn));
replace(document.getElementById('app')!, topbar, main);

function renderLiveDot() {
  const s = live.status;
  liveDot.replaceChildren(h('span', { class: 'pill ' + (s === 'open' ? 'live' : s === 'retrying' ? 'errored' : 'pending') }, h('i', { class: 'dot' }), s === 'open' ? 'live feed' : s === 'retrying' ? 'reconnecting' : s));
}
app.sub(renderLiveDot);
renderLiveDot();

async function pollHealth() {
  try {
    const hl = await api.health();
    healthEl.textContent = `${hl.n_runs} runs · ${hl.n_live} live · ingest ${hl.last_ingest_ts ? ago(hl.last_ingest_ts) : '–'}`;
    healthEl.title = `db ${hl.db}\nruns ${hl.runs_dir}\nlive tap ${hl.live_tap.enabled ? 'on' : 'off'} (${hl.live_tap.live_vms} VMs, ${hl.live_tap.errors} errors)\nschema v${hl.schema_version} derive v${hl.derive_version}`;
  } catch (e) {
    healthEl.textContent = 'API unreachable';
    healthEl.title = String(e);
  }
}
void pollHealth();
setInterval(pollHealth, 15000);

// ---------------------------------------------------------------- router
let current: { name: string; view: View; route: Route } | null = null;
function route() {
  const r = parseHash();
  const name = VIEWS[r.view] ? r.view : 'overview';
  for (const [k, a] of Object.entries(navLinks)) a.classList.toggle('active', k === name || (k === 'runs' && name === 'run'));
  if (current && current.name === name && current.view.update?.(r)) { current.route = r; return; }
  current?.view.destroy();
  const view = VIEWS[name]();
  current = { name, view, route: r };
  replace(main, view.el);
  view.mount(r);
  window.scrollTo(0, 0);
}
function remount() { if (current) { current.view.destroy(); current = null; } route(); }
// Theme changes repaint in place (no refetch, no lost view state such as arcs/zoom/follow); only views
// without an onTheme hook (stateless pages) are re-mounted.
function repaintView() { if (!current) return; if (current.view.onTheme) current.view.onTheme(); else remount(); }
window.addEventListener('hashchange', route);
route();

// one shared SSE connection; views get every message
live.subscribe((m: LiveMessage) => current?.view.onLive?.(m));
