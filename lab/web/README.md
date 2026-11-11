# Browser lab (lab.swarmpit.dev)

A full schizboard swarm (one board + N = 1..4 agents, the shape of the macbook `<iface>-multi4` microsandbox runs)
runs entirely in the visitor's browser. The only internet dependency besides the static page, the CheerpX runtime
and the VM image is the visitor's OpenAI-compatible API key. The page records the trace, converts it, and plays it
back in the embedded swarmpit.dev replay player (no narration). Traces live in the browser (IndexedDB) and can be
downloaded and uploaded as zips. There is no login.

- Design and module contract: [docs/browser-swarm.md](../../docs/browser-swarm.md).
- Byte-level page <-> guest contract (mounts, sealed inputs, mailbox, status, export, file modes, swarm roles,
  test hooks): `lab/browser/PROTOCOL.md` (§9 is the swarm).
- Earlier design and roadmap: `docs/browser-sandbox-plan.md`, `docs/browser-two-vm.md`.

## How a swarm runs (`board-split`)

- **One VM per iframe.** The board and every agent run in their own CheerpX 1.3.9 VM, each in a same-origin
  iframe (`public/vmframe.html`, `js/vmframe.js`), because two `Linux.create` in one JS realm corrupt each other.
  Each VM has its own IndexedDB databases `swarmlab-{overlay,out,export}-<run_id>-<vm>` and the tab holds a Web
  Lock per run and per VM (PROTOCOL.md §1).
