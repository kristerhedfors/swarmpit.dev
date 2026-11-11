// swarmpit.dev: shared types for every module boundary.
//
// Binding companion to site/SPEC.md. The only runtime code here is the frozen code tables
// (EVENT_KINDS etc.), which the exporter mirrors exactly. Edits are append-only: add optional fields
// or new trailing table entries, and never reorder or rename. Announce every edit to all builders.
//
// Conventions
// - All times are integer milliseconds relative to the run's t_origin (dash/CONTRACT.md `t` x 1000).
//   Seed content has negative times. The playback domain is [0, tEndMs].
// - A "node index" is the position in CoreBundle.nodes and every typed array over nodes. The same holds
//   for edges, events and agents. -1 means "none".
// - Ids follow CONTRACT section 2 where it defines them (a<i>, seed:<author>, name:<handle>, s<id>/p<id>/d<off>),
//   plus these site-only ids: 'task', 'board', 'th:<slug>'.

/* ============================================================================================
 * 1. Code tables (wire `k` columns index into these; frozen order)
 * ========================================================================================== */

export const NODE_KINDS = ['task', 'board', 'agent', 'thread', 'post', 'persona', 'handle'] as const;
export type NodeKind = (typeof NODE_KINDS)[number];

export const EDGE_KINDS = [
  // structural
  'contains', // board -> thread, thread -> post
  'authored', // author (persona | agent) -> post
  // action (agent -> entity)
  'works',    // agent -> task: non-board steps (ctf/recon/other/persistence/submit)
  'reads',    // agent -> thread | post | board (listing/doc)
  'writes',   // agent -> thread (post/edit/PUT/PRIVMSG) | channel (irc join)
  // social: obs `interactions` rows, unchanged semantics ("src attends to dst")
  'exposure', 'reply', 'mention', 'dm', 'upvote',
] as const;
export type EdgeKind = (typeof EDGE_KINDS)[number];
export const SOCIAL_EDGE_KINDS: readonly EdgeKind[] = ['exposure', 'reply', 'mention', 'dm', 'upvote'];

export const EVENT_KINDS = [
  'boardStart', 'start', 'end', 'crash', 'retry',
  'think',      // model call
  'cmd',        // non-board step (cat ctf | recon | other | persistence)
  'board',      // step with is_board (the command; linked board reqs are separate events)
  'submit', 'idle',
  'read', 'list', 'doc', 'miss', // board req by target_kind thread/post | listing | doc | none (or status >= 400)
  'post', 'upvote', 'delete', 'dm',
  'join', 'connect', 'disconnect', 'proto', // irc. proto = irc_cmd other than PRIVMSG/NOTICE
] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** Event families used by filters, feed grouping and the colour cue. */
export type EventFamily = 'lifecycle' | 'think' | 'task' | 'boardStep' | 'idle' | 'read' | 'write' | 'social' | 'irc';
export const EVENT_FAMILY: Readonly<Record<EventKind, EventFamily>> = {
  boardStart: 'lifecycle', start: 'lifecycle', end: 'lifecycle', crash: 'lifecycle', retry: 'lifecycle',
  think: 'think', cmd: 'task', board: 'boardStep', submit: 'task', idle: 'idle',
  read: 'read', list: 'read', doc: 'read', miss: 'read',
  post: 'write', upvote: 'social', delete: 'write', dm: 'social',
  join: 'irc', connect: 'irc', disconnect: 'irc', proto: 'irc',
};

export const STEP_CATS = ['idle', 'board', 'submit', 'persistence', 'ctf', 'recon', 'other'] as const;
export type StepCat = (typeof STEP_CATS)[number];

/** EventCols.f bit flags. */
export const EF = {
  BOARD: 1,          // step is_board
  SOLVE: 2,          // step is_solve
  LEAK: 4,           // post carries secret_leak
  PERSIST: 8,        // persistence step
  FREE: 16,          // phase_before == 'free'
  BACKGROUND: 32,    // board event attributed as background (CONTRACT 3.3 step 7)
  HEURISTIC: 64,     // is_board via relay/taint, or an inferred edge
  SUBMIT_OK: 128,    // a submission containing the flag
  ERROR: 256,        // status >= 400, or exit != 0
  LIVE_LIVE: 512,    // fires a live-to-live social edge
} as const;

export type Iface = 'forum' | 'wiki' | 'cache' | 'irc';
export type Pop = 'solo' | 'multi';
export type Phase = 'task' | 'free';
export type PostFlag = 'secret_leak' | 'self_id' | 'injection' | 'memory_note';
export type CaveatCode = 'irc_inbound_broken' | 'tagged' | 'old_harness' | 'final_missing' | 'heuristic_edges' | 'synthetic'
  | 'unsolvable'
  // browser-lab converter only (site/src/convert, docs/browser-swarm.md §5)
  | 'preview' | 'live' | 'incomplete' | 'malformed';
