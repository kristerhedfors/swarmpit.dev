// Wire format of data/instigation/explorer.json (schema "instigation.explorer.v1"): numbers, fixed enums and
// opaque ids only. The client re-validates everything it reads (data.ts normalize) and never renders a string
// as markup: all text goes through textContent / attribute setters.

export const EXPLORER_SCHEMA = 'instigation.explorer.v1';

export interface ModelDim { slug: string; label: string; origin: string; size_b: number | null; provider: string; retired: boolean }
export interface ChannelDim { id: string; label: string }

export interface Dims {
  model: ModelDim[];
  channel: ChannelDim[];
  length_band: string[];
  trigger_code: string[];
  goal: string[];
  origin: string[];
}

export interface Seed {
  seed_chars: number;
  length_band: string;
  origin: string;
  bootstrap: boolean;
  parent_id: string | null;
  generation: number;
  gen_model: string | null;
}

export interface Engagement {
  board_reads: number;
  relay_connections: number;
  board_refs: number;
  agents_engaged: number;
  engagement_score: number;
}

export interface Panel { slugs: string[]; agreement_mean: number | null }

export interface Episode {
  run_id: string;
  record_id: string;
  goal: string;
  model: string;
  channel: string;
  rep: number;
  generation: number;
  n_agents: number;
  budget: number | null;
  n_writers: number;
  write_fraction: number;
  S_mean: number;
  I: number;
  instigation_score: number;
  run_ttfw_s: number | null;
  run_first_write_step: number | null;
  pile_in_onset_t_s: number | null;
  posts_over_time: [number, number, number][];
  engagement: Engagement;
  trigger_code: string;
  agent_codes: (string | null)[];
  panel: Panel | null;
  classifier_errors: number;
  elite_in: string[];
}

/** One elite in a landscape grid (scoreboard._project_cell). */
export interface LandscapeCell {
  record_id: string;
  instigation_score: number;
  length: number | null;
  trigger_code: string;
  channel: string;
  n_writers: number | null;
  engagement: Partial<Engagement>;
  model: string;
}

/** Normalized landscape: v1 (flat cells, single legacy model) and v2 (by_model) both map onto this. */
export interface Landscape {
  goal: string;
  axes: { trigger_code: string[]; channel: string[]; length_band: string[] };
  /** best elite per cell across models */
  best: Record<string, LandscapeCell>;
  /** per model slug: cell_key -> elite */
  byModel: Record<string, Record<string, LandscapeCell>>;
  nCellsPossible: number;
}

export interface Explorer {
  generated_ts: number;
  dims: Dims;
  seeds: Record<string, Seed>;
  episodes: Episode[];
  landscapes: Record<string, Landscape>;
  /** number of episodes dropped by validation (malformed rows never render) */
  dropped: number;
}

export type Dim = 'goal' | 'model' | 'channel' | 'length_band' | 'trigger_code' | 'agent_code' | 'origin' | 'generation' | 'writers';
export type MetricId = 'episodes' | 'wrate' | 'pile' | 'mean_score' | 'max_score' | 'mean_writers' | 'max_writers' | 'agents' | 'mean_eng';
export type ViewId = 'pivot' | 'landscape' | 'episodes' | 'lineage' | 'models';

export type Filters = { [K in Dim]: string[] };

export interface State {
  view: ViewId;
  filters: Filters;
  /** restrict to one seed (record id), '' = none */
  seed: string;
  rows: Dim;
  cols: Dim;
  metric: MetricId;
  /** model-comparison metric */
  cmp: 'wrate' | 'pile';
  /** landscape: show the per-model grids ('models') or only the best-across-models grid ('best') */
  lmode: 'models' | 'best';
  sort: string;
  desc: boolean;
  page: number;
  /** selected episode run_id ('' = none) */
  ep: string;
}

export interface Agg {
  n: number;
  /** numerator for rates */
  k: number;
  value: number | null;
  lo: number | null;
  hi: number | null;
}
