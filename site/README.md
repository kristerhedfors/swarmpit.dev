# swarmpit.dev

swarmpit.dev is the public landing page for the Schizbox/swarmpit research: LLM agent swarms that latch onto bait message boards (forum, wiki, artifact cache, IRC) placed next to a CTF task. Its centrepiece is a **replay player** for real recorded runs:

- **Swarm selector:** pick board interface × population (solo / swarm of 4) × repetition.
- **YouTube-style timeline:** play/pause, speed 0.25×–16×, click/drag scrubbing with hover time + event preview, buffered/progress fill, chapter markers, skip-quiet, auto-hiding controls, touch support.
- **3D swarm graph:** hand-written WebGL2 with a Canvas2D fallback. Orbit, pan, wheel/pinch zoom, inertia, double-click to focus, collision-aware info tags, hover tooltips, click-to-pin detail panel, filters, and a worldline (time-as-height) mode.

The graph state is a pure function of playback time, so scrubbing backwards gives exactly what playing forwards did.

The stack is plain HTML + CSS + TypeScript compiled by `tsc` to browser ES modules. There are **zero runtime dependencies**: no framework, no bundler, no CDN, no external requests. `typescript` is the only devDependency. `public/` is the deploy root.

## Quickstart

```sh
cd site
npm install                 # typescript only
npm run export -- --synth   # ../runs -> public/data (read-only on runs/); --synth keeps the _synth-500 stress bundle
npm run build               # tsc (strict) -> public/js
npm run serve               # http://127.0.0.1:8080  (node tools/serve.mjs public <port>)
npm test                    # node --test tools/test/ (needs build + data; also compiles the dev harness)
npm run check               # tsc --noEmit + public gate (leaks, sizes, import specifiers, external URLs)
```

Python tests for the exporter are `python3 tools/test_export.py`, `python3 tools/test_redact.py`, `python3 tools/redact.py --selftest` and `python3 tools/check_public.py --selftest`.

`npm run export` needs `duckdb`. It reuses the observatory's ingest (`../dash/server`) into a scratch database at `.cache/obs-export.duckdb`. When duckdb isn't importable it re-runs itself with `../dash/server/.venv/bin/python`, falling back to `uv run --no-sync`. It never writes into `dash/`, `lab/` or `runs/` and never contacts the VMs.

### Useful URLs

| URL | Effect |
|---|---|
| `#run=forum-multi4-r1&t=23.4&rate=2&mode=worldline&sel=a0&f=…` | Deep link to a run state. The hash is rewritten as you play; links with `t` or `sel` open paused. |
| `?renderer=2d` | Forces the Canvas2D fallback |
| `?perf=1` | Perf overlay: frame ms p50/p95, draw calls, counts |
| `#run=_synth-500` | Hidden 492-node / 5k-event stress bundle (only if exported with `--synth`) |

### Keyboard

| Key | Action |
|---|---|
| `space` / `k` | Play / pause |
| `j` / `l` | −/+10 s |
| `←` / `→` | −/+5 s, also with the timeline focused (`↑`/`↓` or `Shift+←/→` there: ±1 s). A focused canvas uses arrows for the camera only while engaged ("Tap to explore") |
| `,` / `.` | Previous / next visible event |
| `0`–`9` | Seek to that tenth of the run |
| `Home` / `End` | Start / end |
| `<` / `>` | Slower / faster |
| `f` | Fullscreen |
| `v` | Worldline mode |
| `t` | Labels |
| `c` | Captions |
| `n` | Narration (voice + sound; only on runs that have it) |
| `r` | Reset view |
| `/` | Search |
| `?` | Help |
| `Esc` | Unpin / close |

With focus outside the player, `Space`, arrows and `Home`/`End` keep scrolling the page until you have interacted with the player.

### Engine dev harness (not deployed)

`npm run demo` compiles the whole of `src/`, including `src/engine/dev/` (synthetic model + demo), to `.cache/dev/js`. It then serves `tools/dev/engine-demo.html` at http://127.0.0.1:8081, with options `?run=<id>`, `?renderer=2d`, `?perf=1` and `?agents=&threads=&personas=&budget=&dur=`. `tsconfig.json` excludes `src/engine/dev/**`, so none of this ever lands in `public/`.

