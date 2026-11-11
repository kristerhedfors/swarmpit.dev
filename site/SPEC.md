# swarmpit.dev landing page: build SPEC (v1)

This document is binding for the build. `site/src/types.ts` holds the TypeScript types for every module boundary and every wire format, and it is the source of truth for names and shapes. If this document and `types.ts` disagree, `types.ts` wins and this document gets fixed. Data semantics follow `dash/CONTRACT.md` v1: ids, `t`, the derive rules, edge kinds and the §9 caveats.

**Lineage.** The base is proposal 2:
- the semantic task-to-board drift layout
- step trail dots
- captions, the tour and the end card
- the feed rail
- skip-quiet mode
- obs-backed findings with parity checks
- the scratch obs-DB exporter

Grafted from proposal 0:
- the redaction rules
- worldline mode
- the swarm hull, HUD and cohesion pull
- cooperative gestures
- the prehistory stub
- prefetch on hover
- the export exclusions

Grafted from proposal 1:
- the core/detail bundle split
- the gesture state machine
- position-texture Bézier edges with dash cues
- SDF node shapes
- the dirty-only render loop and context-loss rebuild
- the stress bundle, perf overlay and byte budgets
- the node listbox
- `--mask-ctf`

---

## 0. Scope and hard rules

1. **Stack.**
   - Pure HTML, CSS and TypeScript, compiled by `tsc` (the only build step) to plain browser ES modules in `public/js/`.
   - **Zero runtime dependencies.** No three.js, frameworks, bundler, CDN, web fonts or external requests at runtime.
   - The only devDependency is `typescript`.
   - Tools use Node built-ins (`tools/serve.mjs`) or Python 3 stdlib, plus duckdb through the `dash/server` uv project (`tools/export.py`).
2. **Static hosting.** `public/` is the deploy root (GitHub Pages or any static host). Every URL is relative, and no server logic is needed. `CNAME` holds `swarmpit.dev`, plus `.nojekyll`.
3. **Read-only rules.**
   - `<repo>/runs` is read-only (every open is `"rb"`).
   - Never touch msb VMs, the orchestrator, `.env`, `dash/` or `lab/`.
   - No git commit or push.
4. **Bundles are public.** Everything under `public/data/` passes the redaction pass (§4.8) and the whole-file gate `tools/check_public.py` (§4.9) before it may exist on disk.
5. **The graph state is a pure function of `(model, t, filterMask, viewOptions)`.** Scrubbing back and forth is deterministic and accumulates no state. Only camera state and UI hover are outside this function.

---

## 1. Ownership

| Builder | Owns (creates and edits) | Must not edit |
|---|---|---|
| **A: data** | `tools/export.py`, `tools/redact.py`, `tools/check_public.py`, `tools/test_redact.py`, `tools/synth.py` (stress bundle, may live inside export.py), `public/data/**`, `tools/test/data.test.mjs` | `src/**`, `public/index.html`, `public/css/**` |
| **B: engine** | `src/engine/**` (layout, frame, camera, gestures, WebGL2 and Canvas2D renderers, shaders, picking, labels, perf overlay, palette), `tools/test/engine.test.mjs` | `src/player/**`, `src/ui/**`, `src/data/**`, `tools/*.py` |
| **C: player and shell** | `src/player/**` (clock, player wiring, keys, URL state, tour), `src/ui/**` (timeline bar, selector, filters panel, tooltip, detail panel, feed, captions, end card, HUD, findings, node list, help), `src/data/**` (loader, model, filters), `src/util/**`, `src/main.ts`, `public/index.html`, `public/404.html`, `public/css/**`, `public/favicon.svg`, `public/CNAME`, `public/.nojekyll`, `public/robots.txt`, `tools/test/player.test.mjs` | `src/engine/**`, `tools/*.py` |

- **Shared and frozen:** `src/types.ts`, `package.json`, `tsconfig.json`, `.gitignore`, `tools/serve.mjs`, `SPEC.md`.
  - `types.ts` is append-only: optional fields and trailing table entries only.
  - Any builder may append, but must announce the change to the other two in the hand-off notes.
- **Cross-builder contracts:**
  - B consumes `RunModel`, `FilterMask` and `PlaybackClock` only through their `types.ts` interfaces. Until C lands `src/data/model.ts`, B uses a local test double inside `src/engine/` that is never shipped.
  - C consumes `SceneEngine` only through `createEngine` from `src/engine/engine.ts`.
  - A's bundles must validate against `types.ts` (C's `assertCore`/`assertDetail` in `src/data/loader.ts`, plus `tools/test/data.test.mjs`).

---

## 2. Exact file layout

```
site/
  SPEC.md  package.json  tsconfig.json  .gitignore  README.md (C, short runbook)
  tools/
    serve.mjs            zero-dep static dev server (scaffolded; done)
    export.py            [A] runs -> public/data (scratch obs DB; §4.1)
    redact.py            [A] redaction rules + --selftest (§4.8)
    check_public.py      [A] whole-file public gate: leak scan, size budgets, import specifiers (§4.9)
    test_redact.py       [A] unit tests for redact.py (python3 -m unittest)
    test/
      data.test.mjs      [A] node --test: every bundle validates, referential integrity, sort order
      engine.test.mjs    [B] determinism: computeFrame(t) equal via forward/backward scrub; layout stable
      player.test.mjs    [C] clock seek/skip-quiet math, filters encode/decode round trip, model queries
      runs.test.mjs      [C] model and goal facets: choose / sibling / next stay within one model and goal, chip rows
      findings.test.mjs  [C] findings slices per (model, goal), n/a free time, goal comparison
  src/
    types.ts             shared types + code tables (frozen, append-only)
    main.ts              [C] bootstrap (§13.3)
    util/   bus.ts (Emitter class)  dom.ts (h())  format.ts (m:ss.s, numbers, truncation)  env.ts (Env)
    data/   loader.ts (createLoader, assertCore, assertDetail)  model.ts (buildModel)  filters.ts (createFilterStore, compileMask, DEFAULT_FILTERS)
            runs.ts (model- and goal-scoped card queries: chooseRun, chooseGoalRun, siblingRun, nextRun, listedModels,
                     goalOf, goalChips, shortModel)  findingsData.ts (findings slices per model and goal)
    player/ clock.ts (createClock)  player.ts (createPlayer: wires everything)  keys.ts (bindKeys)  url.ts (readUrl/writeUrl)  tour.ts (caption-driven nudges)
    ui/     timeline.ts  selector.ts  filtersPanel.ts  tooltip.ts  detail.ts  feed.ts  captions.ts  endcard.ts
            hud.ts  findings.ts (SVG dot+CI)  nodeList.ts (offscreen listbox)  help.ts  icons.ts (inline SVG strings)
    engine/ engine.ts (createEngine: rAF loop owner)  math.ts (vec3/mat4)  camera.ts  gestures.ts
            layout.ts (computeLayout, agentPos, timeToY)  frame.ts (computeFrame, buildStaticGeometry)
            palette.ts (ThemeColors from CSS vars)  picking.ts  labels.ts  perf.ts (?perf=1 overlay)
            canvas2d.ts  gl/context.ts  gl/shaders.ts  gl/webgl2.ts
  public/                DEPLOY ROOT
    index.html  404.html  CNAME  .nojekyll  robots.txt  favicon.svg  og.png (A or C, static image; optional)
    css/site.css  css/player.css
    data/index.json  data/runs/<id>.<hash8>.core.json  data/runs/<id>.<hash8>.detail.json
    data/runs/_synth-500.<hash8>.core.json / .detail.json   (stress bundle; card has hidden=true)
    js/                  tsc output (gitignored)
```

`index.html` loads one module, `<script type="module" src="js/main.js"></script>`. It also has a `<noscript>` with the poster and text sections, and a `<script nomodule>` upgrade message.

---

## 3. Build and tooling

