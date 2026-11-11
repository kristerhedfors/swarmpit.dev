// swarmpit-instigation dataset viewer (zero dependencies, no build step).
//
// SAFETY: dataset text is adversarial. Every content string reaches the page through text nodes
// (h()/textContent) only -- this file never assigns markup, never builds URLs from content, and never
// evaluates anything it loads. Hrefs are built from serializeHash() of view state.

import {
  FILES, SEED_SORTS, SEED_FACETS, parseJsonl, get, num, fmtPct, fmtNum, truncate, ctxKey, ctxLabel,
  seedCi, isEvaluated, isInstigated, isPileIn, hasLabel, coverageCtx, seedCluster, markSingletonClusters, perAgentCodes, sortSeeds, filterSeeds, facetValues, buildIndex, ancestry,
  countBy, labelBalance, coverageMatrix, pairMargin, filterPairs, filterResponses, parseHash,
  serializeHash, pick, LENGTH_BANDS, selectRows, exportJsonl, exportFilename, boardModel, safeDataBase,
} from "./lib.js";

const PAGE = 50;
const DATA_BASE = safeDataBase(new URLSearchParams(location.search).get("data"));

const S = { data: null, index: null, manifest: null, bad: {}, missing: [], integrity: {}, facets: null };

// ---------------------------------------------------------------- DOM helpers (text only)

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = String(v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (/^(href|id|for|name|type|value|role|tabindex|scope|colspan|title|lang|dir|download|selected|checked|disabled|open|aria-[a-z]+|data-[a-z-]+)$/.test(k)) {
        if (k === "href" && !String(v).startsWith("#") && !String(v).startsWith("blob:") && !String(v).startsWith(DATA_BASE)) continue;
        el.setAttribute(k, v === true ? "" : String(v));
      }
    }
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

const state = () => parseHash(location.hash);
const facetState = (st = state()) => pick(st, SEED_FACETS);
const href = (st) => serializeHash(st);
/** Link to another view carrying the current seed facets along. */
const viewLink = (view, extra = {}, st = state()) => href({ ...facetState(st), view, ...extra });
const seedHref = (id, st = state()) => viewLink("seed", { id, sort: st.sort, dir: st.dir }, st);
const shortId = (id) => String(id ?? "").slice(0, 16);

function setHash(st, replace = false) {
  const hs = href(st);
  if (hs === location.hash) return render();
  if (replace) { history.replaceState(null, "", hs); render(); } else location.hash = hs;
}

function patch(changes, { resetPage = true } = {}) {
  const st = state();
  const next = { ...st, ...changes };
  if (resetPage) delete next.page;
  for (const k of Object.keys(next)) if (next[k] === "" || next[k] === undefined) delete next[k];
  setHash(next, true);
}

function announce(msg) {
  const live = document.getElementById("live");
  live.textContent = "";
  setTimeout(() => { live.textContent = msg; }, 30);
}

function select(id, label, value, options, onchange, allLabel = "any") {
  return h("label", { class: "facet", for: id },
    h("span", { class: "facet-label" }, label),
    h("select", { id, onchange: (e) => onchange(e.target.value) },
      allLabel === null ? null : h("option", { value: "" }, allLabel),
      options.map((o) => {
        const [v, t] = Array.isArray(o) ? o : [o, o];
        return h("option", { value: String(v), selected: String(v) === String(value ?? "") }, t);
      })));
}

function pager(total, page, onPage, idPrefix) {
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const p = Math.min(Math.max(1, page), pages);
  return h("div", { class: "pager", role: "navigation", "aria-label": "Pagination" },
    h("button", { type: "button", id: `${idPrefix}-prev`, disabled: p <= 1 ? true : null,
      onclick: () => onPage(p - 1) }, "← prev"),
    h("span", null, ` page ${p} / ${pages} `),
    h("button", { type: "button", id: `${idPrefix}-next`, disabled: p >= pages ? true : null,
      onclick: () => onPage(p + 1) }, "next →"));
}

function rateBar(rate) {
  const bar = h("span", { class: "rate", "aria-hidden": "true" }, h("span", { class: "rate-fill" }));
  bar.firstChild.style.width = `${Math.round(Math.max(0, Math.min(1, num(rate))) * 100)}%`;
  return bar;
}