/** CTF goal (an experimental factor, SPEC 4.11). 'solvable' is the historical CTF and is never written: a card,
 *  run meta or findings row without `goal` is solvable. 'unsolvable' runs can never find the flag (no free time). */
export type Goal = 'solvable' | 'unsolvable';

/* ============================================================================================
 * 2. Wire format: public/data/index.json
 * ========================================================================================== */

export const INDEX_FORMAT = 'swarmpit.index/1';
export const CORE_FORMAT = 'swarmpit.run/1';
export const DETAIL_FORMAT = 'swarmpit.detail/1';

export interface IndexBundle {
  format: typeof INDEX_FORMAT;
  generated: string;                        // ISO 8601 UTC
  generator: {
    tool: 'tools/export.py'; obsSchema: number; obsDerive: number; codeVersions: string[];
    ctf: 'keep' | 'mask';
    redactions: Record<string, number>;     // rule id -> hits across all bundles
    truncation: Record<string, number>;     // field -> char limit
  };
  featured: string;                         // run id autoplayed on landing (primary model only)
  models?: ModelInfo[];                     // models with listed runs, primary first (lab/config MODEL_ORDER)
  runs: RunCard[];                          // ordered by model, iface, pop, rep. Tagged runs have hidden=true
  findings: Findings;
}

/** One model lane (lab/config.MODELS). `id` equals RunCard.model; `slug` is the run-id segment (`--glm53f`). */
export interface ModelInfo { id: string; slug: string; label: string; provider: string; nRuns: number }

export interface RunCard {
  id: string;
  core: string; detail: string;             // paths relative to data/, e.g. 'runs/forum-multi4-r1.3f2a9b01.core.json'
  coreBytes: number; detailBytes: number;
  hash: string;                             // 8 hex; also appears in both filenames
  iface: Iface; pop: Pop; nAgents: number; rep: number; tag: string; model: string;
  goal?: 'unsolvable';                      // absent = solvable (run id segment `-unsolvable` after r<rep>)
  durationMs: number;                       // = core.run.tEndMs
  hidden: boolean;                          // tagged/smoke/archive/synthetic: reachable via #run=, not listed
  caveats: CaveatCode[];
  stats: { discovered: number; solved: number; posts: number; liveEdges: number; leak: boolean;
           boardEvents: number; events: number; nodes: number; swarmOnsetMs: number | null };
  density: number[];                        // 64 bins of visible-by-default event counts (selector sparkline)
  score: number;                            // featured ranking (SPEC 2.7)
  blurb: string;                            // '4 agents · 3 found the board · flag posted publicly at 0:18'
}

export interface PooledMetric { v: number; lo: number | null; hi: number | null }

export interface Findings {
  source: 'analysis.json' | 'obs';
  note: string;                             // 'Pilot snapshot: n=… agent sessions per interface, qwen-3.8-27b, budget 40'
  notes?: Record<string, string>;           // model id -> note for that model (note = the primary model's)
  models?: string[];                        // model ids present in pooled/cells, primary first
  goals?: Goal[];                           // only when unsolvable rows exist: ['solvable', 'unsolvable']
  goalNotes?: Partial<Record<Goal, Record<string, string>>>; // non-default goal -> model id -> note
  // model is absent in pre-multi-model indexes: read it as the primary model; goal absent = solvable.
  // Solvable rows have board_steps_free; unsolvable rows have no free phase (n/a) and carry the whole-run
  // task-phase metrics share_board (share of steps on the board) and live_edges (live-live edges out per agent).
  pooled: { model?: string; goal?: 'unsolvable'; iface: Iface; nAgents: number;
            metrics: Record<'contacted' | 'posted' | 'posts' | 'board_steps', PooledMetric>
              & Partial<Record<'board_steps_free' | 'share_board' | 'live_edges', PooledMetric>> }[];
  cells: { model?: string; goal?: 'unsolvable'; iface: Iface; pop: Pop; nAgents: number; discoveryRate: number;
           shareAfterContact: number | null; posts: number; solveRate: number; boardSteps: number;
           excludedByDefault: boolean }[];
  caveats: string[];
  mismatches: string[];                     // analysis.json vs obs v_cell_metrics differences (empty if they agree)
}

/* ============================================================================================
 * 3. Wire format: core bundle  public/data/runs/<id>.<hash>.core.json
 * ========================================================================================== */