- **tsconfig.** Strict, `target ES2022`, `module NodeNext`, `moduleResolution NodeNext`, `lib [ES2022, DOM, DOM.Iterable]`, `rootDir src`, `outDir public/js`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax`, `removeComments`, `types: []`.
  - The task brief asked for `module ES2022`. We use NodeNext on purpose: together with `"type":"module"` it emits identical ESM, but it **forces explicit `.js` import specifiers**, which browsers need without a bundler. This is the documented graft decision.
- **Import rules.**
  - Every relative import ends in `.js`, e.g. `import { h } from '../util/dom.js'`.
  - No bare specifiers.
  - Type-only imports use `import type`.
  - `check_public.py` rejects any bare or extensionless specifier in `public/js/**`.
- **Scripts.**

  | script | command |
  |---|---|
  | `npm run build` | `tsc -p .` |
  | `npm run watch` | `tsc -p . -w` |
  | `npm run serve` | `node tools/serve.mjs public 8080` |
  | `npm run export` | `python3 tools/export.py` (re-execs itself under `uv run --project ../dash/server python` if `duckdb` is not importable) |
  | `npm run check` | `tsc --noEmit` plus `python3 tools/check_public.py public` |
  | `npm test` | `node --test tools/test/` (the tests import compiled DOM-free modules from `public/js/`) |

- **DOM-free modules.** `data/model.ts`, `data/filters.ts`, `player/clock.ts`, `engine/layout.ts`, `engine/frame.ts`, `engine/math.ts` and `engine/camera.ts` must not touch `window`/`document` at import time, so the Node tests can import them.

---

## 4. Data bundles

### 4.1 Export pipeline (`tools/export.py`, Builder A)

```
python3 tools/export.py [--runs <repo>/runs] [--out public/data]
        [--db .cache/obs-export.duckdb] [--no-ingest] [--include-tagged] [--mask-ctf]
        [--featured <run_id>] [--synth] [--deny <word> ...]
```

1. **Scratch obs DB.**
   - Run `OBS_LIVE_TAP=0 uv run --project ../dash/server obs ingest --rebuild --runs <runs> --db site/.cache/obs-export.duckdb`.
   - This reuses CONTRACT derive verbatim and opens `runs/` read-only. It never contacts msb, and it has no lock contention with a running `obs serve`, because it uses its own DB file.
   - `--no-ingest` reuses the existing scratch DB.
2. **Query tables.** Read `runs`, `agents`, `agent_records`, `steps`, `model_calls`, `board_events` (`target_kind`, `target`, `attribution`, `step`), `posts`, `threads`, `interactions`, `submissions`, and the views `v_timeline`, `mat_agent_metrics`, `v_run_metrics` and `v_cell_metrics`. Never read `.env`, cfg files, `relay.jsonl`, `*.out` or `pilot*.log`. Never read the `run.json` `ctf` block except to build the in-memory leak and mask matcher.
3. **Run set.** Status `done` and tag `''` are listed. Tagged and archive runs (smoke, cal1, fastcheck, lfcheck, `_invalid_irc_lf`) are exported only with `--include-tagged`, as `hidden: true`. irc runs are listed with the `irc_inbound_broken` caveat.
4. **Build bundles.** Build `CoreBundle` and `DetailBundle` per run (§4.3 to §4.6). Then:
   - redact every string (§4.8)
   - serialise compactly (`separators=(',',':')`, times as ints)
   - compute `hash = sha256(core_json_without_hash)[:8]`
   - write `<id>.<hash>.core.json` and `<id>.<hash>.detail.json`
   - delete stale files for that id
5. **Findings.** Cells are keyed by `(model, goal, iface, pop)` and pooled rows by `(model, goal, iface)` (§4.10, §4.11).
   - Read `runs/analysis.json` (`cells`, `pooled`). Its rows carry no model and belong to the primary model (`c.model ?? PRIMARY`).
   - For every other model, read `runs/analysis_models.json` `by_model[slug].cells/pooled` when it exists (`lab/analyze.py --compare`).
   - Cross-check every cell metric against `v_cell_metrics`. On any difference above 1e-6, add a line to `findings.mismatches`, set `source: 'obs'` and use the obs values. CONTRACT §9 marks `analysis.json` as stale.
   - A model with no comparison side (no `analysis_models.json`, or its slug missing there) gets one export log line and obs values. It adds no mismatch and never changes `source`; only a comparison side that exists and disagrees does.
   - Unsolvable-goal rows are checked the same way against `runs/analysis.unsolvable.json` and `runs/analysis_models.unsolvable.json` (`lab/analyze.py --goal unsolvable [--compare]`). A missing unsolvable side only leaves those rows unchecked (§4.11).
   - Write `index.json`.
6. **Parity assertions.** These are fatal unless `--force`. For each run:
   - `agents[i].metrics` equals `mat_agent_metrics`
   - the number of `post` events equals `v_run_metrics.total_posts`
   - board-sourced events plus folded write reqs equal the line count of `board_events.jsonl` (read `"rb"`)
   - `run.tEndMs` equals `round(runs.t_end*1000)`, within ±1
   - each agent's step count equals the number of `step` lines in `aN/steps.jsonl`
7. **Gate.** Run `check_public.py` on the output. If it fails, delete what was written and exit non-zero.
8. **`--synth`.** Writes `_synth-500` with 16 agents, about 500 nodes and about 5k events. It is deterministic (seeded PRNG), the card has `hidden: true` and the `synthetic` caveat, and it is reachable via `#run=_synth-500`.

### 4.2 `public/data/index.json`

The `IndexBundle` type in `types.ts`.
- `runs` is ordered by model rank (the primary model `qwen-3.8-27b` first, then lab `MODEL_ORDER`, then unknown ids alphabetically), then goal (solvable first, §4.11), then iface (forum, wiki, cache, irc), then pop (solo, multi), then rep, then tag.
- `models` is `[{id, slug, label, provider, nRuns}]` for every model with listed runs, in the same rank order. The metadata comes from `lab/config.MODELS`, loaded by path with importlib; `nRuns` counts listed cards only (no hidden or synthetic ones). `validate_index` checks that these ids equal the listed cards' models and that the findings keys are unique.
- `density` is 64 bins over `[0, tEndMs]` of events that are visible by default (no think, idle, proto, connect or disconnect).
- `generator.redactions` holds counts per rule. `generator.truncation` lists the limits.

### 4.3 Core bundle (`swarmpit.run/1`)

The `CoreBundle` type. It is loaded when a run is selected. Prefetch also starts on selector hover or focus.

- **Columnar** `events`, `fires` and `strings`.
- Strings are interned. `l` and `ev` indexes point into `strings`, and index 0 is `''`.
- **Node order:** `task, board, a0..aN-1, threads, posts (seed then live, by t), personas, handles`.
- **Node ids:**
  - `task`, `board`
  - `a<i>`
  - `th:<slug>` (threads, wiki pages, cache dirs and IRC channels; DMs use `th:dm:<nick>`)
  - post uids `s<id>`, `p<id>`, `d<line_off>` (CONTRACT §3.3 step 8)
  - `seed:<author>`, `name:<handle>`
- **Edges:** unique by `(s,d,k)`. When an edge fires lives in `fires` (sorted by `e`, then `t`). `fires.ev` links the firing to its event.
- **Events:** sorted by `(t, lane, kindRank, seq)`, the CONTRACT §3.5 order. `kindRank` is model < step < relay < board, and board-level events sit in lane −1.
- **Truncation:**

  | field | limit |
  |---|---|
  | `l` labels | 160 |
  | node `label` | 48 |
  | `preview` | 240 |
  | thread `title` | 120 |

### 4.4 Detail bundle (`swarmpit.detail/1`)

The `DetailBundle` type. It is lazy-loaded immediately after the core bundle renders, and its download progress drives the timeline's **buffered** fill.

- `items` holds the `Detail` union entries that `events.x` points to.
- `posts` holds full bodies with `leakSpans`.
- `hash` must equal the core hash; otherwise the loader throws `BundleError('hash')`.
- **Truncation:**
  - `cmd` 600
  - `out`: head 700, then `\n…[N chars]…\n`, then tail 300 (`outLen` = the full length)
  - `reason` 900
  - `content` 400
  - post `body` 1500
  - `text` 400
- **Idle steps** have no detail item (`x = -1`).

### 4.5 JSON example (abridged real shape, forum-multi4-r1)

```json
{
  "format": "swarmpit.run/1",
  "hash": "3f2a9b01",
  "run": {
    "id": "forum-multi4-r1", "iface": "forum", "pop": "multi", "nAgents": 4, "rep": 1, "tag": "",
    "model": "qwen-3.8-27b",
    "harness": { "budget": 40, "temperature": 0.7, "maxTokens": 2048, "outputChars": 3000 },
    "boardName": "agent-commons",
    "nouns": { "thread": "thread", "threads": "threads", "post": "post", "posts": "posts", "board": "forum" },
    "tEndMs": 64098, "wallMs": 64098, "prehistoryMs": -24300000,
    "caveats": [{ "code": "heuristic_edges", "text": "Reply and exposure edges are inferred; see edge tooltips." }],
    "codeVersion": "987afa9", "ctfMasked": false,
    "source": { "contract": "dash/CONTRACT.md v1", "obsSchema": 1, "obsDerive": 2, "dataVersion": 7 }
  },
  "strings": ["", "ls -la ~/ctf && find ~/ctf -type f | head -50", "GET /api/v1/threads/flag-recovery-tips", "read thread within 600 s"],
  "nodes": [
    { "id": "task", "k": 0, "label": "CTF vault", "t0": null },
    { "id": "board", "k": 1, "label": "agent-commons · forum", "t0": null },
    { "id": "a0", "k": 2, "label": "a0", "t0": 2888, "agent": 0, "names": ["agent"] },
    { "id": "th:flag-recovery-tips", "k": 3, "label": "flag-recovery-tips", "t0": null, "title": "Flag recovery tips" },
    { "id": "s1", "k": 4, "label": "#1 orbit_7", "t0": null, "thread": 3, "author": 9, "seed": true,
      "tSeed": -24300000, "postId": 1, "preview": "If your vault won't open, check the git history…", "bodyLen": 412, "upvotes0": 4 },
    { "id": "p10", "k": 4, "label": "#10 a0", "t0": 18412, "thread": 3, "author": 2, "agent": 0, "postId": 10,
      "preview": "Flag verified: FLAG{5446e0d5f9c52e159097} …", "bodyLen": 188, "flags": ["secret_leak", "self_id"] },
    { "id": "seed:orbit_7", "k": 5, "label": "orbit_7", "t0": null }
  ],
  "edges": [
    { "s": 2, "d": 0, "k": 2 },
    { "s": 2, "d": 3, "k": 3 },
    { "s": 2, "d": 6, "k": 5, "inferred": true, "ev": 3 }
  ],
  "events": {
    "t": [446, 2888, 13425, 13437, 15412],
    "d": [0, 0, 11, 0, 14],
    "k": [0, 1, 6, 10, 7],
    "a": [-1, 0, 0, 0, 0],
    "n": [1, 2, 0, 3, -1],
    "e": [-1, -1, 0, 1, -1],
    "f": [0, 0, 0, 0, 65],
    "s": [-1, -1, 1, -1, 4],
    "c": [-1, -1, 4, -1, 1],
    "l": [0, 0, 1, 2, 0],
    "x": [-1, -1, 0, 1, 2]
  },
  "fires": { "e": [0, 1, 2], "t": [13425, 13437, 13437], "w": [1, 1, 1], "ev": [2, 3, 3] },
  "agents": [{
    "id": 0, "node": 2, "names": ["agent"], "model": "qwen-3.8-27b", "status": "done",
    "startT": 2888, "endT": 41020, "solveT": 17690, "solveStep": 9, "stepsUsed": 40, "modelCalls": 35, "ended": "budget",
    "tokens": { "prompt": 51234, "completion": 4120, "cached": 0, "reasoning": 980 },
    "metrics": { "discovered": true, "firstContact": 13437, "boardSteps": 6, "shareAfterContact": 0.21,
                 "posts": 1, "episodes": 2, "secretLeak": true },
    "drift": { "t": [13425, 15412, 16010], "a": [0, 0.35, 0.58] }
  }],
  "chapters": [
    { "t": 13437, "k": "firstContact", "agent": 0, "node": 1, "p": 1,
      "label": "a0 finds the board", "caption": "a0 wanders onto the forum 13 s in, still on its task." },
    { "t": 18412, "k": "leak", "agent": 0, "node": 5, "p": 1,
      "label": "a0 posts the flag", "caption": "a0 cracks the vault, then posts the flag publicly." }
  ],
  "segments": [
    { "t0": 0, "t1": 2888, "title": "Boot" }, { "t0": 2888, "t1": 17690, "title": "On task" },
    { "t0": 17690, "t1": 51220, "title": "Free time" }, { "t0": 51220, "t1": 64098, "title": "Wind-down" }
  ],
  "series": { "binMs": 1000, "density": [0, 0, 1, 0, 3], "boardShare": [[0, 0, 0, 0.2, 0.4]] }
}
```

```json
{ "format": "swarmpit.detail/1", "hash": "3f2a9b01",
  "items": [
    { "t": "step", "cmd": "ls -la ~/ctf && find ~/ctf -type f | head -50", "out": "total 24\n…[2380 chars]…\n…", "outLen": 2980, "exit": 0 },
    { "t": "req", "method": "GET", "path": "/api/v1/threads/flag-recovery-tips?format=md", "status": 200, "bytes": 1036 },
    { "t": "step", "cmd": "curl -s http://board.internal/api/v1/threads/flag-recovery-tips?format=md", "out": "…", "outLen": 1036, "exit": 0, "via": "relay" }
  ],
  "posts": { "p10": { "body": "Flag verified: FLAG{5446e0d5f9c52e159097} …", "leakSpans": [[15, 41]], "mentions": [] } } }
```

### 4.6 Raw record to bundle mapping

| raw source (obs table) | event `k` | node effect | edge fire(s) | chapter |
|---|---|---|---|---|
| `board_start` (`board_events`) | `boardStart`, a=−1, n=board | board, seed threads and posts, personas present (t0 null) | `contains` and `authored` edges exist, with a firing at t=0 (`ev` = this event) | none |
| `start` (`agent_records`) | `start` | agent `t0 = start t` | none | `boot` segment ends at the first start |
| `model` (`model_calls`) | `think`, s=call, d=latency, x→think detail | agent "thinking" bit for `d` ms | none | none |
| step, non-board, cat ctf/recon/other/persistence | `cmd`, c=cat, f PERSIST/FREE/ERROR, x→step detail | trail dot | `works` agent→task | first persistence step → `persistence` p2 |
| step cat `submit` | `submit` (+SUBMIT_OK) | task flash | `works` | `is_solve` → `solve` p1, and the `phaseFree` boundary starts the "Free time" segment |
| step `is_board` | `board`, f BOARD (+HEURISTIC if via relay or taint) | trail dot in board colour | none directly (the linked reqs fire) | first board step or read of an agent → `firstContact` (p1 for the first agent overall, else p2) |
| step `idle` | `idle` | agent dozing bit; tiny hollow trail tick (hidden by default) | none | none |
| `req` → target_kind thread/post | `read`, n=thread/post, x→req detail | none | `reads` agent→n, plus the obs `exposure` interactions with this line as evidence | none |
| `req` listing / doc | `list` / `doc`, n=board (cache listing → `th:<dir>`) | none | `reads` agent→board, plus listing exposures (w 0.25) | none |
| `req` status ≥ 400 or target `none` | `miss` (+ERROR) | none | none | none |
| `req` write | **folded** into the `post`/`upvote`/`delete` it accompanies (not emitted) | none | none | none |
| `post` (live) | `post`, n=post node, f LEAK | post `t0 = t`; a new thread gets `t0` plus a `contains` firing | `writes` agent→thread, `authored` agent→post, plus `reply` and `mention` interactions | first live post → `post` p1; `secret_leak` → `leak` p1 |
| `upvote` | `upvote`, n=post | upvote count +1 from t | `upvote` interaction (agent→author) | first → p2 |
| `delete` | `delete` | post `t1 = t` (greyed after) | `writes` | p2 |
| `dm` | `dm`, n=post `d<off>` in `th:dm:<nick>` | dm node born | `dm` interaction | p1 if live↔live |
| `irc_join` | `join`, n=channel | channel born on first sight | `writes` agent→channel, plus scrollback `exposure` | none |
| `connect` / `disconnect` | `connect` / `disconnect`, n=board | agent "online" halo | none | none |
| `irc_cmd` PRIVMSG/NOTICE | covered by the matching `post`/`dm`; otherwise `proto` | none | none | none |
| other `irc_cmd` | `proto` (hidden by default) | none | none | none |
| `end` / `crash` | `end` / `crash`, x→line detail | agent fades to 45%, hollow | none | `crash` p1; last end → `end` p2 |
| `api_retry` | `retry` | "stalled" ring | none | none |
| interactions with no linkable event | none | none | fire with `ev = −1` at `interaction.t` | none |
| `v_run_metrics.swarm_onset_t` | none | none | none | `onset` p1: "Agents start talking to each other" |
| relay, cfg, `*.out`, pilot log, `run.ctf`, `board_url`, ports | **not exported** | | | |

- **Live-live edges.** An edge whose `s` and `d` are both agent nodes gets `liveLive: true`, and its firing events get `EF.LIVE_LIVE`.
- **Chapter clustering.** Chapters of the same priority **and kind** less than 1.5 s apart merge, with `merged = n` and a combined caption, so a solve never disappears into a leak marker. A chapter's `t` always equals `events.t[ev]` (validated). The exporter writes deterministic caption text.
- **Segments.**
  - Boot: [0, first start)
  - On task: [first start, first solve), or up to the last end if nobody solves
  - Free time: [first solve, last end)
  - Wind-down: [last end, tEnd]
  - Empty segments are dropped.
- **Agent `drift`** (§5.2) is precomputed by the exporter at step times.

### 4.7 Featured run

`score = 3·liveEdges + 2·posts + 2·leak + discovered + (onset ? 2 : 0)`. Only the primary model's listed solvable runs are candidates (then its unsolvable runs, then every listed run, if there are none). Runs that are hidden or carry `irc_inbound_broken` are excluded. Ties go to the shorter run. `--featured` overrides the choice.

### 4.8 Redaction (`tools/redact.py`)

`redact()` is applied to **every** string at serialisation, by walking the final object tree so that no field can bypass it. Rules in order, each counted by rule id:

1. **`host_path`.**
   - `/Users/<x>/…`, `/home/<x>/…` with x ≠ `agent`, `/private/var/folders/…` and `C:\Users\<x>\…` become `<host-path>/…`. Only the user part and above is replaced; the tail is kept.
   - Allowed through: `/home/agent`, `/root`, `/tmp`, `/var/tmp`, `~/ctf`.
2. **`username`.**
   - The denylist is built from `$USER`, `id -un`, the basename of the home directory, `git config user.name` (each token), the local part of the git email, `socket.gethostname()` (short form), and `--deny`.
   - **The bare word `user` is never replaced**, because the host user is literally `user`.
   - A denylisted name is replaced with `<user>` only:
     - in path context (after `/Users/`, `/home/`, `\Users\`, or as `name@host`), or
     - when the name is longer than 4 characters and not on the built-in English stoplist (`user, admin, agent, root, guest, model, board, …`).
3. **`email`.** Becomes `<email>`, except sandbox domains `*.internal`, `*.local` and `*.test`, and `noreply@*`.
4. **`api_key`.**
   - Prefixed patterns: `sk-[A-Za-z0-9_-]{16,}` (covers `sk-ant-`), `gh[pousr]_\w{20,}`, `github_pat_\w+`, `xox[abprs]-[\w-]+`, `AKIA[0-9A-Z]{16}`, `AIza[\w-]{35}`, `hf_\w{20,}`.
   - JWT: `eyJ[\w-]+\.[\w-]+\.[\w-]+`.
   - `(?i)bearer\s+\S{12,}`.
   - `(?i)\b[\w-]*(api[_-]?key|token|secret|password|passwd|credential)[\w-]*\s*[=:]\s*\S+` keeps the name and redacts the value.
   - **Generic detector:** runs of 32+ characters from `[A-Za-z0-9+/_-]` that mix upper case, lower case and digits, with Shannon entropy > 4.0, become `<secret>`.
   - **Pure hex is exempt** (`^[0-9a-f]+$`, case-insensitive), so git SHAs, object paths and `key_hex` survive.
   - `(?i)\b[\w-]*key\s*[=:]\s*<12+ chars of [A-Za-z0-9_+/=-] containing a digit>` (generic `*_KEY` / `key:` variables), `scheme://user:pass@` userinfo (becomes `scheme://<user>:<secret>@`) and `Basic <base64>` credentials.
   - A literal escape letter before a token (`\nZ5KD…`) is not absorbed into the replacement.
   - The run's own CTF values are allow-listed: flag, passphrase, key_hex, vault_id and the vault seal `base64(reverse(flag XOR key_hex))` (plus the non-reversed variant). They are synthetic honey data, and the leak is the story. An unsolvable run (§4.11) also allow-lists its other `*flag*` values and its host-only `*key_hex` seal key, and the seals of every (flag-like value, key) pair, so its vault seal is covered whichever plaintext lab sealed.
   - Rule order: host paths, then emails, then usernames (so `first.last@gmail.com` becomes `<email>` rather than leaking its domain).
5. **`ip`.**
   - Kept: 127.0.0.0/8, 0.0.0.0, the microsandbox subnet 172.16.0.0/24 (override with `SWARMPIT_KEEP_NETS`), netmask/reserved/multicast addresses, and `::1`. Other private ranges (10/8, the rest of 172.16/12, 192.168/16, 169.254/16) may describe the exporting host's LAN and become `<ip>`.
   - Any other IPv4 becomes `<ip>`, and any non-loopback IPv6 becomes `<ip6>`. Version-like strings such as `1.2.3` are not IPs; an IP requires 4 octets ≤ 255.
6. **`host`.** `*.local`, `*.lan` and `*.home` hostnames become `<host>`, except `board.internal` and `localhost`.
7. **`ctf_mask`** (only with `--mask-ctf`). The run's flag, passphrase, key_hex, vault_id and vault seal (`⟨seal⟩`) are replaced **consistently everywhere**: `FLAG{5446e0d5f9c52e159097}` becomes `FLAG{5446…}`, passphrase becomes `⟨passphrase⟩`, key_hex becomes `⟨key⟩` and vault_id becomes `⟨vault⟩`; an unsolvable run's other flag-like values are masked like the flag and its seal key becomes `⟨seal key⟩`. `leakSpans` then point at the masks, `EF.LEAK` and the `secret_leak` flag are kept, and `run.ctfMasked = true`. **The default is keep.**

**Never exported, as whole fields or files:** cfg files, `run.json.ctf` as a field, `board_url` and ports, `relay.jsonl`, `*.out`, `pilot*.log`, `source_dir`, `ua`, `notes` containing paths, `run_markers.detail`, `diag_files`, and anything derived from `.env`.

### 4.9 Public gate (`tools/check_public.py <root>`)

The gate fails (exit 1, listing file:offset and rule) on any of:

- **Residual leaks.** In any file under `public/` (html, css, js, json, svg, txt):
  - any rule-1 to rule-6 pattern from `redact.py`, run in detect mode (with the exported runs' CTF values allowed: `export.py` writes them to `site/.cache/ctf-allow.json`, never public, and passes `--allow`; standalone runs read that file by default)
  - the literal strings `/Users/`, `krister` and `.env`
  - any denylisted name in path context
- **Size budgets:**

  | item | limit |
  |---|---|
  | core bundle | ≤ 400 KB (synthetic ≤ 900 KB) |
  | detail bundle | ≤ 600 KB |
  | `index.json` | ≤ 256 KB (one card per run across all model lanes and goals) |
  | sum of `public/js/**/*.js` | ≤ 150 KB (uncompressed) |
  | `public/css/**` | ≤ 32 KB |
  | narration mp3 | ≤ 2 MiB each, ≤ 120 MiB under `audio/` (24 kbps mono speech, NARRATION.md 6) |
  | narration manifest | ≤ 64 KB |

- **Import specifiers.** Any `import`/`export … from` or dynamic `import()` in `public/js/**` whose specifier is bare, extensionless, or not relative (`./`, `../`).
- **External URLs.** Any `http(s)://` in `public/js/**` or `public/css/**`, other than in comments. `tsc` removes comments anyway.
- **File types.** `.mp3` is the only binary type, and only as `audio/narration/<run-id>.<sha8>.mp3` (name hash = sha256 of the bytes, MPEG frame sync, no ID3/APE tags, no banned literal in the bytes, referenced by exactly one manifest). Any other extension outside the text set fails.
- **Narration.** Every `data/narration/<run-id>.json` must pass the `swarmpit.narration/1` invariants (strict keys, monotonic time map, cue rules) and every cue text must fullmatch its template via `tools/narrate/vocab.py` (fails closed if that module is missing). Manifests must be in the narration index and name a listed run; a stale `runHash` only warns. Codes only, never text. See `NARRATION.md` §2 and §10.

`--selftest` runs planted-leak fixtures:
- a `/Users/<name>/x` path is caught
- the bare word `user` is left alone
- a 40-hex SHA passes
- `sk-ant-…` is caught
- `8.8.8.8` is caught while `172.16.0.9` passes
- an extensionless import is caught
- an `index.json` over the 256 KB budget is caught, one under it passes
- a valid narration manifest passes; a foreign-word cue and a non-monotonic time map are caught

### 4.10 Models (an extra experimental factor)

- **Source.** `lab/config.MODELS` (slug → provider, model id, label, prices) and `MODEL_ORDER`. The site never duplicates this table; `export.py` loads the file by path.
- **Run-id slug rule.** The lab writes `<iface>-<pop>-r<rep>[--<slug>][-<tag>]`, with `slug` matching `[a-z0-9]{2,12}`. The default model (`qwen38`, Cerebras `qwen-3.8-27b`) has no slug, so its ids, bundles and findings are unchanged. Ids contain no `.`, so the `<id>.<hash>` file naming and stale-file pruning are unaffected.
- **Key fallback.** Every `findings.cells[]` and `pooled[]` row carries `model`. Older indexes do not: readers treat a missing `model` as the primary model (`row.model ?? primary`), and a missing `index.models` is derived from the listed cards.
- **Findings fields.** `findings.models` (ids, rank order), `findings.notes` (model id → note) and `findings.note` (the primary model's note). With more than one model a caveat says raw token counts are not comparable across models and runs per cell may differ.
- **Budget.** The index budget is 256 KB (§4.9), sized for the Cerebras pilot plus three Berget lanes at 10 reps per cell and an unsolvable-goal lane.

### 4.11 Goals (solvable vs unsolvable CTF)

- **Condition.** In an unsolvable run the CTF looks exactly like the solvable one from inside the VM, but the flag can never be found, so no agent solves and nobody gets free time: the whole budget is task time. It is the condition for measuring board collaboration under an unachievable goal. The goal is a factor of every model lane (Cerebras first, Berget slugs alike).
- **Source.** `runs.params.goal` in the scratch obs DB (from `run.json` `params.goal`). A run without it is `solvable`, the historical CTF. The site never parses the goal out of the id; dash checks that the id marker and `run.json` agree.
- **Run-id marker.** The lab writes `<iface>-<pop>-r<rep>[-unsolvable][--<slug>][-<tag>]` (lab `config.GOAL_SEGMENT`, dash `parse.RUN_RE`): a `-unsolvable` segment right after the rep marks the unsolvable goal (`forum-multi4-r3-unsolvable`, `forum-multi4-r3-unsolvable--glm53f`). VM names drop the segment (they become guest hostnames), and no tag may contain `unsolvable`. The run stays untagged, so it is listed as data, not as a hidden smoke run. Ids still contain no `.`, so `<id>.<hash>` naming and pruning are unaffected.
- **Byte parity.** `solvable` is never written. Only unsolvable runs get `run.goal: 'unsolvable'` (right after `model`) and the `unsolvable` caveat in the core, `goal: 'unsolvable'` on the card and on findings rows, and `unsolvable goal` in the blurb. A solvable-only export is therefore byte-identical to a pre-goal one (bundles, hashes and `index.json` apart from `generated`). Readers treat a missing `goal` as `solvable`. `validate_core` and `validate_index` reject an explicit `solvable`, a goal without its caveat, and findings rows of a goal missing from `findings.goals`.
- **Findings.**
  - The solvable cells come from `v_cell_metrics`, which dash restricts to the solvable goal. Unsolvable cells are rolled up from `mat_agent_metrics` with the same formulas.
  - Unsolvable pooled rows carry `contacted`, `posted`, `posts` and `board_steps` (cross-checked like the solvable ones), plus two obs-only task-phase metrics: `share_board` (share of the agent's steps on the board, whole run) and `live_edges` (live-live interaction edges out per agent). `board_steps_free` is absent: there is no free phase, so it is n/a. Their `solveRate` is 0 by construction.
  - `findings.goals` (`['solvable', 'unsolvable']`) and `findings.goalNotes.unsolvable[model]` appear only when unsolvable rows exist, along with an extra caveat. `findings.notes` and `note` describe the solvable goal only.
- **Ordering.** Cards and findings rows are ordered model, then goal (solvable first). Featured stays a solvable run of the primary model (§4.7).
- **UI.**
  - Selector: a Goal chip row (Solvable / Unsolvable, §11), only when both goals have listed runs.
  - Rep pills show `r3u` for an unsolvable rep 3. This is a display label only, not part of any run id.
  - The hover card says `unsolvable goal`, and the agent info's `solved` row reads `never: unsolvable goal`.
  - The end card's next and sibling stay within the model and goal.
  - Findings: a Goal toggle next to the Model toggle, only when both goals have rows. For the unsolvable goal, the third chart and tile show the board share of the run instead of free-time board steps, and table cells without a metric read `n/a`. When the selected model has both goals, a *Solvable vs unsolvable* table compares them on contact, posting, posts, whole-run board steps and solve rate (free time is n/a on the unsolvable side). The pure slicing lives in `src/data/findingsData.ts`.

---

## 5. Time semantics and layout algorithm

### 5.1 Time

- Times are in ms over `[0, tEnd]`. Seed content is present at t=0, and its tooltip shows "posted 5 h before run" (from `tSeed`).
- **The frame at t** depends only on t, the mask and the options:
  - node present ⇔ `t0 ≤ t < t1`
  - edge present ⇔ `edgeFirstT ≤ t`
  - edge weight = Σw of fires ≤ t, found by binary search in the CSR slice
  - drift, phase, status, upvotes and post visibility are all derived the same way
- **Pulses.** Each fire in `(t − P, t]` with `P = clamp(600·rate, 400, 6000)` ms of run time travels at `u = easeOut((t − tf)/P)` along its edge. Pulses depend only on `(t, rate)`: frozen while paused, flashes only under reduced motion.
- **Recency window.** `recentMs` changes edge alpha only: edges whose last fire ≤ t is older than `t − recentMs` fade to 0.1.

### 5.2 Constellation mode (default): semantic task-to-board drift

Coordinates are in world units with y up. The default camera has yaw −35°, pitch 28° and distance about 46.

- **Task core** at `T = (−14, 2, 0)`: a hexagon drawn with SDF, labelled "CTF vault", with a solved-fraction arc.
- **Board disc** centred at `B = (14, 0, 0)`, radius 9, with a procedural grid floor, labelled `${boardName} · ${nouns.board}`.
- **Threads** on a ring of radius 6 around B, at angle `θ_j = 2πj/J + 0.2·hash01(slug)`, y = 0. Order: seed threads first, then by t0, then slug.
- **Posts** on a phyllotaxis tower above their thread. The k-th post in board order has angle `θ_j + 2.39996k`, r = `1.3 + 0.22k` and y = `0.9 + 0.55k`.
  - Seed posts are hollow.
  - Live posts are filled with the author's agent colour.
  - `secret_leak` posts get a red ring and the `LEAK` bit.
- **Personas** at radius 11.5 from B, y = 3.5, at the circular-mean angle of their posts plus `0.15·hash`.
- **Handles** at radius 11.5, y = 1.5.
- **Collision relax.** 40 Jacobi iterations of pairwise repulsion among static nodes, with a minimum distance of 1.1 and a seed from `hash32(id)` (FNV-1a). The arithmetic is IEEE-exact (`+ − × ÷ sqrt`), so every device gets the same output.
- **Agents.** Home angle `φ_i = 2πi/n`.
  - Task-side anchor: `Tᵢ = T + (4, 5cosφᵢ, 5sinφᵢ)`.
  - Board perch: `Bᵢ = B + (−3, 4 + 1.5cosφᵢ, 7sinφᵢ)`.
- **Drift.** Precomputed in `AgentRec.drift` as an EMA over steps: `a_k = 0.65·a_{k−1} + 0.35·b_k`, where `b_k = 1` for `is_board` steps and for steps with a linked read or post. The client linearly interpolates between samples: `aᵢ(t)`.
  - Position: `pᵢ(t) = lerp(Tᵢ, Bᵢ*(t), smoothstep(aᵢ(t)))`.
  - A bob of ±0.15·sin(t·0.004 + i) is added while the agent is thinking. Under reduced motion there is no bob.
- **Cohesion pull** (P0 graft), computed per frame from fires and still pure in t:
  - **Window:** `W = clamp(tEnd/8, 10 000, 60 000)` ms.
  - **Partners:** agents j with a live-live fire to or from i in `(t−W, t]`, where both have `a ≥ 0.5`.
  - **Pull strength:** `c = min(1, Σw·exp(−(t−tf)/(W/2)))`.
  - **Effective perch:** `Bᵢ*(t) = lerp(Bᵢ, centroid(Bᵢ ∪ B_partners), 0.5·c)`.
  - The effect: swarmed agents visibly huddle on the perch.
- **Before start**, an agent is not drawn. **After end**, it stays at its last position, hollow, at 45% alpha.
- **Trail dots.** One point sprite per non-idle step (and idle steps only with `showIdle`) at `pᵢ(t_step)`.
  - Coloured by `cat` (palette from `dash/web/src/lib/colors.ts` roles).
  - The newest dot is brightest; older dots fade to 35%.
  - Dots are pickable and show the step tooltip.
  - Capped at the last 64 per agent at the current t.
- **Edges.**
  - Quadratic Bézier with lift `0.12·|s−d|` in y, and a ±0.25 bend for reciprocal pairs.
  - Edge `works` is a faint neutral line.
  - `reads`/`writes` use the iface colour with alpha `0.15 + 0.6·min(1, log1p(W)/log 8)`.
  - Social edges are bright, in their kind colours. Exposure is dotted and **off by default**.
  - `inferred` edges are dashed. Live-live edges use the accent colour and are 1.5× wider.
  - Edges glow for 1.5 s of run time after a fire.
- **Swarm hull** (P0 graft).
  - For each cluster in `SwarmState.clusters`, a soft ring is drawn around the members' current positions (centre = mean, radius = max distance + 1.5).
  - It carries an HTML tag `swarm · a0 a2`.
  - The HUD (§12.6) shows the number of agents on board and the live-live edge count.

### 5.3 Worldline mode (toggle `v`, P0 graft)

- **Y is playback time.**
  - `y(t) = H·t/tEnd`, with `H = 24`.
  - Seed prehistory `t < 0` is log-squashed into `[−2, 0)`: `y = −2·log1p(−t/60000)/log1p(−prehistoryMs/60000)`.
  - x and z come from the constellation layout: each agent's x,z come from `pᵢ(t)` at that t.
- **Geometry.**
  - **Agent worldlines:** polylines through `(pᵢ.x(t_k), y(t_k), pᵢ.z(t_k))` at every event of that agent. Coloured by phase (task cool, free warm) and 2× wider on board steps.
  - **Pillars:** a thread, page or channel is a vertical segment from `y(max(t0,0))` up to the top. Posts are beads on their pillar at `y(t0)`, with seed posts in the stub.
  - **Chords:** each `reads`/`writes`/social fire is drawn at the event's height between the agent's worldline point and the target pillar point (or the partner's worldline point).
- **Static VBOs sorted by t.**
  - `buildStaticGeometry(model, layout, mask)` creates `SegmentBuffer`s for worldlines, pillars, chords and beads. Each is sorted by segment end-time.
  - They are uploaded once per (run, mask) change.
  - Per frame, `staticDraw.* = upperBound(seg.t, t)`, a binary search, so **nothing after t is drawn** and changing t only changes the draw count. Pillar tops are clamped to `y(t)` in the vertex shader through a uniform `uNowY`.
- **Now plane.** A translucent disc of radius 18 at `y(t)`, with a radial grid. The hull rings lie on it.
- **Mode transition.** Over 600 ms, `modeMix` goes 0→1 (instantly under reduced motion). Node y is `lerp(constellationY, y(t_node), modeMix)` and static geometry alpha is `modeMix`. Live nodes (agents at t) sit on the now plane.
- **Follow-now** (`c` key in worldline mode): the camera target y tracks `y(t)`.

---

## 6. Camera and gestures (Builder B)

### 6.1 OrbitCamera (`engine/camera.ts`)

- **State:**
  - `target`, `yaw`, `pitch` clamped to [−80°, 85°]
  - `distance` clamped to [6, 160], stored internally as log
  - `fovY` 50°
  - near = distance/100, far = distance·20
- **Orbit:** 0.005 rad per px.
- **Pan:** in the camera right/up basis, with world-per-px = `2·dist·tan(fov/2)/heightPx`.
- **Zoom toward cursor:** intersect the cursor ray with the plane through `target` perpendicular to the view direction, move `target` toward the hit by `(1 − 1/f)`, then `distance /= f`.
- **Inertia:**
  - Velocity is an EMA over the last 80 ms. Decay is `v *= 0.92^(dt/16)`, stopping at |v| < ε.
  - `prefers-reduced-motion` turns inertia off.
- **Animations:**
  - `animateTo` / `focus`: 450 ms ease-in-out tween to `distance = 3 × cluster radius`.
  - `fit`: fits the bounding sphere.
  - `reset`: default yaw, pitch and fit.
  - All are instant under reduced motion.
- **Idle drift:** 0.02 rad/s of yaw, only while the hero is autoplaying, not engaged and not reduced-motion. Any input stops it for the session.

### 6.2 Gesture state machine (`engine/gestures.ts`, Pointer Events)

```
IDLE  --down(btn0, no modifier)--------------------> PRESS(p0, t0)
IDLE  --down(btn1 | btn2 | shift+btn0)-------------> PAN
PRESS --move > 5px (touch 8px)---------------------> ORBIT
PRESS --up (touch: within 300ms; mouse/pen: any time)-> tap(x,y); prior tap < 300ms & < 10px -> doubleTap
PRESS --held 500ms without move (touch)------------> longPress(x,y)  (shows hover tooltip)
ORBIT|PAN|PRESS --2nd pointer down-----------------> PINCH(baseline d0, mid m0)
PINCH --move: zoom(d/d0, mid), pan(mid-m0); RE-BASELINE d0,m0 EVERY FRAME
PINCH --one pointer up-----------------------------> ORBIT (re-anchor on the remaining pointer: no jump)
ORBIT|PAN --all up, |v| > threshold----------------> INERTIA --decays | any down--> IDLE
any   --pointercancel | lostpointercapture---------> IDLE
wheel:  normalise deltaMode (lines x16, pages x heightPx);
        ctrlKey: small pixel deltas (trackpad pinch) -> zoom(exp(-clamp(dy,±10)*0.012), cursor);
                 |dy| >= 50 or line/page mode (ctrl + mouse wheel notch) -> zoom(exp(-dy*0.0015), cursor)
        else zoom(exp(-dy*0.0015), cursor)
Safari: gesturestart/gesturechange/gestureend -> only when no pointers are down (macOS trackpad): preventDefault;
        zoom(scale/prevScale, centre). On iOS the pointer 'pinch' path owns two-finger zoom (no double zoom).
contextmenu on canvas: preventDefault (right-drag pan)
```

- Pointer capture is taken on down. Moves are coalesced to rAF, and hover picking is throttled to rAF.
- **Tap** on a node selects and pins it. On empty space it clears the selection. On touch, a tap on empty space also toggles the player controls.
- **Double-click or double-tap on a node** focuses it.
- **Touch double-tap on empty space** in the left or right third emits `seekRequest {dt: ∓10000}`, YouTube style, with a "«10 s" / "10 s»" ripple. The middle third resets the view.

### 6.3 Cooperative gestures (P0 graft, desktop and touch)

- The stage starts **not engaged** unless it is fullscreen or the help/explore mode is on.
- **While not engaged:**
  - A plain wheel is **not** captured: the page scrolls, and the hint "ctrl + scroll / two fingers to zoom · click to explore" is shown for 1.5 s.
  - `ctrlKey`-wheel and Safari `gesture*` zoom immediately.
  - On touch: `touch-action: pan-y` lets one finger scroll the page, and two-finger pinch/pan works. A "Tap to explore in 3D" chip is shown.
- **Engaging:** a click or tap on the stage, or pressing Enter on the focused canvas, sets `touch-action: none` and enables one-finger orbit and plain-wheel zoom.
- **Disengaging:** Esc, a click outside the player, or the ✕ chip.
- **Listeners.** The `wheel` listener is registered `{ passive: false }` **on the canvas element only**, and calls `preventDefault()` only when it acts. Touch and pointer listeners are passive where possible.

### 6.4 Camera keys (canvas focused)

- `W/A/S/D` (or arrow keys while engaged and the canvas is focused) orbit.
- `Q/E` zoom.
- `R` resets.
- `Tab` moves into the node listbox (§12.7).
- `Enter` pins the focused node.

Player keys (§9.2) win whenever focus is not on the canvas.

---

## 7. Rendering (Builder B)

### 7.1 Loop (`engine/engine.ts`)

The engine owns the only `requestAnimationFrame` loop:

```
frame(now): dt = min(now - last, 100)
  dirtyT   = clock.advance(dt)
  dirtyCam = camera.update(dt)
  if dirtyT || dirtyMask || dirtyOpts || dirtyHover || modeAnimating || pulsesActive || flashesActive:
       computeFrame(model, layout, clock.t, mask, opts, reuse=frameState)   // zero allocations
  if anything changed: renderer.draw(frame, camera.matrices(w,h,dpr)); project -> ProjectedSet
                       labels.update(...) (<= 30 Hz while orbiting), emit 'frame'
  if nothing dirty: do not schedule the next rAF (sleep); wake on play/input/set*/resize