function table(headers, rows, caption, wide = false) {
  return h("div", { class: wide ? "tablewrap wide" : "tablewrap" }, h("table", null,
    caption ? h("caption", null, caption) : null,
    h("thead", null, h("tr", null, headers.map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", null, rows)));
}

function section(title, ...kids) {
  return h("section", { class: "card" }, h("h3", null, title), kids);
}

function badge(text, cls = "") { return h("span", { class: `badge ${cls}` }, text); }

// ---------------------------------------------------------------- loading

async function fetchText(name) {
  const res = await fetch(DATA_BASE + name, { cache: "no-store", credentials: "same-origin" });
  if (!res.ok) throw new Error(`E_HTTP_${res.status}`);
  return res.text();
}

async function sha256Hex(text) {
  if (!globalThis.crypto?.subtle) return null;
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function load() {
  const data = {};
  try { S.manifest = JSON.parse(await fetchText("manifest.json")); } catch { S.manifest = null; S.missing.push("manifest.json"); }
  await Promise.all(FILES.map(async (f) => {
    const name = `${f}.jsonl`;
    try {
      const text = await fetchText(name);
      const { rows, bad } = parseJsonl(text);
      data[f] = rows;
      S.bad[f] = bad;
      const want = S.manifest?.sha256?.[name];
      if (want) {
        const got = await sha256Hex(text);
        S.integrity[name] = got === null ? "unchecked" : got === want ? "ok" : "mismatch";
      } else S.integrity[name] = "no manifest hash";
    } catch {
      data[f] = [];
      S.missing.push(name);
    }
  }));
  S.data = data;
  markSingletonClusters(data.seeds);
  S.index = buildIndex(data);
  S.facets = facetValues(data.seeds);
  const v = S.manifest?.version ?? "unversioned";
  document.getElementById("pkg-meta").textContent =
    `${S.manifest?.schema ?? "instigation.dataset.v1"} · version ${v} · ${data.seeds.length} seeds`;
}

// ---------------------------------------------------------------- views

function viewOverview() {
  const d = S.data;
  const m = S.manifest || {};
  const lb = labelBalance(d);
  const out = [h("h2", { tabindex: "-1" }, "Overview")];

  if (m.placeholder_fixture) out.push(h("p", { class: "note" }, "This is the placeholder fixture package (lorem text)."));
  if (S.missing.length) out.push(h("p", { class: "warn" }, `Missing files: ${S.missing.join(", ")}`));
  const badTotal = Object.values(S.bad).reduce((a, b) => a + b, 0);
  if (badTotal) out.push(h("p", { class: "warn" }, `Skipped ${badTotal} malformed JSONL line(s).`));

  out.push(h("div", { class: "stats" }, FILES.map((f) =>
    h("div", { class: "stat" }, h("div", { class: "stat-n" }, String(d[f].length)), h("div", { class: "stat-l" }, f)))));

  // package
  out.push(section("Package",
    h("dl", { class: "kv" },
      ["name", "schema", "version", "created_ts", "builder_git_sha"].map((k) =>
        [h("dt", null, k), h("dd", null, m[k] === undefined ? "–" : String(m[k]))]),
      ["n_run_dirs", "n_episodes", "runs_with_content_sink", "runs_without_content_sink"].map((k) =>
        [h("dt", null, k.replaceAll("_", " ")), h("dd", null, m.source?.[k] === undefined ? "–" : String(m.source[k]))]),
      h("dt", null, "data path"), h("dd", null, DATA_BASE)),
    table(["file", "rows", "malformed", "sha256 vs manifest"], FILES.map((f) =>
      h("tr", null, h("td", null, `${f}.jsonl`), h("td", { class: "n" }, String(d[f].length)),
        h("td", { class: "n" }, String(S.bad[f] ?? 0)),
        h("td", null, badge(S.integrity[`${f}.jsonl`] ?? "missing",
          S.integrity[`${f}.jsonl`] === "ok" ? "ok" : S.integrity[`${f}.jsonl`] === "mismatch" ? "bad" : ""))))),
    h("p", null, h("a", { href: DATA_BASE + "DATASHEET.md" }, "DATASHEET.md"), " · ",
      h("a", { href: DATA_BASE + "schema.json" }, "schema.json"), " · ",
      h("a", { href: DATA_BASE + "manifest.json" }, "manifest.json"))));

  // splits
  const splitRows = ["train", "val", "test"].map((sp) => h("tr", null, h("th", { scope: "row" },
    h("a", { href: viewLink("seeds", { split: sp }, {}) }, sp)),
    ["seeds", "evaluations", "responses", "pairs", "sft"].map((f) =>
      h("td", { class: "n" }, String(f === "responses"
        ? d.responses.filter((r) => S.index.seedById.get(r.seed_id)?.split === sp).length
        : d[f].filter((r) => r.split === sp).length))),
    h("td", { class: "n" }, String(new Set(d.seeds.filter((s) => s.split === sp).map((s) => get(s, "provenance.root_id"))).size))));
  out.push(section("Splits (by lineage root, 80/10/10)",
    table(["split", "seeds", "evaluations", "responses", "pairs", "sft", "lineage roots"], splitRows)));

  // labels: per split; seeds with no evaluation are unlabelled (never counted as negatives)
  const share = (k, n) => `${k} (${fmtPct(k / (n || 1))})`;
  const seedRow = (name, b) => h("tr", null, h("th", { scope: "row" }, name), h("td", { class: "n" }, String(b.total)),
    h("td", { class: "n" }, String(b.evaluated)), h("td", { class: "n" }, String(b.unevaluated)),
    h("td", { class: "n" }, share(b.instigated, b.evaluated)), h("td", { class: "n" }, share(b.pile_in, b.evaluated)),
    h("td", { class: "n" }, share(b.not_instigated, b.evaluated)));
  const evalRow = (name, b) => h("tr", null, h("th", { scope: "row" }, name), h("td", { class: "n" }, String(b.total)),
    h("td", { class: "n" }, String(b.total)), h("td", { class: "n" }, "0"),
    h("td", { class: "n" }, share(b.instigated, b.total)), h("td", { class: "n" }, share(b.pile_in, b.total)),
    h("td", { class: "n" }, share(b.not_instigated, b.total)));
  out.push(section("Label balance (per split; shares are of evaluated units)",
    table(["unit", "total", "evaluated", "unevaluated (no label)", "instigated (≥1 writer)", "pile-in (≥2 writers)",
      "not instigated"], [
      seedRow("seeds · all (pooled over contexts)", lb.seeds),
      ...["train", "val", "test"].map((sp) => seedRow(`seeds · ${sp}`, lb.bySplit[sp].seeds)),
      evalRow("episodes · all", lb.evaluations),
      ...["train", "val", "test"].map((sp) => evalRow(`episodes · ${sp}`, lb.bySplit[sp].evaluations)),
    ]),
    h("p", { class: "hint" }, "A seed label pooled over contexts is coarse; select a context in the seed filters to judge labels within that context only.")));

  // coverage matrix
  const cm = coverageMatrix(d.evaluations);
  const cmRows = cm.rows.map((rk) => {
    const [goal, model, na, bu] = rk.split("|");
    const setup = `${na === "-" ? "?" : na} agents · ${bu === "-" ? "?" : bu} steps`;
    return h("tr", null, h("th", { scope: "row" }, `${goal} · ${model} · ${setup}`), cm.cols.map((ck) => {
      const c = cm.cells[`${rk}||${ck}`];
      if (!c) return h("td", { class: "cell empty" }, h("span", { class: "sr-only" }, "no episodes"), "·");
      const td = h("td", { class: "cell" },
        h("a", { href: viewLink("seeds", { ctx: coverageCtx(rk, ck) }, {}),
          "aria-label": `${goal} ${model} ${setup} ${ck}: writer rate ${fmtPct(c.rate)} over ${c.n} episodes, ${c.seeds} seeds` },
          h("span", { class: "cell-rate" }, fmtPct(c.rate)), h("span", { class: "cell-n" }, `n=${c.n}`)));
      td.style.setProperty("--heat", String(c.rate));
      return td;
    }));
  });
  out.push(section("Context coverage — writer rate (episodes with ≥1 writer) per goal · model · setup × channel",
    table(["goal · model · agents · budget", ...cm.cols], cmRows, null, true),
    h("p", { class: "hint" }, "Click a cell to list the seeds evaluated in that context. Empty cells are untested contexts.")));

  // triggers + lineage
  const trig = {};
  for (const s of d.seeds) for (const [k, v] of Object.entries(s.triggers || {})) trig[k] = (trig[k] || 0) + num(v);
  const trigTotal = Object.values(trig).reduce((a, b) => a + b, 0) || 1;
  out.push(section("Trigger codes over writer agents (model-classified)",
    table(["code", "writer agents", "share", ""], Object.entries(trig).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
      h("tr", null, h("th", { scope: "row" }, h("a", { href: viewLink("seeds", { trig: k }, {}) }, k)),
        h("td", { class: "n" }, String(v)), h("td", { class: "n" }, fmtPct(v / trigTotal, 1)), h("td", null, rateBar(v / trigTotal)))))));

  const byOrigin = countBy(d.seeds, (s) => get(s, "provenance.origin", "?"));
  const byGen = countBy(d.seeds, (s) => get(s, "provenance.generation", "?"));
  const byBandRaw = countBy(d.seeds, (s) => get(s, "stats.length_band", "?"));
  const byBand = Object.fromEntries(Object.entries(byBandRaw).sort((a, b) =>
    (LENGTH_BANDS.indexOf(a[0]) + 1 || 99) - (LENGTH_BANDS.indexOf(b[0]) + 1 || 99)));
  const clusters = S.index.clusters;
  out.push(section("Composition",
    h("div", { class: "cols" },
      table(["origin", "seeds"], Object.entries(byOrigin).map(([k, v]) => h("tr", null,
        h("th", { scope: "row" }, h("a", { href: viewLink("seeds", { origin: k }, {}) }, k)), h("td", { class: "n" }, String(v))))),
      table(["generation", "seeds"], Object.entries(byGen).map(([k, v]) => h("tr", null,
        h("th", { scope: "row" }, h("a", { href: viewLink("seeds", { gen: k }, {}) }, k)), h("td", { class: "n" }, String(v))))),
      table(["length band", "seeds"], Object.entries(byBand).map(([k, v]) => h("tr", null,
        h("th", { scope: "row" }, h("a", { href: viewLink("seeds", { band: k }, {}) }, k)), h("td", { class: "n" }, String(v))))),
      table(["dedup", "count"], [
        h("tr", null, h("th", { scope: "row" }, "distinct exact hashes"), h("td", { class: "n" },
          String(new Set(d.seeds.map((s) => get(s, "dedup.exact_hash"))).size))),
        h("tr", null, h("th", { scope: "row" }, h("a", { href: viewLink("seeds", { cluster: "*" }, {}) }, "near-dup clusters")),
          h("td", { class: "n" }, String(clusters.size))),
        h("tr", null, h("th", { scope: "row" }, "seeds in clusters"), h("td", { class: "n" },
          String([...clusters.values()].reduce((a, b) => a + b.length, 0)))),
      ]))));
  return out;
}

function seedFacetBar(st, { withSort = true } = {}) {
  const fv = S.facets;
  const bar = h("div", { class: "facets", role: "group", "aria-label": "Seed filters" },
    h("label", { class: "facet", for: "f-q" }, h("span", { class: "facet-label" }, "id search"),
      h("input", { id: "f-q", type: "search", value: st.q ?? "",
        onchange: (e) => patch({ q: e.target.value.trim() }) })),
    select("f-split", "split", st.split, fv.split, (v) => patch({ split: v })),
    select("f-origin", "origin", st.origin, fv.origin, (v) => patch({ origin: v })),
    select("f-gen", "generation", st.gen, fv.gen, (v) => patch({ gen: v })),
    select("f-band", "length band", st.band, fv.band, (v) => patch({ band: v })),
    select("f-trig", "trigger code", st.trig, fv.trig, (v) => patch({ trig: v })),
    select("f-ctx", "context", st.ctx, fv.ctx.map((k) => [k, ctxLabel(k)]), (v) => patch({ ctx: v })),
    select("f-label", st.ctx ? "label (in context)" : "label", st.label, [["instigated", "instigated"],
      ["not_instigated", "not instigated (evaluated)"], ["pile_in", "pile-in"], ["unevaluated", "unevaluated"]],
    (v) => patch({ label: v })),
    select("f-cluster", "near-dup cluster", st.cluster, [["*", "in any cluster"], ["none", "singletons"],
      ...fv.cluster.map((c) => [c, `${c} (${S.index.clusters.get(c)?.length ?? 0})`])], (v) => patch({ cluster: v })),
    withSort ? select("f-sort", "sort by", st.sort ?? "writer_rate", Object.entries(SEED_SORTS), (v) => patch({ sort: v }), null) : null,
    withSort ? select("f-dir", "order", st.dir ?? "desc", [["desc", "high → low"], ["asc", "low → high"]], (v) => patch({ dir: v }), null) : null,
    Object.keys(facetState(st)).length
      ? h("button", { type: "button", id: "f-clear", class: "linkish",
        onclick: () => patch(Object.fromEntries(SEED_FACETS.map((k) => [k, ""]))) }, "clear filters") : null);
  return bar;
}

function currentSeedList(st = state()) {
  return sortSeeds(filterSeeds(S.data.seeds, facetState(st)), st.sort, st.dir);
}

function topTrigger(s) {
  const e = Object.entries(s.triggers || {}).filter(([, v]) => num(v) > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return e.length ? `${e[0][0]} ×${e[0][1]}` : "–";
}

function viewSeeds() {
  const st = state();
  const list = currentSeedList(st);
  const page = Math.max(1, parseInt(st.page || "1", 10) || 1);
  const pages = Math.max(1, Math.ceil(list.length / PAGE));
  const p = Math.min(page, pages);
  const slice = list.slice((p - 1) * PAGE, p * PAGE);
  announce(`${list.length} seeds match`);
  const rows = slice.map((s) => {
    const ci = seedCi(s);
    return h("tr", { class: hasLabel(s, "instigated", st.ctx || "") ? "pos" : "" },
      h("td", { class: "mono" }, h("a", { href: seedHref(s.seed_id, st) }, shortId(s.seed_id))),
      h("td", null, s.split ?? "–"),
      h("td", null, get(s, "provenance.origin", "–")),
      h("td", { class: "n" }, String(get(s, "provenance.generation", "–"))),
      h("td", null, get(s, "stats.length_band", "–")),
      h("td", { class: "n" }, String(get(s, "outcomes.n_evals", 0))),
      h("td", { class: "n" }, rateBar(get(s, "outcomes.writer_rate")), " ", fmtPct(get(s, "outcomes.writer_rate"))),
      h("td", { class: "n" }, `${fmtPct(ci[0])}–${fmtPct(ci[1])}`),
      h("td", { class: "n" }, fmtPct(get(s, "outcomes.pile_in_rate"))),
      h("td", { class: "n" }, fmtNum(get(s, "outcomes.best_score"))),
      h("td", { class: "mono small" }, topTrigger(s)),
      h("td", { class: "mono small" }, seedCluster(s) ?? "–"),
      h("td", { class: "preview" }, truncate(String(s.text ?? "").replace(/\s+/g, " "), 90)));
  });
  return [
    h("h2", { tabindex: "-1" }, "Seeds"),
    seedFacetBar(st),
    h("p", { class: "count", id: "seed-count" }, `${list.length} of ${S.data.seeds.length} seeds match. `,
      h("a", { href: viewLink("export", {}, st) }, "Export this selection →")),
    table(["seed", "split", "origin", "gen", "band", "evals", "writer rate", "CI95", "pile-in", "best", "top trigger", "cluster", "preview (text)"], rows, null, true),
    pager(list.length, p, (n) => patch({ page: String(n) }, { resetPage: false }), "seeds"),
  ];
}

function renderBoard(content, { compact = false } = {}) {
  const b = boardModel(content);
  return h("div", { class: `board${compact ? " compact" : ""}` },
    h("div", { class: "board-name" }, b.board_name || "(unnamed board)"),
    b.threads.map((t) => h("article", { class: "thread" },
      h("header", { class: "thread-head" }, h("span", { class: "thread-title" }, t.title || "(untitled)"),
        h("span", { class: "thread-slug mono" }, t.slug)),
      t.posts.map((p) => h("div", { class: "post" },
        h("div", { class: "post-meta" }, h("span", { class: "author" }, p.author || "(anon)"),
          p.ago_min === null ? null : h("span", { class: "ago" }, ` · ${p.ago_min} min ago`)),
        h("div", { class: "post-body" }, p.body))))));
}

function viewSeed() {
  const st = state();
  const s = S.index.seedById.get(st.id);
  if (!s) return [h("h2", { tabindex: "-1" }, "Seed not found"), h("p", null, h("a", { href: viewLink("seeds", {}, st) }, "← back to seeds"))];
  const list = currentSeedList(st);
  const pos = list.findIndex((x) => x.seed_id === s.seed_id);
  const prev = pos > 0 ? list[pos - 1] : null;
  const next = pos >= 0 && pos < list.length - 1 ? list[pos + 1] : null;
  const ci = seedCi(s);
  const pv = s.provenance || {};
  const chain = ancestry(S.index, s.seed_id);
  const kids = S.index.children.get(s.seed_id) || [];
  const cluster = seedCluster(s);
  const sibs = cluster ? (S.index.clusters.get(cluster) || []).filter((x) => x !== s.seed_id) : [];
  const evals = [...(S.index.evalsBySeed.get(s.seed_id) || [])].sort((a, b) => ctxKey(a.context).localeCompare(ctxKey(b.context))
    || String(a.eval_id).localeCompare(String(b.eval_id)));
  const pairs = S.index.pairsBySeed.get(s.seed_id) || [];
  const idLink = (id) => h("a", { class: "mono", href: seedHref(id, st) }, shortId(id));

  const byCtx = Object.entries(get(s, "outcomes.by_context") || {}).sort();
  return [
    h("nav", { class: "crumbs", "aria-label": "Seed navigation" },
      h("a", { href: viewLink("seeds", { sort: st.sort, dir: st.dir }, st), id: "back" }, "← seeds"),
      " ", prev ? h("a", { href: seedHref(prev.seed_id, st), id: "prev-seed" }, "‹ prev") : h("span", { class: "dim" }, "‹ prev"),
      " ", pos >= 0 ? h("span", null, `${pos + 1} / ${list.length}`) : h("span", { class: "dim" }, "not in current filter"),
      " ", next ? h("a", { href: seedHref(next.seed_id, st), id: "next-seed" }, "next ›") : h("span", { class: "dim" }, "next ›")),
    h("h2", { tabindex: "-1" }, "Seed ", h("span", { class: "mono" }, s.seed_id)),
    h("div", { class: "badges" }, badge(s.split ?? "?", `split-${s.split}`), badge(pv.origin ?? "?"),
      badge(`gen ${pv.generation ?? "?"}`), badge(get(s, "stats.length_band", "?")),
      !isEvaluated(s) ? badge("unevaluated") : isInstigated(s) ? badge("instigated", "ok") : badge("not instigated"),
      isPileIn(s) ? badge("pile-in", "ok") : null,
      cluster ? badge(`cluster ${cluster}`) : null),
    h("div", { class: "cols" },
      section("Outcomes",
        h("dl", { class: "kv" },
          h("dt", null, "evaluations"), h("dd", null, String(get(s, "outcomes.n_evals", 0))),
          h("dt", null, "with writers"), h("dd", null, String(get(s, "outcomes.n_evals_with_writers", 0))),
          h("dt", null, "writer rate"), h("dd", null, `${fmtPct(get(s, "outcomes.writer_rate"))} (CI95 ${fmtPct(ci[0])}–${fmtPct(ci[1])})`),
          h("dt", null, "pile-in rate"), h("dd", null, fmtPct(get(s, "outcomes.pile_in_rate"))),
          h("dt", null, "best / mean score"), h("dd", null, `${fmtNum(get(s, "outcomes.best_score"))} / ${fmtNum(get(s, "outcomes.mean_score"))}`),
          h("dt", null, "triggers"), h("dd", { class: "mono small" },
            Object.entries(s.triggers || {}).map(([k, v]) => `${k}×${v}`).join("  ") || "–"))),
      section("Stats & provenance",
        h("dl", { class: "kv" },
          h("dt", null, "chars / threads / posts"), h("dd", null,
            `${get(s, "stats.seed_chars", "–")} / ${get(s, "stats.n_threads", "–")} / ${get(s, "stats.n_posts", "–")}`),
          h("dt", null, "origin"), h("dd", null, `${pv.origin ?? "–"}${pv.bootstrap ? " (bootstrap)" : ""}`),
          h("dt", null, "generator model"), h("dd", null, pv.gen_model ?? "–"),
          h("dt", null, "created"), h("dd", null, pv.created_ts ?? "–"),
          h("dt", null, "exact hash"), h("dd", { class: "mono small" }, truncate(get(s, "dedup.exact_hash", "–"), 24))))),
    section("Lineage",
      h("p", null, "Root → this: ", chain.map((id, i) => [i ? " → " : "", id === s.seed_id ? h("strong", { class: "mono" }, shortId(id)) : idLink(id)])),
      h("p", null, `Children (${kids.length}): `, kids.length ? kids.map((id, i) => [i ? ", " : "", idLink(id)]) : "none"),
      h("p", null, `Near-dup siblings (${sibs.length}): `, sibs.length ? sibs.map((id, i) => [i ? ", " : "", idLink(id)]) : "none")),
    section("Board (rendered as plain text)", renderBoard(s.content)),
    h("details", { class: "card" }, h("summary", null, "Flat text rendering (the ", h("code", null, "text"), " field)"),
      h("pre", { class: "flat" }, String(s.text ?? ""))),
    section("Outcomes per context (goal · model · channel · agents · budget)",
      table(["context", "episodes", "with writers", "max writers", "mean writers", "best score", "mean engagement", ""], byCtx.map(([k, b]) =>
        h("tr", null, h("th", { scope: "row" }, ctxLabel(k)), h("td", { class: "n" }, String(b.n ?? "–")),
          h("td", { class: "n" }, String(b.writers_any ?? "–")), h("td", { class: "n" }, String(b.max_writers ?? "–")),
          h("td", { class: "n" }, fmtNum(b.mean_writers)),
          h("td", { class: "n" }, fmtNum(b.best_score)), h("td", { class: "n" }, fmtNum(b.mean_engagement)),
          h("td", null, rateBar(num(b.writers_any) / (num(b.n) || 1))))))),
    section(`Episodes and elicited responses (${evals.length})`,
      evals.length ? evals.map((e) => {
        const rs = S.index.respByEval.get(e.eval_id) || [];
        const o = e.outcome || {};
        return h("details", { class: "episode", open: rs.length ? true : null },
          h("summary", null, h("span", { class: "mono" }, e.eval_id), " · ", ctxLabel(e.context),
            ` · writers ${o.n_writers ?? "–"} · score ${fmtNum(o.instigation_score)} · primary ${get(e, "triggers.primary", "–")}`,
            ` · ${rs.length} response(s)`),
          h("dl", { class: "kv small" },
            h("dt", null, "agents / budget"), h("dd", null, `${get(e, "context.n_agents", "–")} / ${get(e, "context.budget", "–")}`),
            h("dt", null, "first write"), h("dd", null, `${fmtNum(o.run_ttfw_s, 1)} s (step ${o.run_first_write_step ?? "–"})`),
            h("dt", null, "pile-in onset"), h("dd", null, `${fmtNum(o.pile_in_onset_t_s, 1)} s`),
            h("dt", null, "I / S_mean"), h("dd", null, `${fmtNum(o.I)} / ${fmtNum(o.S_mean)}`),
            h("dt", null, "panel agreement"), h("dd", null, fmtNum(get(e, "triggers.panel_agreement"))),
            h("dt", null, "per agent"), h("dd", { class: "mono small" }, perAgentCodes(get(e, "triggers.per_agent"))
              .map((a) => `${a.agent}:${a.code ?? "–"}`).join("  ") || "–")),
          rs.map(responseCard));
      }) : h("p", null, "Not evaluated yet.")),
    section(`Preference pairs involving this seed (${pairs.length})`,
      pairs.length ? h("p", null, h("a", { href: viewLink("pairs", { pseed: s.seed_id }, st) }, "Open in pairs browser →"),
        ` chosen in ${pairs.filter((p) => p.chosen_seed_id === s.seed_id).length}, rejected in ${pairs.filter((p) => p.rejected_seed_id === s.seed_id).length}`)
        : h("p", null, "none")),
  ];
}

function responseCard(r, { withSeed = false } = {}) {
  return h("div", { class: "resp" },
    h("div", { class: "resp-meta" },
      withSeed ? [h("a", { class: "mono", href: seedHref(r.seed_id) }, shortId(r.seed_id)), " · "] : null,
      h("span", { class: "mono" }, r.agent_id ?? "?"), ` · ${r.kind ?? "?"}`,
      r.thread_ref ? [" · in ", h("span", { class: "mono" }, r.thread_ref)] : null,
      ` · t+${fmtNum(r.t_rel_s, 1)} s · `, badge(r.trigger_code ?? "NONE"),
      withSeed ? ` · ${ctxLabel(r.context)}` : null),
    h("div", { class: "resp-body" }, String(r.body ?? "")));
}

function viewPairs() {
  const st = state();
  const ctxs = [...new Set(S.data.pairs.map((p) => ctxKey(p.context)))].sort();
  const list = filterPairs(S.data.pairs, { ctx: st.pctx, split: st.psplit, seed: st.pseed })
    .sort((a, b) => num(pairMargin(b).n_writers) - num(pairMargin(a).n_writers)
      || num(pairMargin(b).instigation_score) - num(pairMargin(a).instigation_score)
      || String(a.pair_id).localeCompare(String(b.pair_id)));
  const page = Math.max(1, parseInt(st.page || "1", 10) || 1);
  const pages = Math.max(1, Math.ceil(list.length / PAGE));
  const p = Math.min(page, pages);
  const sel = st.pair ? S.data.pairs.find((x) => x.pair_id === st.pair) : null;
  announce(`${list.length} pairs match`);
  const out = [
    h("h2", { tabindex: "-1" }, "Preference pairs"),
    h("p", { class: "hint" }, "Chosen and rejected seeds share a full context (incl. agent count and budget), a split and their episode count; chosen has a higher mean writer count by at least the manifest margin, or on an exact writer tie a higher mean instigation score by at least that margin."),
    h("div", { class: "facets", role: "group", "aria-label": "Pair filters" },
      select("p-ctx", "context", st.pctx, ctxs.map((k) => [k, ctxLabel(k)]), (v) => patch({ pctx: v, pair: "" })),
      select("p-split", "split", st.psplit, ["train", "val", "test"], (v) => patch({ psplit: v, pair: "" })),
      st.pseed ? h("span", { class: "facet" }, h("span", { class: "facet-label" }, "seed"),
        h("span", { class: "mono" }, shortId(st.pseed), " "),
        h("button", { type: "button", class: "linkish", onclick: () => patch({ pseed: "" }) }, "×")) : null),
    h("p", { class: "count" }, `${list.length} of ${S.data.pairs.length} pairs`),
  ];
  if (sel) {
    const m = pairMargin(sel);
    const side = (label, id, o) => {
      const s = S.index.seedById.get(id);
      return h("div", { class: `side ${label}` },
        h("h4", null, label, " · ", h("a", { class: "mono", href: seedHref(id, st) }, shortId(id))),
        h("p", { class: "small" }, `in this context: ${o?.n ?? "–"} episode(s) · mean writers ${fmtNum(o?.mean_writers)} · max writers ${o?.max_writers ?? o?.n_writers ?? "–"} · mean score ${fmtNum(o?.mean_score)} · best score ${fmtNum(o?.best_score ?? o?.instigation_score)}`,
          s ? ` · overall writer rate ${fmtPct(get(s, "outcomes.writer_rate"))} over ${get(s, "outcomes.n_evals", 0)} evaluations` : " · seed missing"),
        s ? renderBoard(s.content, { compact: true }) : null);
    };
    out.push(section(`Pair ${sel.pair_id} · ${ctxLabel(sel.context)} · margin writers ${m.n_writers ?? "–"}, score ${fmtNum(m.instigation_score)}`,
      h("p", null, h("a", { href: href({ ...st, pair: undefined }), id: "close-pair" }, "close")),
      h("div", { class: "pair" }, side("chosen", sel.chosen_seed_id, sel.chosen), side("rejected", sel.rejected_seed_id, sel.rejected))));
  }
  out.push(table(["pair", "context", "split", "chosen", "rejected", "Δ writers", "Δ score"],
    list.slice((p - 1) * PAGE, p * PAGE).map((x) => {
      const m = pairMargin(x);
      return h("tr", { class: x.pair_id === st.pair ? "sel" : "" },
        h("td", { class: "mono" }, h("a", { href: href({ ...st, pair: x.pair_id }) }, shortId(x.pair_id))),
        h("td", null, ctxLabel(x.context)), h("td", null, x.split ?? "–"),
        h("td", { class: "mono" }, h("a", { href: seedHref(x.chosen_seed_id, st) }, shortId(x.chosen_seed_id))),
        h("td", { class: "mono" }, h("a", { href: seedHref(x.rejected_seed_id, st) }, shortId(x.rejected_seed_id))),
        h("td", { class: "n" }, fmtNum(m.n_writers)), h("td", { class: "n" }, fmtNum(m.instigation_score)));
    })));
  out.push(pager(list.length, p, (n) => patch({ page: String(n) }, { resetPage: false }), "pairs"));
  return out;
}

function viewResponses() {
  const st = state();
  const R = S.data.responses;
  const trigs = [...new Set(R.map((r) => r.trigger_code ?? "NONE"))].sort();
  const ctxs = [...new Set(R.map((r) => ctxKey(r.context)))].sort();
  const kinds = [...new Set(R.map((r) => r.kind).filter(Boolean))].sort();
  const list = filterResponses(R, { trig: st.rtrig, ctx: st.rctx, kind: st.rkind, seed: st.rseed });
  const page = Math.max(1, parseInt(st.page || "1", 10) || 1);
  const pages = Math.max(1, Math.ceil(list.length / PAGE));
  const p = Math.min(page, pages);
  announce(`${list.length} responses match`);
  return [
    h("h2", { tabindex: "-1" }, "Elicited responses"),
    h("p", { class: "hint" }, "Posts written by agents during scored episodes — the communication each seed instigated."),
    h("div", { class: "facets", role: "group", "aria-label": "Response filters" },
      select("r-trig", "trigger code", st.rtrig, trigs, (v) => patch({ rtrig: v })),
      select("r-ctx", "context", st.rctx, ctxs.map((k) => [k, k.replaceAll("|", " · ")]), (v) => patch({ rctx: v })),
      select("r-kind", "kind", st.rkind, kinds, (v) => patch({ rkind: v })),
      st.rseed ? h("span", { class: "facet" }, h("span", { class: "facet-label" }, "seed"), h("span", { class: "mono" }, shortId(st.rseed), " "),
        h("button", { type: "button", class: "linkish", onclick: () => patch({ rseed: "" }) }, "×")) : null),
    h("p", { class: "count" }, `${list.length} of ${R.length} responses`),
    h("div", { class: "resps" }, list.slice((p - 1) * PAGE, p * PAGE).map((r) => responseCard(r, { withSeed: true }))),
    pager(list.length, p, (n) => patch({ page: String(n) }, { resetPage: false }), "resp"),
  ];
}

function viewExport() {
  const st = state();
  const filter = facetState(st);
  const seeds = currentSeedList(st);
  const ids = new Set(seeds.map((s) => s.seed_id));
  const version = S.manifest?.version ?? null;
  const rowsFor = (f) => (f === "seeds" ? seeds : selectRows(S.data, f, ids, filter));
  const download = (f) => {
    const rows = rowsFor(f);
    const text = exportJsonl(rows, {
      dataset: S.manifest?.name ?? "swarmpit-instigation", schema: S.manifest?.schema ?? "instigation.dataset.v1",
      version, file: `${f}.jsonl`, filter, sort: f === "seeds" ? { key: st.sort ?? "writer_rate", dir: st.dir ?? "desc" } : null,
      selected_seeds: ids.size, exported_ts: new Date().toISOString(),
      note: "Adversarial content - defensive research only; do not publish.",
    });
    const url = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }));
    const a = h("a", { href: url, download: exportFilename(f, version, filter) });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    announce(`Downloaded ${rows.length} ${f} rows`);
  };
  const filterDesc = Object.entries(filter).map(([k, v]) => `${k}=${v}`).join(", ") || "none (whole package)";
  return [
    h("h2", { tabindex: "-1" }, "Export a subset"),
    h("p", null, "The subset is the current seed filter; every other file is restricted to the selected seeds ",
      "(pairs only when both seeds are selected). When a context is selected, evaluations, responses, pairs and ",
      "sft rows are also restricted to exactly that context. Each download starts with a ", h("code", null, "_header"),
      " row recording the dataset version and the filter."),
    seedFacetBar(st, { withSort: true }),
    h("p", { class: "count", id: "export-filter" }, `Filter: ${filterDesc} · ${ids.size} seed(s) selected`),
    table(["file", "rows in subset", "rows in package", ""], FILES.map((f) =>
      h("tr", null, h("th", { scope: "row" }, `${f}.jsonl`), h("td", { class: "n" }, String(rowsFor(f).length)),
        h("td", { class: "n" }, String(S.data[f].length)),
        h("td", null, h("button", { type: "button", id: `dl-${f}`, onclick: () => download(f) }, `Download ${f}.jsonl`))))),
    h("p", { class: "warn" }, "Exports carry adversarial text. Keep them on research machines; never feed them to a coding agent."),
  ];
}

// ---------------------------------------------------------------- router

const VIEWS = { overview: viewOverview, seeds: viewSeeds, seed: viewSeed, pairs: viewPairs, responses: viewResponses, export: viewExport };
let lastView = null;

function render() {
  if (!S.data) return;
  const st = state();
  const view = VIEWS[st.view] ? st.view : "overview";
  const main = document.getElementById("main");
  const focusedId = document.activeElement?.id || null;
  main.replaceChildren();
  append(main, [VIEWS[view]()]);
  for (const a of document.querySelectorAll("#tabs a")) {
    const on = a.dataset.view === view || (view === "seed" && a.dataset.view === "seeds");
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    a.setAttribute("href", viewLink(a.dataset.view, {}, st));
  }
  const viewChanged = lastView !== `${view}:${st.id ?? ""}:${st.pair ?? ""}`;
  lastView = `${view}:${st.id ?? ""}:${st.pair ?? ""}`;
  const keep = focusedId && document.getElementById(focusedId);
  if (keep && main.contains(keep)) keep.focus();
  else if (viewChanged) { main.querySelector("h2")?.focus({ preventScroll: true }); window.scrollTo(0, 0); }
  document.title = `${view} · swarmpit-instigation`;
}

function onKey(e) {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable);
  const st = state();
  if (e.key === "Escape") {
    if (typing) { t.blur(); return; }
    if (st.view === "seed") { e.preventDefault(); location.hash = viewLink("seeds", { sort: st.sort, dir: st.dir }, st); }
    else if (st.view === "pairs" && st.pair) { e.preventDefault(); patch({ pair: "" }, { resetPage: false }); }
    return;
  }
  if (typing) return;
  const tabs = ["overview", "seeds", "pairs", "responses", "export"];
  if (/^[1-5]$/.test(e.key)) { e.preventDefault(); location.hash = viewLink(tabs[+e.key - 1], {}, st); return; }
  if (e.key === "/") {
    e.preventDefault();
    if (st.view !== "seeds") { location.hash = viewLink("seeds", {}, st); setTimeout(() => document.getElementById("f-q")?.focus(), 0); }
    else document.getElementById("f-q")?.focus();
    return;
  }
  if (st.view === "seed" && (e.key === "j" || e.key === "k")) {
    const a = document.getElementById(e.key === "j" ? "next-seed" : "prev-seed");
    if (a) { e.preventDefault(); location.hash = a.getAttribute("href"); }
  }
}

async function main() {
  window.addEventListener("hashchange", render);
  document.addEventListener("keydown", onKey);
  try {
    await load();
  } catch {
    document.getElementById("main").replaceChildren(h("p", { class: "warn" }, "E_LOAD: could not load the dataset package."));
    return;
  }
  render();
}

main();
