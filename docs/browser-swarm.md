# Browser swarms: design and cross-module contract (as built)

**Goal.** Run a full schizboard swarm (board + N agents, N = 1..4, the same shape as the macbook
`<iface>-multi4` microsandbox runs) entirely in the visitor's browser. The only internet dependency is the
visitor's OpenAI-compatible API key (plus the static page, the CheerpX runtime and the VM image). The
resulting trace is converted, visualised and played back in the page with the swarmpit.dev replay player
(no narration). Full swarm traces can be downloaded and uploaded as zips; local traces persist in IndexedDB.
There is no login.

This file is the design and the contract between the four modules below, updated to what was built (deviations
from the first draft are marked **as built**). `lab/browser/PROTOCOL.md` stays the byte-level guest <-> page
contract (the swarm roles are its §9, the page side of a swarm its §9.5); where the two differ, PROTOCOL.md wins.

## 1. Topology: `board-split`

```
page (lab.swarmpit.dev, top window)
 ├─ fabric (js/fabric.js): one page-owned network, a private loopnet per VM, explicit routes
 ├─ iframe vm board  (vmframe.html?vm=board)  CheerpX VM: labctl, role "board" -> board on 9000..9000+N-1
 ├─ iframe vm a0     (vmframe.html?vm=a0)     CheerpX VM: labctl, role "agent" -> setup_vm, vaultd, relay, harness
 ├─ ...
 └─ iframe vm a{N-1}
     brokers: one per agent VM (own mailbox key, own outbox/inbox), one shared call/token budget
```

- One same-origin iframe per VM is required: two `Linux.create` in one JS realm corrupt each other
  (docs/browser-two-vm.md T1/T5). Each frame (`vmframe.html` + `js/vmframe.js`) imports its own CheerpX module
  and exposes only `window.__vmframe.boot()`; removing the iframe is how the page ends a VM (there is no
  SIGKILL inside CheerpX). The frames are 0x0, `aria-hidden`, and the CSP allows `frame-src 'self'` and
  `frame-ancestors 'self'` (`public/_headers`).
- The role comes from the sealed spec's `role` field (PROTOCOL.md §9), not a command-line flag: every VM runs
  the same `cx.run` of `labctl.py` (PROTOCOL.md §2).
- Agent i's relay connects to `host.microsandbox.internal:<9000+i>` (hosts entry -> 127.0.0.1). The fabric
  routes **only** port `9000+i` from VM `a{i}` to VM `board`; everything else stays private to its VM, and a
  routed port always goes to the board even if the agent VM listens on it itself (a root agent cannot shadow
  the board). This mirrors the microsandbox egress rule `allow@host:tcp:<19000+i>`.
- `fabric.cut(vm)` ends every cross-VM connection touching the VM on both ends (each side reads EOF), refuses
  later routes from or to it, and drops the page's last reference to that VM's loopnet.
- Agents can reach each other only through the board. Root inside an agent VM reaches only that agent's own
  CTF and logs (the same accepted weak-uid preview limitation as the solo lab); it cannot reach the board VM's
  files.
- Runs carry `params.env = "cheerpx"`, `params.topology = "board-split"`, `params.isolation = "weak-uid"`,
  `params.preview = true`, `params.solve_verified = false`, so obs keeps them out of default roll-ups.
- Run id: `browser-<iface>-(solo|multi<N>)-<UTC YYYYMMDDHHMMSS>-<4 hex>` (`solo` iff N = 1; `protocol.js`
  `makeRunId`, `RUN_ID_RE`).
- **As built:** the page uses `board-split` for every N, including N = 1. The legacy co-resident single-VM solo
  mode (a spec with no `role`) is still implemented by labctl and covered by `lab/tests/test_labctl.py`, but the
  page no longer starts it (`js/session.js` and `js/vm.js` are no longer imported by the page).

## 2. Run lifecycle (page side, `js/swarm.js`)