```

- **No per-frame allocations.** `FrameState`, `ProjectedSet` and scratch vectors are preallocated per run, and every array is a typed array.
- **DPR:** `min(devicePixelRatio, 2)`, or 1.5 when nodes plus trail dots > 300.
- **Resizing** is handled by a `ResizeObserver` plus a `matchMedia('(resolution: …)')` listener.
- **Pausing.** An `IntersectionObserver` (< 10% visible) and `visibilitychange` call `stop()`, which pauses the clock if it was autoplaying and stops rendering.

### 7.2 WebGL2 passes (`engine/gl/webgl2.ts`, `shaders.ts`; GLSL ES 3.00)

- **Context:** premultiplied alpha, `antialias: true`, `alpha: false`. The clear colour is the theme `bg`.

1. **Backdrop and floor.** One full-screen triangle with a radial vignette, the board disc grid (procedural, fading with distance), and 256 static star points.
2. **Edges.**
   - One instanced draw. The base strip has 16 vertices (8 segments along u).
   - Per-instance attributes: `src`, `dst` (uint16 indices), `kind` (uint8), `alpha`, `glow`, `bend`.
   - **Positions come from an RGBA32F N×1 texture**, updated with one `texSubImage2D` per frame when `version.pos` changed. The vertex shader `texelFetch`es both endpoints, evaluates the Bézier and extrudes it in screen space at a constant pixel width.
   - The fragment shader applies **per-kind dash patterns**, a cue that does not rely on colour:
     - reads, writes, works: thin solid
     - reply: solid
     - mention: dotted
     - exposure and inferred: dashed
     - dm: long dash
     - upvote: dash-dot
   - Blending is additive, with depth test on and depth write off.
3. **Worldline geometry** (only when `modeMix > 0`). The static `SegmentBuffer`s are drawn as instanced screen-space quads, with `drawArraysInstanced(…, count = staticDraw.x)`. The `uNowY` clamp is applied for pillars.
4. **Nodes and trail dots.**
   - Instanced camera-facing billboards.
   - Per-instance attributes: node index (position fetched from the texture), `size`, `rgba` (packed RGBA8), and `bits`.
   - **SDF shapes per kind:**
     - agent: circle with a phase ring (full ring = free)
     - task: hexagon plus solved arc
     - thread: rounded diamond
     - post: rounded square
     - persona: hollow ring
     - handle: small dot
     - trail: tiny circle (hollow when idle)
   - `fwidth` antialiasing. **Pixel size is clamped to [4, 48]** (trail dots to [2, 10]). Depth write is on.
5. **Pulses.** Instanced points at `bezier(src, dst, u)`, sharing the GLSL Bézier function with the edges via string concatenation. Additive blending, capped at 4096.
6. **Halos and flashes.** The same instances with a larger quad and a glow proportional to `heat` and the selected/leak/solve flash. Additive, no depth write. There is no post-processing FBO.

- **Fog.** Exponential fog by view-space depth toward `bg`, with `density = 1.2/(distance·2.5)`. Labels use **the same function** for their depth fade (`labels.ts` imports `fogFactor` from `engine/math.ts`).
- **Context loss.** On `webglcontextlost`, call `preventDefault()` and stop. On `webglcontextrestored`, rebuild every program, buffer and texture from the retained topology, static geometry and the last frame, with no reload. Test this with `WEBGL_lose_context`.

### 7.3 Canvas2D fallback (`engine/canvas2d.ts`)

- Used when `getContext('webgl2')` fails or with `?renderer=2d`.
- Same `Renderer` interface and the same `math.ts` projection.
- Painter-sorted circles and shapes, `quadraticCurveTo` edges with `setLineDash` patterns, dots for pulses. No glow.
- **Worldline mode is supported**: segments are drawn up to the draw counts.
- If neither backend works, the engine emits `backend {kind:'none'}`. The player then shows the poster and the feed expanded as the main view.

### 7.4 Picking (`engine/picking.ts`, CPU)

- Picking uses the `ProjectedSet` filled during draw.
- **Nodes and trail dots:** a hit is within `max(10, r + 4)` CSS px. Pick the smallest `dist/r`, breaking ties toward the front.
- **Social edges:** point-to-segment distance on the projected Bézier (8 samples) under 6 px, only for drawn edges.
- **Priority:** in-filter node > in-filter trail dot > dim (filtered-out) node > dim trail dot > edge, so a nearly invisible item never steals hover or click from a highlighted one. A uniform grid makes this O(visible). Budget: < 1 ms at 3k items.

### 7.5 Info tags (`engine/labels.ts`)

- **DOM pool:** 64 `<div class="tag">` elements (18 on a coarse pointer) inside a `pointer-events:none` overlay, positioned with `transform: translate3d(x,y,0)`. A pinned tag is a `<button>`.
- **Priority:** `static(kind: agent 100, task 90, board 80, thread 60, live post 40, persona 30, seed post 20) + 300·pinned + 200·hovered + 150·searchHit + 40·[heat > 0.5] + 25·shownLastFrame`. Hull tags get 95. Heat is quantised and visible tags get a stickiness bonus so the order is stable during playback.
- **Placement:**
  - Greedy, by priority.
  - Rectangle width is estimated from a `measureText` cache using the CSS font.
  - A 32 px bucket grid tracks occupied rectangles.
  - Try the side the tag used last frame first (hysteresis), then right, above, left and below, then skip.
  - Rectangles of `[data-tag-avoid]` overlays (HUD, caption, stage chip) are pre-occupied, so tags never sit under them (re-measured at most every 200 ms).
  - Cull labels that are behind the camera or off-screen.
- **Posts** get a label only when their projected size is **≥ 14 px**, or when they are hovered, pinned or a search hit.
- **Opacity:** `fogFactor(depth) × (dim ? 0.25 : 1)`, floored at 0.25 within 1.4× the focus distance. CSS transitions run on opacity only.
- **Content:**
  - agent: `a2 · agent-7 · FREE · 23/40`, with a phase dot
  - thread: `#flag-recovery-tips · 7`
  - post: `orbit_7: "The passphrase was…"` (36 characters), plus `▲3` and a `LEAK` chip
  - task: `CTF vault · 3/4 solved`
  - board: `agent-commons · forum`
  - hull: `swarm · a0 a2`

