# Browser lab: guest <-> page contract

This is the only specification of how the page (`lab/web/public/js/`) and the guest
(`lab/browser/guest/labctl.py`, the mailbox transport in `lab/agent/harness.py`) talk to each other.
Code comments cite it as `PROTOCOL.md §N` instead of restating it. Change this file first, then both sides
in the same commit. `lab/web/test/protocol.test.mjs` checks the shared lists (§5.2), the seal format (§2)
and the error codes (§4) against both sides.

Examples use placeholders only. Run content (seed, steps, board logs) never appears in this file.

**Trust boundary (preview).** CheerpX 1.3.9 enforces no directory permissions (S2p) and honours setuid-root
binaries (S2q), so the agent can obtain root (euid 0) inside the VM via `su`, `passwd`, `mount` and the like.
The per-trick mitigations in §3-§6 (numeric identity, pinned fds, temp-file + `os.replace`, the held
submissions fd, value-checked `summary.json`/`FATAL`, the `/proc` stop) do **not** hold against an agent that
has obtained root: a root agent can write the pinned logs, read `cfg.json` (the ctf and the mailbox key), forge
mailbox requests and fake `/proc`. So `solved`/`solve_step`, `fatal`/`stop_reason`, `secret_exposure*` and
`agent_stopped` are tamper-evident only against a non-root agent; runs carry `params.solve_verified: false`
(§5.3). What does hold regardless: the visitor's provider API key never enters the VM (the page's broker holds
it and fixes endpoint, model and headers, §3), and model spend stays within the call and token caps the visitor
set on the page (`E_BUDGET`, §3), even for forged requests (the token cap is checked before each call, so the
total can pass it by at most the tokens of the calls already in flight: one call in solo, up to one per agent in a
swarm, §3).

## 1. Mounts

| Guest path | CheerpX device | Written by | Read by | Agent user can |
|---|---|---|---|---|
| `/` | ext2 `OverlayDevice(HttpBytesDevice(image), IDBDevice("swarmlab-overlay-<id>"))` | guest | guest | file modes enforced; directory modes only for creating regular files |
| `/root/llmbox/out` | `IDBDevice("swarmlab-out-<id>")`, type `dir` | guest (harness) | page (broker) | write; read files whose mode allows it |
| `/root/llmbox/in` | `DataDevice`, type `dir` | page | guest | read |
| `/root/export` | `IDBDevice("swarmlab-export-<id>")`, type `dir` | guest (labctl) | page | read files whose mode allows it |
| `/dev`, `/dev/pts`, `/proc`, `/sys` | `devs`, `devpts`, `proc`, `sys` | | | |

The spikes found that file modes are enforced on the `dir` mounts too (S2o: the agent cannot read a root
0600 file there), that the agent can create files in the outbox (S2f), and that every inbox file is
readable. Directory modes are enforced nowhere except for creating regular files (S2p). So the inbox holds
only sealed inputs (§2) and replies, every file labctl writes is 0600, and as a second line of defence the
CTF reaches `/root/export` only after every agent process is dead (§5.3). `DataDevice.writeFile` cannot
replace an existing file (spike S5b), so the page never rewrites an inbox file. The page passes
`DataDevice.writeFile` text only: a `Uint8Array` did not arrive byte for byte in CheerpX 1.3.9 (swarm dev-harness
finding), and every inbox file is JSON text anyway (seals, replies, metas, `stop`).

`<id>` is the VM's storage id: the `run_id` for the legacy solo VM, `<run_id>-<vm>` for a swarm VM (`vm` is
`board` or `a<i>`, §9), e.g. `swarmlab-export-<run_id>-a1`. Each swarm VM has its own three databases.

A run's databases belong to the tab that holds the Web Lock `swarmlab-run-<run_id>` for its whole
lifetime. A swarm tab also holds `swarmlab-run-<run_id>-<vm>` for every VM of the run (board and each agent), so
a cleanup that reads the whole name tail after the kind as a run id (the solo `vm.js`) also sees a held lock.
A tab may delete `swarmlab-{overlay,out,export}-<id>` only when it can take the owning run's lock with
`ifAvailable` (no tab holds it); swarm databases are grouped under their `run_id` (the id ends in `-<4 hex>`,
§2, which separates it from a `-board`/`-a<i>` suffix) and deleted together. Databases of the first deployed
version (`swarmpit-*`, no lock) are deleted once their run id is more than 24 h old. Without `navigator.locks`
or `indexedDB.databases()` nothing is deleted. CheerpX 1.3.x keeps an `IDBDevice("<name>")` in the database
`cjFS_/<name>/`, which is the name `indexedDB.databases()` lists; names are unwrapped before they are matched
(`protocol.js unwrapDbName`; the solo `vm.js` cleanup does not, so it never deletes CheerpX databases). One swarm
runs at a time per tab; once the page has stored a finished swarm's trace it deletes that run's databases and
releases its locks (`dispose()`); otherwise the locks are held until the tab closes (a page can start a new swarm
after the previous one ended, with a new `run_id`).

The trace library (IndexedDB `swarmlab-traces`, `lab/web/public/js/library.js`) is page-only: no VM mounts it,
and it holds the visitor's stored trace zips and their converted player bundles (chatter-bearing, never printed).

## 2. Boot inputs

`run_id` is page-generated: `browser-<iface>-(solo|multi<N>)-<UTC YYYYMMDDHHMMSS>-<4 hex>`, `solo` iff the
run has one agent (N = 2..4 otherwise; the legacy co-resident mode is always `solo`); `iface` is one of
`forum|wiki|cache|irc`. Every VM of a swarm (§9) gets the same `run_id`. labctl itself checks only the
character set below, not this grammar.

