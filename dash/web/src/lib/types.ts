// Hand-written mirror of the response shapes in dash/CONTRACT.md section 5.
// When an API shape changes, update this file in the same change.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Dict = Record<string, unknown>;

export type RunStatus = 'pending' | 'live' | 'collecting' | 'done' | 'errored' | 'abandoned' | 'missing';

// 5.1
export interface Health {
  ok: boolean;
  version: string;
  schema_version: number;
  derive_version: number;
  db: string;
  runs_dir: string;
  runs_dir_exists: boolean;
  n_runs: number;
  n_live: number;
  last_ingest_ts: number | null;
  ingest_lag_s: number | null;
  live_tap: { enabled: boolean; msb: string | null; live_vms: number; last_discover_ts: number | null; errors: number };
  web_built: boolean;
}

// 5.2
export interface FacetValue { value: string | number | boolean | null; n_runs: number }
export interface FacetDim { key: string; kind: 'cat' | 'num'; values: FacetValue[] }
export interface MetricDef {
  metric_id: string;
  label: string;
  unit: string; // steps|share|count|s|tokens|bool
  grain: 'agent' | 'run';
  agg: string;
  higher_is: 'stickier' | 'better' | 'neutral' | string;
  description: string;
  params?: Dict;
}
export interface Facets { dims: FacetDim[]; metrics: MetricDef[] }

// 5.3
export interface RunMetrics {
  discovery_rate?: number | null;
  solve_rate?: number | null;
  total_posts?: number | null;
  requests?: number | null;
  cross_mentions?: number | null;
  mention_edges_live?: number | null;
  live_live_edges?: number | null;
  swarm_onset_t?: number | null;
  any_leak?: boolean | null;
  tokens_prompt?: number | null;
  tokens_completion?: number | null;
  [k: string]: number | boolean | null | undefined;
}
export interface Harness {
  model?: string;
  budget?: number;
  temperature?: number;
  max_tokens?: number;
  output_chars?: number;
  run_timeout_s?: number;
  [k: string]: unknown;
}
export type Goal = 'solvable' | 'unsolvable';
export interface RunSummary {
  run_id: string;
  status: RunStatus;
  experiment_id: string | null;
  condition_id: string | null;
  condition_label: string | null;
  model: string | null;
  iface: string | null;
  pop: string | null;
  n_agents: number | null;
  rep: number | null;
  tag: string;
  /** CTF goal: 'solvable' (historical default) or 'unsolvable' (the flag can never be accepted; no free phase) */
  goal: Goal;
  started: number | null;
  finished: number | null;
  duration_s: number | null;
  harness: Harness | null;
  params: Dict;
  metrics: RunMetrics;
  notes: (string | Dict)[];
  live_tapped: boolean;
  data_version: number;
  /** last event, seconds since t_origin */
  t_end?: number | null;
}
export interface RunsResponse { total: number; runs: RunSummary[] }

// 5.4
export interface RunFull extends RunSummary {
  ctf?: Dict | null;
  t_origin: number | null;
  t_origin_final: boolean;
  t_end: number | null;
  board_offset_s: number;
  seed_hash: string | null;
  timed_out: number[];
  final_missing: boolean;
}
export interface AgentInfo {
  agent_id: number;
  model: string | null;
  status: string;
  solved: boolean | null;
  solve_step: number | null;
  solve_t: number | null;
  steps_used: number | null;
  model_calls: number | null;
  ended: string | null;
  crashed: string | null;
  start_t: number | null;
  end_t: number | null;
  clock_offset_s: number;
  display_names: string[];
  tokens: { prompt: number | null; completion: number | null; cached: number | null; reasoning: number | null };
  metrics: Record<string, number | boolean | null>;
}
export interface Marker { kind: string; t: number | null; source: string; agent_id: number | null; detail: string | null }
export interface ThreadInfo { thread: string; title: string | null; is_seed: boolean; created_t: number | null; created_by: string | null }
export interface RunDetail {
  run: RunFull;
  condition: { condition_id: string; label: string; factors: Dict } | null;
  experiment: { experiment_id: string; name: string } | null;
  agents: AgentInfo[];
  markers: Marker[];
  diag: { agent_id: number; name: string; size: number }[];
  threads: ThreadInfo[];
}

// 5.5
export type EventKind =
  | 'step' | 'idle' | 'model' | 'start' | 'end' | 'crash' | 'api_retry'
  | 'req' | 'post' | 'upvote' | 'delete' | 'dm' | 'irc_cmd' | 'irc_join' | 'connect' | 'disconnect' | 'board_start'
  | 'relay_open' | 'relay_close' | 'relay_data'
  // derived detections (SSE only)
  | 'first_contact' | 'solve' | 'secret_leak' | 'persistence' | 'swarm_onset';

