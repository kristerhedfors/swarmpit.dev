import { BundleError, RATES } from '../types.js';
import type {
  AgentState, ChapterKind, Env, FilterMask, IndexBundle, KeyAction, Loader, PickHit, Player, RunCard, RunModel, SwarmState, UrlState, ViewMode,
} from '../types.js';
import { createEngine } from '../engine/engine.js';
import { buildModel, KI } from '../data/model.js';
import { createFilterStore } from '../data/filters.js';
import { nextRun, siblingRun } from '../data/runs.js';
import { createClock } from './clock.js';
import { bindKeys } from './keys.js';
import { readScoped, readUrl, writeScoped, writeUrl } from './url.js';
import { createTour } from './tour.js';
import { createNarration } from './narration.js';
import type { NarrationController } from './narration.js';
import { createPreviewGate } from './preview.js';
import { createTimeline } from '../ui/timeline.js';
import { createSelector } from '../ui/selector.js';
import type { LocalLane } from '../ui/selector.js';
import { createFiltersPanel } from '../ui/filtersPanel.js';
import { createTooltip } from '../ui/tooltip.js';
import { createDetail } from '../ui/detail.js';
import { createFeed } from '../ui/feed.js';
import { createCaptions } from '../ui/captions.js';
import { createEndCard } from '../ui/endcard.js';
import { createHud } from '../ui/hud.js';
import { createNodeList } from '../ui/nodeList.js';
import { createHelp } from '../ui/help.js';
import { createGraphKey } from '../ui/graphKey.js';
import { h, cssVar } from '../util/dom.js';
import { fmtT } from '../util/format.js';

export interface PlayerOpts {
  root: HTMLElement;          // #player
  index: IndexBundle;         // index.runs is owned by the player once passed: change it only via addRun/updateRun/removeRun
  loader: Loader;
  env: Env;
  url: UrlState;
  // ---- embedding options (docs/browser-swarm.md §7). Every default is the swarmpit.dev behaviour.
  /** false: no narration controller and no <audio> element; the narration button stays hidden. Default true. */
  narration?: boolean;
  /** URL hash sync. undefined: the full shareable state (#run=..&t=..). A key string: only `#<key>=<id>`, other hash
   *  keys untouched, hashchange on that key loads the run (must be non-empty). null: no URL writes or hashchange
   *  handling at all. */
  urlKey?: string | null;
  /** The end card's "See the numbers" action (scrolls to #findings). Default true. */
  findings?: boolean;
  /** Selector action chips (+ / upload / download / delete), each shown only when given. */
  onNew?: () => void;
  onUpload?: () => void;
  onDownload?: (id: string) => void;
  onDelete?: (id: string) => void;
  /** The selector's 'Local' lane (see ui/selector.ts LocalLane). */
  local?: LocalLane;
  /** Graph key box open on first visit (before the visitor toggles it). Default: open on wide screens. */
  graphKeyOpen?: boolean;
}
/** What updateRun carries across an in-place reload of the loaded run (a live trace re-converted with more events). */
export interface InPlaceReload { t: number; playing: boolean; sel?: string }

export interface SitePlayer extends Player {
  /** keep: internal in-place reload (updateRun); callers pass only the id. */
  load(id: string, keep?: InPlaceReload): Promise<void>;
  readonly model: RunModel | null;
  autoplay(): void;
  showMe(kinds: ChapterKind[]): void;
  readonly interacted: boolean;
  /** Add a card to the run list (replaces a card with the same id). Does not load it. */
  addRun(card: RunCard): void;
  /** Replace a card (same id). If it is the loaded run, reload it in place keeping time, play state, view mode,
   *  camera, filters and the pinned node (a live trace re-converted with more events). */
  updateRun(card: RunCard): Promise<void>;
  /** Remove a card. If it was loaded, the next listed run loads, or the player goes empty (.pl-empty, no model). */
  removeRun(id: string): Promise<void>;
}

/** A narration controller that is never available (PlayerOpts.narration = false): nothing is fetched or created. */
const NO_NARRATION: NarrationController = {
  available: false, on: false, active: false,
  setRun: () => Promise.resolve(), toggle: () => undefined, dispose: () => undefined,
};

const IFACE_ORDER = ['forum', 'wiki', 'cache', 'irc'];
/** Camera and view mode, saved across engine.setRun (which resets both). */
interface SavedView { mode: ViewMode; target: Float32Array; distance: number; yaw: number; pitch: number }

