// Pure helpers for the swarmpit-instigation dataset viewer (no DOM access; unit-tested under node --test).
//
// Everything here treats dataset text as opaque data: it is never interpreted, evaluated or turned
// into markup. Rendering (always via textContent) lives in viewer.js.

export const FILES = ["seeds", "evaluations", "responses", "pairs", "sft"];
export const SPLITS = ["train", "val", "test"];
export const ORIGINS = ["pilot", "scratch", "mutation"];
export const LENGTH_BANDS = ["le120", "121-200", "201-320", "gt320"];
export const SEED_SORTS = {
  writer_rate: "writer rate",
  ci_lo: "CI95 lower bound",
  best_score: "best score",
  mean_score: "mean score",
  n_evals: "evaluations",
  pile_in_rate: "pile-in rate",
  generation: "generation",
  seed_chars: "seed chars",
};
// Seed facet keys that live in the URL hash (view state) and drive filterSeeds().
export const SEED_FACETS = ["split", "origin", "gen", "band", "trig", "ctx", "label", "cluster", "q"];

// ---------------------------------------------------------------- parsing

/** Parse JSONL text. Malformed lines are skipped and only counted (never echoed). */
export function parseJsonl(text) {
  const rows = [];
  let bad = 0;
  const lines = String(text ?? "").split("\n");
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    try {
      const v = JSON.parse(s);
      if (v && typeof v === "object" && !Array.isArray(v)) rows.push(v);
      else bad++;
    } catch {
      bad++;
    }
  }
  return { rows, bad };
}

// ---------------------------------------------------------------- small utils

export const num = (x, d = 0) => (typeof x === "number" && Number.isFinite(x) ? x : d);
export const get = (o, path, d = undefined) => {
  let v = o;
  for (const k of path.split(".")) {
    if (v == null || typeof v !== "object") return d;
    v = v[k];
  }
  return v === undefined ? d : v;
};

export function fmtPct(x, digits = 0) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "–";
  return (x * 100).toFixed(digits) + "%";
}

export function fmtNum(x, digits = 3) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "–";
  if (Number.isInteger(x)) return String(x);
  return x.toFixed(digits);
}

export function truncate(s, n) {
  s = String(s ?? "");
  return s.length <= n ? s : s.slice(0, Math.max(0, n - 1)) + "…";
}

/** Wilson score interval (fallback when a row lacks writer_rate_ci95). */
export function wilson(k, n, z = 1.96) {
  if (!n) return [0, 0];
  const p = k / n;
  const d = 1 + (z * z) / n;
  const c = (p + (z * z) / (2 * n)) / d;
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}

// ---------------------------------------------------------------- contexts

const ctxPart = (v) => (Number.isInteger(v) ? String(v) : "-");

/**
 * "goal|model|channel|n_agents|budget" for a context object or an already-joined key. A label is valid only
 * within its full context (agent count and step budget included); "-" = unknown.
 */
export function ctxKey(c) {
  if (typeof c === "string") return c;
  if (!c || typeof c !== "object") return "";
  return [c.goal ?? "?", c.model ?? "?", c.channel ?? "?", ctxPart(c.n_agents), ctxPart(c.budget)].join("|");
}

export function parseCtxKey(k) {
  const [goal = "?", model = "?", channel = "?", na = "-", bu = "-"] = String(k ?? "").split("|");
  const int = (x) => (/^\d+$/.test(x) ? Number(x) : null);
  return { goal, model, channel, n_agents: int(na), budget: int(bu) };
}

/** Human label for a context: "goal · model · channel · 4 agents · 40 steps". */
export function ctxLabel(c) {
  const p = parseCtxKey(ctxKey(c));
  return [p.goal, p.model, p.channel, p.n_agents === null ? "? agents" : `${p.n_agents} agents`,
    p.budget === null ? "? steps" : `${p.budget} steps`].join(" · ");
}

// ---------------------------------------------------------------- seed accessors