Before starting labctl the page writes two **sealed** inbox files (the agent can read the inbox, §1):

- `/root/llmbox/in/run.json.sealed`, label `run.json`: the run spec, UTF-8 JSON:
  ```json
  {"run_id": "<run_id>", "iface": "forum", "rep": 0,
   "harness": {"budget": 40, "temperature": 0.7, "max_tokens": 2048, "output_chars": 3000,
               "run_timeout_s": 5400, "model": "<model>"},
   "wire": {"sanitize_tool_args": true},
   "provider_label": "<preset>:<model>", "cx_version": "1.3.9", "image": "swarm-<sha12>.ext2"}
  ```
  `rep` is optional (default 0). `wire` holds only the keys set to `true` among `sanitize_tool_args`,
  `merge_user_after_tool`, `drop_empty_assistant`.
- `/root/llmbox/in/seed.json.sealed`, label `seed.json`: the board seed, byte for byte. It is
  chatter-bearing; neither side prints it. Not needed (and ignored if sent) for a `role: "agent"` spec (§9).

**Seal.** Under the run's mailbox key `K` (below), a label `L` and plaintext bytes `P`:
`block_i = HMAC-SHA256(K, "seal-stream" 0x00 L 0x00 u32be(i))`, `C = P XOR (block_0 block_1 ...)` truncated to
`len(P)`, `tag = HMAC-SHA256(K, "seal-tag" 0x00 L 0x00 C)`. The file is the text
`{"v": 1, "tag": "<hex tag>", "data": "<base64 C>"}`. The key is per run and each label is sealed once, so
there is no nonce. labctl verifies the tag (constant time) before decrypting.

Then the page starts labctl, as root:

```
cx.run("/usr/local/bin/python3", ["/root/lab/labctl.py"], {
  env: ["HOME=/root", "LANG=C.UTF-8",
        "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "LAB_MAILBOX_KEY=<64 lowercase hex = 32 random bytes>"],
  cwd: "/root/lab", uid: 0, gid: 0 })
```

labctl pops `LAB_MAILBOX_KEY` from its environment before starting any child (process environments are
not readable by the agent in CheerpX). It must match `^[0-9a-f]{64}$`, else `E_KEY`. Both sealed files
(only the spec for a `role: "agent"` spec, §9) must exist and verify, and the spec must parse, have a `run_id` matching `^[A-Za-z0-9._-]{1,128}$`, a known
`iface`, a `harness` object and valid role fields (§9), else `E_SPEC`. labctl writes the seed to
`/root/run/seed.json` (0600).

## 3. Mailbox (model calls)

The guest-side key is `cfg.json` `mailbox = {"key": "<64 hex>", "outbox": "/root/llmbox/out",
"inbox": "/root/llmbox/in", "uid": <int>, "gid": <int>}` in `/root/lab/cfg.json` (0600, root). The harness
never sees the provider key. `uid`/`gid` are the agent's numeric identity, which labctl resolves once right
after setup_vm.py creates the user and before the harness starts (§6); in mailbox mode the harness runs every
agent command as those numbers (no per-command name lookup), so a later `/etc/passwd` change cannot rename or
re-privilege the agent. In mailbox mode the harness refuses to run if either is missing or not an integer: it
raises `E_NO_AGENT_ID` (logged as its crash record) before any model call or command, and never falls back to
the name `agent`.

**Request** (guest -> page). The harness writes a random-named temp file in the outbox, then `os.replace`s it
to `<outbox>/a<agent>-<seq>.req.json` (the seq is predictable, so a fixed temp name could be pre-planted as a
link; see §6). The final file is 0600 — the broker reads it through the IDB device, not as the agent:

```json
{"v": 1, "payload": "<JSON string>", "mac": "<hex HMAC-SHA256(key bytes, payload as UTF-8)>"}
```

`payload` decodes to `{"agent": <agent id>, "seq": <int>, "body": {"model", "messages", "tools", "temperature",
"max_tokens"}}`; the agent id is `cfg.json` `agent_id`: 0 in solo, the spec's `agent_id` in a swarm agent VM
(§9), which is also the `<agent>` of the request and reply file names. The MAC is computed over the exact `payload` string.

**Seq.** Starts at 1 and increases by 1 on every request the harness sends, including retries and after
any transport error (the counter lives at module level, never per connection). A seq is never reused in a
run. The broker expects `next` (initially 1) and also accepts `next + 1` (a request the harness gave up
on). It accepts a request only if `v == 1`, the MAC verifies, `payload.agent` is its VM's agent id and `payload.seq` is the
file's seq; then `next = seq + 1`. Anything else is ignored (reported once per seq, never answered).

**Reply** (page -> guest). The broker writes `<inbox>/a<agent>-<seq>.resp.json` with one `writeFile`:

```json
{"v": 1, "seq": 7, "status": 200,
 "body": {"choices": [{"message": {"role": "assistant", "content": "...", "tool_calls": [...]},
                       "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                    "prompt_tokens_details": {"cached_tokens": 0}}}}
```

Error replies carry `"code"` instead of `"body"` and optionally `"retry_after"` (seconds). The harness turns
a missing body into `{"error": {"code": "<code>"}}`. `message` keeps only `role, content, tool_calls[{id,
type, function{name, arguments}}], reasoning, reasoning_content`. Reply files are never deleted.

