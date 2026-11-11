# Swarm evals in the browser: final plan

**Scope.** This plan covers running the schizboard swarm eval (board service, agent harness, CTF, logs) inside each visitor's browser. The guest is a CheerpX x86 Linux VM, built from the patterns already in `aisecurityliteracy.dev` (AISL). Visitors bring their own OpenAI-compatible API key. The board runs as a local service inside the guest, and `/etc/hosts` points `board.internal` at it. The plan also lists everything that must be set up on Cloudflare.

**Constraint used throughout.** The plan names chatter only by file path or record ID. Seeds, run outputs, transcripts and lure text are treated as opaque data. They are moved, served and validated by scripts that print only safe metadata.

---

## 1. Executive summary

**Recommendation.** Build a static lab app on its own origin, `lab.swarmpit.dev`, served by Cloudflare Workers Static Assets with real COOP/COEP/CSP headers. The page boots CheerpX from the hosted Leaning CDN, pinned to one version, and loads an i386 ext2 image from R2 (`vm.swarmpit.dev`, an R2 custom domain behind the CDN cache).

Inside the guest, the existing Python lab code runs with small changes: `lab/board`, `setup_vm.py`, `netrelayd`, `vaultd`, and the PR #4 `harness.py`. A small in-guest orchestrator (`labctl.py`) does what `orchestrate.run_one` / PR #4 `_run_one` does today. Two things change functionally:
- **Topology.** In the pilot the board runs in its own microVM and the agent's `netrelayd` relays to `host.microsandbox.internal:<19000+i>`. In the browser the board runs in the agent's VM and the relay's upstream is guest loopback. This is a real topology change with agent-observable effects (section 4.1), not a byte-identical port.
- **LLM transport.** The harness's `chat()` no longer opens HTTPS from the guest. Instead it writes a request to a root-only mailbox, and a JS LLM broker in the page answers it.

The broker holds the visitor's key in memory and calls the provider directly from the browser. CORS preflights pass for all eight preset hosts, so no relay is planned for v1. However, OpenAI and Cerebras omit `Access-Control-Allow-Origin` on 401 responses, so the broker sees some error statuses only as an opaque network failure (`E_OPAQUE` in `lab/browser/PROTOCOL.md` §3, section 4.6 and spike S9). The broker fixes the endpoint and model, and enforces call, token and wall-clock budgets. The key never enters the guest, the disk overlay, the logs or any Cloudflare component.