- **Network.** `js/fabric.js` gives every VM a private `loopnet.js` (guest loopback) and routes exactly one port
  across VMs: agent `a{i}` reaches the board VM on `9000+i` (its relay's `host.microsandbox.internal:<9000+i>`),
  the browser analogue of the microsandbox egress rule. Nothing else crosses VMs, and nothing leaves the browser
  except the brokers' model calls. When an agent ends, `fabric.cut()` closes its connections on both ends.
- **Guest.** `lab/browser/guest/labctl.py` takes its role from the sealed run spec: `board` runs only the board
  (ports `9000..9000+N-1`), draws the shared CTF and publishes it in `board-ready.json`, waits for the page's
  per-agent metas and `stop`, then writes the merged `run.json` (orchestrate semantics); `agent` runs the
  unchanged `setup_vm.py` and `harness.py` with the shared CTF and exports its own `a<i>/` files plus
  `a<i>/agent-meta.json`. A spec without a role is the old co-resident solo run (kept for tests; the page no
  longer starts it).
- **Model calls.** Each agent VM has its own broker (`js/broker.js`, own HMAC mailbox key, PROTOCOL.md §3) holding
  the key in page memory. All brokers count against one shared budget: calls `N * (budget + 25)`, tokens `N *` the
  per-agent cap typed (it can be passed by at most one in-flight call per agent). Stop aborts every broker; an
  agent's own page deadline aborts only its broker.
- **Lifecycle** (`js/swarm.js createSwarm`): board boots first; agents boot one at a time (3 s apart); the page
  tails every agent's live `steps.jsonl` and the board's live event mirror; an agent whose labctl exits (or that
  is still alive 120 s after Stop or its own deadline) is read, its iframe removed and cut from the fabric; when
  all are gone the board is told to stop and merge. After the trace is stored, the run's VM databases are
  deleted. Outcome codes: docs/browser-swarm.md §6.
- **Output.** One zip in the orchestrate `runs/<run_id>/` layout plus browser extras (`a<i>/agent-meta.json`,
  `manifest.json`), so `obs ingest` reads it. Runs carry `params.env = "cheerpx"`, `params.topology =
  "board-split"`, `params.isolation = "weak-uid"`, `params.preview = true`, `params.solve_verified = false`; `obs`
  tags them `preview` and keeps them out of default roll-ups. These markers are self-reported in a zip the
  visitor can edit: only ingest zips you produced, and do not pool them with microsandbox runs.
- **VM image.** An i386 Debian image with Python 3.12 (`lab/browser/image/`), served from R2 at `vm.swarmpit.dev`
  and streamed with HTTP range requests. `lab/agent/*` is unchanged, so the microsandbox path stays
  byte-identical (`lab/README.md` Invariants).

## Using the page

- **"+"** (selector chip, or the empty-state button) opens the new-swarm dialog. It asks for the API key, with
  the provider preset, base URL and model next to it ("Check key" lists the provider's models). Under "options":
  board interface, agents (1..4, default 4), tool-call budget per agent and the token cap per agent (default 1M;
  the swarm cap is N times it), plus wire-compatibility switches. Only base URLs the page's CSP allows (the
  presets and localhost) are accepted. Starting needs the cost-consent tick. The key stays in page memory: never in
  IndexedDB, localStorage, the URL, a VM or the trace.
- **Live view.** A compact panel shows the swarm totals and one lane per VM (phase, steps, calls, tokens). Every
  10 s the partial trace is re-converted and shown in the player as a live run; the final trace replaces it.
- **Trace library.** IndexedDB `swarmlab-traces` (`js/library.js`) keeps each trace's raw zip and its converted
  card and bundles, so traces survive reloads. Falls back to memory (with a notice) if IndexedDB is unavailable.
- **Download** returns the stored zip byte for byte. **Upload** accepts a lab zip, a zip or folder of a macbook
  `runs/<id>/` directory, or bare run files (a `.zip` can also be dropped on the page); uploads are bounded,
  zip-slip-checked and CRC-verified (`js/zip.js`). **Delete** removes a trace from this browser.
- **Player embedding.** `build.sh` compiles `site/src` and copies `site/public/js` plus the player's two
  stylesheets into `dist/player/`; `dev-server.mjs` serves them from `site/public` at `/player/js/` and
  `/player/css/`. `js/playerhost.js` runs the player with an in-memory loader (`site/src/data/memoryLoader.ts`)
  and `PlayerOpts` `narration: false`, `urlKey: "trace"` (`#trace=<id>`), `findings: false`, the selector chips
  and a local "Traces" lane. The converter (`site/src/convert`, `/player/js/convert/index.js`) is loaded on
  first use; without it the page still lists and downloads traces.
- **Page modules.** `js/app.js` composes the page and holds the `window.__lab` test hooks (PROTOCOL.md §8);
  `js/newswarm.js` is the "+" dialog over `js/settings.js`, `js/run.js` the live run controller (live
  re-conversion, partial snapshots, final store), `js/view.js` the live panel, `js/traces.js` upload / download /
  delete, and `js/page.js` the shared DOM, notice and error-code helpers.

**Memory.** Chromium process-tree RSS peaked at about 2.9 GB with the board and 4 agents running in the swarm dev
harness, and at about 3.2 GB (3170 MB) in `test/e2e.mjs --n 4` (headless Chromium, mock provider, page and player
included; 2026-10-24). Memory of removed VM frames is not returned until the page reloads, so a second swarm in the same tab
starts from that baseline. The dialog warns above 2 agents; desktop Chromium is recommended, other engines are
experimental.

**Preview.** This is a labelled preview: an agent can obtain root (euid 0) inside its own VM. CheerpX 1.3.9 does
not enforce directory permissions (S2p) and honours setuid-root binaries (S2q), so `su`, `passwd`, `mount` and
the like are a path to root (see the spike table below). The board split narrows what that root reaches: only
that agent's own VM (its CTF copy, its logs), never the board VM's files or another agent's VM.

What the hardening does and does not give:
- **Holds even against a root agent:** the visitor's API key never enters any VM (the brokers hold it and fix
  endpoint, model and headers), and model spend stays within the call and token caps the visitor sets; the
  brokers enforce them on every request, forged ones included (the token cap is checked before each call, so
  the swarm can pass it by the tokens of the calls in flight, at most one per agent). An agent can reach other
  agents only through the board, and the fabric routes nothing else out of its VM.
- **Does not hold against a root agent:** the per-trick mitigations (numeric agent identity handed to the
  harness, which refuses to run without it; the submissions file held open `O_NOFOLLOW`; `summary.json`,
  `FATAL`, `DONE` and every mailbox request written via a random-named temp file + `os.replace`, a directory
  planted at the name moved aside first; a FATAL reply ends the run even if its marker cannot be written; the
  solve and fatal status taken from the pinned `steps.jsonl`; no `pkill` exec in mailbox mode; killing every
  process outside the pre-harness `/proc` baseline) close the specific tricks reviewers found for a **non-root**
  agent. A root agent can write its VM's pinned logs, read the ctf and mailbox key in `cfg.json`, forge
  requests, fake `/proc` and write its `agent-meta.json`. So `solved`, `fatal`/`stop_reason`,
  `params.secret_exposure`/`secret_exposure_logs` and `agent_stopped` are tamper-evident only against a non-root
  agent. The board VM runs no agent, so its own logs and `secret_exposure_board` are not agent-writable.

`params.secret_exposure` counts agent step outputs that printed a run secret (a legitimate solve prints them
too, so it is not a pure leak counter); `params.secret_exposure_logs` counts the same secrets in the other
exported logs, including `secret_exposure_board` (board events, board output and the post-content sink).
Neither is a full leak detector.

## Develop, test, build