**Timing.** The broker polls the outbox every 150 ms. The harness polls for the reply every 0.1 s and gives
up after 300 s (an `OSError`, retried with a new seq). Once the reply file exists, the harness re-reads it
until it parses as JSON, within the same 300 s. The broker's provider fetch times out after 240 s.

**Upstream.** `POST <baseUrl>/chat/completions` with only `Authorization: Bearer <key>` and
`Content-Type: application/json`; body `{model: <page model>, messages, tools, temperature clamped to
[0, 2], max_tokens clamped to harness.max_tokens}`. The guest cannot choose endpoint, model or headers.

**Codes.**

| Code | Reply status | When | Harness reaction |
|---|---|---|---|
| (none) | 200 | provider 200 with a choice | continue |
| `E_AUTH` | 401 | provider 401/403 | FATAL |
| `E_BUDGET` | 402 | provider 402, or calls >= the call cap, or tokens >= the token cap | FATAL |
| `E_ABORT` | 402 | the visitor pressed Stop or this agent's page deadline passed (every later request too; an in-flight fetch is aborted) | FATAL |
| `E_RATE` | 429 | provider 429 | backoff, retry |
| `E_BAD_REQUEST` | 400 | provider 400/404/422 | resend up to 3x, then crash |
| `E_UPSTREAM` | 502 | other provider status, unparseable body, no choices, fetch timeout | retry |
| `E_OPAQUE` | 503, then 401 from the 3rd in a row | fetch rejected (network/CORS) | retry, then FATAL |

**Caps.** The call cap is `harness.budget + 25` per agent (`N * (budget + 25)` for N agents); the token cap is the
number the visitor typed, for the whole run. In a swarm every agent VM has its own broker (own mailbox key,
outbox, inbox and `agent` id), and all N brokers count against one shared budget object (`broker.js`
`createBudget`). The cap check and the call count are one synchronous step, so N brokers together never pass the
call cap. The token cap is checked before each call and tokens are added when a reply arrives, so the run can pass
it by at most the tokens of the calls in flight when it is reached: up to one call per agent. Stop aborts every
broker on the budget at once; an agent's own page deadline (§9.5) aborts only its broker. A request file larger
than 8 MiB is not read (ignored like an unsigned one).

FATAL means the harness writes `/root/logs/FATAL` = `{"status": <int>, "detail": "<redacted body
containing {\"error\":{\"code\":\"E_...\"}}>"}` and ends: it raises `ProviderFatal` on the first FATAL reply
whether or not the marker write succeeds (a failed write is logged as a `marker_error` record with only the
marker name and exception class), so a FATAL can never turn into a retry (§6).

## 4. status.json

`/root/export/status.json`, written by labctl (0600, atomic), polled by the page every 2 s:

```json
{"v": 1, "ts": 0.0, "phase": "running", "steps": 3,
 "ended": "finished", "error": "E_BOARD", "detail": "ExceptionClassName"}
```

- `phase`: `setup` -> `running` -> `exporting` -> `done` | `error`. `error` can follow any phase.
- `steps`: number of `kind == "step"` records in steps.jsonl.
- `ended` (only with `done`): `finished` (harness exited), `timeout` (run_timeout_s reached),
  `fatal` (a FATAL file exists).
- `error` (only with `error`): `E_KEY`, `E_SPEC`, `E_BOARD`, `E_SETUP`, `E_AGENT_ALIVE`, `E_EXPORT`,
  `E_LABCTL`. `detail` is only an exception class name (for `E_LABCTL`, `E_EXPORT`).

labctl exits 0 when it ends in `done`, else 1. The live status is display-only (the agent may be able to
write the export mount). The page decides the outcome only from the status it reads **after** `cx.run`
returns, when no agent process is alive.

## 5. Export

### 5.1 Layout

```
/root/export/status.json             live (§4)
/root/export/manifest.json           final only, written last: the commit marker
/root/export/run/<file>              the canonical runs/<run_id>/ directory (§5.2)
```

`manifest.json` = `{"v": 1, "run_id": "<run_id>", "files": ["<file>", ...], "agent_stopped": true}`.
`files` lists the run files actually written. The download is a stored zip with entries `<run_id>/<file>`
for each present `RUN_FILES` entry; `obs ingest --runs` reads it directly. Its name:

- `<run_id>-incomplete.zip` unless manifest.json exists with `agent_stopped: true` and the outcome (§8) is
  `done` or `stopped`;
- else `<run_id>-aborted-<fatal.status>.zip` when run.json has `fatal` (a FATAL marker, including Stop and
  broker caps). Like the pilot's `_aborted/` runs, these are not pooled with finished runs;
- else `<run_id>.zip`.

### 5.2 File lists (the single source; `protocol.js` and `labctl.py` mirror these exactly)

```export-files
live: a0/steps.jsonl
final: run.json
final: cfg-a0.json
final: seed.json
final: board.out
final: board_events.jsonl
final: a0/steps.jsonl
final: a0/summary.json
final: a0/FATAL
final: a0/harness.out
final: a0/setup.out
final: a0/vaultd.out
final: a0/relay.jsonl
final: a0/relay.out
```

`LIVE_FILES` = the `live` names; `RUN_FILES` = the `final` names (the download list). This block is the
legacy co-resident solo run; the swarm roles' lists are the `export-files-agent` and `export-files-board`
blocks in §9. Sources:
`seed.json`, `board.out`, `board_events.jsonl` from `/root/run/<same name>`; `a0/setup.out` from
`/root/run/setup.out`; every other `a0/<name>` from `/root/logs/<name>`. `run.json` and `cfg-a0.json` are written from labctl's memory: they
have no source under `/root/run` or `/root/logs`; on disk the ctf exists only in the 0600
`/root/lab/cfg.json` (§3, §6). Missing sources, and sources that fail the §6 checks, are skipped.