export interface CoreBundle {
  format: typeof CORE_FORMAT;
  hash: string;
  run: RunMeta;
  strings: string[];                        // interned labels; index 0 is ''
  nodes: NodeRec[];                         // order: task, board, agents (a0..), threads, posts, personas, handles
  edges: EdgeRec[];                         // every edge that ever exists, unique by (s, d, k)
  events: EventCols;                        // sorted by (t, lane, kindRank, seq) == CONTRACT 3.5 order
  fires: FireCols;                          // edge firings, sorted by (e, t)
  agents: AgentRec[];                       // index = agent id
  chapters: ChapterRec[];                   // sorted by t
  segments: SegmentRec[];                   // contiguous, cover [0, tEndMs]
  series: { binMs: number; density: number[]; boardShare: number[][] }; // boardShare[agent][bin] in [0,1]
}

export interface RunMeta {
  id: string; iface: Iface; pop: Pop; nAgents: number; rep: number; tag: string; model: string;
  goal?: 'unsolvable';                      // absent = solvable; set together with the 'unsolvable' caveat
  harness: { budget: number; temperature: number; maxTokens: number; outputChars: number };
  boardName: string;                        // 'agent-commons'
  nouns: { thread: string; threads: string; post: string; posts: string; board: string };
  tEndMs: number;                           // CONTRACT runs.t_end x 1000 (max event t)
  wallMs: number;                           // run.json finished - started
  prehistoryMs: number;                     // most negative seed t (<= 0), for the log-squashed stub
  caveats: { code: CaveatCode; text: string }[];
  codeVersion: string;
  ctfMasked: boolean;                       // true when exported with --mask-ctf
  source: { contract: 'dash/CONTRACT.md v1'; obsSchema: number; obsDerive: number; dataVersion: number };
}

export interface NodeRec {
  id: string;
  k: number;                                // index into NODE_KINDS
  label: string;                            // tag text, <= 48 chars
  t0: number | null;                        // appearance ms; null = present from t=0 (task, board, seed, personas)
  t1?: number;                              // disappearance ms (deleted post); absent = never
  agent?: number;                           // agent nodes; posts authored by a live agent
  thread?: number;                          // post -> thread node index
  author?: number;                          // post -> author node index (agent or persona)
  seed?: true;
  tSeed?: number;                           // seed posts: original negative t (ms), shown as '5 h before run'
  postId?: number;
  title?: string;                           // threads
  names?: string[];                         // agents: display_names (post authors / irc nicks)
  preview?: string;                         // posts: first 240 chars of the redacted body
  bodyLen?: number;
  flags?: PostFlag[];
  upvotes0?: number;
}

export interface EdgeRec {
  s: number; d: number;
  k: number;                                // index into EDGE_KINDS
  w?: number;                               // per-fire weight if != 1 (listing 0.25, blind reply 0.5, ambiguous 1/k)
  inferred?: true;                          // heuristic evidence (CONTRACT section 9): dashed + tooltip text
  ev?: number;                              // strings index: evidence text ('read thread within 600 s', 'ambiguous handle')
  liveLive?: true;                          // src and dst are both live agents (CONTRACT live_live)
}

/** Column-oriented events. Every array has the same length. */
export interface EventCols {
  t: number[];   // ms
  d: number[];   // duration ms (steps: t_end - t; model calls: latency; else 0)
  k: number[];   // index into EVENT_KINDS
  a: number[];   // agent id; -1 = board
  n: number[];   // primary target node (thread/post/task/handle/board), -1 none
  e: number[];   // primary edge fired, -1 none (fan-out lives in `fires`)
  f: number[];   // EF bit flags
  s: number[];   // step number (call number for 'think'), -1 none
  c: number[];   // index into STEP_CATS for steps, else -1
  l: number[];   // strings index: one-line label <= 160 chars (command head, 'GET /api/v1/threads', post title)
  x: number[];   // index into DetailBundle.items, -1 none
}

export interface FireCols {
  e: number[];   // edge index
  t: number[];   // ms
  w: number[];   // weight of this firing
  ev: number[];  // causing event index, -1 if none (interaction with no linkable event)
}

export const CHAPTER_KINDS = ['boot', 'firstContact', 'contact', 'solve', 'phaseFree', 'post', 'leak', 'onset',
  'upvote', 'dm', 'persistence', 'crash', 'end'] as const;
export type ChapterKind = (typeof CHAPTER_KINDS)[number];

export interface ChapterRec {
  t: number; k: ChapterKind; agent?: number; node?: number; ev?: number;
  p: 1 | 2 | 3;                             // 1 story beat (caption + tour nudge), 2 timeline marker, 3 feed only
  label: string;                            // marker tooltip: 'a2 finds the board'
  caption: string;                          // lower third: 'a2 wanders onto the forum 13 s in, still on its task.'
  merged?: number;                          // > 1 when clustered ('3 events')
}

export interface SegmentRec { t0: number; t1: number; title: string } // 'Boot' | 'On task' | 'Free time' | 'Wind-down'

