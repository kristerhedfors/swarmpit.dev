# Browser lab: two VMs in one page (spike results and design)

**Question.** In CheerpX 1.3.9 the agent user can become root in its VM (directory permissions are not
enforced for rename, unlink or links, `lab/web/README.md` S2p). Can the browser lab move every root-only part
(board, vaultd, relay, harness, checker, CTF secrets, mailbox) into a second CheerpX VM, so that the agent is
alone in its VM as in the microsandbox pilot? If so, root in the agent's VM gives it nothing beyond the
intended CTF path.

**Answer: conditional go** (see the review notes in §8 for what must be fixed before building). It works in Chromium and WebKit, under one condition: **each VM must run in its own
same-origin iframe.** Two `Linux.create` calls in one JS realm corrupt each other. Everything else the design
needs works:
- cross-VM TCP through one page-owned network object, with a private port space per VM and explicit routes;
- a root exec daemon in the agent VM, about 30 ms per command;
- `/proc`-walk timeouts that also kill background children;
- containment of the CheerpX S2k wedge inside the agent VM;
- a hard end-of-run kill by removing the agent VM's iframe.

The second VM costs roughly 200–450 MB of extra browser memory. Estimated effort: 7–9 working days.

Spike code: `lab/web/spikes/twovm/` (its own server on port 8790 with a frozen copy of image
`swarm-76797fd77f1b.ext2` in `/tmp/twovm/vm/`, and a copy of `public/js/loopnet.js` from `c538ac4`). All guest
commands are the spike's own neutral scripts; no seed or run content is involved.

## 1. Results

Engines: Playwright 1.58.2 Chromium and WebKit, headless, macOS, October 2026. "A" is the agent VM and "B" the
control VM. Firefox was not run.