### 5.3 When files are written (the flag rule)

1. **During the run** labctl mirrors only `status.json` and `LIVE_FILES` (about every 3 s).
   Nothing that contains the CTF, the seed, or board/relay logs is in `/root/export` while the agent user
   has a process.
2. **After the harness process has ended** (it exited, or labctl killed it at `run_timeout_s` or because
   labctl itself failed), labctl stops the agent. Right before starting the harness it recorded the set of
   pids `/proc` lists (the baseline: itself, the board, the daemons setup_vm.py started). Now, in rounds 0.5 s
   apart, it lists `/proc` and sends SIGKILL once to every pid that is not in the baseline and is not labctl or
   one of its ancestors, **whatever its uid** (an agent that ran a setuid-root binary has euid 0); the harness
   is among them if it survived. labctl reaps its own children between rounds. The agent counts as stopped
   only once a re-scan lists no pid outside the baseline; after 10 rounds it counts as alive. CheerpX 1.3.9
   keeps killed orphans listed in `/proc` (state `R`), so a run that leaves such a process ends in
   `E_AGENT_ALIVE` (fail closed: no ctf is exported). If `/proc` does not list labctl itself, the agent counts
   as alive. If the harness never started, no agent command ran and the agent counts as stopped.
   Known gap: in CheerpX 1.3.9 a SIGKILL to an agent process that exited after its shell was killed (a
   timed-out command's background child) never returns (spike S2k), so this step can hang.
3. labctl sets phase `exporting`, removes `/root/export/run` and `/root/export/manifest.json`, writes
   every `RUN_FILES` entry, `run.json` last, then `manifest.json`, then the final status.
4. If agent processes survive step 2, labctl exports without `ctf` in run.json and without
   `cfg-a0.json`, sets `agent_stopped: false`, and ends in `error` / `E_AGENT_ALIVE`.
5. A failure after the board started (`E_BOARD`, `E_SETUP`, an exception) still runs steps 2-4 and then
   reports the error. `E_KEY` and `E_SPEC` export nothing.

`run.json` (canonical, as `orchestrate._run_one`): `run_id, iface, n_agents: 1, rep, harness, started,
finished, ctf {passphrase, key_hex, flag, vault_id}, timed_out: [] | [0], fatal?: {agent: 0, status},
params {env: "cheerpx", topology: "board-co-resident", provider, cx_version, image, agent_stopped,
isolation: "weak-uid", preview: true, solve_verified: false, secret_exposure: <int> | null,
secret_exposure_logs: <int> | null, stop_reason?, omitted?}`.
`stop_reason` is the broker code parsed from FATAL's detail (`E_ABORT`, `E_BUDGET`, `E_AUTH`, ...).
`isolation` and `preview` mark this version (the agent can replace files in root-owned directories and can
obtain root in the VM), so its runs are not comparable with the microsandbox pilot even though `env` is still
`cheerpx`. `solve_verified: false` says the solve comes from a log a root agent could have written (see the
trust boundary above). `obs` tags every browser-lab run `preview` (`preview: true`, `env: "cheerpx"`,
`topology: "board-co-resident"` or a `browser-` run id, so first-version runs without `preview` are tagged too),
at ingest and again at derive, so its default roll-ups (solve rate and the other cell metrics) leave them out
unless asked to include tagged runs. All of these are self-reported in the downloaded zip, so the tag guards
against accidental pooling, not against an edited zip.

`secret_exposure` and `secret_exposure_logs` are the two automated residual-risk counts (each a non-negative
int, or `null` if the count raised — labctl never lets a bad log line cost the visitor the zip). Neither is a
leak detector, and together they cover only the exported logs: a secret that leaves through a channel no
exported log records (e.g. a mailbox request an agent that got root forged, or a file in the agent's home) is
counted by neither, and an agent that got root can edit the logs they are computed from. No secret is exported
to compute them.
- `secret_exposure` counts the agent **step outputs** (a command's combined stdout/stderr in `steps.jsonl`)
  that contain a run secret (the flag, passphrase, key_hex or the mailbox key). A legitimate solve prints the
  flag/passphrase/key_hex, so this includes genuine CTF progress; it is **not** a pure leak counter.
- `secret_exposure_logs` counts the lines of the **other exported logs** (board events and output, relay,
  vaultd, harness and setup output — all read through pinned fds) that contain a run secret, e.g. a secret the
  agent posted to the board (which `steps.jsonl` would not show).

`omitted` (when present) maps a `RUN_FILES` name to a fixed reason code for a file that was present at its
source but rejected: `"shape"` (not a regular single-link file, over the cap, or wrong JSON shape/values) or,
for `a0/summary.json`, `"solve_mismatch"` (see §6).

`a0/summary.json` and `a0/FATAL` are exported only after the agent is stopped and only when the source is a
regular, in-shape JSON file under a small size cap (§6). The `solved`/`solve_step` fields of `summary.json`
are **not trusted**: labctl derives the run's solve from the pinned `steps.jsonl` (the first step whose phase
moved task→free with the flag in its submissions), and a summary whose solve claim disagrees is omitted
(`solve_mismatch`). `fatal`/`stop_reason` come from a genuine `a0/FATAL` marker, or — if FATAL is missing or
forged (a leftover agent process can unlink it in the window between harness exit and the agent stop, or a
directory planted at FATAL could not be moved aside, §6) — from the harness's `ProviderFatal` crash record in
the pinned `steps.jsonl`. That record exists for every FATAL reply because the harness raises `ProviderFatal`
even when its marker write fails (§3). A forged file at either path is
skipped and recorded in `omitted`.

## 6. File modes

- labctl runs with `umask 077`; the board inherits it. setup_vm.py and harness.py are started with
  `umask 022` (as in microsandbox), so the agent's environment and its commands keep pilot modes.
- Every file labctl writes (export, `/root/run/*`, `/root/lab/cfg.json`, logs it opens) is created 0600
  through a random-named temp file (`tempfile.mkstemp`, which opens `O_CREAT|O_EXCL`) then `os.replace`, so a
  link the agent pre-planted at a guessable temp name is never opened and cannot redirect or block the write.
  labctl never follows a symlink under `/root/export`: it checks parents with `lstat` and recreates them if
  they are not real directories. The two harness-written sources `summary.json` and `FATAL` are exported only
  once the agent is stopped and only if they also pass a small size cap (64 KiB) and a shape/value check:
  `summary.json` must be a JSON object with `run` equal to this run's id and `agent` 0 (and its solve claim
  must match the pinned `steps.jsonl`, §5.3); `FATAL` must be **exactly** `{"status": <int in the mark_fatal
  set 200/400/401/402/403/429>, "detail": <str ≤ 200 chars>}` and nothing else. A symlink, hard link, over-cap
  file or a root file the agent renamed onto the path -- even one whose keys happen to match -- fails and the
  file is omitted (recorded in `params.omitted`, §5.3). The integrity-critical facts (whether the run solved,
  whether it is fatal) are taken from the pinned `steps.jsonl`, never from the unpinned `summary.json`/`FATAL`
  alone. That stops a non-root agent's forgeries only: an agent that got root can write `steps.jsonl` itself
  (hence `solve_verified: false`, see the trust boundary at the top).
- CheerpX 1.3.9 lets the agent replace entries in root-owned directories with symlinks or hard links (it
  cannot create regular files there) and ignores `O_NOFOLLOW` (spike S2p). So labctl keeps a read-only fd on
  every file it creates (`/root/run/{seed.json, board.out, board_events.jsonl, setup.out}`,
  `/root/logs/{steps.jsonl, relay.jsonl, relay.out, vaultd.out, harness.out}`) and reads those sources only
  through it. Other sources (`summary.json`, `FATAL`) must pass `lstat` as a regular file with one link owned
  by root, and the opened file must have the inode `lstat` saw.
- labctl runs no binary by PATH lookup (root's PATH starts with `/usr/local/bin`): it starts the board,
  setup_vm.py and the harness with its own interpreter (`sys.executable`, `/usr/local/bin/python3`). In
  mailbox mode the harness's kill fallback for a timed-out command walks `/proc` by session id and never
  execs `/usr/bin/pkill` (the agent can replace that binary in its root-owned dir); the microsandbox path is
  unchanged and still uses `pkill -s`.
- Numeric identity: labctl resolves the agent's `uid`/`gid` once after setup_vm.py creates the user and
  before the harness starts and passes them in the mailbox section (§3); the harness runs commands by those
  numbers and refuses to run without them. Stopping the agent (§5.3) does not depend on uids or `/etc/passwd`
  at all: it kills every process outside the pre-harness `/proc` baseline.
- Submissions (mailbox mode): the harness opens `/var/tmp/submissions` once, `O_NOFOLLOW` and regular-file
  checked, before the agent's first command, and reads new lines through that held fd. Replacing the path
  later (a symlink or rename onto another file, e.g. `cfg.json`) cannot redirect the checker, so a forged
  solve is not possible. The checker reads only that original inode, so if the agent removes and recreates
  the file (`/var/tmp` is 1777) its own later genuine submissions through the new inode are not seen either;
  the microsandbox pilot, which reads by path, would count them. This is an accepted browser-lab trade-off.
- Before setup_vm.py runs, labctl creates empty 0600 files for logs that daemons, the board or the harness
  open in append mode: `/root/logs/{steps.jsonl, relay.jsonl, relay.out, vaultd.out}`,
  `/root/run/board_events.jsonl`. It opens `/root/run/{board.out, setup.out}` and `/root/logs/harness.out`
  itself.
- Every root-side write the harness makes **after the agent has started** must avoid a by-path
  `open(..., "w")`, because the agent can plant a link at any root-owned path (and at a predictable temp name)
  and CheerpX ignores `O_NOFOLLOW`. In mailbox mode the harness writes both its end-of-run markers
  (`summary.json`, `FATAL`, `DONE`) and every outbox model **request** through a shared helper
  (`write_replace`): a random-named temp file created `O_EXCL` (`tempfile.mkstemp`) then `os.replace` over the
  target name. Nothing planted in advance is opened, and `os.replace` follows no link at the target, so neither
  write can be redirected to truncate a pinned log (e.g. `steps.jsonl` or `board_events.jsonl`). That covers
  links only: the agent can also rename a **directory** onto the target name (S2p), and `os.replace` cannot
  overwrite a directory. So when `os.replace` fails, `write_replace` renames whatever sits at the name aside
  under a random `.<name>.aside.<hex>` name (root can; the name is unguessable) and retries, up to 5 times
  against a process that keeps re-planting. The aside entries stay where they are: labctl exports only
  `RUN_FILES` names and the broker reads only exact request names. If every attempt fails, the marker write
  is best-effort: the harness logs `marker_error` and goes on (`mark_fatal` still raises `ProviderFatal`,
  `main` still logs `end`), so a planted entry cannot keep a run going after Stop or the budget cap, nor
  turn a clean run into a crash. The markers contain no secrets. The microsandbox path keeps the plain umask-022 by-path write.
- Image: `/root` is 0700 and `/root/lab` is `go-rwx` recursively (Dockerfile). CheerpX enforces directory
  modes only for creating regular files (§1), so file modes are what protects secrets.
- Agent-readable by design: its own home, files it itself wrote in the writable outbox `/root/llmbox/out`
  (the harness's own request files there are now 0600, so the agent cannot read them back), `/root/llmbox/in/*`
  (its own replies and the sealed inputs), and during the run `status.json` and `run/a0/steps.jsonl` (its
  own transcript). Nothing else under `/root`. `lab/web/test/e2e.mjs --script leak` checks this.

## 7. Guest network

`networkInterface` = `loopnet.js`. Only loopback (`localhost`, `127.0.0.0/8`, `::1`) is routed, to the guest
listener on that port (listeners are keyed by port; CheerpX binds `0.0.0.0`). Everything else and all UDP
is refused. Ports: board 9000; relay `127.0.0.2:8080` (`6667` for irc); vaultd `127.0.0.1:31337`. labctl
appends `127.0.0.1 host.microsandbox.internal`, setup_vm.py `127.0.0.2 board.internal` to `/etc/hosts`.
In a swarm (§9) the board VM listens on `9000..9000+N-1`, and agent VM `i` has no board: its relay connects to
`host.microsandbox.internal:<9000+i>` (still `127.0.0.1`), which the page's fabric routes to the board VM. That
port is the only one routed out of an agent VM.
Every socket object has `opened`, `closed` (resolves once both directions ended) and an async `close()`.

## 8. Test hooks

`window.__lab` (`lab/web/public/js/app.js`). Every hook returns ids, counts, fixed codes or file text the test
itself must not print (run content):

- `RUN_FILES`: §5.2's list.
- `ready()`: resolves `true` once the library and the player loaded.
- `outcome()`: `null` or `{runId, result: "running"|"done"|"stopped"|"error", error?}` of the latest swarm.
- `readFile(vm, mount, path)`: the text of a file on VM `vm`'s (`"board"` or `"a<i>"`) `"export"` or `"outbox"`
  device, or `null`. Paths are device paths (§5.1, e.g. `/status.json`, `/run/a0/steps.jsonl`). Once the VM's
  frame is gone, export reads are answered from what the page read at the VM's end (status.json, manifest.json,
  `run/<file>`), outbox reads give `null`. The two-argument form `readFile(mount, path)` addresses the board VM.
- `consoleTail(vm = "board")`: the last 4 KiB of that VM's labctl console (its own stdout/stderr, e.g. a
  traceback) as of the VM's end; never rendered by the page.
- `traces()`: the trace library as `[{id, source: "run"|"upload", bytes, nFiles, nAgents, events, nodes}]`.
- `loadTrace(blob)`: ingests a zip like the upload button: `{ok: true, id}` or `{ok: false, code}`.
- `startSwarm(override?)`: starts a swarm from the dialog's settings with `override` merged over them (e.g.
  `{nAgents: 2}`; the shared call cap follows `nAgents` unless `caps` is given). Resolves `{runId}` or
  `{error: "E_NOT_ISOLATED"|"E_RUNNING"|"E_SETTINGS_<REASON>"|"E_RUNTIME"|<createSwarm code>}`.