```bash
(cd lab/web && npm ci)                       # Playwright and wrangler, pinned in lab/web/package.json
(cd lab/web && npm test)                     # node unit tests: broker, fabric, library, loopnet, protocol parity, settings, swarm, zip
(cd lab && python3 -m unittest tests.test_labctl tests.test_harness)
(cd site && npm ci && npm run build)         # compiles the player and the converter into site/public/js (dev server + build.sh)
lab/browser/image/build.sh                   # i386 ext2 image -> dist/vm/ (needs Docker with i386 emulation)
node lab/web/dev-server.mjs 8788             # http://127.0.0.1:8788/ with the production headers, placeholder seed
(cd lab/web && node test/ui.e2e.mjs --engine chromium)   # page UI, no VM, no model: CSP, empty state, upload ->
                                             # player, reload, refused zip, "+" dialog, download bytes, delete,
                                             # key never in storage; --real-convert uses module C, --shot PREFIX
(cd lab/web && node test/e2e.mjs --engine chromium --script smoke --n 4)  # a whole swarm through the page UI with
                                             # a mock provider: --n 1..4, --iface forum|wiki|cache|irc,
                                             # --script smoke|irc|leak, --stop-after K, --roundtrip, --macbook RUN,
                                             # --deadline-min M, --zip-out PATH, --base URL, --engine
(cd lab/web && node spikes/swarm/run.mjs --mode net --n 2)           # swarm dev harness: fabric routing / cut in
                                             # real CheerpX, idle memory; --mode swarm --n N [--overlay] [--stop K]
                                             # runs createSwarm end to end with a mock provider; peak RSS per phase
(cd lab/web && node spikes/run.mjs --engine chromium --net loop)     # CheerpX spikes; --only 'S1h|S1i'; a hung test fails after 3 min
lab/web/tools/runmeta.py /tmp/lab-run.zip    # metadata-only summary of a downloaded run, incl. obs ingest
lab/web/build.sh                             # dist site: page + dist/player/ + image manifest + real seed
lab/web/deploy.sh                            # R2 image + Worker deploy (owner only, manual)
lab/web/cloudflare/verify.sh vm/<image>.ext2 # post-deploy header, range and cache checks
```

Tests and harnesses start their own dev server unless `--base` is given, use mock providers only, and print only
status, counts, fixed markers and pass/fail; never run content.

- Unit tests (`test/*.test.mjs`): `broker` (MAC/seq checks, error mapping, the shared budget across brokers),
  `fabric` (routes, private port spaces, `cut`), `library` (whitelisted records, `traceFromEntries` for lab and
  macbook layouts), `loopnet`, `protocol` (PROTOCOL.md §5.2/§9.4 file lists, seal format and §4 error codes
  against both `protocol.js` and `labctl.py`), `swarm` (createSwarm lifecycle against fake frames), `zip`
  (writer round trip, reader limits and malformed archives), `settings` (the provider allowlist against the CSP,
  cap scaling). Shared helpers: `test/helpers.mjs` (unit suites), `test/browser-helpers.mjs` (Playwright drivers).
- `spikes/swarm/run.mjs --overlay` serves the working tree's guest files and copies them over `/root/lab` in every
  VM, so role changes can be tried without an image rebuild. `e2e.mjs` runs whole swarms against the built image.

**Image rebuild needed.** `labctl.py` changed (the swarm roles), so the deployed image must be rebuilt
(`lab/browser/image/build.sh`) before the swarm page works against `vm.swarmpit.dev`; any later change to
`labctl.py`, `harness.py` or `setup_vm.py` needs one too. Deploying (`build.sh` then `deploy.sh`) is a manual step
for the owner. `build.sh` copies the real seed into `dist/data/` without printing it; `dist/**` is
chatter-bearing.

## What the spikes found in CheerpX 1.3.9 (October 2026)