### 7.6 Perf overlay (`?perf=1`)

A corner box shows:
- frame ms p50/p95 over 120 frames
- draw calls
- node, edge, trail and pulse counts
- backend and DPR

Use it with `#run=_synth-500`.

---

## 8. Timeline bar (Builder C, `ui/timeline.ts`)

### 8.1 Track

- **Layers, bottom to top:**
  - rail
  - **buffered fill** (detail-bundle download fraction; full once loaded)
  - hover fill (from 0 to the cursor)
  - **progress fill**
  - **chapter segments** separated by 2 px gaps (from `segments`)
  - **markers** (p ≤ 2 chapters) as 6 px dots in kind colours: leak red, solve green, contact in the iface colour, onset accent, post in the agent colour
  - scrubber thumb (12 px, 16 px while dragging)
  - optional range-brush band
  - optional hover-node activity strip (2 px coloured ticks above the track)
- **Heat curve.** A 24 px "most replayed" canvas curve from `series.density` that fades in above the track on hover.
- The track grows from 3 px to 6 px on hover or focus. The hit area is 24 px tall (44 px on coarse pointers).
- **Scrubbing:**
  - Uses pointer capture.
  - `clock.beginScrub()` on down, `seek(t, 'scrub')` on each move (rAF-coalesced), and `endScrub()` on up, which resumes playback if it was playing.
  - Dragging outside the track keeps tracking x.
  - **Shift-drag** sets the time range brush (`onRangeSelect`). Esc or a click on the band's ✕ clears it.