export const seedCi = (s) => {
  const ci = get(s, "outcomes.writer_rate_ci95");
  if (Array.isArray(ci) && ci.length === 2) return [num(ci[0]), num(ci[1])];
  return wilson(num(get(s, "outcomes.n_evals_with_writers")), num(get(s, "outcomes.n_evals")));
};
export const isEvaluated = (s) => num(get(s, "outcomes.n_evals")) > 0;
export const isInstigated = (s) => num(get(s, "outcomes.n_evals_with_writers")) > 0;
export const isPileIn = (s) => num(get(s, "outcomes.pile_in_rate")) > 0;
export const LABELS = ["instigated", "not_instigated", "pile_in", "unevaluated"];

/**
 * A seed's label, pooled over all contexts or (ctx given) within that one context only:
 * {evaluated, instigated, pile_in}. A seed with no evaluation (there) has no label: it is not a negative.
 */
export function seedLabel(s, ctx = "") {
  if (ctx) {
    const b = (get(s, "outcomes.by_context") || {})[ctx];
    if (!b || !(num(b.n) > 0)) return { evaluated: false, instigated: false, pile_in: false };
    return { evaluated: true, instigated: num(b.writers_any) > 0, pile_in: num(b.max_writers) >= 2 };
  }
  return { evaluated: isEvaluated(s), instigated: isInstigated(s), pile_in: isPileIn(s) };
}

/** Does seed s carry label value `label` (in context ctx when given)? */
export function hasLabel(s, label, ctx = "") {
  const l = seedLabel(s, ctx);
  switch (label) {
    case "instigated": return l.instigated;
    case "not_instigated": return l.evaluated && !l.instigated;
    case "pile_in": return l.pile_in;
    case "unevaluated": return !l.evaluated;
    default: return true;
  }
}
export const seedCluster = (s) => (s && s._ndSingleton ? null : get(s, "dedup.near_dup_cluster", null) || null);

/**
 * The builder gives every seed a near_dup_cluster id (a singleton's id is its own). Mark seeds whose
 * cluster has no other member so facets and lineage treat them as "no near-duplicates". The marker is
 * non-enumerable, so exported rows are unchanged.
 */
export function markSingletonClusters(seeds) {
  const n = new Map();
  for (const s of seeds || []) {
    const c = get(s, "dedup.near_dup_cluster", null);
    if (c) n.set(c, (n.get(c) || 0) + 1);
  }
  for (const s of seeds || []) {
    if (!s || typeof s !== "object") continue;
    const c = get(s, "dedup.near_dup_cluster", null);
    Object.defineProperty(s, "_ndSingleton", { value: !!c && n.get(c) === 1, enumerable: false, configurable: true });
  }
  return seeds;
}

/** Per-agent trigger codes: the builder writes an array indexed by agent id (code or null); objects
 * {agent_id, code, wrote} are accepted too. Returns [{agent, code}]. */
export function perAgentCodes(list) {
  return (Array.isArray(list) ? list : []).map((a, i) => (a && typeof a === "object"
    ? { agent: a.agent_id ?? i, code: a.code ?? null }
    : { agent: i, code: typeof a === "string" ? a : null }));
}

export function seedSortValue(s, key) {
  switch (key) {
    case "ci_lo": return seedCi(s)[0];
    case "writer_rate": return num(get(s, "outcomes.writer_rate"));
    case "best_score": return num(get(s, "outcomes.best_score"));
    case "mean_score": return num(get(s, "outcomes.mean_score"));
    case "n_evals": return num(get(s, "outcomes.n_evals"));
    case "pile_in_rate": return num(get(s, "outcomes.pile_in_rate"));
    case "generation": return num(get(s, "provenance.generation"));
    case "seed_chars": return num(get(s, "stats.seed_chars"));
    default: return 0;
  }
}

/** Stable sort; ties broken by seed_id so the order is deterministic. */
export function sortSeeds(seeds, key = "writer_rate", dir = "desc") {
  const k = key in SEED_SORTS ? key : "writer_rate";
  const m = dir === "asc" ? 1 : -1;
  return [...seeds].sort((a, b) => {
    const d = (seedSortValue(a, k) - seedSortValue(b, k)) * m;
    if (d) return d;
    return String(a.seed_id).localeCompare(String(b.seed_id));
  });
}

/**
 * Filter seeds by facet state f (keys from SEED_FACETS). Empty / missing facet = no constraint.
 * label: instigated | not_instigated | pile_in | unevaluated, judged within f.ctx when a context is selected
 * (not_instigated needs an evaluation; unevaluated seeds are never negatives). cluster: "*" = any near-dup
 * cluster, "none" = singleton,
 * otherwise an exact cluster id. q: substring of seed_id / root_id / parent_id (case-insensitive).
 */