- `stopSwarm()`: presses Stop.
- `refreshLive()`: one live re-conversion now (normally every 10 s).
- `state()`: `{isolated, hostError, running, runId, result, persistent, traces, selected, live: [ids],
  liveConversions, liveErrors, empty, dialog: "new"|"upload"|null}`.

DOM ids driven by the browser tests: in the new-swarm dialog `newDlg preset baseUrl model iface nAgents apiKey
budget tokenCap consent check checkResult start cancelNew`; the live panel `runPanel runId phase steps calls
tokens elapsed lanes error stop saveRaw liveNote`; uploads `uploadDlg pickZip pickDir zipInput dirInput
uploadResult closeUpload`; `player`, `notice`, `emptyNew`, `emptyUpload`; and `download` (the player selector's
download chip, given that id by the page).

## 9. Swarm roles (`board-split`)

A swarm (`docs/browser-swarm.md`) runs one board VM and N = 1..4 agent VMs, each in its own iframe with its own
mounts, mailbox key and labctl. The sealed spec's `role` picks what labctl does; a spec without `role` (or
with `role: null`) is the legacy co-resident solo run of §1-§8, unchanged. Every VM gets §2's inputs, key
and `cx.run` invocation; the role fields below are added to the spec:

- `role: "board"`: `n_agents` (int 1..4). Needs the sealed seed.
- `role: "agent"`: `n_agents` (int 1..4), `agent_id` (int `0..n_agents-1`), `relay_host_port` (exactly
  `9000 + agent_id`), `ctf` (exactly the four keys and value shapes of `new_ctf()`: three distinct words of
  the WORDS list joined by `-`, 16 hex, `FLAG{<20 hex>}`, `vlt-<6 hex>`; the page copies it from
  board-ready.json). No seed.