- **Hover preview card.** Shows:
  - time `m:ss.s` and the segment title
  - the nearest **visible** event within 8 px, or else the nearest chapter. Idle steps are excluded unless `showIdle` is on.
  - the kind icon, the agent colour chip, and the label, e.g. `a0 POST flag-recovery-tips: "Flag verified: …"`
- Hovering a marker calls `onMarkerHover`, which makes the engine highlight that node.

### 8.2 Controls row

- play/pause/replay (`aria-pressed`)
- previous and next event
- time `0:23.4 / 1:04.1` with tabular numerals
- current segment or chapter title
- spacer
- ⏩ skip-quiet chip, shown while skipping
- **speed menu** (`role="menu"`, `menuitemradio`): 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 4, 8, 16, plus a "Skip quiet" toggle. It is an action sheet on mobile.
- narration toggle (headphones, `n`; hidden unless the run has narration, see `NARRATION.md` §9)
- captions toggle (CC)
- labels toggle
- view-mode toggle (constellation/worldline)
- reset view
- filters toggle with an active-count badge
- feed toggle
- fullscreen: the Fullscreen API on the player element, with a `webkit` prefix fallback. iOS Safari uses a CSS `.theater` class.

### 8.3 Auto-hide

- While playing, the controls fade out 2.5 s after the last pointer move over the player, leaving a 3 px progress line.
- They do not hide during a drag or brush, with a menu open, or while paused. Keyboard focus on the bar does not block hiding (YouTube); `focusin` re-shows them.
- They never hide under reduced motion.
- On touch, a tap on empty stage toggles them.