export function filterSeeds(seeds, f = {}) {
  const q = String(f.q ?? "").trim().toLowerCase();
  return seeds.filter((s) => {
    if (f.split && s.split !== f.split) return false;
    if (f.origin && get(s, "provenance.origin") !== f.origin) return false;
    if (f.gen !== undefined && f.gen !== "" && String(get(s, "provenance.generation")) !== String(f.gen)) return false;
    if (f.band && get(s, "stats.length_band") !== f.band) return false;
    if (f.trig && !(num(get(s, `triggers.${f.trig}`)) > 0)) return false;
    if (f.ctx && !(get(s, "outcomes.by_context") || {})[f.ctx]) return false;
    if (f.label && !hasLabel(s, f.label, f.ctx || "")) return false;
    if (f.cluster) {
      const c = seedCluster(s);
      if (f.cluster === "*" ? !c : f.cluster === "none" ? !!c : c !== f.cluster) return false;
    }
    if (q) {
      const hay = [s.seed_id, get(s, "provenance.root_id"), get(s, "provenance.parent_id"), seedCluster(s)]
        .filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

/** Distinct facet values present in the data (sorted), for building select options. */
export function facetValues(seeds) {
  const sets = { split: new Set(), origin: new Set(), gen: new Set(), band: new Set(), trig: new Set(),
    ctx: new Set(), cluster: new Set() };
  for (const s of seeds) {
    if (s.split) sets.split.add(s.split);
    const o = get(s, "provenance.origin"); if (o) sets.origin.add(o);
    const g = get(s, "provenance.generation"); if (g !== undefined && g !== null) sets.gen.add(g);
    const b = get(s, "stats.length_band"); if (b) sets.band.add(b);
    for (const [k, v] of Object.entries(s.triggers || {})) if (num(v) > 0) sets.trig.add(k);
    for (const k of Object.keys(get(s, "outcomes.by_context") || {})) sets.ctx.add(k);
    const c = seedCluster(s); if (c) sets.cluster.add(c);
  }
  const order = (arr, ref) => arr.sort((a, b) => {
    const ia = ref.indexOf(a), ib = ref.indexOf(b);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    return String(a).localeCompare(String(b));
  });
  return {
    split: order([...sets.split], SPLITS),
    origin: order([...sets.origin], ORIGINS),
    gen: [...sets.gen].sort((a, b) => a - b),
    band: order([...sets.band], LENGTH_BANDS),
    trig: [...sets.trig].sort(),
    ctx: [...sets.ctx].sort(),
    cluster: [...sets.cluster].sort(),
  };
}

// ---------------------------------------------------------------- indexes

/** Cross-file lookups used by the detail views. */
export function buildIndex(data) {
  const seedById = new Map();
  const children = new Map();
  const clusters = new Map();
  for (const s of data.seeds || []) {
    seedById.set(s.seed_id, s);
    const p = get(s, "provenance.parent_id");
    if (p) (children.get(p) || children.set(p, []).get(p)).push(s.seed_id);
    const c = seedCluster(s);
    if (c) (clusters.get(c) || clusters.set(c, []).get(c)).push(s.seed_id);
  }
  const evalsBySeed = new Map();
  const evalById = new Map();
  for (const e of data.evaluations || []) {
    evalById.set(e.eval_id, e);
    (evalsBySeed.get(e.seed_id) || evalsBySeed.set(e.seed_id, []).get(e.seed_id)).push(e);
  }
  const respByEval = new Map();
  const respBySeed = new Map();
  for (const r of data.responses || []) {
    (respByEval.get(r.eval_id) || respByEval.set(r.eval_id, []).get(r.eval_id)).push(r);
    (respBySeed.get(r.seed_id) || respBySeed.set(r.seed_id, []).get(r.seed_id)).push(r);
  }
  for (const list of respByEval.values()) list.sort((a, b) => num(a.t_rel_s) - num(b.t_rel_s));
  const pairsBySeed = new Map();
  for (const p of data.pairs || []) {
    for (const id of [p.chosen_seed_id, p.rejected_seed_id]) {
      (pairsBySeed.get(id) || pairsBySeed.set(id, []).get(id)).push(p);
    }
  }
  return { seedById, children, clusters, evalsBySeed, evalById, respByEval, respBySeed, pairsBySeed };
}

/** Lineage chain root..seed (ids), guarding against cycles / missing parents. */
export function ancestry(index, seedId) {
  const chain = [];
  const seen = new Set();
  let cur = seedId;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    chain.unshift(cur);
    const s = index.seedById.get(cur);
    cur = s ? get(s, "provenance.parent_id") : null;
  }
  return chain;
}

// ---------------------------------------------------------------- overview aggregates

export function countBy(rows, fn) {
  const m = {};
  for (const r of rows) {
    const k = fn(r);
    m[k] = (m[k] || 0) + 1;
  }
  return m;
}

function seedBalance(seeds) {
  const evaluated = seeds.filter(isEvaluated);
  const instigated = evaluated.filter(isInstigated).length;
  return { total: seeds.length, evaluated: evaluated.length, unevaluated: seeds.length - evaluated.length,
    instigated, not_instigated: evaluated.length - instigated, pile_in: evaluated.filter(isPileIn).length };
}

function evalBalance(evals) {
  const instigated = evals.filter((e) => num(get(e, "outcome.n_writers")) > 0).length;
  return { total: evals.length, instigated, not_instigated: evals.length - instigated,
    pile_in: evals.filter((e) => num(get(e, "outcome.n_writers")) >= 2).length };
}

/**
 * Label balance over seeds and evaluations, overall and per split. Seeds with no evaluation are counted
 * as unevaluated, never as negatives (not_instigated = evaluated - instigated).
 */
export function labelBalance(data) {
  const seeds = data.seeds || [];
  const evals = data.evaluations || [];
  const bySplit = {};
  for (const sp of SPLITS) {
    bySplit[sp] = { seeds: seedBalance(seeds.filter((s) => s.split === sp)),
      evaluations: evalBalance(evals.filter((e) => e.split === sp)) };
  }
  return { seeds: seedBalance(seeds), evaluations: evalBalance(evals), bySplit };
}

/**
 * Coverage matrix from evaluations: rows = goal|model|n_agents|budget (the context minus its channel),
 * cols = channel, so episodes run with different agent counts / budgets are never pooled.
 * cell = {n, writers_any, rate, seeds} (rate = share of episodes with >= 1 writer).
 */
export function coverageMatrix(evals) {
  const rows = new Set();
  const cols = new Set();
  const cells = {};
  for (const e of evals || []) {
    const c = e.context || {};
    const rk = `${c.goal ?? "?"}|${c.model ?? "?"}|${ctxPart(c.n_agents)}|${ctxPart(c.budget)}`;
    const ck = String(c.channel ?? "?");
    rows.add(rk);
    cols.add(ck);
    const key = `${rk}||${ck}`;
    const cell = cells[key] || (cells[key] = { n: 0, writers_any: 0, rate: 0, seedSet: new Set() });
    cell.n++;
    if (num(get(e, "outcome.n_writers")) > 0) cell.writers_any++;
    cell.seedSet.add(e.seed_id);
  }
  for (const cell of Object.values(cells)) {
    cell.rate = cell.n ? cell.writers_any / cell.n : 0;
    cell.seeds = cell.seedSet.size;
    delete cell.seedSet;
  }
  return { rows: [...rows].sort(), cols: [...cols].sort(), cells };
}

/** The full context key of a coverage-matrix cell (row key + channel column). */
export function coverageCtx(rowKey, channel) {
  const [goal, model, na, bu] = String(rowKey).split("|");
  return [goal, model, channel, na ?? "-", bu ?? "-"].join("|");
}

// ---------------------------------------------------------------- pairs & responses

export function pairMargin(p) {
  const m = p?.margin;
  if (typeof m === "number") return { n_writers: null, instigation_score: m };
  return { n_writers: m?.n_writers ?? null, instigation_score: m?.instigation_score ?? null };
}

export function filterPairs(pairs, f = {}) {
  return (pairs || []).filter((p) => {
    if (f.ctx && ctxKey(p.context) !== f.ctx) return false;
    if (f.split && p.split !== f.split) return false;
    if (f.seed && p.chosen_seed_id !== f.seed && p.rejected_seed_id !== f.seed) return false;
    return true;
  });
}

export function filterResponses(resps, f = {}) {
  return (resps || []).filter((r) => {
    if (f.trig && (r.trigger_code ?? "NONE") !== f.trig) return false;
    if (f.ctx && ctxKey(r.context) !== f.ctx) return false;
    if (f.kind && r.kind !== f.kind) return false;
    if (f.seed && r.seed_id !== f.seed) return false;
    return true;
  });
}

// ---------------------------------------------------------------- URL hash state

/** "#view=seeds&split=train" -> {view:"seeds", split:"train"} (view defaults to overview). */
export function parseHash(hash) {
  const p = new URLSearchParams(String(hash ?? "").replace(/^#/, ""));
  const st = {};
  for (const [k, v] of p) if (v !== "") st[k] = v;
  if (!st.view) st.view = "overview";
  return st;
}

/** Inverse of parseHash; keys sorted (view first) so equal states give equal hashes. */
export function serializeHash(st) {
  const keys = Object.keys(st).filter((k) => st[k] !== undefined && st[k] !== null && st[k] !== "");
  keys.sort((a, b) => (a === "view" ? -1 : b === "view" ? 1 : a.localeCompare(b)));
  const p = new URLSearchParams();
  for (const k of keys) p.set(k, String(st[k]));
  return "#" + p.toString();
}

export function pick(obj, keys) {
  const o = {};
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== "") o[k] = obj[k];
  return o;
}

// ---------------------------------------------------------------- export

/**
 * Rows of `file` restricted to the given seed ids (a Set). pairs keep a row only when BOTH its seeds
 * are selected so an exported subset never references seeds outside it. When the facet state f has a
 * context (f.ctx), evaluations / responses / pairs / sft rows are also restricted to that exact context,
 * so the exported files match the filter recorded in the header row.
 */
export function selectRows(data, file, seedIds, f = {}) {
  const rows = data[file] || [];
  const inCtx = (r) => !f.ctx || ctxKey(r.context) === f.ctx;
  if (file === "seeds") return rows.filter((r) => seedIds.has(r.seed_id));
  if (file === "pairs") {
    return rows.filter((p) => seedIds.has(p.chosen_seed_id) && seedIds.has(p.rejected_seed_id) && inCtx(p));
  }
  return rows.filter((r) => seedIds.has(r.seed_id) && inCtx(r));
}

/** JSONL text whose first row is a header recording the filter that produced the subset. */
export function exportJsonl(rows, header) {
  const head = { _header: { ...header, rows: rows.length } };
  return [head, ...rows].map((r) => JSON.stringify(r)).join("\n") + "\n";
}

export function exportFilename(file, version, filter) {
  const tag = Object.keys(filter || {}).length ? "subset" : "all";
  const v = String(version || "unversioned").replace(/[^A-Za-z0-9._-]/g, "_");
  return `swarmpit-instigation_${v}_${file}_${tag}.jsonl`;
}

// ---------------------------------------------------------------- board model

/** Normalize a seed's content into {board_name, threads:[{slug,title,posts:[{author,body,ago_min}]}]}. */
export function boardModel(content) {
  const c = content && typeof content === "object" ? content : {};
  const threads = Array.isArray(c.threads) ? c.threads : [];
  return {
    board_name: String(c.board_name ?? ""),
    threads: threads.map((t) => ({
      slug: String(t?.slug ?? ""),
      title: String(t?.title ?? ""),
      posts: (Array.isArray(t?.posts) ? t.posts : []).map((p) => ({
        author: String(p?.author ?? ""),
        body: String(p?.body ?? ""),
        ago_min: typeof p?.ago_min === "number" ? p.ago_min : null,
      })),
    })),
  };
}

/** Accept only same-origin relative data paths (no scheme, no protocol-relative, no backslashes). */
export function safeDataBase(raw, fallback = "data/") {
  const s = String(raw ?? "").trim();
  if (!s) return fallback;
  if (/^[a-z][a-z0-9+.-]*:/i.test(s) || s.startsWith("//") || s.includes("\\")) return fallback;
  if (!/^[A-Za-z0-9._/-]+$/.test(s)) return fallback;
  return s.endsWith("/") ? s : s + "/";
}