Anything else (an unknown role, a missing or out-of-range field, a bool where an int is expected) is
`E_SPEC`. The swarm uses no new status error codes.

### 9.1 Board VM

1. Writes the seed to `/root/run/seed.json`, pre-creates (0600, pinned, §6) `/root/run/board_events.jsonl`
   and its content sink `/root/run/board_events.content.chatter.json` (the board's `--log` sibling,
   `lab/board/events.py`, opened in append mode once something is posted), opens `/root/run/board.out`, and
   starts `python3 -m board --interface <iface> --ports 9000,...,<9000+N-1> --seed /root/run/seed.json
   --log /root/run/board_events.jsonl` (port index = agent id). No hosts entry, no setup_vm.py, no harness.
2. Waits at most 60 s in total for every port, else `E_BOARD`. Draws the run's one ctf (`new_ctf()`) and
   writes `/root/export/board-ready.json` (0600, temp + `os.replace`):
   `{"v": 1, "run_id", "ctf": {passphrase, key_hex, flag, vault_id}, "started": "<UTC YYYY-MM-DDTHH:MM:SSZ>",
   "ports": [9000, ...]}`. The board VM has no agent, so the ctf may be in its export at any time; the page
   must hand it only to agent specs (sealed) and never render it.
3. Every 3 s: copies `/root/run/board_events.jsonl` to `/root/export/run/board_events.jsonl` (the live
   mirror, structural events only; the content sink is never mirrored live) and writes status.json
   `{"v": 1, "ts", "phase": "running", "events": <lines in board_events.jsonl>, "metas": <agent metas present>}`.
   It stops waiting when `/root/llmbox/in/stop` exists (`ended: "finished"`), when the deadline
   `started + harness.run_timeout_s (default 3600) + 1800 s` passes (`ended: "timeout"`), or with `E_BOARD`
   when the board process exits first.