export interface AgentRec {
  id: number; node: number; names: string[]; model: string;
  status: 'done' | 'crashed' | 'timed_out' | 'running';
  startT: number | null; endT: number | null; solveT: number | null; solveStep: number | null;
  stepsUsed: number; modelCalls: number; ended: string | null;
  tokens: { prompt: number; completion: number; cached: number; reasoning: number };
  metrics: { discovered: boolean; firstContact: number | null; boardSteps: number; shareAfterContact: number | null;
             posts: number; episodes: number; secretLeak: boolean };
  drift: { t: number[]; a: number[] };      // attraction samples at step times, a in [0,1] (SPEC 5.2)
}

/* ============================================================================================
 * 4. Wire format: detail bundle  public/data/runs/<id>.<hash>.detail.json  (lazy)
 * ========================================================================================== */

export interface DetailBundle {
  format: typeof DETAIL_FORMAT;
  hash: string;                             // must equal CoreBundle.hash
  items: Detail[];                          // EventCols.x indexes here
  posts: Record<string, PostDetail>;        // by node id ('s3', 'p10', 'd812')
}

export type Detail =
  | { t: 'step'; cmd: string; out: string; outLen: number; exit: number | null; via?: 'relay' | 'taint';
      submit?: string[] }
  | { t: 'think'; reason: string; content: string; nTools: number;
      tok: [prompt: number, completion: number, reasoning: number, cached: number] }
  | { t: 'req'; method: string; path: string; status: number; bytes: number }
  | { t: 'line'; text: string };            // irc proto, retry, crash detail

export interface PostDetail {
  body: string;                             // <= 1500 chars, redacted
  leakSpans: [start: number, end: number][]; // character ranges of CTF values (or their masks) in body
  mentions: string[];
}

/* ============================================================================================
 * 5. Data layer (src/data/**): loader, model, filters
 * ========================================================================================== */

export class BundleError extends Error {
  constructor(msg: string, readonly url: string, readonly kind: 'network' | 'version' | 'shape' | 'hash') {
    super(msg);
    this.name = 'BundleError';
  }
}

export interface Loader {
  index(): Promise<IndexBundle>;                                        // cached
  core(card: RunCard, signal?: AbortSignal): Promise<CoreBundle>;      // validated; LRU(4)
  detail(card: RunCard, onProgress?: (frac: number) => void, signal?: AbortSignal): Promise<DetailBundle>;
  prefetch(card: RunCard): void;                                        // core only; on selector hover/focus
}
export type CreateLoader = (base: URL) => Loader;                       // base = new URL('data/', document.baseURI)

export interface AgentState {
  id: number;
  status: 'not_started' | 'running' | 'dozing' | 'done' | 'crashed' | 'timed_out';
  phase: Phase; step: number; calls: number; solved: boolean;
  boardSteps: number; posts: number; boardShare: number;               // rolling share over the last 5 steps
  tokens: [prompt: number, completion: number, reasoning: number];     // cumulative to t
  lastStep: number; lastThink: number; lastEvent: number;              // event indices, -1 none
  drift: number;                                                        // attraction a(t) in [0,1]
}

export interface NodeStats { reads: number; readers: number[]; replies: number; upvotes: number; posts: number }
export interface ThreadView { posts: { node: number; upvotes: number; isNew: boolean }[] }
export interface EventText { icon: string; short: string; long: string; agent: number; node: number }

export interface SwarmState {
  onBoard: number[];                        // agents with drift >= 0.5 and started, not ended
  clusters: number[][];                     // connected components (size >= 2) of live-live fires in (t-W, t]
  liveLive: number;                         // live-live edges that have fired by t
  onsetT: number | null;
}

/** Immutable typed-array view over a CoreBundle (+ DetailBundle once loaded). All queries pure in t. */
export interface RunModel {
  readonly card: RunCard; readonly core: CoreBundle; readonly tEnd: number;
  readonly nNodes: number; readonly nEdges: number; readonly nEvents: number; readonly nAgents: number;
  readonly nodeKind: Uint8Array; readonly nodeT0: Float64Array; readonly nodeT1: Float64Array; // -Inf / +Inf
  readonly nodeById: ReadonlyMap<string, number>;
  readonly edgeSrc: Uint16Array; readonly edgeDst: Uint16Array; readonly edgeKind: Uint8Array;
  readonly edgeFirstT: Float64Array;
  readonly evT: Float64Array; readonly evK: Uint8Array; readonly evA: Int16Array; readonly evN: Int32Array;
  readonly evE: Int32Array; readonly evF: Uint16Array;
  readonly fireStart: Uint32Array;          // CSR offsets into fires per edge (length nEdges + 1)
  readonly fireT: Float64Array; readonly fireW: Float32Array; readonly fireEv: Int32Array;
  readonly fireByT: Uint32Array;            // fire indices sorted by t (pulses, worldline chords)
  readonly nodeEvents: Uint32Array[];       // event indices touching each node (activity strip, prev/next)
  readonly agentEvents: Uint32Array[];      // event indices per agent
  readonly detail: DetailBundle | null;
  attachDetail(d: DetailBundle): void;      // throws BundleError('hash') on mismatch
  label(i: number): string;                 // event label
  eventAt(t: number): number;               // last index with evT <= t, -1 if none
  nextEvent(t: number, dir: 1 | -1, pass?: (i: number) => boolean): number;
  edgeWeightAt(e: number, t: number): { n: number; w: number; lastT: number };
  driftAt(agent: number, t: number): number;
  agentState(agent: number, t: number, out?: AgentState): AgentState;
  nodeStats(node: number, t: number): NodeStats;
  threadAt(thread: number, t: number): ThreadView;
  swarmAt(t: number, out?: SwarmState): SwarmState;
  describe(i: number): EventText;
  search(q: string): { nodes: Uint8Array; events: Uint32Array };      // labels, names, bodies, cmds, reasoning
}
export type BuildModel = (card: RunCard, core: CoreBundle) => RunModel;