## Layout

| Path | Contents |
|---|---|
| `src/types.ts` | Shared contracts (bundle types, code tables, module interfaces) |
| `src/data/` | Loader (hash-checked, progress = buffered fill), in-memory loader for the browser lab (`memoryLoader.ts`), run model (pure queries in `t`), filters |
| `src/convert/` | Raw run dir -> player bundle in the browser or node (TS port of the obs ingest/derive and `tools/export.py`), used by the browser lab |
| `src/player/` | Clock, player wiring, keys, URL state, tour, narration (`timemap.ts`, `narration.ts`) |
| `src/ui/` | Timeline, selector, filters panel, tooltip, detail panel, feed, captions, HUD, findings, help |
| `src/explore/` | Instigation landscape explorer (`explore.html`): data layer, URL state, views |
| `src/engine/` | Layout, frame computation, camera, gestures, picking, labels, WebGL2 (`gl/`) and Canvas2D renderers |
| `tools/` | `export.py`, `redact.py`, `check_public.py`, `synth.py`, `serve.mjs`, `convert_parity.mjs` + `convert_parity_py.py`, tests, `dev/` harness |
| `public/` | Deploy root: `index.html`, `404.html`, `css/`, `js/` (built), `data/` (exported), `CNAME`, `.nojekyll`, `robots.txt` |
| `SPEC.md` | Binding design document |
| `NARRATION.md` | Narration track: pre-generated voice + event sound effects, one mp3 per run |

## Browser lab embedding (lab.swarmpit.dev)

The browser lab (`lab/web`, design in `docs/browser-swarm.md` §5/§7) embeds this player to play back swarm traces
recorded in the visitor's browser. Three pieces here serve it. The player's defaults on swarmpit.dev are unchanged;
the only change to the public site is the nav link to the lab, so deploy the lab (image, then page) before the site.

- **`src/convert/`** (`index.ts`, `parse.ts`, `ingest.ts`, `derive.ts`, `classify.ts`, `metrics.ts`, `build.ts`,
  `py.ts`): a zero-dependency port of `dash/server/obs/{parse,ingest,derive,classify}.py`, the `v_agent_metrics` /
  `v_run_metrics` views and `tools/export.py` (`RunBuilder`, chapters, segments, series, `make_card`, the core
  hash). `convertRun(files, {maskCtf?, live?, runId?, hidden?})` turns a `Map` of run-dir-relative paths to bytes
  (orchestrate `runs/<id>/` or the browser swarm zip) into `{card, core, detail}` that pass `assertCore` /
  `assertDetail`; `runFilesFromEntries()` strips a `<run_id>/` or `runs/<run_id>/` prefix from zip entries.
  `live: true` tolerates a partial run. Only the `--mask-ctf` rule of `redact.py` is ported, so converted bundles
  are for local playback, not for `public/data`. It compiles to `public/js/convert/` with the rest of `src/`.
- **`src/data/memoryLoader.ts`**: `createMemoryLoader({index, resolve?})` is a `Loader` over in-memory
  `{card, core, detail}` entries (`set`, `delete`, `has`, `cards`), with the same validation, hash check and abort
  handling as `data/loader.ts`; `resolve(card, signal)` can supply entries lazily (the lab re-converts a stored
  zip). `memoryIndex(cards)` builds an empty-findings index.