**Phasing.** Delivery is gated on a short spike phase. The design depends on four CheerpX capabilities that nobody has verified:
1. loopback TCP between guest processes, which the CTF (`vaultd` on 127.0.0.1:31337) needs as well as the board. A user report on 1.3.7 (webvm#228) mentions loopback trouble in passing; it is unconfirmed by the maintainers;
2. uid/DAC enforcement plus `setgroups`/`killpg`. The same report says there is no guest-side signal delivery, which puts the 30 s command timeout at risk;
3. a guest-to-JS mailbox channel;
4. running more than one CheerpX instance per page.

Every one of these has a defined fallback. The MVP is the **solo** condition (one agent), which needs no per-agent isolation between agents inside one VM. It still differs from the pilot in agent-observable ways (architecture, base-image details, a visible board process and loopback listener), and all of them are recorded in `params`. Multi-agent runs come in a later phase, once the isolation spike is decided. All browser runs are labelled as a separate condition (`env=cheerpx`) and are never pooled with the microsandbox pilot data.

**Cloudflare work (summary):**
- Confirm the `swarmpit.dev` zone is in the same account as AISL.
- Create R2 bucket `swarmpit-vm` with custom domain `vm.swarmpit.dev`, a CORS policy, a cache rule (with Origin Range Requests on) and a transform rule that sets CORP and static CORS headers. Enable Smart Tiered Cache.
- Create Worker `swarmpit-lab` (static assets plus a tiny CSP-injecting handler) on custom domain `lab.swarmpit.dev`.
- Optionally move the apex `swarmpit.dev` to Workers Static Assets.
- Create two API tokens and four GitHub secrets.
- Set zone settings: no Rocket Loader, no RUM auto-inject, no HTML rewrites.
- Add billing and usage alerts.

v1 needs no relay Worker, Turnstile or rate limits. Those are specified as an optional later phase. The expected cost is $0 on the Free plans, with a $5/month Workers Paid fallback.

**Owner decisions required before Phase 2:**
- **D1, licence:** publish as an individual (CheerpX licence 1.4(a)) or buy a commercial licence. The licensing web page and LICENSE.txt disagree about one-person companies (section 4.8), so get written confirmation from Leaning Technologies if any company is involved.
- **D2, seed publication:** confirm the board seed file may be served publicly from the lab origin.
- **D3, visitor consent text.**
- **D4, sharing:** whether visitor run sharing is in scope (default: no).

---

## 2. Option scores

Scale: 1 = poor, 5 = best. "Fidelity" means how closely a run matches the microsandbox lab, measured by agent-observable bytes and log contracts. "Known feasibility" means how much is already verified, versus depending on spikes.

| # | Option | Fidelity | Known feasibility | Key safety | Performance | Effort (5 = least) | CF complexity (5 = least) | Total |
|---|---|---|---|---|---|---|---|---|
| **A** | **CheerpX, one VM; Python board, relay, vaultd and harness in the guest; JS LLM broker over a mailbox (recommended)** | 4 | 3 | 5 | 3 | 4 | 5 | **24** |
| B | CheerpX, one instance per agent, plus a JS board (or a board instance) bridged in JS | 5 | 2 | 5 | 2 | 2 | 5 | 21 |
| C | JS agent loop (AISL agent-panel pattern); CheerpX only runs tool commands through the exec bridge | 2 | 4 | 5 | 2 | 3 | 5 | 21 |
| D | v86 (real kernel, native loopback), same in-guest lab | 4 | 3 | 5 | 1 | 2 | 5 | 20 |
| E | Pyodide "lite" (board rewritten to async/JS, no shell tools) | 1 | 4 | 5 | 4 | 2 | 5 | 21 |
| F | CheerpX with Tailscale/Headscale egress, so the harness calls providers directly | 4 | 2 | 1 | 3 | 3 | 2 | 15 |

Notes on the scores:
- **A** loses a point on feasibility only because of spikes S1, S2 and S5. If only *connecting* to `127.0.0.1` upstreams is the problem, A switches the board link to the AF_UNIX variant (A′, section 4.4) and stays in-guest. If loopback *listeners* fail, A′ cannot help: `netrelayd` must still listen on TCP for the agent, and the CTF itself depends on a TCP loopback listener (`vaultd` on 127.0.0.1:31337, used by the agent-visible `vaultctl.py`). That case is a go/no-go for A, and D (v86) takes over.
- **B** is the multi-agent fallback if namespacing inside one VM (Phase 4) is rejected. It depends on S4 (several instances per page) and on memory headroom.
- **C** cannot enforce the harness's 30 s command timeout, because `cx.run` has no kill or process handle. It also serializes all agents through one global console and moves the harness loop away from the audited Python code.
- **D** stays as the emergency fallback if CheerpX loopback and AF_UNIX both fail.
- **F** is rejected. Either an auth key is embedded in a public page, or every visitor needs a Tailscale account. Either way the key would enter the guest.

---

## 3. Architecture

```
Visitor browser, origin https://lab.swarmpit.dev  (COOP same-origin, COEP require-corp, strict CSP)
+-------------------------------------------------------------------------------------------+
|  Page (TS, self-hosted assets; xterm vendored)                                            |
|                                                                                           |
|  [Consent + key entry] --> KeyVault (in-memory; optional sessionStorage, opt-in)          |
|          |                                                                                |
|          v                                                                                |
|  [Provider probe: 1 tool-use call, wire-flag detection]                                   |
|          |                                                                                |
|  LLM Broker  <---- mailbox (schema-validated, size-capped) ---->  CheerpX Linux (i386)     |
|   - fixed base URL + model (chosen pre-boot)                      +---------------------+ |
|   - attaches Authorization                                        | labctl.py (root)    | |
|   - caps: calls/tokens/wall-clock/concurrency                     |  setup_vm.py        | |
|   - maps errors to fixed codes                                    |  board 127.0.0.1:   | |
|   - filters response to allowed fields                            |   9000+i (or A' UDS)| |
|          |                                                        |  netrelayd 127.0.0.2| |
|          |                                                        |   :<board_port>     | |
|          | fetch() direct (CORS)                                  |   (board.internal)  | |
|          v                                                        |  vaultd 127.0.0.1:  | |
|   Provider API (OpenAI / OpenRouter / Groq / Cerebras*            |   31337             | |
|   / Berget* / localhost Ollama ...)                               |  harness.py (root)  | |
|                                                                   |   -> bash as agent  | |
|  Live view (textContent only) <-- log pump (IDB export mount) <---|  /root/runs/<id>/   | |
|  Export (.zip of canonical run dir, local download only)          |  -> /root/export/   | |
|                                                                   +---------------------+ |
|                                                                     no networkInterface   |
|  CheerpX runtime  <-- https://cxrtnc.leaningtech.com/<pinned>/ (CORP cross-origin)        |
|  Disk: HttpBytesDevice(vm.swarmpit.dev/vm/swarm-<sha12>.ext2)                             |
|        -> OverlayDevice(IDBDevice("swarm-<sha12>-<runid>"))  (per-run, deleted after)     |
|  Runtime data via DataDevice: cfg.json, seed (*.chatter.json), mailbox responses          |
+-------------------------------------------------------------------------------------------+

Cloudflare (same account as AISL, zone swarmpit.dev)
  lab.swarmpit.dev   Worker "swarmpit-lab": static assets + _headers; Worker only for "/" (CSP)
  vm.swarmpit.dev    R2 custom domain -> bucket swarmpit-vm; Cache Rule; CORP transform; CORS policy
  swarmpit.dev       (optional) Worker "swarmpit-site": static assets for site/public
  [later, optional]  llm-relay Worker: allowlisted hosts, Turnstile, rate limit, no logging
* = CORS on a successful POST must be confirmed in spike S9. For every provider, S9 also
    records whether 400/401/402/429 responses carry ACAO (OpenAI and Cerebras 401s do not).
<board_port> = 8080 for forum/wiki/cache, 6667 for irc.
```

**Trust boundaries:**
1. The **guest** is untrusted. It runs adversarial content and agent-chosen shell commands. Its only path out is the mailbox, and the broker treats every mailbox message as hostile input.
2. The **page** is trusted code, but it renders untrusted text. It uses textContent only, with Trusted Types and CSP as backstops.
3. **Cloudflare** never sees the key in v1.
4. The **provider** sees the full conversation. The consent screen discloses this.

---

## 4. Component design

### 4.1 Guest image (`lab/browser/image/`)

**Base image.** `python:3.12-bookworm` (the full, buildpack-deps-based image) with `--platform=linux/386`, the same tag the pilot uses (`lab/config.py` `IMAGE`). This keeps the README's Python 3.12 requirement and the pilot's tool set (`curl`, `wget`, compilers and so on). Pin it by digest, and confirm in S3 that the 386 manifest exists for the pinned digest.
- Size fallback: `python:3.12-slim-bookworm` (386) plus the buildpack-deps tools agents are likely to use (at least `curl`, `wget`, `git`, `procps`, `netcat-openbsd`, `less`, `file`, `unzip`). Record this as a base-image difference.
- Last fallback: `i386/debian:bookworm` with python3 3.11. The VM-side code compiles on 3.9–3.11, and `tests.test_board` passes 19/19 on 3.11 and 3.10.

**Packages.** Pinned through `snapshot.debian.org` at a fixed date, on top of the base:
- `busybox-static` (i386; replaces the vendored aarch64 busybox), `passwd` (useradd), `procps` (PR #4 uses `pgrep`), plus anything from the list above the base lacks.

`params.image` must be set explicitly in `run.json` (for example `python:3.12-bookworm@sha256:…/linux/386`). Ingest otherwise fills in the pilot default `python:3.12-bookworm` (`dash/server/obs/ingest.py`, `IMAGE` and `ensure_run`) and would mislabel browser runs.

**Required accounts.**
- `user` (CheerpX requires it).
- `agent` (created at runtime by `setup_vm.py`, as today).
- No default passwords. AISL's `password` is not reused.

**Staging is allowlist-only.** The AISL builder runs `git archive main`; this builder does not. It copies only:
- `lab/board/**`
- `lab/agent/harness.py`, `lab/agent/setup_vm.py`
- `lab/browser/guest/**` (`labctl.py`, `llm_mailbox.py`, `ctf.py`)

It never copies seeds, runs, results, configs with secrets, `*.jsonl`, or `lab/orchestrate.py` (which imports the msb `sandbox` and `spend` modules; its CTF helpers are vendored instead, section 4.2).

**Build steps.** Port `aisecurityliteracy.dev/tools/build-vm-image.mjs`:
1. `docker build --platform=linux/386`.
2. `docker export`.
3. `mkfs.ext2 -r 0 -b 4096 -d staging out <size>`.

**Size target: ≤ 400 MB.** This must stay under the 512 MB CDN cacheable-object limit on non-Enterprise plans. The full image is the main threat to this budget; S3 measures it, and the slim-plus-tools fallback applies if it does not fit.

**Naming and manifest.** The image is named `swarm-<sha256[:12]>.ext2`. A manifest `swarm-<sha12>.manifest.json` records the full sha256, size, base digest, snapshot date and CheerpX version.

**Leak gate before upload** (`lab/browser/image/leak_gate.py`). It scans the staged rootfs for:
- forbidden path classes (seeds, runs, results, `*.jsonl`, `*.chatter.json`, `*.out`);
- key-shaped strings;
- any file not on the allowlist.

It emits only fixed error codes and counts, and exits non-zero on any hit.

**Agent-observable differences from the pilot.** Record these in the run's `params` field:
- `uname -m`, release and version as measured in S3, not assumed. The one public report shows `Linux 4.15.0-54-cheerpx (i386)`, against aarch64 in the pilot;
- `/proc/cpuinfo` differs;
- busybox is the i386 build;
- the Python patch level and base-image digest differ (and the base image itself, if the slim fallback is used);
- **board co-residence.** In the pilot the board runs in a separate VM and is invisible to the agent. Here `ps aux` shows `python3 -m board --interface … --seed /root/lab/seed.json --log /root/logs/events.jsonl` and `labctl.py`, and busybox `netstat` (symlinked by `setup_vm.py`) shows the board listening on 127.0.0.1:9000+i (absent under A′);
- **hostname.** In the pilot the msb VM name (`sb-<vm_id>-a<i>`) becomes the guest hostname. `labctl.py` sets the same pattern if CheerpX allows it (S2), and otherwise records the observed hostname.

Hiding the board is optional: run it under a neutral argv (for example via a renamed interpreter symlink). `hidepid` is unlikely to be available (section 4.10). Whatever is chosen, the exact visible process list is recorded as `params.visible_procs` (a fixed list of argv[0] values, no payload).

### 4.2 In-guest orchestrator: `labctl.py` (new; runs as root)

It ports the per-run logic of `orchestrate.run_one` / PR #4 `_run_one` into the guest. The orchestrator module itself is not staged (it imports `config`, `sandbox` (msb) and `spend`), so the CTF helpers (`WORDS`, `new_ctf`, `unsolvable_ctf`, `seal_b64`) are vendored into `lab/browser/guest/ctf.py`, with a unit test that checks parity with `lab/orchestrate.py`.
1. Read `/root/mnt/in/cfg.json` (delivered by DataDevice). It has no `ctf` block.
2. **Generate CTF secrets inside the guest** with the vendored helpers. As in the pilot, `new_ctf` draws the passphrase with `random.sample` and the keys with `secrets`; parity matters more than switching the passphrase to `secrets`. CTF material is never baked into the image. Merge the `ctf` block into the cfg and write it to **`/root/lab/cfg.json`** (mode 0600). That is the path the unchanged harness argv reads, and `setup_vm.py` derives `LAB` (and the busybox path) from its directory. Also write `/root/runs/<run_id>/cfg-a{i}.json` as the exported copy, which is what the pilot's host-side `cfg-a{i}.json` corresponds to.
   - **Unsolvable goal.** PR #4's invariant is that the checker's flag never enters the VM (`harness.py` header; `unsolvable_ctf` passes only the VM ctf with `vault_b64`). Generating it in the guest would leave the host ctf (`flag`, `seal_key_hex`, `sealed_flag`) protected only by DAC on `/root`. Preferred: for unsolvable runs the page generates the draw and both ctfs (a TS port of the helpers using `crypto.getRandomValues`, parity-tested against `orchestrate.py`), puts only the VM ctf into `cfg.json`, and inserts the host ctf into `run.json` when it builds the export zip. Fallback: `labctl.py` generates in the guest, writes only the VM ctf to `/root/lab/cfg.json`, and discards the host ctf after sealing; the changed invariant is recorded as `params.ctf_host_in_guest = true`.
3. Copy the seed from `/root/mnt/in/seed.chatter.json` to `/root/lab/seed.json`. The copy is byte-for-byte and nothing is printed.
4. Run `setup_vm.py` with a new parameter, `relay_upstream`:
   - `127.0.0.1:<9000+i>` (default, replacing the pilot's `host.microsandbox.internal:<relay_host_port>`); or
   - `unix:/run/board/a{i}.sock` (A′ variant, section 4.4).

   Other agent-visible bytes stay unchanged, including the `/etc/hosts` line `127.0.0.2 board.internal`, the `netrelayd` listener on `127.0.0.2:<board_port>` and the harness command line. `FIX_LF` (the LF→CRLF rewrite for IRC, an msb egress workaround) is kept as is, so the relay's byte stream matches the pilot; S1 checks whether it is still needed, and the choice is recorded as `params.relay_fix_lf`.
5. Start the board: `python3 -m board --interface X --ports ... --seed /root/lab/seed.json --log /root/logs/events.jsonl`. The board binds `127.0.0.1` through a new `--bind` option (the pilot binds `0.0.0.0` in a separate VM). Its process is visible to the agent (section 4.1).
6. Start each harness with `nohup python3 /root/lab/harness.py /root/lab/cfg.json > /root/logs/harness.out 2>&1 &`, the pilot's exact `HARNESS_START`.
7. Poll for `DONE` or `FATAL`, then assemble `/root/runs/<run_id>/` in the **canonical layout** that `dash/server/obs/ingest.py` expects:
   - `run.json`, `cfg-aN.json`, `board_events.jsonl`, `board.out`, and **`seed.json`** (CONTRACT §3.2: a `seed.json` in the run directory takes precedence; without it ingest marks the seed as inferred);
   - `aN/{steps.jsonl, summary.json, relay.jsonl, harness.out, vaultd.out[, FATAL]}`.
8. Write `run.json` last, because it is the commit marker.
9. Copy the finished run directory into the export mount `/root/export/<run_id>/` (section 4.9), `run.json` last.

**FATAL and void runs.** In the pilot, a non-default provider's fatal or all-crashed run is moved to `RUNS/_aborted/<run_id>-<status>` and settled in the spend ledger (`fatal_of`, `void_of`, `abort_run`). Browser runs always use a non-default provider, so `labctl.py` ports `fatal_of`/`void_of` and writes `run.json` with `fatal: {agent, status}` (or `void`) set. The export names the zip `<run_id>-aborted-<status>.zip` (implemented as `<run_id>-aborted-<fatal.status>.zip`, `lab/browser/PROTOCOL.md` §5.1; `void_of` is not ported), and the live view shows the run as aborted, not finished. `E_BUDGET` stops count as FATAL with status 402 and code `E_BUDGET` recorded in `params.stop_reason`, so a budget stop is distinguishable from a provider credit failure.

**Run ID.** `<iface>-solo-r<rep>-cx<rand6>`. This matches the ingest grammar `(…)-(solo|multiN)-rN(-tag)?`.

**`run.json` additions.** These go under `params`, with an additive amendment to `dash/CONTRACT.md`. Ingest overrides only the keys present, so every key that differs from the pilot default must be written explicitly:
- `env: "cheerpx"`
- `image` (explicit; see 4.1) and `image_sha`
- `cx_version`
- `layout: "solo-v1" | "shared-vm-v1"`
- `topology: "board-coresident"` and `relay_upstream: "tcp-loopback" | "unix"`
- `uname` (machine, release, version as measured), `visible_procs`, `relay_fix_lf`
- `timeout_enforcement: "killpg" | "vm-teardown"` (section 4.3)
- `browser` (UA family only)
- `broker_caps`, `stop_reason`

### 4.3 Harness transport change (`lab/agent/harness.py`, on top of PR #4)

- Add `cfg.provider.transport ∈ {"https", "mailbox"}`. The default is `"https"`, so the microsandbox path stays byte-identical.
- In `"mailbox"` mode, `chat()` calls `llm_mailbox.request(body) -> (status, json, retry_after)`. Two further harness changes are needed:
  - **Key lookup.** PR #4 reads `KEY = os.environ[P.get("secret_env", "CEREBRAS_API_KEY")]` at import and raises `KeyError` if the variable is unset. In mailbox mode the lookup is skipped. (Alternative: `labctl.py` sets a placeholder env var, as msb does.)
  - **Body shaping.** `chat()` builds a body with `"model"` and sends a `User-Agent` header. As implemented, the harness's `MailboxConn` sends the body as is and the broker forwards only `messages`, `tools`, `temperature` and `max_tokens` with the page's own model (`lab/browser/PROTOCOL.md` §3 Upstream); there is no `E_SCHEMA`.
- Everything else is unchanged:
  - wire shaping (`merge_user_after_tool`, `drop_empty_assistant`, `sanitize_tool_args`);
  - retry and backoff on 408/409/425/429/5xx, and up to 3 resends on 400 when a provider block is present;
  - FATAL on 401/402/403 and credit patterns;
  - token accounting.
- The broker's statuses are chosen to drive these existing paths deliberately; the implemented table is `lab/browser/PROTOCOL.md` §3 Codes (`E_BAD_REQUEST` → 400, `E_ABORT` and `E_BUDGET` → 402, `E_RATE` → 429, `E_UPSTREAM` → 502, `E_AUTH` → 401, `E_OPAQUE` → 503, then 401 from the third in a row).
- The harness still runs as root and drops privileges for agent commands: `Popen(user="agent", group="agent", extra_groups=[], start_new_session=True)`, then `killpg` after 30 s. Spike S2 verifies this, but a 1.3.7 user report says CheerpX has no guest-side signal delivery, so it may not work. **Fallback:** if S2 shows `killpg` cannot stop a CPU-bound loop, a process blocked in `read`/`select`, or a forked tree, the page enforces a run-level wall-clock abort by VM teardown (no per-command timeout), and the run records `params.timeout_enforcement = "vm-teardown"`. That condition is not comparable with the pilot on timeout-related metrics.

### 4.4 Board placement and loopback

**Pilot topology, for reference.** The board runs in a separate board microVM, binds `0.0.0.0`, and its guest ports 9000+i are published on host `127.0.0.1:19000+i`. In the agent VM, `netrelayd` listens on `127.0.0.2:<board_port>` and connects upstream to `host.microsandbox.internal:<relay_host_port>` (= 19000+i).

**Primary path (S1 passes).** This is a **topology change**: the board is co-resident in the agent's VM, and the relay's upstream is guest loopback. The agent still connects to `board.internal:<board_port>` (8080 for forum/wiki/cache, 6667 for irc), which resolves to 127.0.0.2. `netrelayd` relays that to `127.0.0.1:9000+i`, where the board listens. `relay.jsonl` is preserved, so board-step attribution (CONTRACT §3.3 item 2, "`is_board` via relay", line 726 at `4194cd1`) and clock offsets (§3.3 item 3, lines 731–732) keep working. The agent-observable effects (visible board process, extra loopback listener) are listed in 4.1 and recorded in `params.topology`.

**The CTF also needs loopback TCP.** `vaultd` is a `ThreadingTCPServer` on `127.0.0.1:31337` run as root, and the agent-visible `vaultctl.py` connects to it from the agent uid. This is independent of the board placement, so S1 must pass for vaultd/vaultctl regardless of A or A′.

**A′, the AF_UNIX hardening and fallback.** Use this if S1 shows that connecting to a 127.0.0.1 upstream fails while listeners work. Also consider it for multi-agent attribution.
- The board gains an optional `--unix-dir /run/board` mode, with one socket per agent, `a{i}.sock`, in a root-only 0700 directory. The HTTP interfaces (forum, wiki, cache) use `ThreadingUnixStreamServer` in `http_common.py`. The irc interface is a separate asyncio server in `board.py` and needs `asyncio.start_unix_server` instead. Agent identity comes from the socket path instead of the port. Agents never see this change, and the board's loopback listener disappears from `netstat`.
- `netrelayd` connects to the Unix socket upstream. It still has to *listen* on 127.0.0.2:<board_port> for the agent, so A′ only helps if the listener side works.
- If listener-side loopback fails entirely, A″ applies. The agent-facing endpoint has to become something other than TCP, and vaultd breaks too. That changes agent-observable behavior (the README, tools such as `curl`, `nc`, `vaultctl.py`), and it is a **go/no-go for CheerpX**. In that case, evaluate D (v86) before shipping a degraded condition.

**Direct board access.** With the primary path, an agent can bypass the relay and connect to 127.0.0.1:9000+i directly. That traffic would show up in `board_events.jsonl` but not in `relay.jsonl`. **The pilot has the same bypass:** the agent VM's egress rule `allow@host:tcp:<19000+i>` applies to the whole VM, so the agent uid can reach `host.microsandbox.internal:19000+i` directly. A′ is therefore a hardening beyond pilot parity, not a regression fix. It is recommended for multi-agent runs (where attribution matters more), but solo-v1 keeps TCP loopback as the default because that is closer to the pilot.

### 4.5 Mailbox (guest ↔ page)

Superseded by the implemented contract in `lab/browser/PROTOCOL.md` (mounts, mailbox, status, export).

### 4.6 LLM broker (page JS, implemented as `lab/web/public/js/broker.js`)

**Configuration is fixed before boot:**
- base URL (preset, or a visitor-entered host shown in the CSP; see 4.8);
- model ID;
- wire flags (detected by the probe);
- caps.

**Probe.** Two steps:
1. **Key check:** `GET <base>/v1/models`. On OpenAI (401) and Cerebras (403) this error response does carry ACAO, so a bad key is reported as `E_AUTH` instead of an opaque failure.
2. **Tool-call check:** one non-streaming chat completion with a single neutral tool definition and a neutral placeholder instruction. It confirms CORS on a successful POST and tool-call support. Wire flags are chosen from a per-provider preset table first. A 400 is used to try the next flag set only where S9 showed that the provider's 400 responses are CORS-readable; an opaque failure ends the probe with a fixed reason.

The probe shows the result as pass/fail plus a fixed reason.

**Caps.** Defaults are editable before start but not during a run:
- calls per agent: `budget + 25`;
- total tokens per run: default 1.5 × the PR #4 prior (350k prompt + 20k completion per agent);
- wall clock: default 90 min;
- concurrency: 2 in-flight requests.

When a cap is hit, the broker returns `status 402, code E_BUDGET`. The harness's FATAL path then ends the run cleanly.

**Cost preview.** Before start, show estimated tokens × visitor-entered (or preset) prices per Mtok. Live spend appears in the header.

**Abort.** Stop the broker (pending requests get `E_ABORT`), then stop the VM by tearing down the instance or reloading. Delete the run's IDB overlay unless the visitor exports first.

**Headers.** Send only `Authorization: Bearer <key>` and `Content-Type: application/json`, and do not set User-Agent. This minimises the preflight surface and fingerprinting. (It is not a provider restriction: the Cerebras, OpenAI, Groq, Together and DeepSeek preflights echo whatever headers are requested.)

**Not logged.** Bodies, headers and errors are never logged to the console. There is no telemetry and no analytics.

### 4.7 Key lifecycle

| Stage | Rule |
|---|---|
| Entry | Password-type input on the consent/settings screen; never placed in a URL. |
| Storage | In-memory by default. A "Remember for this tab" checkbox opts in to `sessionStorage`. Never `localStorage`, and never IndexedDB. |
| Use | Only the broker's `fetch` reads it. |
| Never | Never in guest env, files, mounts, cfg.json, overlay IDB, run exports, live view, console, error messages or screenshots. |
| Clear | On abort, on "Forget key", and on tab close (sessionStorage). |
| Test | A canary fake key is entered; after a full run, a tool scans the overlay IDB, the mailbox and export IDB mounts, and the export zip, and reports only `PRESENT`/`ABSENT`. |

### 4.8 Page security

- **Dedicated origin.** `lab.swarmpit.dev` hosts no decks, no third-party scripts and no other apps.
- **Self-hosted assets.** xterm (MIT) is vendored, as is all of the app's JS and CSS.
- **CheerpX.** It loads from `https://cxrtnc.leaningtech.com/<pinned>/cx.esm.js`. The no-self-hosting and attribution terms come from the licensing page (cheerpx.io/docs/licensing: self-hosting needs a Commercial License; the community licence allows unmetered use from the hosted CDN). LICENSE.txt itself (1.2(b)/(c)) does not prohibit self-hosting, and the page and LICENSE.txt 1.4(b) disagree about one-person companies, hence the D1 caveat. Licence 1.5 ties the Licensee Application to a single origin, which `lab.swarmpit.dev` (including `/obs/`) satisfies. CSP restricts script sources to that exact version path. SRI is not used: `cx.esm.js` is a small stub that imports `cx_esm.js`, which loads `cheerpOS.js` through a script element and `cxcore.js`/`workerclock.js`/`cxbridge.js` into Workers.
- **Import path.** The page uses a direct `await import("https://cxrtnc.leaningtech.com/1.3.9/cx.esm.js")`, marked external in Vite. The npm wrapper `@leaningtech/cheerpx` is used for `index.d.ts` types only: its `index.js` calls `new Function("x", "return import(x)")`, which would need `'unsafe-eval'` and is a Trusted Types sink.
- **Rendering.** All transcript, board, model and log text is rendered with `textContent` only. AISL's `renderMarkdown` is not reused. CSP includes `require-trusted-types-for 'script'`.
- **Trusted Types and CheerpX.** The 1.3.9 runtime sets a script element's `.src` to `<base>cheerpOS.js` and calls `new Worker(<string>)` with `blob:` URLs and `<base>cxbridge.js`. Both are TrustedScriptURL sinks and would throw with only a `swarmpit` policy. Before importing CheerpX, the page registers `trustedTypes.createPolicy("default", {createScriptURL})` that allows only `blob:https://lab.swarmpit.dev/…` and `https://cxrtnc.leaningtech.com/1.3.9/…` and throws on anything else. The CSP allows `trusted-types swarmpit default`. The runtime contains no `eval` or `new Function`, so `'wasm-unsafe-eval'` is enough. This is the expected S8 outcome; if it still fails, drop `require-trusted-types-for` and record that here.
- **Custom endpoint.** The CSP is static per page load, so a custom OpenAI-compatible host is chosen *before* load as `?endpoint=<host>`. The Worker validates the hostname syntactically and adds only that host to `connect-src`. Preset hosts and `http://localhost:*` / `http://127.0.0.1:*` are always allowed.
- **Disk image is fail-closed.** There is no fallback to `wss://disks.webvm.io`. The overlay ID is `swarm-<sha12>-<runid>`. Overlays for other image hashes are garbage-collected on load.
- **Gate.** Key entry is refused unless `crossOriginIsolated === true`.

### 4.9 Live view and export

**MVP live view (`liveview.ts`):**
- per agent: phase, steps used, model calls, tokens, spend;
- the latest command and the latest output tail (textContent, truncated);
- board events, counted by kind.

Data comes from the log pump. `/root/runs` lives on the ext2 root, whose IDBDevice is only the overlay's block cache, so the page cannot read it with `readFileAsBlob`. Instead a separate `IDBDevice` is mounted as a `"dir"` filesystem at `/root/export` (under 0700 `/root`). `labctl.py` periodically copies the live logs there (rename-into-place) and copies the finished run directory there at the end. The page reads only from that mount. `readFileAsBlob` is not streaming, so the pump re-reads whole files at a modest interval (2–5 s) and keeps the copies small by copying only appended tails during the run. The `livetap.py` framed-tail protocol via `cx.run` is an alternative (the implemented mailbox uses files, not console framing). As implemented, only `a0/steps.jsonl` is mirrored during the run and the full run directory only after every agent process is stopped (`lab/browser/PROTOCOL.md` §5.3).

**Export.** A local-only download: a zip of the canonical run directory, read from `/root/export/<run_id>/`. The maintainer or the visitor can load it with `obs ingest` locally; this is a parity check for the observatory. There is no upload in v1.

**Later (Phase 5).** A `local` backend for `dash/web`, next to `mock/server.ts` (`handle` plus `liveStream`). `api.ts` needs an `await` for async handlers (it calls `m.handle` synchronously today). The port is larger than a parser: run-view responses are shaped by SQL in `api.py` and by views in `schema.sql`, and `derive.py` is itself SQL-driven (about 40 statements). `mock/server.ts` only generates fixtures, so there is no data-derived logic to reuse. Phase 5 therefore either ports `parse.py`, the run-view parts of `derive.py`, `api.py`'s response shaping and the `schema.sql` views used by the run view to TS, or ships DuckDB-WASM in the page and reuses the SQL. (The "no i686 wheels" argument only rules out DuckDB inside the guest, not in the page.) The choice is made at the start of Phase 5.

### 4.10 Multi-agent (Phase 4, after S2 and S4)

- **Option B (one CheerpX instance per agent).** Use it only if S4 shows that several `Linux.create` calls per page work and memory allows N=4. The board runs in one instance, and the page bridges each agent's relay to the board. This needs TCP-over-mailbox inside the relay, and is complex.
- **Option A-multi (one VM, namespaced).** Per-agent users `agent0..3`, homes, submission files, vaultd ports and Unix board sockets (A′).
  - **Attribution.** `setup_vm.py` appends one fixed line, `127.0.0.2 board.internal`, to the shared `/etc/hosts`, so every agent resolves `board.internal` to the same address and per-agent relay IPs (`127.0.0.2..5`) are unreachable through the name agents are told to use. Instead, one `netrelayd` listens on 127.0.0.2:<board_port> and attributes each connection to an agent by the peer's uid (look up the socket inode in `/proc/net/tcp` and map it to the owning uid; S2 checks this works in CheerpX), then relays to that agent's Unix board socket. Per-agent mount namespaces are not assumed, since guest `mount()` returned "Function not implemented" on 1.3.7.
  - **Hard-coded paths to parameterize** (all in PR #4): harness `LOGDIR = "/root/logs"` (`steps.jsonl`, `summary.json`, `DONE`, `FATAL`); `/var/tmp/submissions`; `/root/vaultd.py` and its port 31337 (also in `vaultctl.py` and `config.ini`); `/usr/sbin/netrelayd`; `/root/logs/relay.jsonl`, `vaultd.out`, `relay.out`; `/home/agent`; and the `agent` user/group names in the harness `Popen`.
  - Breaks: `ps aux` shows the other agents' processes, and the observable bytes change.
  - Classification: it is a **new experiment version** (`layout=shared-vm-v1`), not a replication.
  - `hidepid` is assumed **unavailable**: guest `mount()` is unimplemented on 1.3.7, and the 1.3.9 `MountPointConfiguration {type:"proc", path, dev}` has no options field. "Other agents are visible in `ps aux`" is a fixed property of `shared-vm-v1`. S2 only confirms this.
  - **Default choice if S4 fails.**
- Either way, start at N=2. Measure memory with `performance.measureUserAgentSpecificMemory` and cap N on observed headroom.

---

## 5. Repository changes

All new lab work lands in **schizboard**, on a branch from PR #4's merge (`4194cd1`), never from `b235844`. The AISL repo is not modified; its policy is main-only, with no Actions Pages and no CI deck rebuilds. Code copied from AISL notes its origin in a header comment. Both repos are the owner's.

| Path | Change |
|---|---|
| `data/synthetic-agent-chatter/` | New. Holds `seed-<id>.chatter.json` and any lure/breadcrumb text records. |
| `tools/chatter/migrate.py` | New. Moves `lab/seeds/seed.json` and the inline breadcrumb text in `setup_vm.py` into `data/synthetic-agent-chatter/*.chatter.json` without printing. Rewrites references to record IDs and reports only counts and status. |
| `tools/chatter/validate.py` | New. Schema and type validation (store.py seed schema). Prints pass/fail, record counts, fixed error codes, line/column only. |
| `lab/agent/harness.py` | `provider.transport` switch; `chat()` delegates to `llm_mailbox` in mailbox mode; `KEY` env lookup skipped in mailbox mode. The https path is byte-identical. Phase 4: `LOGDIR`, submissions path and agent user name taken from cfg. |
| `lab/agent/setup_vm.py` | `relay_upstream` parameter (TCP or unix; replaces the hard-coded `host.microsandbox.internal`); breadcrumb text loaded from a cfg-supplied record instead of inline; busybox path configurable. Phase 4: `/var/tmp/submissions`, `/root/vaultd.py` + port, `/usr/sbin/netrelayd`, `/root/logs/*`, `/home/agent` parameterized; uid-based relay attribution. |
| `lab/board/__main__.py`, `http_common.py`, `board.py` | Optional `--bind` and `--unix-dir` options, parsed in `__main__.py` (`start_http_servers()` already accepts `host=`). `--unix-dir` uses `ThreadingUnixStreamServer` for HTTP interfaces and `asyncio.start_unix_server` for irc (which binds `0.0.0.0` in `board.py` today). Defaults unchanged. Tests added to `tests/test_board.py`. |
| `lab/browser/guest/labctl.py`, `llm_mailbox.py`, `ctf.py` | New in-guest orchestrator, mailbox client (strips `model`/headers to schema v1) and CTF helpers vendored from `orchestrate.py`, plus a parity test (stdlib only). |
| `lab/browser/image/{Dockerfile,build_image.mjs,leak_gate.py,package-allowlist.txt}` | New i386 image pipeline (section 4.1). |
| `lab/web/` | New Vite+TS app: `boot.ts` (device chain from AISL `terminal-panel.js:375-440`), `broker.ts`, `mailbox.ts`, `keyvault.ts`, `consent.ts`, `probe.ts`, `liveview.ts`, `export.ts`, `spikes/` (spike harness page). |
| `lab/web/worker/{wrangler.toml,src/index.ts}` | `swarmpit-lab` Worker (section 6). |
| `lab/web/public/_headers` | Static-asset headers. The Vite build copies `public/` into `dist/`, which is where Workers Static Assets reads `_headers`. |
| `lab/web/dev-server.mjs` | COOP/COEP/CSP dev server for spikes and local runs (`site/tools/serve.mjs` is frozen by `site/SPEC.md` §1 and is not changed). |
| `lab/web/tests/` | Playwright tests (Chromium, Firefox, WebKit): boot, solo run against a **local mock provider** that returns neutral placeholder completions, key-absence canary, export schema validation. The mock never uses chatter; seeds are loaded from the JSON file and never printed. |
| `.github/workflows/deploy-vm-image.yml` | Manual dispatch: build, leak gate, `aws s3 cp` (multipart) to `swarmpit-vm`, then upload the manifest. |
| `.github/workflows/deploy-lab.yml` | On push to `lab/web/**`: build, test, `wrangler deploy`. The image manifest is pinned in `lab/web/vm-manifest.json` (committed, fail-closed). |
| `dash/CONTRACT.md` | Additive: `params.env/image/image_sha/cx_version/layout/topology/relay_upstream/uname/visible_procs/relay_fix_lf/timeout_enforcement/browser/broker_caps/stop_reason`; `-cx<rand>` run-ID tag. No ingest code change is needed for `params.image`, because `run.json` params override the ingest default. |
| `site/` | Link to the lab only. The lab is a separate app, so `check_public.py` and SPEC §0.1 are unchanged. |
| `AGENTS.md` + `CLAUDE.md` | Add `lab/web/dist/data/**`, the IDB/exported run zips and `lab/browser/image/staging/**` to the known chatter-bearing paths. Keep both files identical and propagate to every worktree. |
| `.gitignore` | Add `/aisecurityliteracy.dev/`. It is **not** ignored today: the main checkout's `.gitignore` has only `/runs/` and `lab/vendor/` among relevant entries, and `git status` lists `aisecurityliteracy.dev/` as untracked. (Alternatively, add it to `.git/info/exclude` to keep it local.) |

---

## 6. Cloudflare setup checklist

Do these in order. Steps marked "verify" have a command in step 10.

### Step 0: Account and zone
1. In the dashboard, confirm `swarmpit.dev` is a zone in account `<CLOUDFLARE_ACCOUNT_ID>`, the AISL account. Public DNS already shows Cloudflare NS `angela`/`dimitris`, but the account is unverified. An R2 custom domain needs the zone and the bucket in the same account. If the zone sits in another account, move it or put everything in that account.
2. **SSL/TLS:** set the mode to **Full (strict)**, turn on **Always Use HTTPS**, and set minimum TLS 1.2. `.dev` is HSTS-preloaded, so certificates must exist before any hostname goes live. Workers custom domains and R2 custom domains provision certificates automatically.
3. **DNSSEC:** enable it on the zone, and add the DS record at the registrar.
4. **Speed and content settings for the zone.** Turn all of these off: Rocket Loader, Email Obfuscation, Automatic HTTPS Rewrites (it is not needed and only rewrites HTML), Cloudflare Fonts, and **Web Analytics / RUM auto-injection**. Injected scripts would break the CSP and COEP. Auto Minify is deprecated; confirm it is off. Mirage was retired on 2025-09-15, and Polish exists only on Pro and above; turn Polish off if the zone is ever upgraded.
5. Leave **Bot Fight Mode** off for `lab.` and `vm.` until it is tested. JS challenges on range requests would break disk streaming.
6. **Smart Tiered Cache:** enable it on the zone (Caching → Tiered Cache), as the R2 public-bucket docs recommend. Misses for `vm.swarmpit.dev` then converge on one upper tier near the bucket instead of every colo pulling from R2.

### Step 1: R2 bucket for the disk image
1. Create R2 bucket **`swarmpit-vm`**, separate from `aisl-vm`.
2. Under Settings → Public access, **connect custom domain `vm.swarmpit.dev`** and keep **r2.dev disabled** (it is rate-limited and not for production).
3. Add a CORS policy (dashboard JSON editor, S3-style). It is a backstop only: R2 adds `Access-Control-*` headers only when the request carries a matching `Origin`, and those headers are cached with the object. Cloudflare ignores `Vary` by default, and adding `Origin` to the cache key is Enterprise-only, so whichever request fills a colo's cache would decide the cached CORS headers for up to a year. The authoritative CORS headers therefore come from the static transform rule in Step 2.2. Do **not** list localhost origins on the production bucket; local development uses a separate dev bucket (`swarmpit-vm-dev`) or the local dev server.
   ```json
   [
     {
       "AllowedOrigins": ["https://lab.swarmpit.dev"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["Range"],
       "ExposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
       "MaxAgeSeconds": 86400
     }
   ]
   ```
   If you prefer wrangler (`npx wrangler r2 bucket cors set swarmpit-vm --file cors.json`), note it expects Cloudflare's `rules`/`allowed` JSON shape, not the S3 shape above.
4. Optional: add an R2 **bucket lock** rule on prefix `vm/` so published images cannot be overwritten or deleted early.
5. Uploaded objects get these settings (done in the workflow): `Cache-Control: public, max-age=31536000, immutable` and `Content-Type: application/octet-stream`.

### Step 2: Cache, CORP and rate settings for `vm.swarmpit.dev`
1. **Cache Rule** ("vm-immutable"): `http.host eq "vm.swarmpit.dev"` → Eligible for cache.
   - Edge TTL: **"Use cache-control header if present, bypass cache if not"** (`bypass_by_default`). It fails closed if an object lacks `Cache-Control`; the workflow always sets it.
   - Browser TTL: respect origin.
   - **Origin Range Requests: on** (API `origin_range_requests: {mode: "on"}`). It is off by default, in which case a cacheable miss strips the client's `Range` and pulls the whole ~400 MB object from R2 before answering, which looks like a boot hang on a cold colo. Spike S7 measures cold-colo first-range latency.
2. **Response Header Transform Rule** ("vm-corp"): `http.host eq "vm.swarmpit.dev"` → **set** (static, on every response, cached or not):
   - `Cross-Origin-Resource-Policy: cross-origin`
   - `X-Content-Type-Options: nosniff`
   - `Access-Control-Allow-Origin: https://lab.swarmpit.dev`
   - `Access-Control-Expose-Headers: Content-Range, Content-Length, ETag, Accept-Ranges`

   Single-range `Range` headers are CORS-safelisted in the Fetch spec, but browser support varies. Any preflight (`OPTIONS`) that does occur is not cached and is answered by the bucket CORS policy from Step 1.3, which carries the production origin.
3. Keep each image **≤ 512 MB**, the cacheable-object limit on Free/Pro/Business. Otherwise every range request hits R2.

### Step 3: Lab Worker (`swarmpit-lab`)
`lab/web/worker/wrangler.toml`:
```toml
name = "swarmpit-lab"
main = "src/index.ts"
compatibility_date = "2026-09-01"
account_id = "<CLOUDFLARE_ACCOUNT_ID>"   # confirm in Step 0

routes = [{ pattern = "lab.swarmpit.dev", custom_domain = true }]

[assets]
directory = "../dist"
binding = "ASSETS"
not_found_handling = "404-page"
run_worker_first = ["/", "/index.html"]     # only the HTML shell goes through the Worker (CSP)

[vars]
CX_VERSION = "1.3.9"                         # set from spike S1-S8 outcome; pin exactly
VM_HOST = "https://vm.swarmpit.dev"

[observability]
enabled = true
head_sampling_rate = 0.05
invocation_logs = false                      # default invocation logs record "<Method> <URL>", i.e. ?endpoint=
```

Without `invocation_logs = false`, Workers Logs records the full request URL for every sampled request, including `?endpoint=<visitor host>`. If that setting is unavailable, set `enabled = false`. Only custom status-only log lines are allowed.

`lab/web/public/_headers` (copied to `dist/_headers` by the build). It applies to static assets; the Worker sets the same headers on `/`. Values for the same header from several matching rules are joined with commas, and `_headers` replaces Cloudflare's default `Cache-Control: public, max-age=0, must-revalidate`, so `/*` carries the full default plus `no-transform`, and the specific paths detach it with `! Cache-Control`:
```
/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Resource-Policy: same-origin
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=(), usb=(), serial=()
  Cache-Control: public, max-age=0, must-revalidate, no-transform
/assets/*
  ! Cache-Control
  Cache-Control: public, max-age=31536000, immutable, no-transform
/data/*
  ! Cache-Control
  Cache-Control: public, max-age=300, no-transform
```

The Worker (`src/index.ts`, roughly 60 lines) works like this:
- It serves `env.ASSETS.fetch(new URL("/", request.url))` with the headers above plus the CSP below. (The binding applies `html_handling`; under the default auto-trailing-slash mode `/index.html` answers 307 to `/`, and a bare path is not a valid URL for the binding.) A unit test asserts status 200 and `text/html` for `/`.
- If `?endpoint=` is present, it validates the value against `^[a-z0-9.-]{1,253}(:\d{1,5})?$`. It rejects IP literals other than localhost, rejects anything ending `.swarmpit.dev`, and adds only `https://<endpoint>` to `connect-src`.
- It never logs query strings, and invocation logs are off (above).

The CSP:
```
default-src 'none';
script-src 'self' https://cxrtnc.leaningtech.com/1.3.9/ 'wasm-unsafe-eval';
worker-src 'self' blob: https://cxrtnc.leaningtech.com/1.3.9/;
connect-src 'self' https://cxrtnc.leaningtech.com https://vm.swarmpit.dev
            https://api.openai.com https://openrouter.ai https://api.groq.com
            https://api.cerebras.ai https://api.berget.ai https://api.together.xyz
            https://api.mistral.ai https://api.deepseek.com
            http://localhost:* http://127.0.0.1:* [https://<validated endpoint>];
img-src 'self' data:; style-src 'self'; font-src 'self';
frame-ancestors 'none'; base-uri 'none'; form-action 'none';
require-trusted-types-for 'script'; trusted-types swarmpit default;
```
The `default` policy exists only for the CheerpX runtime's script-URL sinks (section 4.8) and allows only `blob:` URLs of the lab origin and the pinned CDN path. `'unsafe-eval'` is not expected to be needed (no `eval`/`new Function` in the 1.3.9 runtime, as long as the npm wrapper is not used). Spike S8 confirms whether CheerpX needs anything more, such as extra `worker-src`/`connect-src` origins. Any addition must be documented here with a reason.

Seed delivery: the build copies `data/synthetic-agent-chatter/<seed>.chatter.json` into `dist/data/` with a script that prints nothing. This step requires owner decision **D2**.

### Step 4 (optional, recommended): Apex site `swarmpit.dev`
Today `swarmpit.dev` has no A/CNAME records and GitHub Pages has `cname=null`, so the domain does not resolve. Choose one:
- **(a) Workers Static Assets.** Worker `swarmpit-site` with `[assets] directory = "site/public"` and `routes = [{pattern="swarmpit.dev", custom_domain=true}, {pattern="www.swarmpit.dev", custom_domain=true}]`. Add a `_headers` file with `Cache-Control: no-transform`; the player does not need COOP/COEP. Then retire the Pages workflow, or keep it as a mirror.
- **(b) Keep GitHub Pages.** Set the Pages custom domain to `swarmpit.dev` (repo settings), add DNS `CNAME swarmpit.dev → kristerhedfors.github.io`, proxied, and SSL Full (strict). A Workers Custom Domain cannot be created over an existing CNAME for the same hostname, so switching from (b) to (a) later requires deleting that record first.

### Step 5: API tokens
1. **`CLOUDFLARE_API_TOKEN`** (custom token, for wrangler deploys from schizboard CI):

   | Scope | Permission | Notes |
   |---|---|---|
   | Account | Workers Scripts: Edit | |
   | Zone (swarmpit.dev) | Workers Routes: Edit | |
   | Zone (swarmpit.dev) | Zone: Read | |
   | Zone (swarmpit.dev) | DNS: Edit | For `custom_domain` record creation. Drop it if deploys succeed without it. |
   | Account | Workers R2 Storage: Edit | Only if wrangler manages bucket CORS or config. |
   | Account | Account Settings: Read | Wrangler account lookup. |

2. **R2 S3 token** (R2 → Manage API tokens): **Object Read & Write, restricted to bucket `swarmpit-vm`**. This gives `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. Do not reuse the AISL account-wide key.

### Step 6: GitHub secrets (schizboard repo; it currently has none)
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (or keep `account_id` in wrangler.toml, as AISL does), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.

### Step 7: Image upload workflow (core of `deploy-vm-image.yml`)
```yaml
- run: node lab/browser/image/build_image.mjs --out dist/vm
- run: python3 lab/browser/image/leak_gate.py dist/vm/staging   # fixed codes only; fails the job on any hit
- env:
    AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
    AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
    AWS_DEFAULT_REGION: auto
    CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}   # or hardcode, as AISL's deploy-vm.yml does
  run: |
    F=$(jq -r .filename dist/vm/manifest.json)
    aws s3 cp "dist/vm/$F" "s3://swarmpit-vm/vm/$F" \
      --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" \
      --cache-control "public, max-age=31536000, immutable" \
      --content-type application/octet-stream
    aws s3 cp dist/vm/manifest.json "s3://swarmpit-vm/vm/${F%.ext2}.manifest.json" \
      --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" \
      --cache-control "public, max-age=31536000, immutable" --content-type application/json
```
`wrangler r2 object put` caps uploads at 315 MB (one object at a time; AISL's workflow comment says 300 MB), so use the S3 multipart path as AISL does. The workflow outputs the manifest, and a PR updates `lab/web/vm-manifest.json`. That removes AISL's manual hash-sync problem.

### Step 8: Alerts and budget
- Notifications: **Billing usage alerts**, **R2 storage/operations** usage, and **Workers usage** (Free-plan daily requests).
- Expected Free-plan footprint:
  - static assets: free and unlimited;
  - disk image: served from cache after the first miss per upper tier (Smart Tiered Cache); R2 Class B only on misses (10M/month free); 10 GB storage covers about 25 images;
  - Worker invocations: one per page load of `/`.
- The **$5/month Workers Paid** plan is needed only if the relay (Phase 6) is added, or if `/` traffic exceeds 100k/day.
- **Overrun failure mode.** On the Free plan, paths matched by `run_worker_first` get **429** after the daily limit instead of falling back to static assets. `/` is the only HTML entry point, so the lab is unreachable until 00:00 UTC while other assets keep working. Set the Workers usage alert well below 100k/day (for example 60k). If traffic approaches the limit, either move to Workers Paid or serve `/` as a static asset with headers from `_headers` and a fixed preset-only CSP (custom endpoints then need a Paid-plan Worker or are dropped).

### Step 9 (optional, Phase 6 only): LLM relay Worker
Build it only for providers confirmed by S9 to lack browser CORS:
- allowlisted hosts;
- POST to `/v1/chat/completions` only, https only, `redirect: "manual"`;
- pass through only `Authorization` and `Content-Type`;
- body ≤ 1 MB; responses stripped to a safe header set;
- no `console.log`; Logpush and Tail off for this script; `[observability] invocation_logs = false` (or `enabled = false`), since default invocation logs record method and URL;
- **Turnstile** widget (`wrangler secret put TURNSTILE_SECRET`): after siteverify, mint a short-lived signed session cookie, because tokens are single-use with a 300 s TTL;
- **rate limiting**: Workers rate-limit binding (10/60 s) plus one WAF rate-limit rule on `/relay/*` (Free: 1 rule, 10 s). On Free the rule expression can use only Path and Verified Bot, not Host, so the rule applies to `/relay/*` on every hostname in the zone; that is acceptable because no other hostname uses that path;
- `placement` pinned to avoid OpenAI region 403s;
- a Cache Rule that bypasses `/relay/*`;
- a kill switch via an env var.

The lab CSP blocks Turnstile before COEP matters. Turnstile needs `script-src https://challenges.cloudflare.com` and `frame-src https://challenges.cloudflare.com` (plus `connect-src 'self'` if pre-clearance is used), and its loader is not known to work under `require-trusted-types-for 'script'`. Preferred: put the challenge on a separate page on the lab origin (for example `/verify`) that is served without COEP and without Trusted Types, with only those CSP additions, and that sets the session cookie the relay checks. Adding Turnstile to the main lab page requires those CSP additions, a Trusted Types exception, and a COEP test of the iframe.

### Step 10: Post-deploy verification
```bash
# Headers on the lab shell (expect HTTP 200 and text/html, not a 307)
curl -sI https://lab.swarmpit.dev/ | grep -iE '^(HTTP|content-type|cross-origin|content-security-policy|cf-ray)'
# 0. Purge vm.swarmpit.dev from the cache (dashboard: Caching -> Purge by hostname), as R2 docs
#    require after any CORS change.
# 1. Fill the cache first with a request that has NO Origin header
curl -s -o /dev/null -D - -H 'Range: bytes=0-1' https://vm.swarmpit.dev/vm/swarm-<sha12>.ext2 \
  | grep -iE '^(HTTP|content-range|access-control-allow-origin|cf-cache-status)'
# 2. Range + CORS + CORP with Origin; must show cf-cache-status: HIT AND access-control-allow-origin
curl -s -o /dev/null -D - -H 'Range: bytes=0-1' -H 'Origin: https://lab.swarmpit.dev' \
  https://vm.swarmpit.dev/vm/swarm-<sha12>.ext2 \
  | grep -iE '^(HTTP|content-range|access-control-(allow-origin|expose-headers)|cross-origin-resource-policy|cf-cache-status)'
# 3. A deep-offset range on a cold object must answer 206 promptly (Origin Range Requests on)
curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -H 'Range: bytes=300000000-300004095' \
  https://vm.swarmpit.dev/vm/swarm-<sha12>.ext2
# HEAD must carry Content-Length
curl -sI https://vm.swarmpit.dev/vm/swarm-<sha12>.ext2 | grep -i content-length
# r2.dev must be disabled (expect failure)
```
In the browser console, `crossOriginIsolated === true`. The Playwright smoke test against production passes (boot plus mock-provider solo run, using a test-only build flag that points the broker at a local mock).

---

## 7. Phased roadmap

### Phase 0: Prerequisites (1–2 days)
- Confirm PR #4 (`4194cd1`) is on GitHub `main`, then branch from it.
- Run `tools/chatter/migrate.py` and `validate.py`. Commit the moved files and the reference updates; no content appears in commit messages.
- Owner decisions:
  - **D1, licence:** publish as an individual (licence 1.4(a); hosted CDN and attribution in the footer, both per cheerpx.io/docs/licensing), or get a commercial licence if published under a company. Because the licensing page lists one-person companies under the free Community License while LICENSE.txt 1.4(b) limits any Business to evaluation, get written confirmation from Leaning Technologies before publishing through any company.
  - **D2, seed:** the seed is served publicly from the lab origin, or the lab is not public.
  - **D3, consent text:** adversarial content; the provider receives transcripts; token-cost estimate; the key stays in the browser.
  - **D4, sharing:** out of scope for v1.
- Cloudflare Step 0 (account and zone check) and Step 5 (tokens).

### Phase 1: Spikes (3–5 days); gate G1
A local spike page in `lab/web/spikes/`, served by a COOP/COEP-enabled dev server (`lab/web/dev-server.mjs` or `wrangler dev`; `site/tools/serve.mjs` is frozen by `site/SPEC.md` and stays unchanged). Run each spike on CheerpX **1.3.9** and **1.2.6**, on Chrome, Firefox and Safari. AISL pins 1.2.6, so the AISL device-chain code being reused has only been exercised on 1.2.6; 1.3.9 has no in-repo precedent. CheerpX has no public changelog (cheerpx.io/docs/changelog is 404), so version-specific fixes are judged by observed spike behaviour only. Spikes print only pass/fail and numbers.

| ID | Spike | Pass criterion | If it fails |
|---|---|---|---|
| S1 | Loopback TCP: `python3 -m http.server` bound to 127.0.0.1 and to 127.0.0.2, `curl` from another `cx.run`; `nc` raw TCP; asyncio server (IRC path); ThreadingHTTPServer; **vaultd/vaultctl** (agent uid → root `ThreadingTCPServer` on 127.0.0.1:31337); `getsockname()` on a non-socket fd (must fail with `ENOTSOCK`); `select()` correctly clearing fd_set bits; whether `netrelayd`'s `FIX_LF` is still needed for IRC. Note that 1.3.9 also exports `DirectSocketsNetwork` (Isolated Web Apps only; probably irrelevant) | All connect, no networkInterface; probes correct | Connect-side failure only: AF_UNIX board link (A′). **Listener-side failure is a go/no-go for the CTF itself** (vaultd), not only the board: optionally test a Headscale `controlUrl`-only config (no auth key in the page), else go/no-go for D (v86) |
| S2 | Isolation primitives: boot as uid 0 → `Popen(user=agent, extra_groups=[])`; read 0700 root file (must fail); `setgroups([])`; `ps aux` lines; useradd; `sethostname`; git ops; uid lookup of a TCP peer via `/proc/net/tcp` (for A-multi); `hidepid` (confirmation only). **Timeout enforcement:** `setsid`+`killpg` must actually kill (a) a CPU-bound loop, (b) a process blocked in `read`/`select`, and (c) a forked tree | All pass | Isolation failure: do **not** put CTF secrets in the guest; the lab is not viable in CheerpX as designed; evaluate D. Kill failure only: run-level abort by VM teardown, `params.timeout_enforcement = "vm-teardown"` (section 4.3) |
| S3 | Image: i386 `python:3.12-bookworm` (full) + packages boots; `tests.test_board` 19/19 in guest; size ≤ 400 MB; measure `uname -m/-r/-v` for params | Pass | Slim + buildpack tools (curl, wget, …); then bookworm python 3.11 |
| S4 | Two and four `Linux.create` instances in one page; memory per instance with board + 1 harness | Works, measured | Multi-agent uses A-multi (namespaced) |
| S5 | Mailbox: guest writes on a dedicated `IDBDevice` `dir` mount visible to `readFileAsBlob` while the guest runs (including `null` for missing files); console framing on a root-only console device, and the agent uid **cannot** write to it; DataDevice response visibility (string `writeFile` on 1.3.9); export mount `/root/export` readable by the page; round-trip p50/p95; agent uid cannot read or write mailbox paths | One channel works, p95 < 500 ms | Use the other channel; if neither, use the exec-bridge pattern with a dedicated poller process |
| S6 | Throughput: harness step latency with a mock broker; CTF git ops; 30 s timeout hit rate | Solo run finishes < 60 min | Raise `run_timeout_s` for the cheerpx condition (record it in params) |
| S7 | `HttpBytesDevice` from R2 custom domain, cross-origin under COEP require-corp; HEAD/Content-Length; block size and request count per boot; cold-colo first-range latency at a deep offset (Origin Range Requests on) | Boots; ≤ a few thousand requests per cold boot; first deep range < 2 s | Serve same-origin through the lab Worker with the Cache API (Workers Paid) |
| S8 | CheerpX under the section 6 CSP and Trusted Types, with the `default` policy from 4.8 registered before import. Expected: fails without the `default` policy (script `.src` and `new Worker(string)` sinks), boots with it | Boots with `default` policy | Drop `require-trusted-types-for`, keep the rest of the CSP, document it |
| S9 | Provider CORS (owner runs it with real keys; outputs status code plus ACAO presence only): a **successful** POST, and ACAO presence on **400, 401, 402 and 429** responses, plus `GET /v1/models` with a bad key: Cerebras, Berget, OpenAI, OpenRouter, Groq, local Ollama (`OLLAMA_ORIGINS`). Known: OpenAI and Cerebras 401s on `/chat/completions` lack ACAO; their `/v1/models` errors carry it. For Ollama, record Chrome's Local Network Access prompt outcome separately from CORS | Status recorded per error class | Opaque error classes map to `E_OPAQUE`; wire flags come from the preset table, not 400-probing. No successful-POST CORS: mark the provider "relay required" (Phase 6) or unsupported |

**Gate G1 (go/no-go).** Go with A if S1 (or A′), S2 isolation, S3, S5 and S7 pass. A failed S2 kill test does not block G1 but forces `timeout_enforcement = "vm-teardown"`. Record the CheerpX version choice based on the S1 `getsockname`/`select` probes and the other spike results, not on assumed fixes. If S2 isolation fails, stop and re-plan around v86.

### Phase 2: Solo MVP (1.5–2 weeks); gate G2
- Image pipeline and leak gate; `labctl.py`; harness transport; `setup_vm.py` relay parameter.
- `lab/web`: boot (fail-closed, per-run overlay), consent, key vault, probe, broker with caps, mailbox, live view, export, abort.
- Tests: Playwright solo run against a local mock provider; key-absence canary; export validated by schema-only tool; `obs ingest` of an exported zip succeeds (status and counts only) with `seed.json` present (seed not inferred) and `params.image` set; a mock-provider FATAL run and an `E_BUDGET` stop export as aborted runs; CTF helper parity test against `orchestrate.py`.
- **G2:**
  - 10 consecutive mock-provider solo runs complete on Chromium and Firefox; WebKit boots and completes at least one.
  - Canary key `ABSENT` everywhere.
  - Export ingests with `iface`/`pop` parsed.
  - The owner completes one real-key solo run per supported provider (S9 list).

### Phase 3: Cloudflare deploy (2–3 days, can overlap Phase 2); gate G3
- Steps 1–4 and 6–8, then the first image upload and the lab deploy.
- **G3:** every Step 10 check passes; cache HIT on the second fetch; `crossOriginIsolated` true; a production smoke test passes; alerts configured.

### Phase 4: Multi-agent (1–2 weeks, path set by S4); gate G4
- Implement B or A-multi with Unix board sockets. For A-multi: uid-based relay attribution behind the single `/etc/hosts` entry, and parameterization of every hard-coded path listed in 4.10 (harness `LOGDIR`, submissions, vaultd, netrelayd, relay log, home). Start at N=2, then N=4 if memory allows.
- Add a `layout` param and a CONTRACT amendment. Update the consent screen with the token estimate for N agents.
- **G4:** no cross-agent file reads in a scripted check run as `agent{i}`; submissions attributed correctly; relay-based board attribution consistent with `board_events.jsonl`; memory within headroom on an 8 GB laptop.

### Phase 5: In-browser observatory (1–2 weeks)
- `dash/web` local backend, fed by the log pump: either a TS port of `parse.py`, the run-view parts of `derive.py`, `api.py`'s response shaping and the `schema.sql` views the run view uses, or DuckDB-WASM in the page reusing the SQL (section 4.9). Decide at phase start; the estimate assumes DuckDB-WASM and grows to 2–3 weeks for a full TS port. Build `dash/web` as a static bundle served under `lab.swarmpit.dev/obs/`.
- Optional: local site-player replay through an in-memory `Loader` (finished runs only).

### Phase 6 (optional): Relay and sharing
- Relay Worker per Step 9, only for S9 "relay required" providers.
- Opt-in run sharing (only if D4 changes):
  - Turnstile-gated upload to a **private** R2 bucket (`swarmpit-uploads`, no public domain), stored as `*.chatter.json`;
  - host-side key-pattern scan before upload;
  - rate limits;
  - visitor-facing deletion.

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CheerpX loopback `bind()` fails without a tailnet (unconfirmed side note in webvm#228 on 1.3.7) | Medium | High (listener failure also breaks the CTF's vaultd) | S1 on 1.3.9 and 1.2.6, including vaultd; A′ AF_UNIX for connect-side failures; Headscale controlUrl-only test; v86 fallback |
| uid/DAC or `setgroups` not enforced | Low–Medium | Critical (CTF trivially solved, mailbox abuse) | S2 is a hard gate; the broker caps limit damage even if the mailbox is reachable |
| `killpg` cannot stop CPU-bound or blocked commands (webvm#228: no guest-side signal delivery) | Medium–High | Medium (30 s timeout unenforced; run hangs until wall clock) | S2 kill tests (a)–(c); run-level abort by VM teardown; `params.timeout_enforcement` recorded |
| Board co-residence changes what the agent can see (`ps`, `netstat`, hostname) | Certain | Low–Medium (comparability) | Recorded in `params` (`topology`, `visible_procs`); optional neutral argv; never pooled with msb data |
| Several agents in one VM break the isolation invariants | Certain if namespaced | Medium | Solo-first MVP; multi-agent labelled `shared-vm-v1`; B if S4 allows |
| Key exfiltration through XSS from rendered model or board text | Low with controls | Critical | textContent only, Trusted Types, strict CSP, dedicated origin, memory-only key |
| Visitor spend runaway (loops, retries) | Medium | Medium | Broker hard caps on calls, tokens and time; cost preview; abort; `E_BUDGET` → FATAL |
| Guest uses the mailbox as a key oracle | Low | Medium | Fixed endpoint and model; schema forbids URL, header and model fields; root-only mailbox; caps |
| Raw provider errors echo partial keys into logs | Medium | Medium | The broker forwards only status plus a fixed code |
| Performance 5–10x slower; 30 s timeouts hit more often | High | Medium | `env=cheerpx` condition never pooled with msb data; per-step latency logged; scaled `run_timeout_s` recorded |
| Browser memory exhaustion | Medium | Medium | N starts at 1–2; memory polling; refuse to start below a threshold; desktop only in v1 |
| CheerpX licence (company publication, no self-hosting, attribution; page and LICENSE.txt disagree on one-person companies) | Low (if D1 = individual) | High | D1 decided before launch; written confirmation if any company is involved; hosted CDN with a pinned version; attribution |
| CDN supply chain (cxrtnc) | Low | High | Exact version path in CSP; no other third-party script origins |
| CheerpX version or API drift | Medium | Medium | Pin the version; spike results recorded per version; Playwright boot test in CI |
| Image > 512 MB, so it is not edge-cached and costs grow | Medium (higher with the full base image) | Low–Medium | Size budget enforced in the build (fail above 450 MB); slim + tools fallback |
| Cached disk-image responses carry the wrong CORS headers (cache filled by a no-Origin or dev request) | Medium without mitigation | High (lab fails to boot at that colo for up to a year) | Static ACAO/Expose headers in the vm-corp transform rule; no localhost origin on the production bucket; Step 10 purge-and-fill test |
| Cold-colo range read pulls the whole image from R2 | Medium | Medium (boot looks hung) | Origin Range Requests on; Smart Tiered Cache; S7 measurement |
| Free-plan daily Worker limit hit, so `/` returns 429 | Low | High (lab offline until 00:00 UTC) | Usage alert well below 100k/day; Workers Paid or static `/` fallback (Step 8) |
| Worker invocation logs record `?endpoint=` | Certain with defaults | Low–Medium (visitor endpoint metadata retained) | `invocation_logs = false` for every lab Worker |
| Chatter leaks into build logs, CI artifacts, traces or agent context | Medium | Policy breach | Allowlist staging; leak gate with fixed codes; Playwright traces/screenshots off for run pages; CLAUDE.md/AGENTS.md path list updated |
| Seed publication is irreversible | Certain once published | Owner-dependent | Explicit decision D2 |
| Cerebras or Berget lack CORS on a 200 response | Medium | Low–Medium | S9; Phase 6 relay or unsupported |
| Error responses without ACAO (OpenAI and Cerebras 401s confirmed) make errors opaque | Certain for those two | Medium (bad key indistinguishable from network failure; no 400-based flag probing) | Key check via `GET /v1/models`; `E_OPAQUE` with bounded retries; preset wire flags; S9 records ACAO per error class |
| Safari: no COEP credentialless; localhost is mixed content | Certain | Low | require-corp everywhere; local endpoints documented as Chrome/Firefox only |
| IDB overlays pile up and fill the quota | Medium | Low | Per-run IDs deleted after export; GC of other image hashes on load |

---

## 9. Open questions

1. Does loopback TCP between guest processes work in CheerpX 1.3.9 with no `networkInterface` (S1), for both the board relay and vaultd? Do AF_UNIX stream sockets work fully, including `asyncio.start_unix_server` for IRC?
2. Which mailbox channel is reliable and fast enough: IDB-mount visibility or console framing (S5)?
3. Can one page host several `Linux.create` instances, and what does each cost in memory (S4)?
4. Is `swarmpit.dev` in account `90aff449…` (Step 0)?
5. **D1:** is the site published by Krister as an individual, or under a company?
6. **D2:** may the board seed be served publicly from `lab.swarmpit.dev`?
7. **D4:** is visitor run sharing wanted at all? If yes, who reviews uploaded material, and under what retention?
8. Should the browser condition keep `budget=40` and the 30 s command timeout exactly, or use scaled values recorded in `params`?
9. Which providers are first-class presets at launch (pending S9)? Is local Ollama in scope given Chrome's Local Network Access prompt?
10. Does `/proc` `hidepid` work in CheerpX? Planning assumes no (guest `mount()` unimplemented on 1.3.7; no mount options in the 1.3.9 API), so A-multi treats visible processes as fixed. S2 only confirms.
11. Should the apex `swarmpit.dev` move to Workers Static Assets (recommended) or stay on GitHub Pages?
12. Can `killpg` stop runaway agent commands in CheerpX (S2)? If not, is a run-level wall-clock abort an acceptable condition, or does the browser lab drop to a demo without the timeout metric?
13. Should the board process be disguised (neutral argv) in solo-v1, or left visible and recorded?
14. Should `netrelayd`'s `FIX_LF` (an msb egress workaround for IRC) stay in the cheerpx condition for byte parity, or be dropped if S1 shows it is unnecessary?
15. For the unsolvable goal, is page-side generation of the host ctf acceptable, or is the guest-side "discard after sealing" fallback enough?

---

## Appendix A: Review history

The plan went through two review rounds.

### Round 1: critique of an incomplete draft

Three reviewers (technical, security, ops) critiqued a draft whose generation had been interrupted, so most of the document was written in response. In summary:
- **Adopted:** spike-gated design (S1 loopback, S2 isolation, S5 mailbox) with fallbacks A′ and v86; solo-first MVP with multi-agent as a separate phase and `shared-vm-v1` labelling; harness loop kept in the guest (no exec-bridge timeouts); i386 busybox; PR #4 (`4194cd1`) as the base with a transport switch; key held only in page memory behind a schema-validated, root-only mailbox; textContent rendering, Trusted Types, strict CSP, dedicated origin, vendored xterm, version-pinned CheerpX; no relay in v1 (Phase 6 spec kept); chatter-policy migration and allowlist image staging with a leak gate; R2 custom domain, Cache Rule, CORP transform, `_headers` plus Worker for COOP/COEP; zone hygiene, scoped tokens, manifest handoff, alerts and verification commands; raw-error suppression, spend caps, cost preview, consent.
- **Not adopted:** self-hosting CheerpX or SRI (licensing page forbids self-hosting on the community licence; SRI on the stub would not cover runtime-loaded assets); COEP credentialless (unsupported in Safari); the licence as a blanket blocker (individual use is allowed by 1.4(a), so it became decision D1); per-visitor board seeds (the seed is a fixed experimental input; CTF secrets are per run); IDB as the only mailbox design (S5 also tests console framing); fixing AISL's Range parser (valid, but out of scope; the AISL repo is not changed); treating Python 3.11 vs 3.12 as blocking (resolved by the i386 3.12 image).

### Round 2: fact-check of the complete plan

Three fact-checkers verified the plan against the code at `4194cd1`, the AISL repo, the CheerpX 1.3.9 package and docs, live provider CORS behaviour and the Cloudflare docs. Evidence for the major items was spot-checked against the code before applying them.

**Corrections applied (major):**
- *Topology.* The pilot's board runs in a separate VM (binds `0.0.0.0`, published on host 19000+i), and `netrelayd`'s upstream is `host.microsandbox.internal`. The plan now calls board co-residence a topology change (1, 4.4) and records it in `params.topology`.
- *Agent-observable differences.* Added the visible board and `labctl` processes, the extra loopback listener in `netstat`, the hostname, and the measured `uname` (the one public report shows `i386`, not `i686`) (4.1).
- *Base image.* Switched to the pilot's full `python:3.12-bookworm` (386) instead of slim, with a slim-plus-tools fallback; `params.image` set explicitly because ingest defaults it (4.1, 4.2).
- *CTF needs loopback TCP.* vaultd/vaultctl on 127.0.0.1:31337 added to S1. Listener-side failure is a go/no-go for the CTF, and the section 2 note on A′ was corrected.
- *A-multi.* The shared `/etc/hosts` line defeats per-agent relay IPs; replaced by uid-based attribution, and every hard-coded PR #4 path is listed for parameterization (4.10, Phase 4, section 5).
- *Trusted Types.* CheerpX 1.3.9 uses script `.src` and `new Worker(string)` sinks, so a restricted `default` policy is now registered and the CSP allows `trusted-types swarmpit default`; this is the expected S8 outcome (4.8, Step 3, S8).
- *Error-status CORS.* OpenAI and Cerebras 401s lack ACAO. Added `E_OPAQUE`/`E_LNA`, a status-mapping table, a `GET /v1/models` key check, preset wire flags instead of 400-probing, and error-class ACAO checks in S9 (4.5, 4.6, S9, risks).
- *Timeout enforcement.* webvm#228 reports no guest-side signal delivery. Risk raised to Medium–High; S2 kills three process kinds; VM-teardown fallback recorded in `params.timeout_enforcement` (4.3, S2, risks).
- *Worker `ASSETS.fetch`.* Now `env.ASSETS.fetch(new URL("/", request.url))` with a 200/text-html test (Step 3).
- *Cached CORS on R2.* ACAO and Expose-Headers are set statically in the transform rule; localhost removed from the production bucket; Step 10 purges and fills the cache without `Origin` first (Steps 1–2, 10).
- *Invocation logs.* `invocation_logs = false` for the lab Worker and the relay (Steps 3, 9).

**Corrections applied (minor):** labctl writes the merged cfg to `/root/lab/cfg.json` with `cfg-a{i}.json` only as the export copy; CTF helpers vendored into `lab/browser/guest/ctf.py` with a parity test, the passphrase still drawn with `random.sample`, and the unsolvable-goal host ctf kept out of the guest (page-side generation, or discard after sealing); harness `KEY` lookup skipped and `model`/headers stripped in mailbox mode; console framing moved off harness stdout to a root-only console device, with no other console capture while it is in use; log pump and export read a dedicated IDBDevice `dir` mount at `/root/export`; `board.internal:<board_port>` (8080 or 6667) and `FIX_LF` handling; `lab/board/__main__.py` added and an asyncio Unix server specified for IRC under A′; direct board bypass noted as already present in the pilot, so A′ is hardening, not parity; CONTRACT citations changed to §3.3 items 2 and 3 (lines 726 and 731–732 at `4194cd1`); `.gitignore` action changed to adding `/aisecurityliteracy.dev/`; dev server moved off the frozen `site/tools/serve.mjs`; Phase 5 scope widened to `api.py` shaping and `schema.sql` views, or DuckDB-WASM in the page; webvm#228 loopback note described as unconfirmed, and the 1.3.9 preference based on S1 `getsockname`/`select` probes; header rule justified by preflight surface, not a Cerebras restriction; `retry_after` made best-effort; licence terms attributed to the licensing page, with the one-person-company discrepancy and licence 1.5 noted; `hidepid` assumed unavailable; direct `import()` of CheerpX instead of the npm wrapper (`new Function`); Origin Range Requests on and Smart Tiered Cache enabled; Edge TTL mode named (`bypass_by_default`); wrangler upload limit corrected to 315 MB; `_headers` Cache-Control merging fixed with `! Cache-Control`; `_headers` moved to `lab/web/public/`; Free-plan 429 failure mode stated; Mirage removed and Polish made Pro-only; Turnstile CSP requirements specified, with a separate challenge page preferred.

**Missing items added:** `seed.json` in the canonical run layout; explicit `params.image`; FATAL/void/`E_BUDGET` handling and aborted-run export; status mapping for `E_SCHEMA`/`E_ABORT`; AISL's 1.2.6 pin and the lack of a 1.3.9 precedent or public changelog; `DirectSocketsNetwork` noted in S1; `readFileAsBlob` null handling and string-only `writeFile` on 1.3.9; multi-instance kept as a pure spike (S4); Chrome Local Network Access mapped to `E_LNA`; AWS env mapping and region `auto` in the upload workflow; Free WAF rule scope (Path only, no Host); Workers Custom Domain vs existing CNAME conflict.

**Corrections not adopted, or adopted differently, and why:**
- *Hide the board via `hidepid`* (fix option for the agent-observable differences). Evidence in the same review set shows `hidepid` cannot be configured in CheerpX. The plan records the visible processes and offers a neutral argv as the optional mitigation.
- *Per-agent mount namespaces for A-multi attribution.* Guest `mount()` is unimplemented on 1.3.7, so uid-based relay attribution is the planned scheme; namespaces are not assumed.
- *Drop `require-trusted-types-for`* (alternative Trusted Types fix). Kept Trusted Types with a narrowly scoped `default` policy, because it is the main backstop against XSS reaching the key; dropping it remains the S8 fallback.
- *Serve `/` statically with a meta-tag CSP to avoid the Free-plan 429.* Kept as a fallback only. The dynamic `connect-src` for custom endpoints needs the Worker, and a meta-tag CSP cannot express `frame-ancestors`. The primary mitigation is an early usage alert and, if needed, Workers Paid.
- *Generate the CTF passphrase with `secrets`.* Not changed. The pilot draws it with `random.sample`, and the distribution is part of the experiment; the plan's earlier wording was corrected instead.
- *Edge TTL `override_origin` with 1 year* (one of two offered options). `bypass_by_default` was chosen because it fails closed when an object lacks `Cache-Control`.