| Check | Result | Handling |
|---|---|---|
| Loopback TCP between guest processes | Fails without a `networkInterface` | `loopnet.js` provides it |
| Closing a listener with no pending connection (S1j) | CheerpX reads `.remoteAddress` of the ended accept stream and throws | `loopnet.js` leaves that accept pending |
| AF_UNIX sockets | Work | (not needed now) |
| File permissions (0600/0700) for the agent user | Enforced for reads, on the ext2 root and the `dir` mounts | Secrets and exports are 0600 files |
| Directory permissions (S2p) | Not enforced for traversal, unlink, rename, symlinks or hard links; only creating a regular file is refused | labctl reads its logs through fds it opened itself |
| Setuid-root exec (S2q) | **Honored.** A setuid-root copy of the `python3` ELF run as the agent reports `euid=0` | Confirms the weak-uid label: the agent can reach `euid 0` via a setuid-root binary (e.g. a shipped `su` with a swapped `/etc/shadow`/`/etc/passwd`). The in-VM mitigations do not hold against a root agent; the structural fix is the two-VM split ([docs/browser-two-vm.md](../../docs/browser-two-vm.md)) |
| `O_NOFOLLOW` | Ignored (`lstat` and `fstat` inode numbers are correct) | `lstat` + inode check for other sources |
| IndexedDB / DataDevice mounts (S2f, S2o) | File modes enforced; the agent reads the inbox and can write the outbox | Requests are 0600 (the agent cannot read them back); HMAC + strict seq on requests; sealed inbox inputs; 0600 exports; CTF exported only after the agent is stopped |
| `DataDevice.writeFile` on an existing file (S5b) | Throws; the file is unchanged | The page never rewrites an inbox file |
| `/proc/<pid>/environ` | Not exposed | The mailbox key reaches `labctl` via env |
| `/proc/<pid>/status`, `stat` | Only `Name` and `Pid`; state always `R`, ppid 0; killed orphans stay listed | labctl kills each pid outside its pre-harness baseline once, whatever its uid; if one stays listed the run ends `E_AGENT_ALIVE` without the CTF (fail closed) |
| `os.killpg` (S2g) | `ProcessLookupError` | Harness falls back to a `/proc` walk by session id (mailbox mode; never execs `pkill`) and `os.kill` of the shell |
| Harness kill of a timed-out command (S2h-S2j) | Ends a single-process command; a session-id match finds nothing (no session ids in `/proc`), so the command's background children keep running | labctl kills every process outside its baseline after the harness |
| `kill` / `pkill` | Work, but a killed child reports exit 0 | |
| SIGKILL to an orphan that exited after its shell was killed (S2k, run alone) | Never returns; CheerpX throws `e is not a function` | None yet: labctl's end-of-run kill can hang on such a process |
| Page <-> guest mailbox | ~100–500 ms round trip | Polling at 150 ms |
| Two `Linux.create` in one JS realm (two-vm T1/T5) | The instances corrupt each other | One same-origin iframe per VM (`vmframe.html`) |
| `DataDevice.writeFile` with a `Uint8Array` (swarm dev harness) | Content not byte-exact in the guest | The page writes every inbox file as text (all are JSON) |
| Appends to a file on the `/root/export` mount from several guest threads (swarm dev harness) | The file's content was lost | Only single-threaded labctl writes the export (temp file + `os.replace`); the dev harness's threaded test server writes under `/tmp` and a foreground `cp` copies it out |
| Memory of a removed VM iframe | Not returned to the OS until the page reloads | The page drops every reference to a removed frame (devices, broker, loopnet, promises); the dialog warns about memory above 2 agents |

Known limitations of this version:
- At most 4 agents and one swarm at a time per tab; agents boot one after another, so a full swarm takes several
  minutes to start. Board + 4 agents need about 3.2 GB of browser memory, which is only returned on reload.
- **The agent can become root inside its VM.** CheerpX 1.3.9 does not enforce directory permissions, so the
  agent can delete or replace (with links or renames) files in root-owned directories, including the lab's own
  code and system binaries; only reading 0600 files and creating regular files there are refused. CheerpX also
  honors the setuid bit on exec (spike S2q: a setuid-root binary run as the agent reports `euid 0`), so a
  shipped setuid-root binary (`su`, `passwd`, `mount`, ...) with a swapped `/etc/passwd`/`/etc/shadow` is a
  path to real root; rerun S2q before trusting a fresh image. The mitigations listed under **Preview** stop
  the specific tricks reviewers found only while the agent is not root; a root agent can change a run's
  recorded outcome and read its secrets. New root-side code must still keep using the pinned-fd /
  temp-file-plus-`os.replace` pattern. Runs are labelled accordingly and are not pooled with microsandbox
  runs. Only the API key (never in the VM) and the page's call/token caps hold regardless.
- In an agent VM the agent sees the extra hosts entry (the board runs in its own VM).
- The agent can read its own model replies in the inbox (its outbox request files are now 0600).
- Timer signals are not delivered, so `timeout N` never fires.
- When a command times out, its background children keep running until the run ends. If one of them has
  exited by then, labctl's end-of-run kill can hang (S2k). In a swarm this wedges only that agent's VM: the page
  removes it 120 s after Stop or its deadline and the zip is marked `-incomplete`. A killed
  process that CheerpX keeps listing makes the run end `E_AGENT_ALIVE` (download without the CTF).
- Local model servers on `http://localhost` work in Chrome and Firefox, but not Safari (mixed content).