export interface FilterState {
  nodeKinds: Set<NodeKind | 'trail'>;
  agents: Set<number> | null;               // null = all
  neighbourhoodOf: number | null;           // node index: keep it + 1-hop neighbours
  eventKinds: Set<EventKind>;
  edgeKinds: Set<EdgeKind>;
  phases: Set<Phase>;
  recentMs: number | null;                  // edge recency window; null = all history
  range: [t0: number, t1: number] | null;   // time brush (shift-drag on track)
  text: string;
  mode: 'dim' | 'hide';
}
/** 0 hidden, 1 dim, 2 normal, 3 search hit. Recomputed only when the state changes, never per frame. */
export interface FilterMask {
  version: number;
  node: Uint8Array; edge: Uint8Array; event: Uint8Array;
  hits: Uint32Array;                        // matching event indices, for up/down cycling
  activeCount: number;                      // number of non-default facets (badge on Filters button)
}
export interface FilterStore extends Emitter<{ change: { state: Readonly<FilterState>; mask: FilterMask } }> {
  get(): Readonly<FilterState>;
  set(patch: Partial<FilterState>): void;
  reset(): void;
  bind(m: RunModel): void;                  // recompiles the mask for a new run
  mask(): FilterMask;
  encode(): string;                         // compact URL form
  decode(q: string): void;
}

/* ============================================================================================
 * 6. Playback clock (src/player/clock.ts)
 * ========================================================================================== */

export const RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 4, 8, 16] as const;
export type PlaybackRate = (typeof RATES)[number];
export type SeekCause = 'scrub' | 'key' | 'marker' | 'feed' | 'url' | 'tour' | 'end' | 'api' | 'audio';

export interface ClockEvents {
  tick: { t: number; dtWall: number };
  state: { playing: boolean; rate: PlaybackRate; skipQuiet: boolean; skipping: boolean };
  seek: { t: number; from: number; cause: SeekCause };
  chapter: { index: number };               // crossed forward during play
  ended: Record<string, never>;
}
export interface PlaybackClock extends Emitter<ClockEvents> {
  readonly duration: number; readonly t: number; readonly playing: boolean;
  readonly rate: PlaybackRate; readonly skipQuiet: boolean; readonly skipping: boolean;
  setModel(m: RunModel): void;              // t = 0, paused
  play(): void;                             // at end: restart from 0
  pause(): void; toggle(): void;
  seek(t: number, cause: SeekCause): void;  // clamps to [0, duration]
  seekBy(dt: number, cause: SeekCause): void;
  stepEvent(dir: 1 | -1, pass?: (i: number) => boolean): void;
  setRate(r: PlaybackRate): void; setSkipQuiet(on: boolean): void;
  beginScrub(): void; endScrub(): void;     // pauses while dragging, restores play state
  advance(dtWallMs: number): boolean;       // called only by the engine loop; true if t changed
  /** Narration (NARRATION.md 9.3): while a driver is set, advance() takes t from it and ignores rate/skip-quiet. */
  setDriver(d: ClockDriver | null): void;
  readonly driven: boolean;
  syncDriver(): void;                       // one driver step outside the frame loop (audio ended, wake)
}
export interface ClockDriver { t(): number; done(): boolean }

/* ============================================================================================
 * 7. Engine (src/engine/**): layout, frame, camera, gestures, renderer, picking, labels
 * ========================================================================================== */

export type Vec3 = Float32Array;            // length 3
export type Mat4 = Float32Array;            // length 16, column-major
export type ViewMode = 'constellation' | 'worldline';