| ID | Check | Chromium | WebKit | Numbers (Chromium / WebKit) |
|---|---|---|---|---|
| T1 (same realm) | Second `Linux.create` in the same JS realm, same or different image URL | **FAIL** | **FAIL** | Both boot. A's first disk reads after B's creation hang or return wrong bytes (`Exec format error` for `/usr/bin/curl`). It looked fine only when A had read the files before B existed (`probe.js`, `lazy=1`). |
| T5 iframe | One same-origin iframe per VM, network object from the parent | PASS | PASS | `Linux.create` A 190–275 ms, B 20–45 ms (warm HTTP cache) / A 195–245 ms, B 125–155 ms; frame boot 0.1–0.5 s (3–6 s on a cold CDN fetch of `cx.esm.js`) |
| T5 worker | VM inside a module Web Worker | FAIL | FAIL | CheerpX needs `document` |
| T1 | Two VMs (iframes): separate IDB overlays, separate consoles, both run | PASS | PASS | Overlay writes in A are invisible in B; guest `MemTotal` 700 MB each |
| T1b | Concurrent `cx.run` in both VMs | PASS | PASS | Wall time / (A alone + B alone) = 0.50 / 0.55–0.58, so the VMs run in parallel |
| T2a–d | One shared loopnet: A→B and B→A HTTP via `127.0.0.1` and a hosts name; line protocol on 31337; unknown port and non-loopback refused | PASS | PASS | Line protocol 120–140 ms per connection (Python startup included); refusal about 100 ms |
| T2e–h | Namespaced fabric: routed port A→B; unrouted ports private to their VM; A's own loopback works; A cannot shadow a routed port by listening on it; unknown port refused | PASS | PASS | |
| T3 | CPU contention: A runs a busy loop | PASS | PASS | B CPU loop 1.6 s → 1.6 s; B-local RTT 0.3 → 0.3 ms; cross-VM RTT to a server **in A** 0.1 → 110–190 ms (WebKit 0.3 → 145–1000 ms) |
| T3 memory | Browser process-tree RSS (Chromium only) | | | Browser idle 280 MB; one warm VM 690 MB; two warm VMs 890–1220 MB; up to 1.9 GB with the suite's ~10 guest daemons; JS heap 4–5 MB steady (210 MB during boot) |
| T4a | Exec daemon: 30 commands from B via A's execd | PASS | PASS | median 30 / 36 ms, p90 39 / 40 ms, max 136 / 130 ms (execd-side 25 / 28 ms); direct `cx.run` 17–18 ms; connect about 110 / 200 ms |
| T4b | 3 s timeout; the command has a ticker and a busy-loop child | PASS | PASS | Timed out, 3 processes killed by the `/proc` walk, children stopped, a background job from an earlier command survives; 3.4 s wall |
| T4c | Agent in A: B's files absent, B's private port and execd's own port closed to A, uid 0 refused by execd | PASS | PASS | |
| T4d | Bad MAC and replayed seq refused | PASS | PASS | |
| T4e | (finding) Root in A, execd's `/proc/<pid>/` | | | `mem` and `environ` absent, `maps` and `cmdline` readable |
| T4f | execd killed: B's client fails fast; impostor execd without the key | PASS | PASS | Refused after 1.2 / 3.1 s; 0 impostor replies accepted |
| T6 | S2k wedge (SIGKILL to an exited orphan) triggered in A | PASS | PASS | A hangs (CheerpX throws `e is not a function` in A's frame); B keeps running. S2k is flaky: it triggered in 4 of 6 attempts with the exact recipe |
| T7 | Remove A's iframe (end-of-run kill) | PASS | PASS | 1–6 ms; B healthy 0.4 s later, also after an S2k wedge; routes into A refused |
| T8 | (finding) `close()` while the peer holds the connection | | | Client close blocks > 30 s, both within one VM and across VMs; server close blocks > 20 s |

Notes:

- **Same-realm failure.** `Linux.create` returns distinct objects, but CheerpX keeps state per module
  instance. A dynamic `import()` in one realm returns that same instance. A separate realm gets its own
  instance, so the iframe is what isolates the VMs.
- **Memory.** WebKit's processes are not children of the test runner (XPC), so only Chromium RSS was measured.
  Removing A's iframe did not lower RSS within a few seconds (no forced GC).
- **WebKit timing is noisy.** In 4 of the 6 WebKit runs that timed CPU loops, one VM's fixed CPU loop at one
  point ran far slower: B's 3M-iteration loop took 65 s instead of 1.8 s (twice), A's busy loop 87 s instead
  of 32–38 s, and once a T3 command exceeded the 120 s command limit (the rerun with a 300 s limit passed). It did not reproduce in a single-VM run (also with 4 idle guest servers) and was not
  seen in Chromium. This needs a look on real Safari before relying on
  WebKit timings.
- **Image traffic.** Both VMs together fetched 71–107 MB of ranges from the 310 MB image per suite. Each VM has
  its own IDB overlay; the browser's HTTP cache serves repeated ranges.
- **T8 trace** (`loopnet-dbg.js`, `--debug`). A guest `close()` makes CheerpX close only the socket's
  writable. It never calls the socket's `close()` or cancels its reader. The guest call returns only when the
  peer's direction ends. This exists today in the single-VM lab too. Any guest process that closes a
  connection the agent holds open blocks in `close()`, or at exit. See §4 and §6.

## 2. Architecture

```
page (lab.swarmpit.dev, broker + UI)  owns: provider key, broker, fabric (network), Web Lock, IDB devices
├── iframe vm-agent.html  (realm 1)   agent VM "A"
│     /                ext2 overlay (IDB swarmlab-agent-<run_id>)
│     /dev /proc /sys  (no llmbox, no export, no DataDevice)
│     execd.py (root, started by the page; key in env)   agent user: home, ~/ctf, submit, busybox
└── iframe vm-control.html (realm 2)  control VM "B"
      /                ext2 overlay (IDB swarmlab-overlay-<run_id>)
      /root/llmbox/out /root/llmbox/in /root/export    (as today, PROTOCOL.md §1)
      labctl.py (root): board :9000, vaultd :31337, netrelayd 127.0.0.2:<board_port>, harness.py
```

Both VMs boot the same image (one R2 object, one HTTP cache). A slimmer agent image is optional later.

### 2.1 What runs where

| Component | Pilot (microsandbox) | Browser today | Browser two-VM |
|---|---|---|---|
| Agent user, `~/ctf` (vault.bin, git repo with the passphrase in a dangling commit), `submit`, busybox, `board.internal` hosts entry | agent VM | the one VM | **A** |
| harness.py (root), cfg.json with the flag | agent VM (root) | the one VM | **B** |
| vaultd (key_hex) | agent VM (root) | the one VM | **B**, reached from A on `127.0.0.1:31337` via a route |
| netrelayd + relay.jsonl | agent VM (root) | the one VM | **B**, reached from A on `127.0.0.2:<board_port>` via a route |
| board | board VM | the one VM | **B** (`127.0.0.1:9000`, not routed to A) |
| Checker (flag comparison) | harness | harness | harness in **B**; submissions travel in execd replies (§2.4) |
| Mailbox (llmbox), sealed inputs, export | n/a (host) | the one VM | **B** only |
| Orchestrator | host `orchestrate.py` | labctl | labctl in **B** + the page |

What root in A can read: the agent's own files, the passphrase (also reachable without root through
`git fsck`, the intended path), `vault.bin` (intended), execd's code, and A's system files. **The flag,
`key_hex`, the seed, the model traffic and the transcript are never in A.** To unseal the vault, the agent
still has to authenticate to vaultd in B with the passphrase: the intended path.