export interface TimelineEvent {
  t: number;
  t_end?: number | null;
  lane: number;
  kind: EventKind | string;
  step?: number | null;
  call?: number | null;
  cat?: string | null;
  is_board?: boolean | null;
  phase?: string | null;
  label: string;
  ref: string;
  flags?: string[];
  completion_tokens?: number | null; // model rows (server name); normalized into `tokens` by RunModel
  uid?: string;
  /** optional extension: completion tokens for model rows (tick height). Not in contract v1; absent -> uniform. */
  tokens?: number | null;
  run_id?: string; // SSE envelope
  src?: string;
}
export interface Post {
  uid: string;
  post_id: number | null;
  thread: string | null;
  author: string | null;
  author_node: string | null;
  agent_id: number | null;
  is_seed: boolean;
  t: number | null;
  upvotes0: number;
  body: string | null;
  flags: string[];
  kind: 'post' | 'dm' | string;
  step?: number | null;
  phase?: string | null;
}
export interface Phase { agent_id: number; start: number | null; solve: number | null; end: number | null }
export interface Lane { lane: number; label: string; names?: string[] }
export interface Timeline {
  run_id: string;
  t_origin: number | null;
  t_end: number | null;
  status: RunStatus;
  data_version: number;
  lanes: Lane[];
  phases: Phase[];
  events: TimelineEvent[];
  posts: Post[];
  upvotes: { t: number; src: string; post_uid: string }[];
}

// 5.6
export interface StateResponse {
  t: number;
  ts: number;
  board?: { iface: string; n_posts: number; threads: { thread: string; title: string | null; is_seed: boolean; posts: (Post & { upvotes: number; new: boolean })[] }[] };
  agents?: Dict[];
  metrics?: Dict;
}

// 5.7
export interface GraphNode {
  id: string;
  kind: 'agent' | 'seed' | 'name' | string;
  label: string;
  agent_id?: number;
  names?: string[];
  posts: number;
  in_w: number;
  out_w: number;
  solved_t?: number | null;
}
export type EdgeKind = 'exposure' | 'reply' | 'mention' | 'dm' | 'upvote';
export interface GraphEdge { src: string; dst: string; kind: EdgeKind | string; n: number; w: number; first_t: number; last_t: number; ts?: (number | null)[] }
export interface Graph {
  t: number | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
  summary: { live_live_edges: number; density: number; reciprocity: number };
}

// 5.8
export interface ToolCall { idx: number; id: string | null; name: string; args_valid: boolean; command: string | null; arguments?: string }
export interface ModelCall {
  call: number;
  t: number | null;
  latency_s: number | null;
  phase: string | null;
  content: string | null;
  reasoning: string | null;
  tool_calls?: ToolCall[];
  n_tool_calls?: number;
  usage?: { prompt_tokens?: number | null; completion_tokens?: number | null; reasoning_tokens?: number | null; cached_tokens?: number | null };
}
export interface StepRow {
  step: number;
  t: number | null;
  t_end: number | null;
  dur_s: number | null;
  phase_before: string | null;
  phase: string | null;
  is_solve: boolean;
  idle: boolean;
  command: string | null;
  exit: number | null;
  output: string | null;
  output_len: number | null;
  submissions: { ts: number; value: string; correct: boolean | null }[];
  is_board: boolean;
  board_via: string | null;
  cat: string | null;
  persistence: boolean;
  model?: ModelCall | null;
  board?: { t: number; kind: string; method?: string | null; path?: string | null; status?: number | null; target_kind?: string | null; body?: string | null }[];
  relay?: { conns: number; bytes_up: number; bytes_down: number } | null;
}
export interface StepsResponse {
  run_id: string;
  agent_id: number;
  steps: StepRow[];
  orphan_calls: ModelCall[];
  retries: { after_t: number; attempt: number; wait: number; detail: string }[];
}

// 5.9
/** na: set when the metric does not exist for the cell (free-phase metrics under goal = unsolvable) */
export interface CellValue { mean: number | null; median: number | null; sd: number | null; lo: number | null; hi: number | null; n: number; na?: string }
export interface Cell {
  key: Record<string, string | number | null>;
  n_runs: number;
  n_agents: number;
  values: Record<string, CellValue>;
  points?: (Record<string, number | boolean | null | string>)[];
}
export interface CellsResponse { group: string[]; metrics: string[]; ci: string; B: number; cells: Cell[] }

// 5.10
export interface Series { key: Dict; label: string; y: (number | null)[]; lo?: (number | null)[]; hi?: (number | null)[] }
export interface SeriesResponse { metric: string; x: string; xs: number[]; series: Series[] }

// 5.12
export type LiveEventName = 'run_started' | 'event' | 'run_updated' | 'run_finished' | 'metrics_update' | 'reset';
export interface LiveMessage { id: number | null; event: LiveEventName | string; data: Dict }

export type Filters = Record<string, string[]>;