export interface Layout {
  pos: Float32Array;                        // static xyz per node (agents: unused, see agentPos)
  agentAnchors: { task: Float32Array; board: Float32Array; phi: number }[];
  task: Float32Array; board: Float32Array; boardRadius: number;
  worldline: { height: number; prehistory: number }; // Y span for [0, tEnd]; stub depth below 0
  bounds: { center: Float32Array; radius: number };
  cohesionWindowMs: number;                 // W
}
export type ComputeLayout = (m: RunModel) => Layout;          // deterministic, ~1 ms
export type AgentPos = (m: RunModel, L: Layout, agent: number, t: number, out: Vec3) => Vec3;
/** Map time to worldline height. t < 0 is log-squashed into [-prehistory, 0). */
export type TimeToY = (L: Layout, tEnd: number, t: number, prehistoryMs: number) => number;

export interface SegmentBuffer {            // static, uploaded once per (run, filter, mode); sorted by t
  count: number; t: Float64Array; a: Float32Array; b: Float32Array; rgba: Float32Array; width: Float32Array;
  style: Uint8Array;                        // 0 solid, 1 dashed, 2 dotted
}
export interface StaticGeometry { worldlines: SegmentBuffer; pillars: SegmentBuffer; chords: SegmentBuffer; beads: SegmentBuffer }

export const NB = { VISIBLE: 1, DIM: 2, HOVER: 4, PIN: 8, ACTIVE: 16, NEW: 32, MATCH: 64, HOLLOW: 128,
                    LEAK: 256, THINKING: 512, DOZING: 1024 } as const;

export interface FrameState {
  t: number; mode: ViewMode; modeMix: number;                // 0 constellation .. 1 worldline (600 ms lerp)
  version: { pos: number; style: number };
  nodes: { count: number; pos: Float32Array; size: Float32Array; rgba: Float32Array; shape: Uint8Array;
           bits: Uint16Array; heat: Float32Array };          // indexed by node index (count == nNodes)
  trail: { count: number; pos: Float32Array; size: Float32Array; rgba: Float32Array; event: Int32Array };
  edges: { count: number; edge: Int32Array; alpha: Float32Array; glow: Float32Array; bend: Float32Array };
  pulses: { count: number; edge: Int32Array; u: Float32Array; strength: Float32Array };
  staticDraw: { worldlines: number; pillars: number; chords: number; beads: number }; // draw counts at t
  plane: { y: number; radius: number; alpha: number };
  hulls: { cx: number; cy: number; cz: number; r: number; members: number[] }[];
  flashes: { node: number; k: 'solve' | 'leak' | 'post'; age: number }[];
}
export interface FrameOpts {
  recentMs: number | null; rate: PlaybackRate; showTrails: boolean; showIdle: boolean;
  hover: number; selected: number; reducedMotion: boolean; mode: ViewMode; modeMix: number;
}
export type ComputeFrame = (m: RunModel, L: Layout, t: number, mask: FilterMask, o: FrameOpts,
                            reuse?: FrameState) => FrameState;  // pure: same inputs => identical output

export interface CameraMatrices {
  view: Mat4; proj: Mat4; viewProj: Mat4; eye: Vec3; widthPx: number; heightPx: number; dpr: number;
}
export interface OrbitCamera {
  target: Vec3; distance: number; yaw: number; pitch: number; fovY: number;
  orbit(dxPx: number, dyPx: number): void;
  pan(dxPx: number, dyPx: number): void;
  zoom(factor: number, anchorX?: number, anchorY?: number): void;   // toward the cursor (CSS px)
  fling(vYaw: number, vPitch: number, vPanX: number, vPanY: number): void;
  animateTo(p: Partial<{ target: Vec3; distance: number; yaw: number; pitch: number }>, ms: number): void;
  fit(center: Vec3, radius: number, animate: boolean): void;
  reset(animate: boolean): void;
  setReducedMotion(on: boolean): void;
  update(dtMs: number): boolean;            // inertia + tweens; true while moving
  matrices(widthPx: number, heightPx: number, dpr: number): CameraMatrices;
  project(p: ArrayLike<number>, o: number, cam: CameraMatrices, out: Float32Array, oo: number): void; // x,y,depth,scale
}

export type GestureState = 'idle' | 'press' | 'orbit' | 'pan' | 'pinch' | 'inertia';
export interface GestureHandlers {
  orbit(dx: number, dy: number): void; pan(dx: number, dy: number): void;
  zoom(f: number, x: number, y: number): void;
  tap(x: number, y: number, pointerType: string): void;
  doubleTap(x: number, y: number, pointerType: string): void;
  longPress(x: number, y: number): void;
  hover(x: number, y: number): void; leave(): void;
  fling(vx: number, vy: number, kind: 'orbit' | 'pan'): void;
  state(s: GestureState): void;
  engage(): void; hint(show: boolean): void; // cooperative gestures (SPEC 6.3)
}
export interface GestureController { setEngaged(on: boolean): void; readonly engaged: boolean; dispose(): void }
export type AttachGestures = (el: HTMLElement, h: GestureHandlers, o: { alwaysEngaged: () => boolean }) => GestureController;