4. Kills the board (bounded 10 s wait), counts `secret_exposure_board` (§9.3), reads the inbox files
   `agent-<i>.meta.json` for `i < N`, merges them (§9.3) and exports (§9.4). This step also runs after a failure
   once the spec verified (`E_BOARD` from the port wait or the loop, an exception as `E_LABCTL`), which is then
   reported; a failing merge or export is `E_EXPORT`. `E_KEY` and `E_SPEC` export nothing. Phases and
   `ended`/`error`/`detail` are as §4: `setup` is written right after the spec verified, `steps` is replaced by
   `events` (present in `running` and `done`), and `ended` is `fatal` when the merged run.json has `fatal`, else
   `finished` or `timeout` as above. Exit 0 iff `done`.

The board VM's inbox control files `agent-<i>.meta.json` and `stop` are **plain** (not sealed) files the page
writes with `writeFile` (never rewritten, §1): the board VM runs no agent, so nothing inside it can read or
forge them. The page writes every meta first and `stop` last. A meta is read with a 64 KiB cap and accepted
only if it parses and passes the §9.2 shape check with `run_id` equal to the board's and `agent == i`; a
present but rejected meta counts as missing and is recorded in `params.omitted` as `a<i>/agent-meta.json:
"shape"`. The check is about shape only: the page copies the meta from the agent VM's export, which an agent
that got root can write (trust boundary at the top), so a merged meta is no more trusted than that export.
For an agent that started (its VM boot began) but exported no meta (frame or boot failure, removed after the
grace), the page writes a stub of the same shape instead, so `params.agent_errors` names the page-side code:
`{"v": 1, "run_id", "agent": i, "page": true, "timed_out": true, "agent_stopped": false, "solved": false,
"solve_step": null, "started", "finished", "fatal": null, "stop_reason": null, "error": "<E_ code>",
"secret_exposure": null, "secret_exposure_logs": null}` (`started`/`finished` epoch seconds; a code outside
`E_[A-Z_]{1,32}` is written as `E_PAGE`). It counts as timed out and not stopped, as a missing meta does. An agent
skipped by Stop (never booted, or its boot abandoned) gets no stub.

### 9.2 Agent VM