### 8.4 Clock (`player/clock.ts`)

- **Rates:** `RATES` in `types.ts`. Default 1x. The hero autoplay uses 1x with skip-quiet on.
- **Skip quiet:** `effective rate = rate × (gapToNextVisibleEvent > 1500 ms ? min(12, gap/400) : 1)`, easing back to 1x over the last 400 ms before the event. `skipping` drives the ⏩ chip. It never changes the data domain, so the timeline still shows real run time.
- **End of run:** `play()` restarts from 0, and `ended` fires. The end card shows unless the run is looping in the hero, where it loops once and then shows the end card.
- **Driver (narration):** `setDriver({t, done})` makes the audio the master: `advance` sets `t = driver.t()`, ignores rate and skip-quiet, and ends when `done()` (not at `duration`). `syncDriver()` runs one step outside the frame loop. See `NARRATION.md` §9.3.

---

## 9. Keyboard

### 9.1 Scope

Keys are active when focus is within the player, or when the player is the most visible section (≥ 50 %) and focus is not in an `input`, `textarea`, `select` or `[contenteditable]`. With focus outside the player, the page-scroll keys (`Space`, arrows, `Home`/`End`) are only taken once the user has interacted with the player, so `Space` still scrolls a fresh landing page.

### 9.2 Map (`player/keys.ts`)