export interface ThemeColors {
  bg: [number, number, number]; text: [number, number, number];
  agents: [number, number, number][]; kinds: Record<NodeKind, [number, number, number]>;
  edges: Record<EdgeKind, [number, number, number]>; cats: Record<StepCat, [number, number, number]>;
  leak: [number, number, number]; accent: [number, number, number]; liveLive: [number, number, number];
}

export interface ProjectedSet {             // screen space, CSS px; filled by the engine each drawn frame
  nodeXY: Float32Array; nodeDepth: Float32Array; nodeR: Float32Array; nodeVisible: Uint8Array; nodeCount: number;
  trailXY: Float32Array; trailR: Float32Array; trailCount: number;
  edgeXY: Float32Array; edgeCount: number;  // 4 per edge (src xy, dst xy); only drawn social edges
  edgeIdx?: Int32Array;                     // [B, appended] edge index per projected social edge
  edgeCtl?: Float32Array;                   // [B, appended] 2 per edge: projected Bezier control point
}

export interface Renderer {
  readonly kind: 'webgl2' | 'canvas2d';
  setTopology(m: RunModel, theme: ThemeColors): void;  // edge src/dst/kind/bend, node shapes
  uploadStatic(g: StaticGeometry | null): void;
  resize(cssW: number, cssH: number, dpr: number): void;
  draw(f: FrameState, cam: CameraMatrices, o: { reducedMotion: boolean; timeMs: number }): void;
  dispose(): void;
}
export type CreateRenderer = (canvas: HTMLCanvasElement, prefer?: 'webgl2' | 'canvas2d') => Renderer | null;

export type PickHit = { kind: 'node'; node: number } | { kind: 'step'; event: number } | { kind: 'edge'; edge: number };
export type PickFn = (p: ProjectedSet, f: FrameState, x: number, y: number) => PickHit | null;

export interface LabelLayer {
  update(p: ProjectedSet, f: FrameState, m: RunModel, s: { selected: number; hover: number;
         focusDepth: number; enabled: boolean; coarse: boolean;
         /** screen rects (x0,y0,x1,y1 in layer px) tags must not cover: HUD, caption, chip ([data-tag-avoid]) */
         avoid?: Float32Array; avoidCount?: number }): void;
  clear(): void; dispose(): void;
}
export type CreateLabels = (host: HTMLElement, max?: number) => LabelLayer;

export interface EngineEvents {
  hover: { hit: PickHit | null; x: number; y: number };
  select: { hit: PickHit | null; x: number; y: number };
  focus: { node: number };
  cameraInteract: Record<string, never>;
  engaged: { on: boolean };
  backend: { kind: 'webgl2' | 'canvas2d' | 'none'; reason?: string };
  frame: { t: number; ms: number };         // perf overlay
  seekRequest: { dt: number };              // touch double-tap on empty left/right third
}
export interface SceneEngine extends Emitter<EngineEvents> {
  readonly backend: 'webgl2' | 'canvas2d' | 'none';
  readonly camera: OrbitCamera;
  readonly mode: ViewMode;
  setRun(m: RunModel): void;                // computes layout, uploads topology, frames the camera
  setMask(mask: FilterMask): void;
  setOptions(o: Partial<Pick<FrameOpts, 'recentMs' | 'showTrails' | 'showIdle' | 'reducedMotion'> & { labels: boolean }>): void;
  setMode(m: ViewMode, animate: boolean): void;
  setFollow(on: boolean): void;             // worldline: keep target on the now plane
  highlight(nodes: number[] | null): void;  // marker/feed hover
  focusNode(node: number, o?: { nudge?: number; ms?: number }): void; // nudge (0,1] = partial move (tour)
  resetView(): void;
  setEngaged(on: boolean): void;
  swarm(): SwarmState;                      // latest computed (HUD)
  requestRender(): void;
  start(): void; stop(): void;              // IntersectionObserver + visibilitychange drive these
  dispose(): void;
  select?(hit: PickHit | null): void;       // [B, appended] set/clear the pinned node (PIN bit) without emitting 'select'
  readonly canvas?: HTMLCanvasElement;      // [B, appended] the stage canvas (role=img; C updates its aria-label)
  /** Stage edges covered by floating UI (CSS px); the camera centres and frames the uncovered area. */
  setInsets?(i: { left: number; top: number; right: number; bottom: number }, animate?: boolean): void;
}
export type CreateEngine = (stage: HTMLElement, clock: PlaybackClock, o: { prefer?: 'webgl2' | 'canvas2d'; perf?: boolean }) => SceneEngine;

/* ============================================================================================
 * 8. Player + UI (src/player/**, src/ui/**)
 * ========================================================================================== */