1. **"+"** (a chip in the player's selector, or the empty-state button) opens the new-swarm dialog. It asks for
   the API key; provider preset, base URL and model sit next to it, and board interface, agents (default 4),
   tool-call budget per agent (5..40, default 40), token cap per agent (default 1 000 000; the shared swarm cap
   is this times N, as the call cap) and the compatibility switches are under "options". The cost-consent box
   must be ticked. Start is disabled with a fixed reason while the page is not cross-origin isolated, a swarm is
   running, the form is incomplete, or the base URL's host is not one the page's CSP allows (a preset or
   localhost; `settings.js providerAllowed`, checked against `_headers` by `test/settings.test.mjs`).
2. Storage: the tab takes the Web Locks of the run and of every VM, then deletes unowned stale VM databases
   (PROTOCOL.md §1; CheerpX lists them as `cjFS_/<name>/`). It fetches `/vm-manifest.json` (image name) and `/data/seed.json`.
3. Boot the board frame, seal its spec (`role: "board"`, `n_agents`) and the seed into its inbox, start labctl.
   labctl starts the board on ports `9000..9000+N-1`, draws the CTF once (`new_ctf()`), and writes
   `/root/export/board-ready.json` `{"v":1,"run_id","ctf":{...},"started":"<iso>","ports":[...]}`. The page polls
   it (up to 240 s).
4. Boot `a0..a{N-1}` one at a time: the next boot starts once the previous VM's boot settled, plus 3 s.
   Each gets a fresh mailbox key, a sealed spec with `role: "agent"`, `agent_id: i`, `n_agents: N`, the shared
   `ctf` from board-ready and `relay_host_port: 9000+i`, and its own broker (`agent: i`) on the shared budget.
   All agents share one CTF, as in orchestrate.
5. Agents run concurrently. Every 2 s the page reads each VM's `status.json`, the new part of each agent's live
   `a{i}/steps.jsonl` (new complete lines go to `onRecord`) and the board's live `board_events.jsonl` mirror; a
   tick while the previous poll is still reading is skipped (a wedged VM cannot build a backlog). Agent-writable
   files are read only below a size cap (64 KiB status/manifest, 1 MiB small JSON, 64 MiB logs; above it the
   agent ends `E_EXPORT_SIZE`). Every 10 s the lab page re-converts the partial trace (`liveFiles()`) and shows it
   in the player as a live run; at most every 60 s it also stores that partial trace in the library (flagged
   `partial`), so a crashed or reloaded tab keeps what the run produced so far.
6. When an agent's labctl exits, the page reads that agent's export (status, manifest, every `run/` file) into
   page memory, closes its broker, removes its iframe and calls `fabric.cut("a{i}")` (fixes two-vm review R1).
   After Stop every broker answers `E_ABORT` and a VM boot in progress is abandoned (that agent is `skipped`).
   Each agent has its own page deadline: its labctl start + `harness.run_timeout_s` + 15 min, but at most the
   board's own deadline (board labctl start + `run_timeout_s` + labctl `BOARD_MARGIN_S` 30 min) less 5 min; at
   its deadline only that agent's broker answers `E_ABORT`. An agent whose labctl has not exited 120 s after Stop
   or its deadline (extended by up to 180 s while its live status says `exporting`; the grace starts when its
   labctl starts if that is after the Stop) is removed the same way (`E_AGENT_LOST`, not an error after a Stop
   or deadline). If the board's labctl ends while agents still run (the board process died), the page stops the
   swarm. Stop during `loading` or the board boot boots nothing more; a board stopped before it was ready gets
   30 s (not 180 s) to exit. Stop once the run is `ending` is ignored.
7. After every agent is gone, the page writes `agent-<i>.meta.json` (each agent's exported `a{i}/agent-meta.json`,
   byte for byte as text; for an agent that started but exported none, a page-made stub with its page-side
   error code, `protocol.js pageAgentMeta`, PROTOCOL.md §9.1) and then `stop` into the board inbox. Board labctl merges them into the final
   `run.json` (orchestrate semantics: `timed_out` = list of agent indices, `fatal` = lowest agent's
   `{agent, status}`, secret exposure summed, plus board-log secret exposure; PROTOCOL.md §9.3), exports the board
   files and exits. The page waits at most 180 s for it.
8. The page assembles one zip in the orchestrate `runs/<run_id>/` layout (§3), stores it in the trace library,
   converts it (§5) and selects it in the player. If storing fails it offers the zip as a plain download
   (`saveRaw`).

Every device read has a 15 s bound, so a wedged VM cannot hang the page.

## 3. Swarm zip layout (= orchestrate `runs/<id>/` + browser extras)

```
<run_id>/run.json                     (board labctl, merged)
<run_id>/seed.json
<run_id>/board.out
<run_id>/board_events.jsonl
<run_id>/board_events.content.chatter.json   (absent if nothing was posted)
<run_id>/cfg-a{i}.json                (no mailbox section; only if agent i was stopped)
<run_id>/a{i}/steps.jsonl  summary.json  FATAL?  harness.out  setup.out  vaultd.out  relay.jsonl  relay.out
<run_id>/a{i}/agent-meta.json         (browser extra)
<run_id>/manifest.json                {"v":1,"run_id","files":[...],"agent_stopped":[bool x N],"topology":"board-split"}
```

A file the page never got from a final export falls back to the live copy (the board's live event mirror, an
agent's live `steps.jsonl`). Name suffixes: `-incomplete` unless the board exported, every agent's export says
`agent_stopped` and the outcome is `done` or `stopped`; else `-aborted-<status>` if `run.json.fatal` is set.
The zip is stored (uncompressed, `js/zip.js makeZip`).

Uploads accept: lab zips (`<run_id>/...`), zips or folders of a macbook `runs/<id>/` (also nested under
`runs/`), and bare run files. `library.js traceFromEntries` finds the single shallowest run directory (marked by
`run.json`, `manifest.json`, `a<i>/steps.jsonl` or `board_events.jsonl`), drops OS litter (`__MACOSX/`,
`.DS_Store`, `._*`), and takes the run id from `run.json`/`manifest.json` `run_id`, else the directory name. A
folder upload is re-zipped as `<run_id>/...` so download returns a lab-style zip; a zip upload is stored as is.

## 4. Module ownership

| Module | Owns | Provides |
|---|---|---|
| G guest | `lab/browser/**` (labctl roles, PROTOCOL.md §9), `lab/tests/test_labctl.py`; `lab/agent/*` only if unavoidable and byte-identical for microsandbox | `labctl.py` role `board` / `agent`, picked by the sealed spec's `role` |
| R runtime | `lab/web/public/js/{fabric,vmframe,swarm,broker,protocol}.js`, `lab/web/public/vmframe.html`, `lab/web/public/_headers`, `lab/web/test/{fabric,broker,protocol,swarm}.test.mjs`, `lab/web/test/helpers.mjs`, `lab/web/spikes/swarm/**` | `createSwarm()` (§6) |
| C converter | `site/src/convert/**`, `site/tools/test/convert*.test.mjs`, `site/tools/convert_parity.mjs` + `convert_parity_py.py` | `convertRun()` (§5) |
| P player + UI | `site/src/player/**`, `site/src/ui/**`, `site/src/data/memoryLoader.ts`, `lab/web/public/{index.html,lab.css}`, `lab/web/public/js/{app,page,newswarm,run,traces,view,settings,library,zip,playerhost}.js`, `lab/web/build.sh`, `lab/web/dev-server.mjs`, `lab/web/test/{library,settings,zip}.test.mjs`, `lab/web/test/{ui.e2e,browser-helpers}.mjs` | the lab page |

`lab/web/test/e2e.mjs` drives whole swarms through the page UI (`--n 1..4`, `--iface`, `--stop-after K`,
`--roundtrip`, `--macbook RUN`; see its header) against the image built by `lab/browser/image/build.sh`.

## 5. Converter API (module C)

`site/src/convert/index.ts`, compiled by site `tsc` to `site/public/js/convert/index.js` (plain ES module, no
deps, runs in browser and node; the lab serves it at `/player/js/convert/index.js` and loads it on first use):

```ts
export type RunFiles = Map<string, Uint8Array>;  // keys relative to the run dir: "run.json", "a0/steps.jsonl", ...
export interface ConvertOpts {
  maskCtf?: boolean;
  live?: boolean;    // partial run: tolerate missing end files, unfinished agents read 'running'
  runId?: string;    // as built: fallback when no run.json / manifest / start record names the run
  hidden?: boolean;  // as built: RunCard.hidden (default false: a visitor's own traces are listed)
}
export interface Converted { card: RunCard; core: CoreBundle; detail: DetailBundle }
export function convertRun(files: RunFiles, opts?: ConvertOpts): Promise<Converted>;
export function runFilesFromEntries(entries: {name: string, data: Uint8Array}[]): RunFiles; // strips "<run_id>/" or "runs/<run_id>/"
```

It ports `dash/server/obs/{parse,ingest,derive,classify}.py`, the `v_agent_metrics` / `v_run_metrics` views and
`site/tools/export.py` (`RunBuilder`, chapters, segments, series, `make_card`, number normalisation, the core
hash) to TS (`parse.ts`, `ingest.ts`, `derive.ts`, `classify.ts`, `metrics.ts`, `build.ts`, `py.ts`). Not
ported: `tools/redact.py` (only the `--mask-ctf` rule), the git `lab_commit` lookup (stubbed), findings and
index assembly. Malformed input lines never throw; they are skipped and counted in a caveat. Post bodies come
from `board_events.content.chatter.json` (joined by `post_id`) or from legacy inline records. Output satisfies
`src/data/loader.ts` `assertCore` / `assertDetail`.

Structural parity with `export.py` is checked by `site/tools/convert_parity.mjs` (driving
`convert_parity_py.py` in the dash/server venv) over `dash/server/tests/fixtures/runs/**` and, when present,
`runs/`: both sides reduce their bundles to the same numbers-only projection, and the tool prints only run ids,
PASS/FAIL, counts and field names.

## 6. Runtime API (module R)

`lab/web/public/js/swarm.js`:

```js
export function createSwarm(settings, hooks, opts?) -> {
  runId,                       // set synchronously
  run(): Promise<Outcome>,     // never rejects; idempotent. Outcome {result: "done"|"stopped"|"error", error?, detail?}
  stop(): void,                // aborts every broker (E_ABORT), ends agents, still exports what exists
  zip(): Promise<{filename, blob}>,  // §3 layout, valid after run() settles
  liveFiles(): Promise<Map<string, Uint8Array>>, // current partial run dir (final files once read, else live ones
                               //   + a run.json built from the spec with params.live = true)
  outcome(): Outcome | null,
  readFile(vm, mount, path), consoleTail(vm), stats(),   // test hooks (PROTOCOL.md §8); stats is numbers only
}
// settings = readSettings() result (+ nAgents 1..4); opts (dev harness / tests only): {timing, frameUrl,
//   container, assets: {manifest, seed}, prepareVm(vm, handle)}
// hooks: onStatus({phase, elapsedMs, board:{phase}, agents:[{i, phase, steps, calls, tokens, ended}]}),
//        onRecord(i, rec) (parsed steps.jsonl records, page memory only), onCall(i, info), onIgnored(i, seq)
```

Swarm phases: `init`, `loading`, `board`, `agents`, `running`, `ending`, then the outcome's result. VM phases
are the live status phases (PROTOCOL.md §4; anything else shows as `?`) plus the page's own `pending`,
`booting`, `starting`, `ending`, `skipped`.

Caps (`calls` = `N * (budget + 25)`, `tokens` = `N *` the per-agent token cap) are shared by all brokers
(`broker.js createBudget`, which refuses caps that are not positive integers with `E_CAPS`): the swarm as a whole
never exceeds the call cap, and passes the token cap by at most one in-flight call per agent (PROTOCOL.md §3).
One Stop aborts every agent. A harness resends its whole history on every call, so one 40-step agent can use
about 0.8M tokens; the per-agent default (1M) leaves a default swarm comparable with multi-agent pilot runs.

`dispose()` (after `run()` settled; the lab page calls it once the trace is stored) deletes the run's VM
databases and releases its Web Locks, so a second swarm in the same tab does not leave 3 x (N+1) databases
behind.

**Outcome codes** (fixed strings; `error` is the first failing VM's, `detail` names it):

| Code | Where | Meaning |
|---|---|---|
| `E_N_AGENTS`, `E_IFACE`, `E_RUN_ID` | `createSwarm` throws | bad settings |
| `E_ASSET_<status>`, `E_ASSET_MANIFEST` | run | the image manifest or seed could not be fetched |
| `E_FRAME_LOAD`, `E_BOOT_TIMEOUT` (180 s), `E_FRAME_BOOTED`, `E_FRAME_ARGS` | a VM | its frame did not load or boot |
| `E_BOARD_READY_TIMEOUT` (240 s), `E_BOARD_EXIT` | board | no board-ready.json; the board's labctl ended first (its status error, e.g. `E_BOARD`, wins when present) |
| `E_STOPPED_BEFORE_AGENTS` | board | Stop before the agents started: reported as `stopped`, not an error |
| `E_BOARD_INBOX`, `E_BOARD_END_TIMEOUT` (180 s) | board | the meta/stop files could not be written; the board's labctl did not exit |
| `E_AGENT_LOST` | agent | removed after the 120 s grace; ignored after Stop or its deadline |
| `E_STOPPED` | agent | its boot was abandoned by Stop (`skipped`); ignored |
| `E_EXPORT_SIZE` | agent | an agent-writable export file was over its read cap and was not read |
| `E_INTERNAL`, `E_BOOT`, `E_LABCTL_RUN`, `E_AGENT_START` | any VM | an exception without a fixed code (its text is never shown) |
| `E_EXIT_<n>`, `E_NO_STATUS`, `E_STATUS` | any VM | labctl exited non-zero / no readable final status / status not `done` |
| labctl codes (`E_KEY`, `E_SPEC`, `E_BOARD`, `E_SETUP`, `E_AGENT_ALIVE`, `E_EXPORT`, `E_LABCTL`) | any VM | the final status error (PROTOCOL.md §4); a status error that is not a fixed `E_` code (it is agent-writable) becomes `E_STATUS` |

`stopped` carries `detail: "deadline"` (an agent reached its page deadline and the visitor did not press Stop) or
`"board"` when the page (not the visitor) stopped the swarm; an error
outcome's `detail` is `"board"` or a list like `a0:E_X,a2:E_Y` (at most 200 chars).

## 7. Lab page (module P)

- The page is centred on the replay player (compiled from `site/src`; `build.sh` copies `site/public/js` and the
  player's two stylesheets into `dist/player/`, `dev-server.mjs` serves them from `site/public` at
  `/player/js/` and `/player/css/`, never `site/public/data`). `js/playerhost.js` feeds it an in-memory `Loader`
  (`site/src/data/memoryLoader.ts`) over the local trace library: stored traces are converted lazily on first
  load (and the bundles cached in the library record); a card whose converter hash changed is updated in place.
- Player embedding options (`PlayerOpts`): `narration: false`, `urlKey: "trace"` (the hash holds only
  `#trace=<id>`), `findings: false`, `graphKeyOpen: false`, the four selector chips `onNew`/`onUpload`/
  `onDownload`/`onDelete`, and a `local` lane titled "Traces" (live traces labelled `· live`, a crashed run's
  stored snapshot `· partial`). A viewer who played a live trace to its end follows the live edge: the next
  re-conversion with a later end resumes playing, and no end card is shown for a live trace.
- Selector chips: **+** (new swarm dialog), **upload** (zip or folder; a `.zip` can also be dropped anywhere on
  the page), **download** (the stored zip of the selected trace, byte for byte), **delete** (with a confirm).
- Trace library: IndexedDB `swarmlab-traces` (`js/library.js`), two stores: `traces` (metadata + card, read by the
  list) and `blobs` (zip blob + cached core/detail bundles), written in one transaction. Records are whitelisted
  field by field, so no API key or other setting can be stored. Without IndexedDB it falls back to memory and the
  page says traces last until the tab closes. The page asks `navigator.storage.persist()` and says whether
  storage is persistent or best-effort (evictable as a whole under storage pressure). Nothing leaves the browser.
- During a run a compact live panel shows the swarm totals and one lane per VM (phase, steps, calls, tokens) plus
  a short event log; every 10 s the partial trace is re-converted (bounded at 30 s) and shown as a live run.
  The final conversion replaces it after the run.
- Guards: an upload whose run id is the running swarm's is refused (`E_TRACE_RUNNING`); an upload whose run id
  is already stored with other bytes is refused (`E_TRACE_EXISTS`; the same bytes again just select it, and a
  run's own record is never replaced by an upload); the running swarm's trace cannot be deleted or downloaded
  until it has ended (leaving the page asks first); only one swarm runs at a time. A finished run whose zip could
  not be stored stays downloadable from its card for the rest of the tab's life.
- Upload errors are fixed texts with codes: `E_ZIP_*` (`FORMAT`, `CRC`, `DEFLATE`, `DUPLICATE`, `ENCRYPTED`,
  `METHOD`, `MULTIDISK`, `NAME`, `SIZE`, `LIMIT`, `INPUT`), `E_NOT_A_RUN`, `E_MANY_RUNS`, `E_RUN_ID`,
  `E_CONVERTER` (no converter in this build), `E_TRACE_*` (library record checks), `E_PLAYER`. Upload limits
  (`zip.js ZIP_LIMITS`): 4000 entries, 64 MiB per entry, 128 MiB total (the largest pilot run dir is about
  17 MiB), 400-byte names; names are checked
  against zip-slip, CRCs verified.
- All run content is rendered with `textContent` only. The API key stays in page memory (never IndexedDB,
  localStorage, the URL, a VM or the zip); `test/ui.e2e.mjs` checks the storage side.

## 8. Limits

- Memory (Chromium process-tree RSS): about 690 MB for the first VM plus 200–530 MB per extra VM (two-vm T3);
  board + 4 agents running peaked at about **2.9 GB** in the swarm dev harness and about **3.2 GB** (3170 MB) in
  `test/e2e.mjs --n 4` with the page and player (headless Chromium, mock provider, 2026-10-24). Memory of removed frames is not
  returned to the OS until the page reloads, so a second swarm in the same tab starts from that baseline. The
  dialog warns above N = 2; desktop Chromium is recommended, other engines are experimental.
- One swarm at a time per tab; agents boot one after another (several minutes before all N run).
- Same CheerpX caveats as the solo lab (`lab/web/README.md`): weak-uid, no timer signals, S2k wedge (now
  contained to one agent VM, which the page removes after the grace).
- The token cap can be passed by up to one in-flight call per agent.
- Background tabs: Chromium throttles timers of a tab hidden for more than 5 minutes to about once a minute, so
  the live view and the 3 s boot stagger slow down there (the boot hand-off itself waits on a promise, not a
  timer). Keep the lab tab visible while agents boot.
- A crashed tab's VM databases are deleted at the next Start (their lock is free); what survives is the partial
  trace stored at most 60 s before the crash.
- The converter does not run the full `redact.py`; traces stay local, so this matters only if a converted
  bundle were ever published (it is not).