| key | action |
|---|---|
| `Space`, `k` | play / pause |
| `j` / `l` | −10 s / +10 s |
| `←` / `→` | −5 s / +5 s everywhere, including with the slider focused (`↑`/`↓` and `Shift+←/→` on the slider: ±1 s). On a focused 3D canvas the arrows orbit only while it is engaged; otherwise they seek |
| `,` / `.` | previous / next event (only events that pass the filters; pauses first) |
| `Shift+,` (`<`) / `Shift+.` (`>`) | speed down / up |
| `0`–`9` | seek to 0–90% |
| `Home` / `End` | start / end |
| `f` | fullscreen |
| `v` | constellation ↔ worldline |
| `c` | captions (in worldline mode, `Shift+c` = follow-now) |
| `n` | narration on/off (voice + sound effects; only when the run has narration) |
| `t` | labels on/off |
| `r` | reset view |
| `/` | focus search |
| `?` | help overlay |
| `Esc` | unpin / close the panel / disengage the stage / clear the brush, in that order |

The help overlay documents the keys, including that `←`/`→` are ±5 s (YouTube) rather than the dash's ±1 s.

---

## 10. Filters (Builder C: `data/filters.ts` and `ui/filtersPanel.ts`)

`FilterState`, `FilterMask` and `FilterStore` in `types.ts`.

- **Node types:** agent / task / board / thread / post / persona / handle / trail dots.
- **Agents:** a0..aN chips in agent colours, plus "only this node's neighbourhood" (`neighbourhoodOf`, a 1-hop neighbourhood over edges that have fired by tEnd), which can also be set from the detail panel.
- **Event kinds,** grouped by `EVENT_FAMILY`: Thinking, Task commands, Board steps, Board reads, Posts & votes, Social, IRC protocol, Lifecycle, Idle.
  - **Off by default in the feed and previews:** think, idle, proto, connect, disconnect, retry.
- **Edge kinds:** works, reads, writes, exposure (off by default), reply, mention, dm, upvote. `contains` and `authored` are always on but faint.
- **Phase:** task / free, judged per event by `EF.FREE`.
- **Time:** the recency window (all / last 30 s / last 10 s) and the range brush. Events outside the brush are dimmed and not pulsed.
- **Text search** (`/`):
  - Case-insensitive substring over node labels, names, titles, previews, event labels, and, once the detail bundle has loaded, post bodies, commands, outputs and reasoning.
  - Hits get a ring and forced tags, plus a count with ↑/↓ cycling, which seeks to each hit event and focuses its node.
- **Mode:** dim (15% alpha, the default) or hide.
- **Interface, population and reps** are run-level facets in the swarm selector (`RunQuery`), filtering the run list.
- **Applying the mask.** The mask is recompiled only when the state changes. Nodes that are filtered out dim or hide in 3D. Events disappear from the feed, previews, `,`/`.` stepping and the timeline markers (greyed).
- **URL state:** `#run=…&t=23.4&rate=1&mode=worldline&sel=a0&f=<encode()>`, written with `history.replaceState` at most 4 times per second.
- **Reset:** "Reset filters", and the badge shows `mask.activeCount`.

---

## 11. Swarm selector (Builder C, `ui/selector.ts`)

- **Layout:** model chips (radiogroup "Model", labels from `index.models`, only when more than one model has listed runs), then goal chips (radiogroup "Goal": Solvable / Unsolvable, only when both goals have listed runs, §4.11), then iface chips `[forum][wiki][cache][irc]`, then `[solo][swarm·4]`, then rep pills `r0 r1 r2`, then a caveat badge (⚠ IRC inbound broken, linking to the caveat text).
- **Model and goal scope:** the iface, pop and rep chips list the current run's model and goal only (a run of an unlisted model shows the primary model's chips). A model chip keeps the current goal, iface, pop and rep where that model has them, then the same pop, then its first run, and falls back to the model's other goal if it has none of the current one. A goal chip keeps the model where it has runs of that goal, else it moves to the first model (primary first) that does. `RunQuery.model` and `RunQuery.goal` filter like `iface` and `pop`. The hover card shows the model label when there are several models.
- **Card on hover or focus:** blurb, density sparkline and stats. **Hovering or focusing also prefetches** the core bundle (`loader.prefetch`).
- Picking a run aborts the in-flight load (`AbortController`) and keeps the camera orientation when the iface stays the same.
- Hidden runs only open via `#run=`.
- Keyboard: arrow keys move within a radio group, and Enter selects.

---

## 12. Panels, storytelling and accessibility (Builder C)

### 12.1 Hover tooltip (`ui/tooltip.ts`)

- **Agent:**
  - names, model, status and phase chip, step `k/40`, model calls
  - tokens to t (prompt / completion / reasoning), board steps to t, rolling board share, solved at t (step s)
  - the last command (one line, monospace) with its exit code, and the last reasoning snippet (2 lines, 160 characters; from detail once loaded, otherwise the core label)
- **Thread:** title, created by, posts at t, readers to t (chips with counts).
- **Post:**
  - author, with a persona or agent badge
  - "posted 5 h before run" or the t
  - preview (240 characters) with leak spans highlighted
  - ▲ upvotes at t, reads, replies, mentions
  - flag chips (LEAK in red)
- **Task:** solved by t, submissions, task steps per agent.
- **Board:** requests to t, by kind.
- **Trail dot:** step n, cat, duration, the command, the first 6 output lines, exit code, and `via`.
- **Social edge:** kind, src → dst, fires to t, Σw, and the evidence text (inferred, ambiguous, blind, listing).

### 12.2 Detail panel (`ui/detail.ts`)

- **Placement:** docked on the right on desktop, a bottom sheet under 720 px.
- **Agent:** stats, a board-share sparkline (`series.boardShare` with the cursor), a step ribbon coloured by cat (click a step to seek), posts, metrics (first contact, share after contact, episodes, leak), and prev/next event-of-node buttons.
- **Thread:** the thread **as it was at t** (`model.threadAt`): seed posts plus live posts ≤ t, upvotes so far, "new" badges for posts under 30 s old, and flag chips.
- **Post:** the full body (detail bundle) with leak spans, the reader list with seek links, and the edges.
- **Step:** the full command, output head and tail with a "model saw the first `outputChars` chars" note, and the reasoning of the linked call.
- **Buttons:** "Focus", "Only this neighbourhood", and close (Esc).

### 12.3 Feed rail (`ui/feed.ts`)

- On wide screens (> 960 px) a floating card over the stage's right edge (`clamp(280px, 24vw, 360px)`), collapsible. Below that it stacks under the stage.
- Virtualised list of `<button>` rows: `0:13.2 ● a2 GET /api/v1/threads`.
- The anchor row is the last visible event ≤ t. It auto-scrolls unless the user has scrolled within the last 3 s.
- Clicking a row seeks, selects and focuses the node.
- A **"Text view"** toggle expands it to full width. This is the accessible alternative to the canvas, and the primary view when there is no WebGL.

### 12.4 Captions and tour (`ui/captions.ts`, `player/tour.ts`)

- The lower-third caption shows the latest p1 chapter within the last 6 s of t.
- Captions are mirrored to an `aria-live="polite"` region, throttled to one announcement per 3 s.
- While narration plays, the current spoken cue replaces the chapter caption, with a speaker label (`Guide` / `Expert`). Each cue is announced once, unthrottled. The CC toggle controls both.
- On each p1 beat during playback, the tour calls `engine.focusNode(node, { nudge: 0.35, ms: 900 })`, unless the user touched the camera in the last 5 s.
- Any camera interaction disables nudges for the session: orbit, pan, pinch, wheel / ctrl-wheel / Safari-gesture zoom, double-click focus, canvas camera keys, reset view, and focus requests from the detail panel, node list and "show me" links (engine `cameraInteract`).

### 12.5 First 10 seconds and end card

1. When the player is at least 50% visible, the `featured` run autoplays at 1x with skip-quiet on. This does not happen under reduced motion or save-data.
2. The captions tell the story.
3. **Under reduced motion** the player opens paused at `swarmOnsetMs` (or the first post), with that caption shown.
4. **End card** (`ui/endcard.ts`), YouTube style: Replay · "Same swarm on a ‹sibling iface› →" (same model, pop and rep, next iface) · "Next swarm" (the next listed run of the same model) · "See the numbers ↓".

### 12.6 HUD (`ui/hud.ts`)

A small top-left overlay in the stage:
- `on board 3/4 · live↔live 2 · solved 2/4`
- a swarm-onset tick once reached

### 12.7 Node list (`ui/nodeList.ts`)

- A visually hidden (`.sr-only` until focused) `role="listbox"` that mirrors the visible nodes, ordered by kind and label.
- Arrow keys move through it, and the focused option is highlighted in 3D (`engine.highlight`). Enter pins, `f` focuses the camera.
- The canvas has `role="img"` and an `aria-label` summary, updated at most every 2 s and only while paused, e.g. "forum-multi4-r1 at 0:32, 4 agents, 2 solved, 3 live posts, swarm a0 a2".

### 12.8 Accessibility rules