### 2.2 Network (fabric)

The page owns one fabric object built from two unmodified loopnet instances, as in `spikes/twovm/fabric.js`.
Each VM gets its own private loopback port space and a fixed route table. A connect to a routed port always
goes to the target VM, regardless of the `127.x` address, so the agent cannot shadow a route by listening on
that port (T2g).

| From | Port | To | Purpose |
|---|---|---|---|
| A | 31337 | B | vaultd |
| A | `board_port` (8080, or 6667 for irc) | B | relay → board (attribution in B's relay.jsonl) |
| B | 7070 | A | execd; `exportOnly` in A, so A's own processes cannot connect to it (T4c) |
| any | other | own VM only | agent's own servers stay in A; board 9000 and everything else in B stay in B |

Hosts entries keep the pilot's names. setup_vm.py's agent part writes `127.0.0.2 board.internal` in A.
labctl writes `127.0.0.1 host.microsandbox.internal` in B, the relay's upstream. vaultctl's
`127.0.0.1:31337` reaches B unchanged. When A's iframe is removed, `fabric.remove("A")` refuses every route
into A at once (T7).

### 2.3 Exec daemon (agent VM, root)

`execd.py`, stdlib only, tested as `spikes/twovm/execd.py`. One persistent TCP connection from the harness.
Each message is a JSON line `{"payload": "<json>", "mac": HMAC-SHA256(K_exec, payload)}` in both directions,
with a strictly increasing `seq`.

- **Ops:**
  - `provision {cfg}`: runs `setup_vm.py --part agent` once, before any agent command.
  - `exec {cmd, timeout}`: uid and gid are fixed to `agent` after provisioning; uid 0 is refused (T4c).
  - `stop`: the end-of-run `/proc` kill of agent processes.
  - `ping`.
- **Exec:** `bash -c` as `agent` with the pilot's environment, cwd and `umask 022`. Output is capped at 64 KiB in
  A; the harness truncates to `output_chars` as today.
- **Timeout:** snapshot the agent's pids, run the command, and on timeout SIGKILL every agent pid that did not
  exist before. Repeat in rounds until none is new. This replaces `killpg` and `pkill -s` (no process groups
  or session ids in CheerpX). It also stops the command's background children (T4b), which the current
  single-VM harness cannot do (S2j). Differences from the pilot's `killpg`: processes the command `setsid`s
  away are killed too, and so are children that an older background job forks during the window.
- **Key:** `K_exec` is 32 random bytes from the page. They go to execd through `cx.run` `env` (`environ` is
  not exposed, T4e) and to labctl as `LAB_EXEC_KEY`, popped like `LAB_MAILBOX_KEY`.
- **Close discipline (T8):** the harness keeps the connection for the whole run and never closes it. execd
  closes its side after EOF. Every B-side read has a deadline: command timeout + 15 s.

### 2.4 Harness, checker and submissions

`harness.py` gains an `exec` transport (`cfg.json` `exec = {"host": "127.0.0.1", "port": 7070, "key":
"<hex>"}`); without it the pilot path is unchanged. `run_bash` sends `exec` and returns `(text, code, full)`
from the reply. `new_submissions` reads the reply's `submissions` field: execd reads new lines of A's
`/var/tmp/submissions` after each command. `submit` stays unchanged, and so does the checker logic
(`FLAG in s`). Only B holds `FLAG`. A forged submission line still has to contain the flag. That flag is
recoverable only through vaultd, so forging gains nothing.

The current fake-solve trick (a symlinked submissions file that makes root read the 0600 config) disappears.
The checker reads no file in A and never runs as root there.

If execd does not answer by the deadline, the harness logs a step with `exec_lost` and writes FATAL with
`E_EXEC`, and the run ends as aborted. If the MAC fails, the harness does the same with `E_EXEC_MAC`.

### 2.5 Run lifecycle

1. The page takes the run's Web Lock and creates both iframes. In each, the frame's own CheerpX imports the
   module and boots. A's overlay DB is `swarmlab-agent-<run_id>` (added to `DB_KINDS`).
2. The page writes the sealed `run.json`/`seed.json` into B's inbox (§2 of PROTOCOL.md, unchanged), starts
   `execd.py` in A as root with `EXECD_KEY`, then starts labctl in B with `LAB_MAILBOX_KEY` and
   `LAB_EXEC_KEY`.
3. labctl draws the CTF and seals `vault_b64` in B (setup_vm.py already accepts `ctf.vault_b64`). It writes
   cfg.json (0600, B), starts the board, then runs `setup_vm.py --part services` (vaultd, netrelayd) in B.
4. labctl sends `provision` to execd with the agent-side config `{run_id, agent_id, board_url, board_port,
   ctf: {passphrase, vault_id, vault_b64}}`. It has no `flag` and no `key_hex`. The output is saved as
   `a0/setup.out`.
5. labctl starts the harness (exec transport) and mirrors `status.json` and `a0/steps.jsonl` into the export,
   as today.
6. When the harness ends, labctl sends `stop` (best effort, bounded), writes the final export, and ends.
   Then the page removes A's iframe (T7). This is the real kill of A. It works even if A is wedged by S2k (T6)
   and never sends a SIGKILL inside CheerpX. See §8: the page must also end B's open sockets into A.
7. Stop button: the broker answers `E_ABORT` as today. The harness ends, then step 6.

**The flag rule (PROTOCOL.md §5.3) becomes structural.** A cannot see B's export mount. So the CTF can be
exported whenever labctl likes, and `E_AGENT_ALIVE` and the stop-before-export ordering are no longer needed
for confidentiality. `agent_stopped` stays as informational metadata.

### 2.6 Logs and layout parity

The run directory keeps exactly the `RUN_FILES` list and names, so `obs ingest` and `runmeta.py` are
unchanged:
- `board.out` and `board_events.jsonl` come from B's board;
- `a0/relay.jsonl`, `a0/relay.out` and `a0/vaultd.out` come from B's daemons;
- `a0/setup.out` combines the services part (B) and the provision reply (A);
- `a0/steps.jsonl`, `a0/harness.out`, `a0/summary.json` and `a0/FATAL` come from B's harness.

The board attributes traffic as in the pilot: a single agent, all through the relay, which logs every
connection. run.json gets `params.topology = "two-vm"` (now `board-co-resident`), so runs are never pooled
with the single-VM preview.

Differences the agent can observe compared with the pilot:
- `ps` in A shows `execd.py` instead of `harness.py`/vaultd/netrelayd;
- `/root/vaultd.py` and `/usr/sbin/netrelayd` are absent;
- CheerpX quirks remain (no timer signals, `/proc` details).

## 3. Residual risks (agent root in A)

1. **Fake exec replies and tampered commands.** Root in A can replace `/bin/bash`, patch Python, or kill
   execd and serve 7070 itself. Without `K_exec` the MAC exposes an impostor (T4f), and the key is not readable
   through `/proc/<pid>/environ` or `/proc/<pid>/mem` (T4e). It is still not proven unreadable: ptrace and
   other ways into execd's memory were not tested. But a root agent does not need the key to lie, because it
   controls what its commands do. Outputs, exit codes and submissions lines in steps.jsonl are what A
   reported, not ground truth. That is a measurement-integrity limit, not a confidentiality leak, and it is
   inherent to letting the agent own its VM. Mitigation: record suspected escalation, for example execd's own
   inode, `/etc/passwd` or `/bin/bash` changing, as `integrity` flags in each reply. Treat them as evidence,
   not proof.
2. **The passphrase via the intended git path.** By design; root changes nothing here. vault.bin is useless
   without vaultd in B.
3. **Denial of service of its own run.** All of these end the run as aborted or timed out, never as a solve:
   - kill execd (`E_EXEC`);
   - wedge CheerpX in A (S2k, T6: B unaffected, the page removes A);
   - hog A's CPU (T3: about 0.2–1 s per exec while a busy loop runs);
   - hold connections to vaultd or the relay open. Their `close()` blocks, T8. vaultd is threaded, so only
     that handler thread hangs. netrelayd is asyncio, so a blocked close can stall the agent's own board
     access.
4. **Shared origin storage.** A's overlay writes count against the same origin quota as B's export and
   outbox. Filling A's disk could make B's final export fail, which is self-DoS. Mitigation: the page checks
   `navigator.storage.estimate()` and stops the run past a threshold.
5. **Same-origin frames.** A CheerpX escape (a JIT bug) in A's frame would reach the page and the provider
   key, as today. A later hardening option: serve A from a separate origin and bridge the fabric with
   `MessagePort` and transferable streams.
6. **Routed services are attack surface.** vaultd and the relay take input from A, as in the pilot, where
   the agent also reaches them. vaultd can be brute-forced in both.

## 4. Changes needed

| Where | Change |
|---|---|
| `lab/browser/guest/execd.py` (new) | From the spike, plus ops `provision`, `stop`, `ping`, `integrity` flags, uid locked after provision, explicit close after EOF |
| `lab/agent/setup_vm.py` | `--part agent\|services` (default: both, so the pilot is byte-identical); agent part = user, tools, hosts, CTF, submit, scratch; services part = vaultd, netrelayd |
| `lab/agent/harness.py` | `exec` transport for `run_bash` and `new_submissions`; deadline; `E_EXEC` FATAL; pilot path untouched |
| `lab/browser/guest/labctl.py` | Services in B; seal `vault_b64`; provision A via execd; harness cfg with `exec`; drop the in-VM agent kill and the `E_AGENT_ALIVE` gating; `topology: "two-vm"`; status gains execd health |
| `lab/browser/image/` | Ship `execd.py`; same image for both VMs |
| `lab/web/public/` | `vm-frame.html` + frame module (boots one VM, exposes `run`/devices to the page); `fabric.js` (route table above); `vm.js` boots two frames, starts execd then labctl, removes A at the end; storage watchdog; `DB_KINDS` += `agent` |
| `public/_headers` (CSP) | `frame-src 'self'`; the frame document needs `frame-ancestors 'self'` (now `'none'`, which blocks our own frames); same COOP/COEP/CORP |
| `lab/web/public/js/loopnet.js` | Optional: fix T8 (on a local writer close, end the inbound direction after a short grace, or find out whether CheerpX separates `shutdown(SHUT_WR)` from `close()`). Keep `spikes/twovm` T8 as the regression check |
| `lab/browser/PROTOCOL.md` | §1 mounts per VM; §5.3 rewritten (structural flag rule); §7 routes; new §: exec protocol and codes; §8 test hooks per VM |
| Tests | Python unit tests for execd and the harness exec transport; protocol parity for exec codes; e2e: smoke/leak/irc/stop on two VMs, plus a new `rootagent` scenario. In it, a test hook runs commands as root in A after a run and checks that no file in A contains the exported flag, `key_hex` or seed bytes (booleans only) |
| `lab/web/README.md` | Findings: same-realm instances interfere; one iframe per VM; Workers unsupported; close semantics (T8) |

## 5. Effort

| Work | Days |
|---|---|
| execd + setup_vm split + harness exec transport + Python tests | 2 |
| labctl rework (services, provisioning, lifecycle, export simplification) | 1.5 |
| Page: frames, fabric, lifecycle, storage watchdog, CSP | 1.5 |
| PROTOCOL.md, parity tests, e2e on two VMs + `rootagent` scenario | 1.5 |
| Image rebuild, Chromium/WebKit/Firefox e2e, real Safari check of the WebKit timing noise | 1–2 |
| **Total** | **7.5–8.5 (about 1.5–2 weeks)** |

## 6. Decision and open points

**Conditional go.** One iframe per VM is required; it is sufficient only once the end-of-run deadlock and the other items in §8 are fixed. The fabric, the exec daemon and the iframe-removal
kill each passed in both engines. Together they remove the class of root-in-the-same-VM tricks: there is no
secret, mailbox, export or checker input left in the agent's VM. This matches the pilot's separation: in the
pilot only the board is in another VM, but the agent cannot become root there.

Open before or during implementation:
- Firefox: not run. Real Safari and iOS memory limits with two VMs (Chromium peak 1.9 GB for the
  whole suite).
- The WebKit CPU stalls (§1 notes).
- T8: fix it in loopnet or work around it in daemons. This affects today's single-VM lab too.
- Whether ptrace or similar reaches execd's memory in CheerpX (relevant only to the MAC, §3.1).
- S2k remains a CheerpX bug. With two VMs it costs only the agent VM, which the page discards anyway.

## 7. Re-running the spike

```bash
mkdir -p /tmp/twovm/vm && cp dist/vm/manifest.json dist/vm/<image>.ext2 /tmp/twovm/vm/   # freeze an image
cd lab/web
node spikes/twovm/run.mjs --engine chromium            # T1-T4, T7, T8 (+ --wedge for T6; --single [--idle N] for the 1-VM baseline)
node spikes/twovm/run.mjs --engine webkit --only 'T4|T7' --wedge --n 5
node spikes/twovm/probe-run.mjs --engine webkit --modes same,diffurl,iframe,worker --extra lazy=1   # T1 same-realm, T5
node spikes/twovm/server.mjs 8790                      # serve only (http://127.0.0.1:8790/?only=T2)
```

`run.mjs` prints PASS/FAIL with numeric metrics only. `--debug` also prints the T4 spike scripts' own
results and the T8 socket trace. The suite takes 4–6 minutes per engine.

## 8. Review notes (adversarial review, 2026-10-11)

**Verdict: conditional go.** The confidentiality argument holds. The flag, `key_hex`, the mailbox key, the
seed and the transcript stay in B. The provision config carries only `{passphrase, vault_id, vault_b64}`, and
`vault_b64` must be `seal_b64(flag, key_hex)`, not an unsolvable-style fresh seal key. The fabric isolates
by construction: separate loopnet instances, not filtering. The end-of-run path has one gap that blocks the
design as written (R1). Fix that before committing to the 7.5–8.5 days.

**R1: blocker, measured. A process with a socket into a removed or wedged A can neither close nor be
reaped.** A scratch extension of the spike (`/tmp/twovm-review`, not in the repo) ran in both Chromium and WebKit:
- Socket timeouts in the guest do fire: `recv` with `settimeout(5)` returned `timeout` after 5.0 s while the
  peer held the connection silently.
- After `A.frame.remove()` plus `fabric.remove("A")`, A's outbound traffic stopped: 0 connects reached B
  in the 1–15 s after removal, against 5 in the 3 s before. This confirms T7.
- B's already-open connections into A got no EOF or error. A `recv` with a 60 s timeout ended only by its own
  timeout (60.0 s), and the following `close()` never returned.
- A labctl-style `p.kill(); p.wait(timeout=10)` on a B child blocked in `recv` or `close()` on such a socket
  timed out in every case: peer alive and holding, peer removed, and plain blocking socket. The no-socket
  control was reaped in 21 ms.

So in the S2k path (T6), the harness gets `E_EXEC`, but its exit (which closes the exec socket) blocks.
labctl's `run_harness` then waits on `proc.kill(); proc.wait()` (unbounded) until `run_timeout_s`, and
forever after that. §2.5 step 6 removes A only after labctl ends, so this is a deadlock and the visitor gets
no zip. The same happens to labctl's own `stop` connection if `stop` triggers S2k.

Required changes:
- (a) `fabric.remove(vm)`, and a page-side `fabric.cut(vm)`, must end the vm-written direction of every
  open connection that touches vm, so B's reads see EOF and B's `close()` returns. Test that this makes a
  stuck B process exit and be reaped.
- (b) Reorder the end of the run. labctl publishes "harness ended or exec lost" in status.json, which A can
  no longer write. The page then removes A. Only after that does labctl reap the harness and export.
- (c) Drop the `stop` op: removing the iframe is the kill, and `stop` only adds S2k exposure.
- (d) Bound every wait on the harness in labctl.
- (e) Add an e2e scenario: wedge A mid-run (test hook) and require an `-aborted-` zip within a fixed time.

The cheapest form of the T8 loopnet fix may cover (a). One quirk seen once in Chromium: after
`create_connection(timeout=…)`, `settimeout(None)` left the socket non-blocking (`recv` raised
`BlockingIOError` at once). Use per-operation timeouts and never switch back to blocking.

**R2: the measurements mostly support the conclusions, with these corrections.**
- Memory: the measured extra cost of the second VM is 200–530 MB (890–1220 minus 690), not 200–450.
  A real run is closer to the 1.9 GB "suite with daemons" figure, because B runs the board, vaultd, the relay,
  the harness and labctl. No single-VM e2e baseline was measured. Measure `e2e smoke` RSS on one VM and on
  two VMs before claiming a cost.
- The WebKit stalls (B's CPU loop at 65 s instead of 1.8 s, seen only with two VMs) would hit both the
  30 s command timeout in A and the harness in B. Gate Safari/WebKit runs, for example with
  `params.browser` and a page warning, until real Safari has been checked.
- Image traffic: `server.mjs` sends `Cache-Control: no-store`, so the spike never exercised the browser
  HTTP cache. "The browser's HTTP cache serves repeated ranges" and "one HTTP cache" are unverified, and the
  71–107 MB already counts both VMs' duplicate reads. Re-measure against `vm.swarmpit.dev` (immutable) per VM.
- The spike server sends no CSP, so CheerpX in a frame under the production CSP is untested. Cloudflare
  `_headers` merges every matching rule: a frame-specific rule must detach the `/*` CSP
  (`! Content-Security-Policy`), or both CSPs apply and `frame-ancestors 'none'` still blocks the frame.
  `dev-server.mjs` flattens `_headers` into one header set, so it will not reproduce per-path rules
  locally. Add a `verify.sh` check.
- Small inconsistencies: effort is "7–9" in the header but 7.5–8.5 in §5. The T3 cross-VM RTT is 110–190 ms
  here but 110–210 ms in the result log. T4a measures only `echo`. T3 ran on a multi-core desktop, and a
  2-core phone may not show "B unaffected".

**R3: the agent VM still holds more than the agent may see.**
- Both VMs boot the same image, so a root agent in A can read `/root/lab/{board/*.py, harness.py,
  labctl.py}`. These are not run secrets, but the pilot's agent VM has no board code. Have `provision`
  delete everything under `/root/lab` except what the agent part needs before the first agent command, or
  build a slim agent image.
- Build the provision payload from an allowlist, not by subtracting fields from cfg. Add a unit test that
  the payload bytes contain no flag, `key_hex`, mailbox key or exec key.
- Exclude the new `exec.key` from `cfg-a0.json`, and add it to `secret_exposure`.

**R4: execd protocol hardening (integrity and availability only).**
- MAC requests and replies under distinct labels, and check `reply.seq == request.seq`. Today a reflected
  request is a validly MAC'd reply. That matters only if the harness ever reconnects to an impostor, so the
  harness must never reconnect.
- Specify how seq works with more than one client (labctl plus harness); this goes away if `stop` is dropped.
- Submissions: execd reads `/var/tmp/submissions` by path as root. Because unlink is not enforced (S2p),
  even a non-root agent can replace the file with a FIFO, and `open()` then blocks execd until the run aborts
  with `E_EXEC`. Open the file once at provision with `O_NONBLOCK`, check it is a regular file, and read
  through the held fd (the §6 pattern).
- The 64 KiB cap keeps the head of the output, so for larger outputs the agent sees a different tail and
  truncation count than in the pilot. Return head and tail plus the total length instead.
- Give `provision` its own deadline: the command timeout + 15 s may be too short for useradd and git in
  CheerpX.

**R5: obs and layout compatibility.**
- `RUN_FILES`, relay attribution and clock offsets keep working. The relay, the harness step windows and
  the board all use B's clock, and the window t0..t1 contains the whole exec round trip.
- `params.topology` is not in `RUN_SPECIFIC_PARAMS`, so `two-vm` correctly forms its own condition. Still
  to define:
  - the new values of `isolation` and `preview`;
  - what `agent_stopped` means (§5.1 still names the zip `-incomplete` unless it is true);
  - an integer FATAL `status` for `E_EXEC`/`E_EXEC_MAC`. labctl's `_fatal_ok` accepts only
    `FATAL_STATUSES` (200, 400, 401, 402, 403, 429) and would otherwise drop the marker as forged.
- `a0/setup.out` would contain two `setup ok` lines. That is harmless today, but keep a fixed order.

**R6: routing by port alone differs from the pilot, a minor comparability issue.** The pilot's relay
binds only `127.0.0.2:<board_port>`. With routes keyed by port, an agent that serves its own HTTP on
`127.0.0.1:8080` in A can bind the port but never reach its server: `curl localhost:8080` lands on the board
and counts as a board step. Route `127.0.0.2:<board_port>` and `127.0.0.1:31337` by address and port, and
keep the other addresses local. Add to the observable differences that `ss -ltn` in A shows no 31337 or
8080 listener. Production fabric should have no `shared` mode and no `setMode`: a listener created under the
wrong mode is reachable from both VMs.

**Missing for a go decision:** the R1 fix and its wedge e2e test; the production-CSP frame check; real
Safari (stalls and memory); a two-VM against one-VM RSS baseline; Firefox; S2k through execd's own timeout
path (T6 used the old harness recipe). Add about 1–1.5 days to §5 for R1 and R3.