The solo flow (§3-§6) minus the board: no `start_board`, the spec's `ctf` is used instead of drawing one,
`127.0.0.1 host.microsandbox.internal` is still appended to `/etc/hosts`, and `cfg.json` has
`agent_id: <i>`, `relay_host_port: 9000 + i` (so mailbox files are `a<i>-<seq>.*`, §3). Every `a0` name of
§5.2/§6 becomes `a<i>`: the live file is `a<i>/steps.jsonl`, `summary.json` must name agent `i`, a FATAL is
`{agent: i, status}`, `timed_out` is `[i]`. Sources are as §5.2 (`a<i>/setup.out` from `/root/run/setup.out`,
every other `a<i>/<name>` from `/root/logs/<name>`); `secret_exposure_logs` scans only `a<i>/{harness.out,
setup.out, vaultd.out, relay.jsonl, relay.out}`. The export has no `run.json`, `seed.json` or board files;
`a<i>/agent-meta.json` is written last (from memory, in run.json's place), then
`manifest.json` = `{"v": 1, "run_id", "files", "agent_stopped": <bool>, "agent": i}`. As in solo,
`cfg-a<i>.json` (the ctf) is exported only if the agent was stopped.

`a<i>/agent-meta.json` (no secrets):

```json
{"v": 1, "run_id": "<run_id>", "agent": 0, "started": 0.0, "finished": 0.0, "timed_out": false,
 "fatal": null, "stop_reason": null, "secret_exposure": 0, "secret_exposure_logs": 0,
 "agent_stopped": true, "solved": false, "solve_step": null, "error": null}
```

- `started`/`finished`: epoch seconds of labctl's run start and of finish_meta; `timed_out`: the harness hit
  `run_timeout_s`.
- `fatal`: `null` or `{"status": <int in 200/400/401/402/403/429>}` (from FATAL or the ProviderFatal crash
  record, §5.3); `stop_reason`: `null` or the broker code
  (`E_[A-Z_]{1,32}`; a longer or odd code is recorded as `null`, so the meta still passes the board's check).
- `secret_exposure`, `secret_exposure_logs`: as §5.3 for this agent's own logs, int >= 0 or `null`.
- `agent_stopped`: §5.3 step 2's result. `solved`/`solve_step`: derived from the pinned `steps.jsonl` (§6),
  `solve_step` an int iff `solved`.
- `error`: `null` or the §4 error code this labctl ends in (`E_AGENT_ALIVE` if the agent was not stopped);
  an `E_EXPORT` raised while writing the export itself cannot be recorded here.
- `omitted` (optional): as `params.omitted` (§5.3), keys `a<i>/<name>` or `cfg-a<i>.json`.

### 9.3 Merged run.json (board VM)

As `orchestrate._run_one`: `run_id, iface, n_agents: N, rep, harness, started` (the board's start, epoch
seconds), `finished` (merge time), `ctf`, `timed_out` (sorted agent ids whose meta says `timed_out` or that
have no accepted meta: orchestrate's stragglers), `fatal?` = `{agent, status}` of the lowest agent id whose
meta has `fatal` (orchestrate.fatal_of; no void_of, as in solo), and `params` = §5.3's with
`topology: "board-split"` plus:

- `stop_reason?`: the fatal agent's `stop_reason`.
- `agent_stopped`: every agent reported `agent_stopped: true`; `agents_stopped`: the per-agent list
  (`false` for a missing meta).
- `secret_exposure_board`: lines of `board_events.jsonl`, `board.out` and the content sink that contain the
  ctf's flag, passphrase or key_hex (no mailbox key: no agent key reaches the board VM), or `null`.
- `secret_exposure`: the sum of the agents' counts; `secret_exposure_logs`: the sum of the agents' counts
  plus `secret_exposure_board`. Each is `null` if any agent's count (or the board's) is `null` or any meta is
  missing.
- `agents_missing?`: agent ids with no accepted meta; `agent_errors?`: `{"<i>": <error code>}`;
  `omitted?`: the agents' `omitted` maps merged, plus rejected metas.

### 9.4 Export file lists

The board VM rebuilds `/root/export/run` with every present `final` name (the content sink only if it is
non-empty), `run.json` last, then `manifest.json` = `{"v": 1, "run_id", "files", "agent_stopped": [<bool> x
N], "topology": "board-split"}`; `board-ready.json` stays. Sources are `/root/run/<same name>`; run.json is
written from memory.

```export-files-board
live: board_events.jsonl
final: run.json
final: seed.json
final: board.out
final: board_events.jsonl
final: board_events.content.chatter.json
```

An agent VM's lists, with `{i}` its agent id (for `i = 0` the `a0/` names are §5.2's):

```export-files-agent
live: a{i}/steps.jsonl
final: cfg-a{i}.json
final: a{i}/steps.jsonl
final: a{i}/summary.json
final: a{i}/FATAL
final: a{i}/harness.out
final: a{i}/setup.out
final: a{i}/vaultd.out
final: a{i}/relay.jsonl
final: a{i}/relay.out
final: a{i}/agent-meta.json
```

The page assembles the swarm zip (`docs/browser-swarm.md` §3) from the board VM's `run/` and every agent VM's
`run/`; the agent VMs' `manifest.json` and `board-ready.json` are not zip entries. The zip's own `manifest.json`
is written by the page (`{"v": 1, "run_id", "files", "agent_stopped": [<bool> x N], "topology": "board-split"}`,
`agent_stopped[i]` from agent `i`'s export manifest). Zip names follow §5.1 with the swarm's completeness rule:
`-incomplete` unless the board exported (its manifest exists), every agent's export manifest says
`agent_stopped: true` and the outcome is `done` or `stopped`; then `-aborted-<fatal.status>` if the merged
run.json has `fatal`.

### 9.5 Page side of a swarm

`lab/web/public/js/swarm.js` (`createSwarm`) drives the roles; `docs/browser-swarm.md` §2/§6 has the lifecycle and
the page-side outcome codes. The parts the guest relies on:

- Every VM boots in its own same-origin iframe (`vmframe.html`, `js/vmframe.js`) with the §1 mounts under its own
  storage id and the §2 `cx.run`; the network interface is the page's fabric (`js/fabric.js`, §7).
- The board's inputs are sealed with the board VM's key. Agent VMs boot one at a time (the next boot starts once
  the previous VM left booting, plus 3 s), each with a fresh key, its sealed spec (with the ctf read from the
  board's `board-ready.json`) and its own broker on the shared budget (§3).
- When an agent's labctl exits, the page reads its export (status, manifest, `run/` files), closes its broker,
  removes its iframe and cuts it from the fabric. After Stop every broker answers `E_ABORT`; at an agent's own page
  deadline (its labctl start + `harness.run_timeout_s` + 15 min, at most the board's deadline less 5 min) only
  its broker does. An agent whose labctl has not exited 120 s later (extended by up to 180 s while its live
  status says `exporting`) is removed the same way (the S2k wedge) and the page keeps whatever its export held
  (no ctf: §5.3).
- If the board's labctl ends while agents still run, the page stops the swarm as Stop does.
- After every agent is gone the page writes the metas (each agent's `a<i>/agent-meta.json`, byte for byte as
  text, or the §9.1 page stub) and then `stop`, and waits at most 180 s for the board's labctl. As in §4, the outcome is decided from
  each VM's status read after its labctl returned.