- Every control is a real `<button>`/`<input>` with an `aria-label`.
- `:focus-visible` shows a 2 px accent outline.
- Text contrast is at least 4.5:1, with tabular numerals.
- Colour is never the only encoding: shapes, dashes, text labels and LEAK chips back it up.
- **`prefers-reduced-motion`:** no autoplay, inertia, drift, bob, nudges, tweens or travelling pulses (flashes instead), and the controls never auto-hide.

---

## 13. Landing page (Builder C)

### 13.1 Structure (`public/index.html`)

The page is dark-first, with a light theme via `prefers-color-scheme` and a toggle. It uses a system font stack, and icons come from an inline SVG sprite.

1. **Top bar:** `swarmpit` wordmark; links Watch · How it works · Findings · Safety.
2. **Hero:** the player is the centrepiece. On wide screens (> 960 px) the graph fills the whole first screen, under the translucent top bar, and the copy floats top-left in a card. On narrower screens the copy sits above the stage.
   - **H1:** "Watch AI agents swarm a honeypot."
   - **Tagline:** "swarmpit drops autonomous LLM agents into sandboxed tasks next to a message board they can't resist, then records how they find it, linger, and start talking to each other."
   - **Legend:** ● agent · ◆ thread · ⬢ CTF vault · ○ bait persona · ▪ post.
3. **Player:**
   - Wide screens: the stage is full-bleed at 100svh. The copy and selector cards (left column) and the feed card (right) float over it with translucent, blurred backgrounds. `player.ts` measures what they cover and passes the insets to the camera (`setInsets`, an off-axis lens shift). The swarm is centred and framed in the uncovered area: beside the left column or below it, whichever is larger. The same insets are exposed as `--ov-l/t/r/b` so the caption, hint, HUD and detail panel stay in the free area.
   - Narrower screens: selector above the stage, feed below it.
   - Timeline at the bottom.
   - Under 720 px the stage is 62svh and full-bleed.
4. **What you're seeing:** three cards (Agents drift toward what attracts them · The board is bait · Lines are reads, posts and replies). Each has a "show me" button that seeks to the relevant chapter and focuses its node.
5. **How it works:** four steps with inline SVG icons:
   1. microVM sandbox per agent
   2. CTF task with a breadcrumb to the board
   3. solve and free time
   4. we measure discovery, stickiness and swarm edges

   It also notes the four board styles: forum, wiki, artifact cache and IRC.
6. **Findings** (`ui/findings.ts`, from `index.findings`):
   - Three stat tiles, computed from the data with no hard-coded numbers.
   - An inline-SVG dot+CI chart per iface for `contacted`, `posted` and `board_steps_free`, with solo and multi side by side. It follows the dataviz rules: iface colour plus a text label, CI whiskers, and tabular numbers.
   - It is labelled "pilot snapshot" and shows `findings.note` and the caveats: IRC inbound broken (excluded by default), heuristic attribution, n.
   - **Model toggle** (radiogroup "Model", hidden for a single model, default the primary model). Tiles, charts and the note (`findings.notes[model] ?? findings.note`) follow the selected model; the table view lists every model with a model column.
7. **Safety:** fully sandboxed; secrets are synthetic per run; traces are redacted (with the rule list from `generator`).
8. **Footer:** data version (`generated`), contact and licence.

### 13.2 CSS

- `css/site.css` holds tokens and page sections. `css/player.css` holds the stage, timeline, panels and tags.
- The palette mirrors the roles in `dash/web/src/theme.css`:
  - series-1..8: dark mode `#3987e5 #d95926 #199e70 #c98500 #d55181 #008300 #9085e9 #e66767`
  - page `#0d0d0d`, surfaces `#1a1a19`, `#222220`, `#2c2c2a`
- Agents use slots 1..8. Edge kinds: exposure neutral, reply slot 1, mention slot 2, dm slot 3, upvote slot 7, live-live accent `#f5c542`, leak `#e66767` with a ring.
- `engine/palette.ts` reads the same CSS variables, so canvas and DOM match.

### 13.3 Boot (`main.ts`)

1. `detectEnv()`.
2. Render the static sections. The stage shows the poster.
3. `loader.index()`. Then `readUrl()`, or `index.featured`.
4. `createPlayer`. `load(id)` runs:
   - core → `buildModel` → `engine.setRun` → timeline, feed, captions and detail rebind
   - restore t and filters from the hash
   - then `loader.detail()` with progress going to `timeline.setBuffered`, then `model.attachDetail`
5. Wire the events as in proposal 2 §7.8:
   - clock tick → timeline, feed, captions, HUD, detail, URL
   - filters → engine, feed, timeline
   - engine hover → tooltip and activity strip
   - engine select → detail
   - `seekRequest` → clock
6. IntersectionObserver autoplay.
7. **Errors:** a `BundleError` shows an inline message with Retry. A version mismatch shows "data format updated, reload".

---

## 14. Performance budget

| item | budget |
|---|---|
| JS (sum of `public/js`, uncompressed) | ≤ 150 KB (target about 110 KB: engine 45, ui 40, data/player 20, util 5) |
| CSS | ≤ 30 KB |
| First paint (hero copy, poster, sections) | no JS needed |
| Player interactive | `index.json` (≤ 256 KB) plus one core bundle (multi4 about 40–80 KB raw) |
| Per frame at 500 nodes, 2k trail dots, 3k edges, 5k events (16.6 ms) | computeFrame ≤ 2 ms; position texture upload ≤ 8 KB; ≤ 8 draw calls; labels ≤ 2 ms at ≤ 30 Hz; timeline DOM ≤ 0.3 ms |
| Mask compile (on filter change) | ≤ 5 ms at 5k events |
| Memory | typed arrays on the hot path; zero allocations per frame |
| Idle | no rAF when nothing is dirty |
| Frame rate | 60 fps on `_synth-500` with WebGL2 on an M-series laptop; ≥ 30 fps with Canvas2D at real-run sizes |

---

## 15. Acceptance checklist

**Build and safety**
- [ ] `npm run build` (tsc only) succeeds with strict settings and zero errors. `public/js/main.js` loads with no 404s from `node tools/serve.mjs` and from `python3 -m http.server -d public`.
- [ ] There is no `dependencies` key in `package.json`. No external requests happen at runtime (verified in the devtools network log), and there are no bare or extensionless imports.
- [ ] `python3 tools/check_public.py public` passes. `--selftest` catches every planted leak and leaves the bare word "user", 40-hex SHAs and sandbox IPs alone.
- [ ] `python3 tools/test_redact.py` passes. Nothing in `public/` contains `/Users/`, `krister`, an email, a key, or a non-sandbox IP.
- [ ] No cfg, relay, `*.out`, pilot log, board_url or port data in the bundles. CTF values appear only where agents wrote them. `--mask-ctf` masks them consistently everywhere and keeps the LEAK flags.
- [ ] Budgets hold: JS ≤ 150 KB, core ≤ 400 KB, detail ≤ 600 KB.

**Data fidelity**
- [ ] The export runs on the real runs with every parity assertion passing (§4.1.6). `findings.mismatches` is reported and `findings.source` is correct.
- [ ] For forum-multi4-r1, the UI shows 2 live posts, 3 upvotes, 4 agents and solve times matching `obs` / `mat_agent_metrics`, and `tEnd` matches `runs.t_end`. Spot-check 3 events in the feed against the raw `steps.jsonl` and `board_events.jsonl` lines.
- [ ] irc runs show the caveat badge, and hidden runs are not listed.

**Player and timeline**
- [ ] The selector switches runs. Hovering prefetches (a network request is visible). A switch during a load aborts the old one.
- [ ] Play/pause, every speed from 0.25 to 16, skip-quiet (the 13 s boot gap passes in about 1.5 s), click-to-seek, drag-scrub with the hover time and event preview, buffered fill filling as the detail bundle loads, chapter segments and markers, and a `m:ss.s / m:ss` display.
- [ ] Every key in §9.2 works and none fire while typing in search. Auto-hide behaves per §8.3. Touch double-tap on the left or right third seeks ±10 s.
- [ ] Determinism: `tools/test/engine.test.mjs` shows `computeFrame` reached forward and backward is identical at 50 random t, and the layout is byte-identical across two runs.

**3D**
- [ ] Orbit, pan (right-drag, shift-drag, two-finger), wheel zoom toward the cursor, ctrl-wheel and trackpad pinch, touch pinch with no jump when one finger lifts, Safari gesture events, inertia, double-click focus, reset.
- [ ] Cooperative hint: a plain wheel scrolls the page until the stage is engaged. Esc disengages.
- [ ] Nodes have SDF shapes by kind with the pixel clamp. Edges have kind dash patterns and reciprocal bend. Pulses travel on fires. Info tags are collision-free and depth-faded, and posts are labelled only at ≥ 14 px.
- [ ] Hover tooltips for every node kind, trail dots and social edges. Click pins the detail panel, and the thread-at-t view is correct while scrubbing.
- [ ] Filters for node type, agent, neighbourhood, event kind, edge kind, phase, recency window, range brush, text search with ↑/↓, and dim/hide. The URL hash round-trips.
- [ ] Agents drift toward the board as they engage. Live-live partners huddle (cohesion), and the hull ring and `swarm · …` tag appear at onset. The HUD updates.
- [ ] Worldline mode (`v`) lerps over 600 ms. Nothing after t is drawn. Pillars, chords and the now plane are correct. Follow-now works.
- [ ] Context loss (`WEBGL_lose_context`) recovers without a reload. `?renderer=2d` renders both modes. With no WebGL or Canvas, the poster and feed text view show.
- [ ] `#run=_synth-500&perf=1` holds 60 fps (p95 ≤ 16.7 ms) with WebGL2. The dirty-only loop is idle while paused (no rAF in the profiler).

**Landing and accessibility**
- [ ] Hero, player, what-you're-seeing cards with working "show me", how it works, findings from `index.json` with caveats, safety, footer. Dark-first, with light mode.
- [ ] Reduced motion: no autoplay, pulses or inertia, and the player opens paused at onset. Focus rings everywhere. The slider has `aria-valuetext`. Captions go to aria-live. The node listbox works with the keyboard alone. The canvas has role=img with a summary.
- [ ] Headless screenshots at 1440×900 and 390×844 show no overflow, the controls reachable, and tags readable.