- **`PlayerOpts`** embedding options (`src/player/player.ts`; every default is the swarmpit.dev behaviour):
  `narration: false` (no narration controller or `<audio>`), `urlKey` (`"trace"`: only `#trace=<id>` in the hash,
  other keys untouched; `null`: no URL sync), `findings: false` (no "See the numbers" end-card action),
  `onNew` / `onUpload` / `onDownload` / `onDelete` (selector action chips, each shown only when given), `local`
  (the selector's Local lane: `match`, `title`, `label`) and `graphKeyOpen`. The player also gains `addRun`,
  `updateRun` (in-place reload keeping time, play state, camera, filters and pin, for a live trace) and
  `removeRun` (empties the player when the last run goes).

Tests: `tools/test/convert.test.mjs` (synthetic run dirs, placeholder text) and `tools/test/memoryLoader.test.mjs`
(memory loader, scoped URL sync, Local-lane helpers) run under `npm test`. Structural parity with `export.py`:

```sh
npm run build && (cd ../dash/server && uv sync)
node tools/convert_parity.mjs              # dash/server/tests/fixtures/runs + runs/ (this checkout or the main one)
node tools/convert_parity.mjs --fixtures   # fixtures only; also --runs, --max N, or explicit DIRs
```

`convert_parity.mjs` runs `convert_parity_py.py` in the dash venv (scratch obs ingest + `RunBuilder`); both sides
reduce their bundles to the same numbers-only projection (counts, times, indices, flags, enum codes, sha1
prefixes of ids). It prints only run ids, PASS/FAIL, counts and field names, never run content, and exits 1 on any
FAIL.

## Instigation landscape explorer (`explore.html`)

`public/explore.html` is a second page, linked from the top nav. It lets you browse every candidate-seeded study episode in `data/instigation/explorer.json` (schema `instigation.explorer.v1`: numbers, fixed enums and opaque ids only). The page has the same zero-dependency stack as the replay. Its sources are in `src/explore/` and compile to `public/js/explore/`, and its styles are in `public/css/explore.css`.

- **Filters:** one multi-select chip group per dimension: goal, model, channel, seed length band, trigger code, seed origin, generation and writers > 0. Each chip shows how many episodes it would match given the other filters.
- **Pivot:** any dimension × any dimension, with one metric per table: episodes, writer-episode rate or pile-in rate (Wilson 95% CI), mean or max instigation score, mean or max writers, or mean engagement. Results show as a heatmap table with counts and "All" margins. Click a cell to list its episodes.
- **Landscape:** the MAP-Elites grids per goal × model (or best across models). Rows are trigger codes and columns are channel × length band. Click an elite to open its episode.
- **Episodes:** a sortable, paginated table with every field, plus a CSV download of the filtered set. A row opens a detail panel with a posts-over-time sparkline, engagement, per-agent codes, the classifier panel and the seed lineage.
- **Lineage:** the seed family tree, one column per generation. Each node is coloured by the seed's best score.
- **Models:** writer-episode or pile-in rate per model per goal, with Wilson intervals. Retired models (GPT-OSS 120B) are marked.

The whole view state lives in the URL hash, for example `explore.html#v=pivot&rows=channel&cols=length_band&goal=unsolvable&m=wrate`, so a link reproduces the view. The client re-validates the JSON (id, enum and finite-number whitelists) and drops malformed rows. It renders text only through `textContent` and attributes. `tools/test/explore.test.mjs` tests the DOM-free data layer (`src/explore/data.ts`, `state.ts`) against the synthetic fixture in `tools/test/explore-fixture.test.mjs`. To preview the page without real data, write `makeFixture()` from that file to `data/instigation/explorer.json` in a scratch copy of `public/`.

## Deploying (swarmpit.dev or any static host)

1. Run `npm ci && npm run export -- --synth && npm run build && npm test && npm run check`.
2. Publish **`public/` as-is**. It contains only static files with relative URLs. `404.html` sets a `<base>` at load time: `/<repo>/` on `*.github.io` project sites, `/` everywhere else, so nested missing URLs still get styles.
   - **GitHub Pages (wired up):** `.github/workflows/pages.yml` builds on every push to `main` that touches `site/**` (or by manual dispatch). It runs `npm ci`, `tsc`, `npm test` and the leak gate (`check_public.py --no-budget --allow tools/ctf-allow.json`), then deploys `site/public`. CI does not run the exporter, because `runs/` is not in git: run `npm run export` locally and commit `public/data/`.
     One-time setup: Settings → Pages → Source **GitHub Actions**. The site first appears at `https://<user>.github.io/<repo>/`. For swarmpit.dev, set the custom domain in Settings → Pages (`public/CNAME` already says `swarmpit.dev`), point DNS at Pages (apex `A` 185.199.108–111.153, `AAAA` 2606:50c0:8000–8003::153, or `ALIAS`/`CNAME` to `<user>.github.io`), then tick *Enforce HTTPS*.
     After re-exporting, refresh the allow list with `cp .cache/ctf-allow.json tools/ctf-allow.json`. It holds the synthetic CTF values that are shown on purpose.
   - **Cloudflare Pages / Netlify / S3+CDN:** set the build output directory to `site/public`. No server logic or rewrites are needed. The `404.html` is picked up automatically on Pages/Netlify.
3. Caching: data bundles are content-addressed (`<id>.<hash8>.core.json`), so they can be cached immutably. `index.html`, `js/`, `css/` and `data/index.json` should be revalidated (short max-age or `no-cache`).
4. MIME: `.js` must be served as `text/javascript` (ES modules). Every mainstream static host does this.

## Narration (optional)

Some runs have a pre-generated narration track: one static mp3 per run with two voices (a guide for play-by-play, an expert for analysis) and quiet event sound effects, plus a small manifest under `public/data/narration/`. The headphones button or `n` turns it on (never autoplayed). While it plays the audio is the clock: the run holds or slows while a line is spoken and speeds through quiet stretches, so speed and skip-quiet are disabled. Spoken lines appear as captions with a speaker label. Runs without narration hide the button. Design and pipeline: `NARRATION.md`.

## Data bundles (summary; full spec in SPEC.md §4)

- **`data/index.json`** (`IndexBundle`) has the format version, generator info (redaction counts, truncation limits), the featured run, the run cards and the findings. Run cards carry id, iface, pop, rep, duration, a 64-bin event density, file names for core/detail, `hidden` and caveats. Findings come from `runs/analysis.json`, cross-checked against the observatory database.
- **`data/runs/<id>.<hash8>.core.json`** (`swarmpit.run/1`) is loaded when a run is selected. It holds the run metadata and harness, agents (model, start/end/solve times, metrics), nodes (task, board, agents, threads, posts, personas, handles), unique edges `(src,dst,kind)`, and the columnar `events` and `fires` arrays with interned `strings`. It also holds chapters (first contact, solves, leaks, posts and so on) and segments (task → free phases). Events are sorted by `(t, lane, kindRank, seq)`, as in the observatory CONTRACT.
- **`data/runs/<id>.<hash8>.detail.json`** (`swarmpit.detail/1`) is lazy-loaded after the core renders and drives the buffered fill. It holds per-event detail items (command, output head/tail, reasoning, HTTP requests, IRC lines) and full post bodies with leak spans. Its `hash` must equal the core hash.
- **Redaction:** every string passes `tools/redact.py` (host paths, usernames, emails, API-key-like strings, non-sandbox IPs, LAN hostnames). Long fields are truncated at whitespace. CTF values are synthetic and kept unless you pass `--mask-ctf`. `tools/check_public.py` gates the whole of `public/` and the export aborts on any hit.

## Adding new runs

1. Let the lab write the new run directory into `../runs/<run_id>/` (`run.json`, `board_events.jsonl`, `aN/steps.jsonl`, and so on). Only runs with status `done` and an empty tag are listed. Tagged, smoke and archive runs need `--include-tagged` and are then exported as hidden.
2. Re-run `npm run export -- --synth`. It rebuilds the scratch database, regenerates all bundles and the index, and runs the parity assertions against the raw logs: step counts, post totals, board-event line counts, end time and agent metrics. It also runs the public gate. Unchanged runs produce byte-identical files and hashes. Files that are no longer listed get pruned, so always pass `--synth` if you want to keep the stress bundle.
3. If the new runs update the pilot analysis, regenerate `runs/analysis.json` with the lab's `analyze.py` first. The exporter cross-checks every cell against the observatory database and reports mismatches in `findings.mismatches`.
4. Run `npm test` and spot-check the run in the player (`#run=<run_id>`), then deploy `public/`.
5. To feature a different run on first load, pass `--featured <run_id>`. The default is the highest-scoring multi-agent run.