export interface UrlState { run?: string; t?: number; rate?: PlaybackRate; mode?: ViewMode; sel?: string; f?: string;
                            renderer?: 'webgl2' | 'canvas2d'; perf?: boolean }

export type KeyAction =
  | 'toggle' | 'back10' | 'fwd10' | 'back5' | 'fwd5' | 'prevEvent' | 'nextEvent' | 'home' | 'end'
  | 'fullscreen' | 'mode' | 'follow' | 'reset' | 'captions' | 'labels' | 'search' | 'help' | 'escape'
  | 'faster' | 'slower' | 'narration' | { seekPct: number };
/** owned(): has the user interacted with the player? Outside the player, page-scroll keys (Space, arrows,
 *  Home/End) are only captured once it returns true, so Space still scrolls a fresh landing page. */
export type BindKeys = (scope: HTMLElement, run: (a: KeyAction) => void, isPrimary: () => boolean, owned?: () => boolean) => () => void;

export interface TimelineOptions {
  clock: PlaybackClock;
  isEventVisible: (i: number) => boolean;
  onMarkerHover: (ch: ChapterRec | null) => void;
  onRangeSelect: (r: [number, number] | null) => void;
  onToggle: (what: 'filters' | 'labels' | 'captions' | 'fullscreen' | 'resetView' | 'mode' | 'narration') => void;
}
export interface TimelineBar {
  readonly el: HTMLElement;
  setModel(m: RunModel): void;              // segments, markers (merged within 1% width), heat curve
  setBuffered(frac: number): void;          // detail-bundle download progress 0..1
  setNodeActivity(ticks: Float64Array | null, color: string): void;
  setControlsVisible(v: boolean): void;
  refreshMarkers(): void;
  render(t: number): void;                  // cheap; only writes changed DOM
  dispose(): void;
}

export interface SwarmSelector { readonly el: HTMLElement; setActive(id: string): void; setQuery(q: RunQuery): void }
export interface RunQuery { model?: string[]; goal?: Goal[]; iface?: Iface[]; pop?: Pop[]; includeHidden?: boolean }
export interface Feed { setModel(m: RunModel): void; setMask(m: FilterMask): void; setTime(t: number): void;
                        onPick(cb: (eventIndex: number) => void): void; setExpanded(on: boolean): void }
export interface Captions { setChapters(c: ChapterRec[]): void; setTime(t: number, playing: boolean): void;
                            enabled: boolean; onBeat(cb: (ch: ChapterRec) => void): void }
export interface Tooltip { show(hit: PickHit, x: number, y: number, t: number): void; hide(): void }
export interface DetailPanel { pin(hit: PickHit | null): void; setTime(t: number): void;
                               onSeek(cb: (t: number) => void): void; onFocus(cb: (node: number) => void): void }
export interface EndCard { show(next: RunCard | null, sibling: RunCard | null): void; hide(): void;
                           onAction(cb: (a: 'replay' | 'next' | 'sibling' | 'findings') => void): void }
export interface Hud { update(s: SwarmState, agents: AgentState[]): void }
export interface NodeList { setModel(m: RunModel): void; update(mask: FilterMask, t: number): void;
                            onPick(cb: (node: number) => void): void }
export interface Player { load(id: string): Promise<void>; readonly clock: PlaybackClock;
                          readonly filters: FilterStore; readonly engine: SceneEngine; dispose(): void }

/* ============================================================================================
 * 9. Utilities (src/util/**)
 * ========================================================================================== */

export interface Emitter<E extends object> {
  on<K extends keyof E>(k: K, f: (e: E[K]) => void): () => void;
  emit<K extends keyof E>(k: K, e: E[K]): void;
}
export interface Env { webgl2: boolean; reducedMotion: boolean; coarse: boolean; saveData: boolean; dpr: number;
                       touch: boolean }

/* ============================================================================================
 * 10. Narration track (NARRATION.md section 2): pre-generated voice + SFX, one mp3 per run
 * ========================================================================================== */

export const NARRATION_FORMAT = 'swarmpit.narration/1';
export const NARRATION_INDEX_FORMAT = 'swarmpit.narration-index/1';
export type NarrationVoice = 'guide' | 'expert';
export interface NarrationCue { audioMs: number; endMs: number; runMs: number; voice: NarrationVoice; tpl: string; text: string }
export interface NarrationManifest {
  schema: 'swarmpit.narration/1'; generator: string; runId: string; runHash: string; audio: string;
  audioBytes: number; durationMs: number; runEndMs: number;
  timeMap: [number, number][];              // [[audioMs, runMs], ...] monotonic, piecewise linear
  cues: NarrationCue[]; sfx: { counts: Record<string, number>; dropped: number };
}
export interface NarrationIndex { schema: 'swarmpit.narration-index/1'; runs: Record<string, { hash: string; manifest: string; durationMs: number }> }