export function createPlayer(o: PlayerOpts): SitePlayer {
  const { root, index, loader, env } = o;
  const rm = env.reducedMotion;
  const stage = root.querySelector<HTMLElement>('.stage')!;
  const glHost = root.querySelector<HTMLElement>('.stage-gl')!;
  const body = root.querySelector<HTMLElement>('.pl-body')!;
  const poster = root.querySelector<HTMLElement>('.poster');

  const clock = createClock();
  const filters = createFilterStore();
  const engine = createEngine(glHost, clock, { prefer: o.url.renderer, perf: o.url.perf });

  let m: RunModel | null = null;
  let mask: FilterMask = filters.mask();
  let first = true, interacted = false, loopsLeft = 0, labelsOn = true, follow = false;
  // A live trace (caveat 'live', re-converted as it grows) whose playback reached its end while playing: the
  // viewer is following the live edge, so the next updateRun with a later end resumes playing.
  let liveEdge = false;
  const isLive = (): boolean => !!m && m.card.caveats.includes('live');
  let abort: AbortController | null = null;
  let pinned: PickHit | null = null;
  const states: AgentState[] = [];
  let swarmScratch: SwarmState = { onBoard: [], clusters: [], liveLive: 0, onsetT: null };

  const isVis = (i: number): boolean => (mask.event[i] ?? 2) >= 2;

  // ---------- UI pieces
  const timeline = createTimeline({
    clock, coarse: env.coarse,
    isEventVisible: isVis,
    onMarkerHover: (ch) => engine.highlight(ch && ch.node !== undefined ? [ch.node] : pinnedHighlight()),
    onRangeSelect: (r) => filters.set({ range: r }),
    onToggle: (w) => act(w === 'filters' ? 'filters' : w === 'resetView' ? 'reset' : w),
    onFeed: () => act('feed'),
  });
  const selector = createSelector(index, {
    onPick: (id) => { interacted = true; void api.load(id); }, onPrefetch: (c) => loader.prefetch(c),
    actions: { onNew: o.onNew, onUpload: o.onUpload, onDownload: o.onDownload, onDelete: o.onDelete }, local: o.local,
  });
  const fpanel = createFiltersPanel(filters);
  const tooltip = createTooltip(stage);
  const detail = createDetail();
  const feed = createFeed();
  const captions = createCaptions();
  const endcard = createEndCard();
  endcard.setFindings(o.findings !== false);
  const hud = createHud();
  const nodeList = createNodeList();
  const help = createHelp();
  const graphKey = createGraphKey({ initialOpen: o.graphKeyOpen });
  const errBox = h('div.pl-error', { role: 'alert', hidden: true });
  const ripple = h('div.ripple', { 'aria-hidden': 'true', hidden: true });
  const loading = h('div.pl-loading', { 'aria-hidden': 'true', hidden: true }, h('span.spinner'), 'loading swarm…');

  root.querySelector('.sel-host')?.appendChild(selector.el);
  stage.append(hud.el, captions.el, ripple, loading, errBox, endcard.el, fpanel.el, detail.el, help.el, nodeList.el, timeline.el);
  // The graph key is a card in the left column; while it is open it takes the swarm selector's place there
  // (CSS .gk-open), so it never covers the graph. Its "Key" button sits at the end of the short hero legend.
  (root.querySelector('.pl-left') ?? stage).appendChild(graphKey.el);
  const legendList = root.querySelector('.legend');
  if (legendList) legendList.appendChild(h('li.gk-li', null, graphKey.chip)); else stage.appendChild(graphKey.chip);
  const syncKey = () => root.classList.toggle('gk-open', graphKey.isOpen);
  graphKey.onToggle(() => { syncKey(); queueInsets(); });
  syncKey();
  feed.el.dataset['tagAvoid'] = '';
  body.appendChild(feed.el);
  root.appendChild(captions.live);
  createTour(engine, captions, clock, rm);
  // Narration (NARRATION.md 9.4): one <audio> for the player's lifetime, nothing fetched until turned on.
  const preview = createPreviewGate();
  const narration = o.narration === false ? NO_NARRATION : createNarration({
    clock, captions, timeline, base: new URL('data/', document.baseURI), audio: new Audio(),
    onAvailability: (v) => timeline.setNarrationAvailable(v),
    onInteract: () => { interacted = true; },
    startAtTop: () => preview.active,   // turned on over the hero autoplay preview: start with the intro duet
  });
  if (narration === NO_NARRATION) timeline.setNarrationAvailable(false);
  const urlKey = o.urlKey;   // undefined: full state, string: one scoped key, null: off (PlayerOpts.urlKey)

  // ---------- helpers
  const card = (id: string): RunCard | undefined => index.runs.find((r) => r.id === id);
  const listed = (): RunCard[] => index.runs.filter((r) => !r.hidden);
  const nodeOfHit = (hit: PickHit | null): number => {
    if (!hit || !m) return -1;
    if (hit.kind === 'node') return hit.node;
    if (hit.kind === 'step') { const a = m.evA[hit.event]!; return a >= 0 ? m.core.agents[a]!.node : -1; }
    return m.edgeSrc[hit.edge]!;
  };
  const pinnedHighlight = (): number[] | null => { const n = nodeOfHit(pinned); return n >= 0 ? [n] : null; };
  const colorOf = (node: number): string => {
    if (!m) return '#888';
    const a = m.core.nodes[node]?.agent;
    const slot = a !== undefined ? (a % 8) + 1 : IFACE_ORDER.indexOf(m.card.iface) + 1;
    return cssVar(`--series-${slot}`, root) || '#888';
  };
  function pin(hit: PickHit | null, focus = false): void {
    pinned = hit;
    detail.pin(hit);
    root.classList.toggle('pinned', !!hit);
    engine.highlight(pinnedHighlight());
    const n = nodeOfHit(hit);
    engine.select?.(n >= 0 ? { kind: 'node', node: n } : null); // keep the engine's pin ring in sync (no 'select' emit)
    if (focus && n >= 0) userFocus(n);
    syncUrl();
  }
  /** The pinned node's id (pinned steps and edges have none), for the URL and in-place reloads. */
  const pinnedNodeId = (): string | undefined => (m && pinned?.kind === 'node' ? m.core.nodes[pinned.node]?.id : undefined);
  function pinNodeById(model: RunModel, id: string | undefined): void {
    const n = id ? model.nodeById.get(id) : undefined;
    if (n !== undefined) pin({ kind: 'node', node: n });
  }
  /** A user-requested camera move: tells the caption tour to stop steering the camera. */
  function userFocus(n: number): void { engine.emit('cameraInteract', {}); engine.focusNode(n); }
  function saveView(): SavedView {
    const c = engine.camera;
    return { mode: engine.mode, target: Float32Array.from(c.target), distance: c.distance, yaw: c.yaw, pitch: c.pitch };
  }
  /** orient: yaw and pitch. frame: view mode, target and distance. */
  function restoreView(v: SavedView, { orient, frame }: { orient: boolean; frame: boolean }): void {
    const c = engine.camera;
    if (frame && v.mode !== engine.mode) engine.setMode(v.mode, false);
    if (orient) { c.yaw = v.yaw; c.pitch = v.pitch; }
    if (frame) { c.target.set(v.target); c.distance = v.distance; }
    if (orient || frame) engine.requestRender();
  }
  const evHit = (i: number): PickHit | null => {
    if (!m) return null;
    const k = m.evK[i]!;
    if ((k === KI.cmd || k === KI.board || k === KI.submit) && m.evA[i]! >= 0) return { kind: 'step', event: i };
    const n = m.evN[i]!;
    if (n >= 0) return { kind: 'node', node: n };
    const a = m.evA[i]!;
    return a >= 0 ? { kind: 'node', node: m.core.agents[a]!.node } : null;
  };

  function syncUrl(): void {
    if (!m || urlKey === null) return;
    if (urlKey !== undefined) { writeScoped(urlKey, m.card.id); return; }
    writeUrl({ run: m.card.id, t: clock.t, rate: clock.rate, mode: engine.mode, sel: pinnedNodeId(), f: filters.encode() || undefined,
      renderer: o.url.renderer, perf: o.url.perf });
  }

  // ---------- fullscreen
  const fsEl = (): Element | null => document.fullscreenElement ?? (document as Document & { webkitFullscreenElement?: Element }).webkitFullscreenElement ?? null;
  function toggleFullscreen(): void {
    const r = root as HTMLElement & { webkitRequestFullscreen?: () => void };
    const d = document as Document & { webkitExitFullscreen?: () => void };
    if (fsEl() || root.classList.contains('theater')) {
      if (fsEl()) { if (document.exitFullscreen) void document.exitFullscreen(); else d.webkitExitFullscreen?.(); }
      root.classList.remove('theater');
      onFs();
      return;
    }
    if (r.requestFullscreen) r.requestFullscreen().catch(() => { root.classList.add('theater'); onFs(); });
    else if (r.webkitRequestFullscreen) r.webkitRequestFullscreen();
    else { root.classList.add('theater'); onFs(); }
  }
  function onFs(): void {
    const on = !!fsEl() || root.classList.contains('theater');
    timeline.setToggle('fullscreen', on);
    root.classList.toggle('is-fs', on);
    if (on) engine.setEngaged(true);
  }
  document.addEventListener('fullscreenchange', onFs);
  document.addEventListener('webkitfullscreenchange', onFs);

  // ---------- auto-hide controls (SPEC 8.3)
  let hideTimer = 0, controlsShown = true;
  function showControls(v: boolean): void {
    if (v === controlsShown) return;
    controlsShown = v;
    timeline.setControlsVisible(v);
    root.classList.toggle('controls-hidden', !v);
  }
  function poke(): void {
    showControls(true);
    clearTimeout(hideTimer);
    if (rm) return;
    hideTimer = window.setTimeout(() => {
      if (clock.playing && !timeline.busy() && !fpanel.isOpen && help.el.hidden) showControls(false);
      else poke();
    }, 2500);
  }
  stage.addEventListener('pointermove', poke, { passive: true });
  stage.addEventListener('pointerdown', poke, { passive: true });
  root.addEventListener('focusin', () => showControls(true));

  // ---------- actions
  type Act = KeyAction | 'filters' | 'feed' | 'labels' | 'captions' | 'fullscreen' | 'mode' | 'narration';
  function act(a: Act): void {
    interacted = true;
    if (a !== 'narration') preview.end();   // the narration toggle reads the preview state first (startAtTop)
    if (typeof a === 'object') { clock.seek((m?.tEnd ?? 0) * a.seekPct / 100, 'key'); return; }
    switch (a) {
      case 'toggle': clock.toggle(); break;
      case 'back10': clock.seekBy(-10000, 'key'); break;
      case 'fwd10': clock.seekBy(10000, 'key'); break;
      case 'back5': clock.seekBy(-5000, 'key'); break;
      case 'fwd5': clock.seekBy(5000, 'key'); break;
      case 'prevEvent': clock.stepEvent(-1, isVis); break;
      case 'nextEvent': clock.stepEvent(1, isVis); break;
      case 'home': clock.seek(0, 'key'); break;
      case 'end': clock.seek(clock.duration, 'key'); break;
      case 'fullscreen': toggleFullscreen(); break;
      case 'mode': {
        const nm: ViewMode = engine.mode === 'worldline' ? 'constellation' : 'worldline';
        engine.setMode(nm, !rm);
        timeline.setToggle('mode', nm === 'worldline');
        if (nm === 'constellation' && follow) { follow = false; engine.setFollow(false); }
        syncUrl();
        break;
      }
      case 'follow': if (engine.mode === 'worldline') { follow = !follow; engine.setFollow(follow); } break;
      case 'reset': engine.resetView(); break;
      case 'captions': captions.enabled = !captions.enabled; timeline.setToggle('captions', captions.enabled); captions.setTime(clock.t, false); break;
      case 'labels': labelsOn = !labelsOn; engine.setOptions({ labels: labelsOn }); timeline.setToggle('labels', labelsOn); break;
      case 'search': fpanel.focusSearch(); break;
      case 'help': help.toggle(); break;
      case 'filters': if (fpanel.isOpen) fpanel.close(); else fpanel.open(); break;
      case 'feed': {
        const on = root.classList.toggle('feed-collapsed');
        timeline.setToggle('feed', !on);
        queueInsets();
        break;
      }
      case 'narration':
        if (!narration.available) { flash('No narration'); break; }
        narration.toggle();
        preview.end();
        timeline.setToggle('narration', narration.on);
        flash(narration.on ? 'Narration on' : 'Narration off');
        break;
      case 'faster': case 'slower': {
        if (narration.active) { flash('Speed is set by narration'); break; }
        const i = RATES.indexOf(clock.rate) + (a === 'faster' ? 1 : -1);
        const r = RATES[Math.max(0, Math.min(RATES.length - 1, i))]!;
        clock.setRate(r);
        flash(`${r}×`);
        break;
      }
      case 'escape':
        if (help.close() || timeline.closeMenu()) break;
        if (pinned) { pin(null); break; }
        if (fpanel.close()) break;
        if (root.classList.contains('text-view')) { setTextView(false); break; }
        if (engine.backend !== 'none' && (stage.classList.contains('engaged'))) { engine.setEngaged(false); break; }
        if (filters.get().range) { filters.set({ range: null }); break; }
        if (root.classList.contains('theater')) toggleFullscreen();
        break;
    }
  }

  let flashT = 0;
  function flash(s: string): void {
    ripple.textContent = s;
    ripple.hidden = false;
    ripple.classList.remove('go');
    void ripple.offsetWidth;
    ripple.classList.add('go');
    clearTimeout(flashT);
    flashT = window.setTimeout(() => { ripple.hidden = true; }, 700);
  }

  function setTextView(on: boolean): void {
    root.classList.toggle('text-view', on);
    feed.setExpanded(on);
    if (on) root.classList.remove('feed-collapsed');
    queueInsets();
  }

  // ---------- floating panels (wide layout, player.css): the graph fills the player and the intro/selector column
  // and the feed float over it. Tell the camera which stage edges they cover so it centres and frames the swarm in
  // the uncovered area, and expose the same insets as --ov-* for overlays centred on the stage (caption, hint).
  const leftCol = root.querySelector<HTMLElement>('.pl-left');
  let insetsRaf = 0;
  function syncInsets(): void {
    insetsRaf = 0;
    const s = stage.getBoundingClientRect();
    const floating = (el: HTMLElement | null): DOMRect | null => {
      if (!el || getComputedStyle(el).position !== 'absolute') return null;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 ? r : null;
    };
    const col = floating(leftCol), fd = floating(feed.el);
    const top0 = parseFloat(getComputedStyle(root).getPropertyValue('--top-pad')) || 0;
    const ins = { left: 0, top: col || fd ? top0 : 0, right: fd ? s.right - fd.left : 0, bottom: col || fd ? timeline.el.offsetHeight : 0 };
    if (col) {
      // the column covers the top-left corner: free either the strip beside it or the band below it, whichever is larger
      const beside = col.right - s.left, below = col.bottom - s.top;
      const W = s.width - ins.right, H = s.height - ins.bottom;
      if ((W - beside) * (H - ins.top) >= W * (H - below)) ins.left = beside; else ins.top = below;
    }
    for (const [k, v] of Object.entries(ins)) root.style.setProperty(`--ov-${k[0]}`, `${Math.round(v)}px`);
    engine.setInsets?.(ins, root.classList.contains('loaded'));
  }
  function queueInsets(): void { if (!insetsRaf) insetsRaf = requestAnimationFrame(syncInsets); }
  const insetsRo = typeof ResizeObserver === 'function' ? new ResizeObserver(queueInsets) : null;
  for (const el of [stage, leftCol, feed.el]) if (el) insetsRo?.observe(el);
  syncInsets();

  // ---------- wiring
  let visibleRatio = 0;
  const io = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver((es) => { for (const e of es) visibleRatio = e.intersectionRatio; }, { threshold: [0, 0.25, 0.5, 0.75, 1] }) : null;
  io?.observe(root);
  bindKeys(root, (a) => { poke(); act(a); }, () => visibleRatio >= 0.5, () => interacted);

  let lastHud = 0, lastAria = 0;
  // Pure in t (engine.swarm() is the last *rendered* frame, one rAF behind a seek, so the HUD would lag scrubs).
  function refreshHud(t: number): void {
    if (!m) return;
    for (let a = 0; a < m.nAgents; a++) states[a] = m.agentState(a, t, states[a]);
    states.length = m.nAgents;
    swarmScratch = m.swarmAt(t, swarmScratch);
    hud.update(swarmScratch, states);
  }
  clock.on('tick', ({ t }) => {
    timeline.render(t);
    if (!controlsShown) root.style.setProperty('--prog', String(t / Math.max(1, clock.duration)));
    feed.setTime(t);
    captions.setTime(t, clock.playing);
    detail.setTime(t);
    nodeList.update(mask, t);
    const now = performance.now();
    if (m && (now - lastHud > 200 || !clock.playing)) { lastHud = now; refreshHud(t); }
    if (m && !clock.playing && now - lastAria > 2000) { lastAria = now; updateAria(t); }
    if (t < clock.duration && !endcard.el.hidden) endcard.hide();
    syncUrl();
  });
  let wasPlaying = false;
  clock.on('state', (s) => {
    if (s.playing !== wasPlaying) {
      wasPlaying = s.playing;
      root.classList.toggle('playing', s.playing);
      if (!s.playing) { showControls(true); clearTimeout(hideTimer); if (m) { refreshHud(clock.t); updateAria(clock.t); } } else poke();
    }
    syncUrl();
  });
  clock.on('seek', (e) => { preview.seek(e.cause); if (e.cause !== 'scrub' && e.cause !== 'api' && e.cause !== 'end') tooltip.hide(); });
  clock.on('ended', () => {
    if (isLive()) { liveEdge = true; return; }  // no end card at a live edge: more events are coming
    if (loopsLeft > 0 && !interacted) { loopsLeft--; clock.play(); return; }
    showEndCard();
  });
  // leaving the live edge: a user seek or playing again (updateRun's own 'api' seek keeps it)
  clock.on('seek', (e) => { if (e.cause !== 'api' && e.cause !== 'end') liveEdge = false; });
  clock.on('state', (st) => { if (st.playing) liveEdge = false; });

  function updateAria(t: number): void {
    const cv = glHost.querySelector('canvas');
    if (!cv || !m) return;
    const sw = m.swarmAt(t);
    const solved = m.core.agents.filter((a) => a.solveT !== null && a.solveT <= t).length;
    let live = 0;
    for (let i = 0; i < m.nNodes; i++) if (m.nodeKind[i] === 4 && !m.core.nodes[i]!.seed && m.nodeT0[i]! <= t && t < m.nodeT1[i]!) live++;
    cv.setAttribute('role', 'img');
    cv.setAttribute('aria-label', `${m.card.id} at ${fmtT(t, false)}, ${m.nAgents} agents, ${solved} solved, ${live} live ${m.core.run.nouns.posts}`
      + (sw.clusters.length ? `, swarm ${sw.clusters.map((c) => c.map((a) => 'a' + a).join(' ')).join('; ')}` : ''));
  }

  function showEndCard(): void {
    if (!m) return;
    // next and sibling stay within the current run's model
    const L = listed(), cur = m.card;
    endcard.show(nextRun(L, cur), siblingRun(L, cur) ?? null);
  }
  endcard.onAction((a) => {
    interacted = true;
    preview.end();
    endcard.hide();
    if (a === 'replay') { clock.seek(0, 'api'); clock.play(); }
    else if (a === 'findings') document.getElementById('findings')?.scrollIntoView({ behavior: rm ? 'auto' : 'smooth' });
    else {
      const L = listed(), cur = m?.card;
      if (!cur) return;
      let target: RunCard | undefined;
      if (a === 'next') target = nextRun(L, cur) ?? undefined;
      else target = siblingRun(L, cur);
      if (target) void api.load(target.id).then(() => clock.play());
    }
  });

  filters.on('change', ({ state, mask: mk }) => {
    mask = mk;
    engine.setMask(mk);
    engine.setOptions({ showTrails: state.nodeKinds.has('trail'), recentMs: state.recentMs, showIdle: state.eventKinds.has('idle') });
    feed.setMask(mk);
    timeline.refreshMarkers();
    timeline.setBadge(mk.activeCount);
    timeline.setRange(state.range);
    clock.setVisible(isVis);
    syncUrl();
  });
  fpanel.onToggle((on) => { timeline.setToggle('filters', on); root.classList.toggle('filters-open', on); });
  fpanel.onHit((i) => {
    if (!m) return;
    preview.end();
    clock.pause();
    clock.seek(m.evT[i]!, 'api');
    const hit = evHit(i);
    if (hit) pin(hit, true);
  });

  engine.on('hover', ({ hit, x, y }) => {
    if (hit && m) {
      tooltip.show(hit, x, y, clock.t);
      stage.classList.add('hovering');
      const n = nodeOfHit(hit);
      if (n >= 0 && hit.kind === 'node') {
        const evs = m.nodeEvents[n]!.filter((i) => isVis(i));
        timeline.setNodeActivity(Float64Array.from(evs, (i) => m!.evT[i]!), colorOf(n));
      }
    } else {
      tooltip.hide();
      stage.classList.remove('hovering');
      timeline.setNodeActivity(null, '');
    }
  });
  engine.on('select', ({ hit }) => {
    interacted = true;
    tooltip.hide();
    if (!hit && env.coarse) { showControls(!controlsShown); if (controlsShown) poke(); }
    pin(hit);
  });
  engine.on('focus', () => { interacted = true; });
  engine.on('cameraInteract', () => { interacted = true; });
  engine.on('engaged', ({ on }) => stage.classList.toggle('engaged', on));
  engine.on('seekRequest', ({ dt }) => { interacted = true; preview.end(); clock.seekBy(dt, 'api'); });
  engine.on('backend', ({ kind }) => {
    root.classList.toggle('no-gl', kind === 'none');
    if (kind === 'none') { setTextView(true); startOwnLoop(); }
  });
  if (engine.backend === 'none') { root.classList.add('no-gl'); setTextView(true); startOwnLoop(); }

  // Without a renderer nobody drives clock.advance(): run a tiny loop of our own.
  let ownLoop = false;
  function startOwnLoop(): void {
    if (ownLoop) return;
    ownLoop = true;
    let last = performance.now();
    const tick = (now: number): void => { clock.advance(Math.min(100, now - last)); last = now; requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }

  feed.onPick((i) => {
    if (!m) return;
    interacted = true;
    clock.seek(m.evT[i]!, 'feed');
    const hit = evHit(i);
    if (hit) pin(hit, true);
  });
  feed.onExpand((on) => setTextView(on));
  feed.onHover((i) => engine.highlight(i !== null && m ? [m.evN[i]! >= 0 ? m.evN[i]! : nodeOfHit(evHit(i))].filter((n) => n >= 0) : pinnedHighlight()));

  detail.onSeek((t) => { interacted = true; preview.end(); clock.seek(t, 'api'); });
  detail.onFocus((n) => userFocus(n));
  detail.onOpenNode((n) => { interacted = true; pin({ kind: 'node', node: n }, true); });
  detail.onNeighbourhood((n) => { filters.set({ neighbourhoodOf: n }); });
  detail.onClose(() => { pinned = null; root.classList.remove('pinned'); engine.highlight(null); engine.select?.(null); syncUrl(); });

  nodeList.onPick((n) => pin({ kind: 'node', node: n }));
  nodeList.onHighlight((n) => engine.highlight(n === null ? pinnedHighlight() : [n]));
  nodeList.onFocusNode((n) => userFocus(n));

  // Track interaction for autoplay suppression.
  root.addEventListener('pointerdown', () => { interacted = true; }, { passive: true });

  // ---------- load
  const api: SitePlayer = {
    clock, filters, engine,
    get model() { return m; },
    get interacted() { return interacted; },
    async load(id: string, keep?: InPlaceReload) {
      const c = card(id);
      if (!keep) liveEdge = false;
      abort?.abort();
      const ac = new AbortController();
      abort = ac;
      errBox.hidden = true;
      preview.end();
      if (!c) { showError(`Unknown run “${id}”.`, null); return; }
      loading.hidden = false;
      try {
        const core = await loader.core(c, ac.signal);
        if (ac.signal.aborted) return;
        const model = buildModel(c, core);
        const prev = m;
        const view = saveView();
        m = model;
        root.classList.remove('pl-empty');
        void narration.setRun(null);
        clock.pause();
        engine.setRun(model);
        // same interface: keep the orientation; in-place reload: also keep mode and framing
        restoreView(view, { orient: !!prev && prev.card.iface === c.iface, frame: !!keep });
        root.dataset['iface'] = c.iface;
        detail.setModel(model);
        pinned = null; root.classList.remove('pinned'); engine.select?.(null);
        tooltip.setModel(model);
        feed.setModel(model);
        nodeList.setModel(model);
        fpanel.setModel(model);
        hud.setTotal(model.nAgents);
        captions.setChapters(model.core.chapters);
        timeline.setModel(model);
        clock.setModel(model);
        endcard.hide();
        const u = first && !keep ? o.url : {};
        if (first && u.f) filters.decode(u.f);
        filters.bind(model);
        selector.setActive(id);
        if (poster) poster.hidden = true;
        root.classList.add('loaded');
        if (keep) resumeReload(model, keep);
        else if (first) { first = false; applyStartUrl(model, u); }
        timeline.setToggle('captions', captions.enabled);
        timeline.setToggle('labels', labelsOn);
        timeline.setToggle('feed', !root.classList.contains('feed-collapsed'));
        captions.setTime(clock.t, false);
        updateAria(clock.t);
        loading.hidden = true;
        syncUrl();
        // Narration: awaited only when armed, so a following clock.play() (end card) starts the audio too.
        const np = narration.setRun(c, ac.signal);
        if (narration.on) await np;
        if (abort !== ac) return;
        // Detail bundle (drives the buffered fill).
        loader.detail(c, (f) => { if (abort === ac) timeline.setBuffered(f); }, ac.signal).then((d) => {
          if (abort !== ac || m !== model) return;
          model.attachDetail(d);
          timeline.setBuffered(1);
          if (filters.get().text.trim()) filters.set({});
          detail.pin(pinned);
        }).catch((e: unknown) => {
          if ((e as Error).name === 'AbortError') return;
          timeline.setBuffered(0);
          console.warn('detail bundle failed', e);
        });
      } catch (e) {
        if ((e as Error).name === 'AbortError' || abort !== ac) return;
        loading.hidden = true;
        showError(e instanceof BundleError && e.kind === 'version' ? 'The data format was updated. Please reload the page.'
          : `Could not load this swarm (${(e as Error).message}).`, e instanceof BundleError && e.kind === 'version' ? null : () => void api.load(id));
      }
    },
    autoplay() {
      if (rm || env.saveData || interacted || !m || clock.playing || o.url.t !== undefined || o.url.sel) return;
      clock.setSkipQuiet(true);
      loopsLeft = 1;
      preview.start();
      clock.play();
    },
    showMe(kinds) {
      if (!m) return;
      interacted = true;
      preview.end();
      const ch = m.core.chapters.find((c) => kinds.includes(c.k));
      if (!ch) return;
      clock.pause();
      clock.seek(Math.max(0, ch.t - 300), 'api');
      if (ch.node !== undefined && ch.node >= 0) { userFocus(ch.node); engine.highlight([ch.node]); }
      root.scrollIntoView({ behavior: rm ? 'auto' : 'smooth', block: 'center' });
      clock.play();
    },
    addRun(c) {
      const i = index.runs.findIndex((r) => r.id === c.id);
      if (i >= 0) index.runs[i] = c; else index.runs.push(c);
      selector.refresh();
    },
    async updateRun(c) {
      api.addRun(c);
      if (!m || m.card.id !== c.id) return;
      // at the live edge (ended while playing, not paused or moved since), keep following it
      const atEdge = liveEdge && !clock.playing && clock.t >= clock.duration - 1;
      await api.load(c.id, { t: clock.t, playing: clock.playing || atEdge, sel: pinnedNodeId() });
      if (atEdge && !clock.playing) liveEdge = true;  // nothing new yet: still at the edge
    },
    async removeRun(id) {
      const i = index.runs.findIndex((r) => r.id === id);
      if (i >= 0) index.runs.splice(i, 1);
      selector.refresh();
      if (!m || m.card.id !== id) return;
      const L = listed();
      const nx = L[Math.min(i, L.length - 1)] ?? L[0];
      if (nx) await api.load(nx.id); else unload();
    },
    dispose() { narration.dispose(); engine.dispose(); timeline.dispose(); io?.disconnect(); insetsRo?.disconnect(); cancelAnimationFrame(insetsRaf); },
  };

  /** First load only: the shared URL's rate / mode / time / pinned node; without a time, reduced motion starts at
   *  the swarm onset (or the first post or leak). */
  function applyStartUrl(model: RunModel, u: UrlState): void {
    if (u.rate) clock.setRate(u.rate);
    if (u.mode === 'worldline') { engine.setMode('worldline', false); timeline.setToggle('mode', true); }
    if (u.t !== undefined) clock.seek(u.t, 'url');
    else if (rm) {
      const ch = model.core.chapters.find((x) => x.k === 'onset') ?? model.core.chapters.find((x) => x.k === 'post' || x.k === 'leak');
      const t0 = model.card.stats.swarmOnsetMs ?? ch?.t;
      if (t0 !== undefined && t0 !== null) clock.seek(t0, 'url');
    }
    pinNodeById(model, u.sel);
  }
  /** updateRun's in-place reload: same time (clamped), pinned node and play state. */
  function resumeReload(model: RunModel, keep: InPlaceReload): void {
    clock.seek(Math.min(keep.t, model.tEnd), 'api');
    pinNodeById(model, keep.sel);
    if (keep.playing && keep.t < model.tEnd) clock.play();
  }
  /** removeRun of the last run: no model; the host page shows its empty state over .pl-empty. */
  function unload(): void {
    abort?.abort();
    abort = null;
    clock.pause();
    void narration.setRun(null);
    pin(null);
    endcard.hide();
    m = null;
    root.classList.remove('loaded');
    root.classList.add('pl-empty');
    if (poster) poster.hidden = false;
    if (urlKey) writeScoped(urlKey, null);
  }

  function showError(msg: string, retry: (() => void) | null): void {
    errBox.replaceChildren(h('p', null, msg));
    if (retry) {
      const b = h('button.ec-btn.primary', { type: 'button' }, 'Retry');
      b.addEventListener('click', () => { errBox.hidden = true; retry(); });
      errBox.appendChild(b);
    } else {
      const b = h('button.ec-btn.primary', { type: 'button' }, 'Reload');
      b.addEventListener('click', () => location.reload());
      errBox.appendChild(b);
    }
    errBox.hidden = false;
  }

  // Theme toggle: the engine reads colours on setRun, so re-run it keeping camera, mode and mask.
  window.addEventListener('themechange', () => {
    if (!m) return;
    const view = saveView();
    engine.setRun(m);
    engine.setMask(mask);
    restoreView(view, { orient: true, frame: true });
    engine.highlight(pinnedHighlight());
    const pn = nodeOfHit(pinned);
    engine.select?.(pn >= 0 ? { kind: 'node', node: pn } : null); // setRun cleared the pin ring
    engine.requestRender();
    timeline.setModel(m);
    timeline.setBuffered(m.detail ? 1 : 0);
  });

  window.addEventListener('hashchange', () => {
    if (urlKey === null) return;
    const r = urlKey === undefined ? readUrl().run : readScoped(urlKey);
    if (r && r !== m?.card.id && (urlKey === undefined || card(r))) void api.load(r);
  });

  return api;
}
