// One browser swarm (board + N agents, board-split topology; one at a time per tab). docs/browser-swarm.md §2 is
// the lifecycle and §6 the API; lab/browser/PROTOCOL.md (§9.5 for the page side) the guest <-> page contract.
//
// Run phases (onStatus `phase`), one function each in run():
//   init -> loading (locks, stale cleanup, assets; §2 step 2) -> board (boot, seal, labctl, board-ready; step 3)
//   -> agents (boot one at a time; step 4) -> running (until every agent ended; steps 5-6)
//   -> ending (metas + stop into the board inbox, board export; step 7) -> done | stopped | error.
// Each VM has its own phase (boardState / agentState below); its end is decided from the status.json read after
// its labctl returned (PROTOCOL.md §4). zip() assembles the §3 layout; dispose() deletes the run's VM databases and
// releases its Web Locks once the page has stored the trace.
//
// Run content (steps, board events, seed, model text) only moves between devices, page memory and the hooks; it
// is never logged or rendered here. The API key stays in the brokers (page memory) and never reaches a VM.
import {
  BOARD_INBOX, BOARD_LIVE_FILES, BOARD_READY_PATH, BOARD_VM, BOOT_INPUTS, CX_VERSION, DB_KINDS, LEGACY_DB_PREFIX,
  MANIFEST_PATH, MAX_AGENTS, ROLE_INPUTS, RUN_DIR, STATUS_PATH, agentFiles, agentLiveFiles, agentMetaFile, agentSpec,
  agentVm, boardFiles, boardSpec, bytesToHex, dbRunId, hmacKey, makeRunId, pageAgentMeta, runLock, seal, swarmVms,
  unwrapDbName, vmStorageId,
} from "./protocol.js";
import { createBudget, startBroker } from "./broker.js";
import { createFabric, swarmRoutes } from "./fabric.js";
import { makeZip } from "./zip.js";

// Timings (ms). Every one can be overridden through createSwarm's third argument (dev harness, tests).
const TIMING = {
  pollMs: 2000,              // live tail and status, as the solo page (PROTOCOL.md §4)
  frameLoadMs: 30000,        // iframe load + vmframe.js module
  bootMs: 180000,            // one VM's Linux.create
  boardReadyMs: 240000,      // board labctl start -> board-ready.json
  staggerMs: 3000,           // pause between agent boots (one VM boots at a time)
  stopGraceMs: 120000,       // after Stop / its deadline: time an agent's labctl gets to export before its frame goes
  exportGraceMs: 180000,     // ... extended by up to this while its live status says "exporting"
  boardEndMs: 180000,        // board labctl after the stop file -> exit
  boardEarlyEndMs: 30000,    // the same, for a board stopped before it was ready (it has nothing to export)
  readMs: 15000,             // one device read (a wedged VM must not hang the page)
  // Each agent's page deadline: its own labctl start + harness.run_timeout_s + this (labctl enforces
  // run_timeout_s itself, from after setup_vm.py; this covers setup and the export) ...
  deadlineMarginMs: 900000,
  // ... but never later than the board's own deadline (labctl BOARD_MARGIN_S after the board's labctl start)
  // less boardReserveMs, so every agent has ended and its meta is in the board's inbox before the board gives up.
  boardMarginMs: 1800000,
  boardReserveMs: 300000,
};
const DEFAULT_RUN_TIMEOUT_S = 3600;  // harness.run_timeout_s when it is missing or negative
// Read caps (bytes): the agent VMs' export and outbox are agent-writable IndexedDB devices, so a rooted agent
// could grow a file until the tab runs out of memory. Above its cap a file is not read (OVERSIZE).
const SMALL_MAX = 64 << 10;       // status.json, manifest.json, board-ready.json
const READ_CAPS = {
  agentJson: 1 << 20,   // an agent's small run files (cfg-a<i>.json, summary.json, agent-meta.json)
  agentLog: 64 << 20,   // an agent's logs (steps.jsonl, harness.out, relay.jsonl, ...)
  board: 512 << 20,     // the board VM runs no agent; this only bounds a runaway board
};
// DataDevice.writeFile is given text only: a Uint8Array did not arrive byte for byte in CheerpX 1.3.9 (dev
// harness finding), and every inbox file is JSON anyway (seals, replies, metas, stop).
const STOP_BODY = JSON.stringify({ v: 1 });
const LEGACY_MAX_AGE_MS = 24 * 3600 * 1000;
// status.json phases (PROTOCOL.md §4). The live status is agent-writable, so anything else is shown as "?".
const LIVE_PHASES = new Set(["setup", "running", "exporting", "done", "error"]);
const livePhase = (st) => (st && LIVE_PHASES.has(st.phase) ? st.phase : "?");

// ---- small helpers ----

const dec = new TextDecoder(), enc = new TextEncoder();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randHex = (n) => bytesToHex(crypto.getRandomValues(new Uint8Array(n)));
const epochS = () => Date.now() / 1000;  // run.json / agent-meta times (PROTOCOL.md §9.3)
const parseJson = (bytes) => { try { return bytes ? JSON.parse(dec.decode(bytes)) : null; } catch { return null; } };
// Error codes are ours (fixed E_ codes); anything else (an exception text, an agent-written status error) is
// replaced by the fallback, so no guest- or agent-chosen text reaches the page or the outcome.
const CODE_RE = /^E_[A-Z0-9_]{1,40}$/;
const errCode = (e, fallback = "E_INTERNAL") => {
  const m = typeof e === "string" ? e : e?.message;
  return typeof m === "string" && CODE_RE.test(m) ? m : fallback;
};
const OVERSIZE = Symbol("oversize"), STOPPED = Symbol("stopped");
const real = (b) => (b === OVERSIZE ? null : b);
function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}
// p, or `fallback` after ms (p keeps running; its late result or rejection is dropped).
function withinOr(p, ms, fallback) {
  let timer;
  return Promise.race([p, new Promise((r) => { timer = setTimeout(() => r(fallback), ms); })]).finally(() => clearTimeout(timer));
}

async function fetchBytes(path, init) {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`E_ASSET_${Number(r.status) || 0}`);
  return new Uint8Array(await r.arrayBuffer());
}

// ---- device reads ----

// take(blob) for `path` on a CheerpX device; null if the file does not exist, vanished while being read, or the
// read did not finish within ms; OVERSIZE (nothing read) if it is larger than max bytes.
function readBlob(dev, path, ms, max, take) {
  const read = (async () => {
    try {
      const blob = await dev.readFileAsBlob(path);
      if (!blob) return null;
      if (blob.size > max) return OVERSIZE;
      return await take(blob);
    } catch { return null; }
  })();
  return withinOr(read, ms, null);
}

// Bytes are copied into this realm: an ArrayBuffer of a frame's realm would keep that realm (and its whole VM)
// alive after the frame is removed.
const copyBytes = async (blobPart) => new Uint8Array(await blobPart.arrayBuffer()).slice();

// A file's bytes, null or OVERSIZE (readBlob).
const readDev = (dev, path, ms, max = SMALL_MAX) => readBlob(dev, path, ms, max, copyBytes);

// The bytes of a growing file from offset `from` on: {size, bytes} (bytes empty if the file is not longer than
// `from`), null or OVERSIZE. Only the new part is copied (live tail of steps.jsonl).
const readDevFrom = (dev, path, from, ms, max) => readBlob(dev, path, ms, max, async (blob) =>
  ({ size: blob.size, bytes: blob.size <= from ? new Uint8Array(0) : await copyBytes(blob.slice(from)) }));

// A small JSON file (status.json, board-ready.json) parsed, or null.
const readJson = async (dev, path, ms) => parseJson(real(await readDev(dev, path, ms)));

// Reads `path` on a VM's export device into st.cache and returns the bytes (or null / OVERSIZE).
async function readCached(st, path, ms, max = SMALL_MAX) {
  const bytes = await readDev(st.vm.exportDev, path, ms, max);
  if (bytes && bytes !== OVERSIZE) st.cache.set(path, bytes);
  return bytes;
}
const readCachedJson = async (st, path, ms) => parseJson(real(await readCached(st, path, ms)));

// labctl's exit as a promise of this realm, {status} or {status: null, error}. A frame-realm promise (cx.run's)
// kept in the swarm's state would keep the removed frame's realm alive.
function labctlExit(handle, keyHex) {
  return new Promise((done) => {
    handle.runLabctl(keyHex).then((status) => done({ status: Number(status) }), (e) => done({ status: null, error: errCode(e, "E_LABCTL_RUN") }));
  });
}

// ---- storage (PROTOCOL.md §1): one Web Lock per run plus one per VM storage id ----

// Takes runLock(runId) and runLock(<runId>-<vm>) for every VM and holds them until release() (dispose(), after
// the page stored the trace; otherwise for the rest of this tab's life). The per-VM locks make the solo page's
// cleanup (vm.js, which treats <runId>-<vm> as a run id) see these databases as owned.
// -> {held: every lock taken, release()}
export async function claimSwarmStorage(runId, vms) {
  if (!navigator.locks) return { held: false, release() {} };
  const ids = [runId, ...vms.map((vm) => vmStorageId(runId, vm))];
  const releases = [];
  const held = await Promise.all(ids.map((id) => new Promise((ok) => {
    navigator.locks.request(runLock(id), () => {
      ok(true);
      return new Promise((r) => { releases.push(r); });
    }).catch(() => ok(false));
  })));
  return { held: held.every(Boolean), release() { for (const r of releases.splice(0)) r(); } };
}

const canListDatabases = () => typeof indexedDB !== "undefined" && !!indexedDB.databases;

// Deletes every database of this run (its VMs' overlay / out / export), whose locks this tab holds.
async function deleteRunStorage(runId) {
  if (!canListDatabases()) return 0;
  const names = (await indexedDB.databases()).map((d) => d.name).filter((name) => dbRunId(name) === runId);
  await deleteDatabases(names);
  return names.length;
}

// Deletes the databases of runs (solo or swarm) no tab owns: a run's databases go only if its lock is free right
// now. Swarm databases are grouped under their run id (protocol.js dbRunId), so a live swarm's are never taken.
// Legacy (LEGACY_DB_PREFIX, no lock) databases go once their run is a day old. Without navigator.locks or
// indexedDB.databases() nothing is deleted.
export async function deleteStaleSwarmStorage() {
  if (!navigator.locks || !canListDatabases()) return;
  const legacy = new RegExp(`^${LEGACY_DB_PREFIX}(${DB_KINDS.join("|")})-(.+)$`);
  const runs = new Map(), old = [];
  for (const { name } of await indexedDB.databases()) {
    const id = dbRunId(name), l = legacy.exec(unwrapDbName(name));
    if (id) runs.set(id, [...(runs.get(id) || []), name]);
    else if (l && Date.now() - runStartMs(l[2]) > LEGACY_MAX_AGE_MS) old.push(name);
  }
  await deleteDatabases(old);
  for (const [id, names] of runs) {
    await navigator.locks.request(runLock(id), { ifAvailable: true }, async (lock) => {
      if (lock) await deleteDatabases(names);
    });
  }
}

function runStartMs(runId) {
  const m = /-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})-[0-9a-f]{4}$/.exec(runId);
  return m ? Date.UTC(m[1], m[2] - 1, m[3], m[4], m[5], m[6]) : Infinity;
}

function deleteDatabases(names) {
  return Promise.all(names.map((name) => new Promise((done) => {
    const req = indexedDB.deleteDatabase(name);
    req.onsuccess = req.onerror = req.onblocked = () => done();
  })));
}

// ---- per-VM state ----

// Fields both roles share: phase (shown by onStatus), the live frame (vm = its handle, null once the frame is
// gone: from then on its devices are never touched), labctl's exit (labctlExit), the end-of-life export read
// (status, manifest, files: run/<name> -> bytes) and cache: export path -> bytes of every end-of-life read, so the
// readFile() test hook still answers from page memory after the frame is gone. tail: the console tail as of the
// VM's end (PROTOCOL.md §8).
const vmState = () => ({ phase: "pending", vm: null, frame: null, exit: null, error: null, tail: null,
  status: null, manifest: null, files: new Map(), cache: new Map() });

// Board phases: pending -> booting -> starting -> (its live status) -> running -> ending -> done | error.
const boardState = () => ({ ...vmState(),
  live: new Map(),  // run/<BOARD_LIVE_FILES> as last polled
});

// Agent phases: pending -> booting -> starting -> (its live status) -> done | error | skipped (ended: true).
const agentState = (i) => ({ ...vmState(), i, ended: false,
  steps: 0, calls: 0, tokens: 0,  // onStatus counters
  broker: null,
  startedAt: null, endedAt: null,  // epoch s, for the page-made meta stub
  booted: deferred(),  // its boot settled: the next agent's boot may start
  labctl: false,       // its labctl was started (the grace applies)
  force: deferred(),   // resolved: end it now, without waiting for labctl (grace over, or the run failed)
  forced: false,       // it was ended that way
  deadlined: false,    // its own page deadline passed
  graceTimer: null, deadlineTimer: null,
  done: null,          // its whole life: startAgent -> finishAgent
  consumed: 0,         // bytes of the live steps.jsonl handed to onRecord
  live: null,          // those bytes (liveFiles(), and the zip if it exported none)
  oversize: false,     // a file of its was over its read cap
});

const clearAgentTimers = (a) => { clearTimeout(a.graceTimer); clearTimeout(a.deadlineTimer); };
const stepsFile = (i) => agentLiveFiles(i)[0];

// ---- end codes and outcome (pure) ----

// The board's end code from labctl's exit r and its final status st, or null if it ended done.
function boardEndError(r, st) {
  if (r && !r.error && r.status === 0 && st?.phase === "done") return null;
  return r?.error || (st?.phase === "error" ? errCode(st.error, "E_STATUS")
    : r?.status != null ? `E_EXIT_${r.status}` : "E_NO_STATUS");
}

// An agent's end code from its end r ({forced} or labctl's exit) and its final status st, or null if it ended
// done. The status error is agent-writable (PROTOCOL.md §4): only a fixed E_ code is taken from it.
function agentEndError(r, st) {
  if (r.forced) return "E_AGENT_LOST";
  if (r.error) return r.error;
  if (r.status === 0 && st?.phase === "done") return null;
  return st?.phase === "error" ? errCode(st.error, "E_STATUS")
    : !st ? "E_NO_STATUS" : r.status !== 0 ? `E_EXIT_${r.status}` : "E_STATUS";
}

// The run's outcome once every VM ended; stopping is the Stop reason ("user" | "board" | "error") or null.
function decideOutcome(board, agents, stopping) {
  if (board.error) return { result: "error", error: board.error, detail: "board" };
  // a frame removed after the grace, or a boot abandoned, because of Stop or the agent's deadline is no error
  const stopLike = (a) => (stopping || a.deadlined) && (a.error === "E_AGENT_LOST" || a.error === "E_STOPPED");
  const bad = agents.filter((a) => a.error && !stopLike(a));
  if (bad.length) return { result: "error", error: bad[0].error, detail: bad.map((a) => `a${a.i}:${a.error}`).join(",").slice(0, 200) };
  if (stopping === "board") return { result: "stopped", detail: "board" };
  if (stopping === "user" || stopping === "error") return { result: "stopped" };
  if (agents.some((a) => a.deadlined)) return { result: "stopped", detail: "deadline" };
  return { result: "done" };
}

// The zip name's suffix (PROTOCOL.md §5.1 naming): -incomplete, else -aborted-<status> for a run.json with fatal.
function zipSuffix(complete, fatal) {
  if (!complete) return "-incomplete";
  return fatal ? `-aborted-${String(fatal.status).replace(/[^\w]/g, "")}` : "";
}

// ---- the swarm ----

// settings = readSettings() + {nAgents}; hooks {onStatus, onRecord, onCall, onIgnored} (docs/browser-swarm.md §6).
// opts (optional, dev harness / tests; the lab page passes none): {timing: {...TIMING overrides}, frameUrl,
// container, assets: {manifest, seed} URLs, prepareVm(vm, handle) awaited after each boot before the inputs,
// readCaps: {agentJson, agentLog, board} byte caps}.
export function createSwarm(settings, hooks = {}, opts = {}) {
  const n = settings.nAgents ?? 1;
  if (!Number.isInteger(n) || n < 1 || n > MAX_AGENTS) throw new Error("E_N_AGENTS");
  const runId = makeRunId(settings.iface, n);
  const timing = { ...TIMING, ...(opts.timing || {}) };
  const caps = { ...READ_CAPS, ...(opts.readCaps || {}) };
  // extensionless: Workers static assets redirect /vmframe.html to /vmframe (html_handling auto-trailing-slash),
  // which would cost every VM frame a round trip; dev-server.mjs serves /vmframe the same way
  const frameUrl = opts.frameUrl || "/vmframe";
  const assets = { manifest: "/vm-manifest.json", seed: "/data/seed.json", ...(opts.assets || {}) };
  const vmNames = swarmVms(n);
  const call = (name, ...args) => { try { hooks[name]?.(...args); } catch {} };

  const t0 = performance.now();
  let phase = "init", outcome = null, runPromise = null, disposed = null;
  let stopping = null;            // the Stop reason once beginStop / a failure ran ("user" | "board" | "error")
  const stopSignal = deferred();  // resolved with stopping: a VM boot in progress is abandoned
  let storage = null, fabric = null, budget = null, pollTimer = null;
  // From loading on: the image name, the seed as fetched (liveFiles: live views map the seed threads), the run's
  // start (epoch s, as run.json; the board's own start once ready) and its run_timeout_s.
  let image = null, seedBytes = null, started = null, runTimeoutMs = DEFAULT_RUN_TIMEOUT_S * 1000;
  // From board on: when the board's labctl started (performance.now(), the base of the board's own deadline),
  // whether board-ready.json was read, and its ctf (only ever handed to the sealed agent specs and liveFiles()).
  let boardLabctlAt = null, boardReady = false, ctf = null;
  let boardEnding = false;  // endBoard ran (once): the board's inbox files are written at most once (§1)
  const board = boardState();
  const agents = Array.from({ length: n }, (_, i) => agentState(i));
  const frames = new Set();
  const stateOf = (vm) => (vm === BOARD_VM ? board : agents.find((a) => agentVm(a.i) === vm));

  function emit() {
    call("onStatus", {
      phase, elapsedMs: Math.round(performance.now() - t0), board: { phase: board.phase },
      agents: agents.map(({ i, phase: p, steps, calls, tokens, ended }) => ({ i, phase: p, steps, calls, tokens, ended })),
    });
  }
  const setPhase = (p) => { phase = p; emit(); };

  // ---- VM frames (js/vmframe.js) ----

  function createFrame(vm) {
    const frame = document.createElement("iframe");
    frame.className = "vmframe";
    frame.title = `vm ${vm}`;
    frame.setAttribute("aria-hidden", "true");
    frame.tabIndex = -1;
    frame.width = "0";
    frame.height = "0";
    frame.src = `${frameUrl}?vm=${encodeURIComponent(vm)}`;
    frames.add(frame);
    return frame;
  }

  function removeFrame(frame) {
    if (!frame) return;
    frames.delete(frame);
    try { frame.remove(); } catch {}
  }

  // Resolves once the frame loaded and its vmframe.js module is ready (E_FRAME_LOAD after frameLoadMs).
  async function frameReady(frame, loaded) {
    if (await withinOr(loaded.then(() => true), timing.frameLoadMs, false) !== true) throw new Error("E_FRAME_LOAD");
    const t = performance.now();
    while (!frame.contentWindow?.__vmframe) {
      if (performance.now() - t > timing.frameLoadMs) throw new Error("E_FRAME_LOAD");
      await sleep(50);
    }
  }

  // Boots one VM in its own iframe on its fabric interface. Resolves to {frame, handle}.
  async function bootFrame(vm) {
    const frame = createFrame(vm);
    const loaded = new Promise((r) => frame.addEventListener("load", r, { once: true }));
    (opts.container || document.body).append(frame);
    try {
      await frameReady(frame, loaded);
      if (stopping) throw new Error("E_STOPPED");
      const booting = new Promise((ok, fail) => frame.contentWindow.__vmframe.boot({ runId, vm, image,
        networkInterface: fabric.iface(vm) }).then(ok, (e) => fail(new Error(errCode(e, "E_BOOT")))));
      // Stop abandons a boot in progress (the frame goes below) instead of finishing it.
      const handle = await withinOr(Promise.race([booting, stopSignal.promise.then(() => STOPPED)]), timing.bootMs, null);
      if (handle === STOPPED) throw new Error("E_STOPPED");
      if (!handle) throw new Error("E_BOOT_TIMEOUT");
      return { frame, handle };
    } catch (e) {
      // A frame that failed to load or boot goes at once (a late boot would hold a whole VM's memory) and its
      // VM is cut out of the fabric before anything could connect.
      removeFrame(frame);
      fabric.cut(vm).catch(() => {});
      throw e;
    }
  }

  // Seals and writes a VM's boot inputs into its inbox (PROTOCOL.md §2: the agent can read the inbox).
  async function writeInputs(vm, role, keyHex, spec, seed) {
    const data = { spec: enc.encode(JSON.stringify(spec)), seed };
    for (const input of ROLE_INPUTS[role]) {
      const { path, label } = BOOT_INPUTS[input];
      await vm.inbox.writeFile(path, await seal(keyHex, label, data[input]));
    }
  }

  // ---- live tail (display only: the live export is writable by the agent, PROTOCOL.md §4) ----

  // The VM handle is taken once: finishAgent may drop a.vm while a poll's read is in flight.
  async function tailAgent(a) {
    const vm = a.vm;
    if (!vm || a.ended) return;
    const st = await readJson(vm.exportDev, STATUS_PATH, timing.readMs);
    if (st && !a.ended) a.phase = livePhase(st);
    const from = a.consumed;
    const r = await readDevFrom(vm.exportDev, `${RUN_DIR}/${stepsFile(a.i)}`, from, timing.readMs, caps.agentLog);
    if (r === OVERSIZE) a.oversize = true;
    else if (r && !a.ended) consume(a, from, r.bytes);
  }

  // New complete lines of a{i}/steps.jsonl (read from offset `from` on) -> onRecord; the bytes up to the last
  // newline are kept for liveFiles(). steps.jsonl only grows (the harness appends; labctl's final copy is the
  // whole pinned file), so a shorter file is a transient one (export rebuild, §5.3) and gives no new bytes, as
  // session.js does: restarting would hand every record to onRecord twice. A read that started at another offset
  // (two tails raced) is dropped for the same reason.
  function consume(a, from, chunk) {
    if (from !== a.consumed) return;
    const end = chunk.lastIndexOf(10) + 1;
    if (end <= 0) return;
    const lines = dec.decode(chunk.subarray(0, end)).split("\n").filter(Boolean);
    const live = new Uint8Array(from + end);
    if (a.live) live.set(a.live.subarray(0, from));
    live.set(chunk.subarray(0, end), from);
    a.consumed = from + end;
    a.live = live;
    for (const line of lines) {
      let rec;
      try { rec = JSON.parse(line); } catch { continue; }
      if (rec?.kind === "step") a.steps++;
      call("onRecord", a.i, rec);
    }
  }

  async function tailBoard() {
    const vm = board.vm;
    if (!vm || boardEnding) return;
    const st = await readJson(vm.exportDev, STATUS_PATH, timing.readMs);
    if (st && !boardEnding) board.phase = livePhase(st);
    for (const name of BOARD_LIVE_FILES) {
      if (boardEnding) return;
      const bytes = real(await readDev(vm.exportDev, `${RUN_DIR}/${name}`, timing.readMs, caps.board));
      if (bytes && !boardEnding) board.live.set(name, bytes);
    }
  }

  // Polls never overlap and never queue: a tick while one is in flight is skipped (a slow or wedged VM read must
  // not build a backlog). Errors only affect the display.
  let polling = null;
  const poll = () => (polling ||= (async () => {
    await tailBoard();
    for (const a of agents) await tailAgent(a);
    emit();
  })().catch(() => {}).finally(() => { polling = null; }));

  // ---- board ----

  async function startBoard(base) {
    board.phase = "booting";
    emit();
    const { frame, handle } = await bootFrame(BOARD_VM);
    board.frame = frame;
    board.vm = handle;
    if (opts.prepareVm) await opts.prepareVm(BOARD_VM, handle);  // dev harness only (spikes/swarm)
    const keyHex = randHex(32);
    await writeInputs(handle, "board", keyHex, boardSpec(base, n), seedBytes);
    board.phase = "starting";
    emit();
    boardLabctlAt = performance.now();
    board.exit = labctlExit(handle, keyHex);
  }

  // board-ready.json for this run, or throws: the board's labctl ended first (its status error), timeout, Stop.
  async function waitBoardReady() {
    const t = performance.now();
    let exited = null;
    board.exit.then((r) => { exited = r; });
    for (;;) {
      const ready = await readJson(board.vm.exportDev, BOARD_READY_PATH, timing.readMs);
      if (ready && ready.v === 1 && ready.run_id === runId && ready.ctf && typeof ready.ctf === "object") return ready;
      if (exited) {
        const st = await readJson(board.vm.exportDev, STATUS_PATH, timing.readMs);
        throw new Error(st?.phase === "error" ? errCode(st.error, "E_BOARD_EXIT") : "E_BOARD_EXIT");
      }
      if (stopping) throw new Error("E_STOPPED_BEFORE_AGENTS");
      if (performance.now() - t > timing.boardReadyMs) throw new Error("E_BOARD_READY_TIMEOUT");
      await sleep(Math.min(timing.pollMs, 500));
    }
  }

  // §2 step 7: each agent's meta (its exported agent-meta.json as text, or the §9.1 page-made stub for an agent
  // that started but exported none, protocol.js pageAgentMeta), then stop. Each file is written once.
  async function writeBoardInbox() {
    for (const a of agents) {
      const meta = a.files.get(agentMetaFile(a.i));
      if (meta) await board.vm.inbox.writeFile(BOARD_INBOX.agentMeta(a.i), dec.decode(meta));
      else if (a.error && a.error !== "E_STOPPED" && a.startedAt != null) {
        await board.vm.inbox.writeFile(BOARD_INBOX.agentMeta(a.i), JSON.stringify(pageAgentMeta(runId, a.i, a.error,
          a.startedAt, a.endedAt ?? epochS())));
      }
    }
    await board.vm.inbox.writeFile(BOARD_INBOX.stop, STOP_BODY);
  }

  // After every agent is gone: the board inbox, then wait (up to endMs) for the board's labctl and read its export.
  async function endBoard(endMs = timing.boardEndMs) {
    if (!board.vm || boardEnding) return;
    boardEnding = true;
    board.phase = board.phase === "error" ? board.phase : "ending";
    emit();
    try { await writeBoardInbox(); } catch { board.error ||= "E_BOARD_INBOX"; }
    const r = board.exit ? await withinOr(board.exit, endMs, { status: null, error: "E_BOARD_END_TIMEOUT" }) : null;
    await readBoardExport();
    board.status = await readCachedJson(board, STATUS_PATH, timing.readMs);
    const endError = boardEndError(r, board.status);
    if (endError) board.error ||= endError;
    board.phase = endError ? "error" : "done";
    emit();
  }

  async function readBoardExport() {
    board.manifest = await readCachedJson(board, MANIFEST_PATH, timing.readMs);
    for (const name of boardFiles) {
      const data = real(await readCached(board, `${RUN_DIR}/${name}`, timing.readMs, caps.board));
      if (data) board.files.set(name, data);
    }
  }

  // ---- agents ----

  // Boots agent a and starts its labctl and broker; resolves (a.done) once it has ended (finishAgent). A failed
  // start ends it at once with its error code.
  async function startAgent(a, base, provider, harness) {
    a.phase = "booting";
    a.startedAt = epochS();
    emit();
    try {
      let booted;
      try { booted = await bootFrame(agentVm(a.i)); } finally { a.booted.resolve(); }
      const { frame, handle } = booted;
      a.frame = frame;
      a.vm = handle;
      if (opts.prepareVm) await opts.prepareVm(agentVm(a.i), handle);
      if (stopping) throw new Error("E_STOPPED");
      const keyHex = randHex(32);
      await writeInputs(handle, "agent", keyHex, agentSpec(base, n, a.i, ctf));
      a.broker = startBroker({ outbox: handle.outbox, inbox: handle.inbox, key: await hmacKey(keyHex), agent: a.i,
        provider, harness, budget,
        onCall: (info) => { a.calls = info.calls ?? a.calls; a.tokens = info.tokens ?? a.tokens; call("onCall", a.i, info); emit(); },
        onIgnored: (seq) => call("onIgnored", a.i, seq) });
      a.phase = "starting";
      emit();
      a.exit = labctlExit(handle, keyHex);
      a.labctl = true;
      armDeadline(a);
      if (stopping) armGrace(a);  // a Stop that came during this agent's start: the budget already answers E_ABORT
    } catch (e) {
      a.error = errCode(e, "E_AGENT_START");
      a.exit = Promise.resolve({ status: null, error: a.error });
    }
    a.done = (async () => {
      const r = await Promise.race([a.exit, a.force.promise.then(() => ({ forced: true }))]);
      await finishAgent(a, r);
    })();
    return a.done;
  }

  // §2 step 6: read the agent's export, then release its VM and decide how it ended. A forced end (labctl did not
  // exit within the grace after Stop / deadline, e.g. the S2k wedge) reads whatever the export holds: the CTF is
  // exported only after the agent was stopped (PROTOCOL.md §5.3), so a partial export never has it.
  async function finishAgent(a, r) {
    if (a.ended) return;
    a.forced = !!r.forced;
    clearAgentTimers(a);
    try {
      if (a.vm) await readAgentExport(a);
    } catch {}
    a.endedAt = epochS();
    await releaseAgentVm(a);
    a.error ||= agentEndError(r, a.status);
    if (a.oversize) a.error ||= "E_EXPORT_SIZE";
    a.phase = a.error === "E_STOPPED" ? "skipped" : a.error ? "error" : "done";
    a.ended = true;
    emit();
  }

  // The last live tail, then status, manifest and every run/ file into page memory (live steps if none exported).
  async function readAgentExport(a) {
    await tailAgent(a);
    a.status = await readCachedJson(a, STATUS_PATH, timing.readMs);
    a.manifest = await readCachedJson(a, MANIFEST_PATH, timing.readMs);
    for (const name of agentFiles(a.i)) {
      const data = await readCached(a, `${RUN_DIR}/${name}`, timing.readMs, (name.endsWith(".json") ? caps.agentJson : caps.agentLog));
      if (data === OVERSIZE) a.oversize = true;
      else if (data) a.files.set(name, data);
    }
    if (!a.files.has(stepsFile(a.i)) && a.live) a.files.set(stepsFile(a.i), a.live);
  }

  // Keeps the console tail, then drops every reference to the frame's realm: the handle (polls and hooks skip the
  // agent from here on), the broker (it holds the frame's devices), the iframe; and cuts the VM out of the fabric.
  async function releaseAgentVm(a) {
    try { a.tail = a.vm?.consoleTail(); } catch {}
    a.vm = null;
    if (a.broker) await withinOr(a.broker.close().catch(() => {}), timing.readMs, null);
    a.broker = null;
    removeFrame(a.frame);
    a.frame = null;
    if (fabric) await fabric.cut(agentVm(a.i)).catch(() => {});
  }

  // An agent's grace (§6): after Stop or its deadline, its labctl gets stopGraceMs to export (extended while its
  // live status says "exporting", by at most exportGraceMs) before its frame is removed. Armed per agent once its
  // labctl runs, so an agent that starts after a Stop gets the whole grace too.
  function armGrace(a) {
    if (a.graceTimer || a.ended || !a.labctl) return;
    const hardEnd = performance.now() + timing.stopGraceMs + timing.exportGraceMs;
    const check = () => {
      if (a.ended) return;
      if (a.phase === "exporting" && performance.now() < hardEnd) { a.graceTimer = setTimeout(check, timing.pollMs); return; }
      a.force.resolve();
    };
    a.graceTimer = setTimeout(check, timing.stopGraceMs);
  }

  // An agent's own page deadline (from its labctl start, capped by the board's deadline; see TIMING): its broker
  // answers E_ABORT (the harness ends through FATAL, as on Stop), then the grace. Only that agent is affected.
  function armDeadline(a) {
    const now = performance.now();
    const own = now + runTimeoutMs + timing.deadlineMarginMs;
    const boardCap = (boardLabctlAt ?? now) + runTimeoutMs + timing.boardMarginMs - timing.boardReserveMs;
    a.deadlineTimer = setTimeout(() => {
      if (a.ended || a.deadlined) return;
      a.deadlined = true;
      a.broker?.stop();
      armGrace(a);
      emit();
    }, Math.max(0, Math.min(own, boardCap) - now));
  }

  // ---- Stop ----

  // Every broker on the budget answers E_ABORT (the harnesses end through FATAL) and a VM boot in progress is
  // abandoned.
  function markStopping(reason) {
    stopping = reason;
    stopSignal.resolve();
    budget?.stop();
  }

  // Stop (§6): markStopping, and each running agent gets its grace. Ignored once the run is ending (every agent is
  // gone).
  function beginStop(reason) {
    if (stopping || outcome || phase === "ending") return;
    markStopping(reason);
    for (const a of agents) armGrace(a);
    emit();
  }

  // ---- run phases ----

  // loading: storage, assets, and the run-wide objects (fabric, shared budget, poll timer). -> the specs' base.
  async function loadPhase() {
    setPhase("loading");
    storage = await claimSwarmStorage(runId, vmNames);
    await deleteStaleSwarmStorage().catch(() => {});
    const manifest = parseJson(await fetchBytes(assets.manifest, { cache: "no-store" }));
    if (!manifest || typeof manifest.filename !== "string") throw new Error("E_ASSET_MANIFEST");
    image = manifest.filename;
    seedBytes = await fetchBytes(assets.seed);
    const { preset, iface, harness, wire, provider, caps: budgetCaps } = settings;
    started = epochS();
    const base = { run_id: runId, iface, rep: 0, harness, wire, provider_label: `${preset}:${provider.model}`,
      cx_version: CX_VERSION, image };
    fabric = createFabric({ names: vmNames, routes: swarmRoutes(n) });
    budget = createBudget(budgetCaps);
    if (stopping) budget.stop();
    runTimeoutMs = (Number(harness.run_timeout_s) >= 0 ? Number(harness.run_timeout_s) : DEFAULT_RUN_TIMEOUT_S) * 1000;
    pollTimer = setInterval(poll, timing.pollMs);
    return base;
  }

  // board: boot it and wait for board-ready.json (the shared ctf). A Stop up to here ends the run stopped.
  async function boardPhase(base) {
    if (stopping) throw new Error("E_STOPPED_BEFORE_AGENTS");  // Stop while the assets loaded: no board boot
    setPhase("board");
    try { await startBoard(base); } catch (e) {
      throw stopping && errCode(e) === "E_STOPPED" ? new Error("E_STOPPED_BEFORE_AGENTS") : e;
    }
    const ready = await waitBoardReady();
    boardReady = true;
    ctf = ready.ctf;
    if (Number.isFinite(Date.parse(ready.started))) started = Date.parse(ready.started) / 1000;
    board.phase = "running";
    // A board whose labctl ends while agents still run (E_BOARD: the board process died) leaves them talking to
    // nothing until run_timeout_s: stop the swarm as Stop does; endBoard then reads the board's error.
    board.exit.then(() => { if (!boardEnding) beginStop("board"); });
  }

  // agents: one VM boots at a time; the next boot starts once this one's boot settled (a.booted; no timer loop,
  // which a background tab would throttle), plus staggerMs. -> every started agent's life (a.done).
  async function agentsPhase(base) {
    setPhase("agents");
    const { provider, harness } = settings;
    const running = [];
    for (const a of agents) {
      if (stopping) break;
      const agentRun = startAgent(a, base, provider, harness);
      running.push(agentRun);
      await Promise.race([a.booted.promise, agentRun]);
      if (a.i < n - 1 && !stopping) await Promise.race([sleep(timing.staggerMs), stopSignal.promise]);
    }
    if (stopping) budget.stop();  // agents that started during the stop race also get E_ABORT
    return running;
  }

  // running: until every started agent ended; an agent never started is skipped.
  async function runningPhase(running) {
    setPhase("running");
    await Promise.all(running);
    for (const a of agents) if (!a.ended) { a.phase = "skipped"; a.ended = true; }
  }

  // ending: a last poll, then the board's end.
  async function endingPhase() {
    clearInterval(pollTimer);
    setPhase("ending");
    await withinOr(poll(), timing.readMs, null);
    await endBoard();
  }

  // After a failure in any phase whatever started still ends cleanly: agents are stopped and read, the board is
  // told to stop (a board that never got ready has nothing to export: it gets the short boardEarlyEndMs).
  async function abortRun() {
    if (!stopping) markStopping("error");
    for (const a of agents) a.force.resolve();
    await Promise.all(agents.map((a) => a.done).filter(Boolean)).catch(() => {});
    if (board.vm && board.exit && !board.manifest) {
      await endBoard(boardReady ? timing.boardEndMs : timing.boardEarlyEndMs).catch(() => {});
    }
  }

  // Every timer, broker, frame and fabric route goes, whatever happened.
  async function teardown() {
    for (const a of agents) clearAgentTimers(a);
    clearInterval(pollTimer);
    for (const a of agents) if (a.broker) a.broker.close().catch(() => {});
    for (const f of [...frames]) removeFrame(f);
    if (fabric) for (const vm of vmNames) await fabric.cut(vm).catch(() => {});
    try { board.tail = board.vm?.consoleTail() ?? board.tail; } catch {}
    board.vm = null;
    board.frame = null;
  }

  async function run() {
    let fatal = null;
    try {
      const base = await loadPhase();
      await boardPhase(base);
      await runningPhase(await agentsPhase(base));
      await endingPhase();
    } catch (e) {
      fatal = errCode(e);
      await abortRun();
    } finally {
      await teardown();
    }
    // A Stop before the agents started is a stop, not an error; any other exception is the run's error.
    outcome = fatal && fatal !== "E_STOPPED_BEFORE_AGENTS" ? { result: "error", error: fatal } : decideOutcome(board, agents, stopping);
    setPhase(outcome.result);
    return outcome;
  }

  // ---- trace ----

  // The partial run dir (§6 liveFiles): the final files once read, else the live ones: a run.json built from
  // the run spec (no end fields), the seed as fetched, the board's live event mirror and each agent's live steps.jsonl.
  async function liveFiles() {
    const out = new Map();
    out.set("run.json", board.files.get("run.json") || enc.encode(JSON.stringify(liveRunJson())));
    if (seedBytes) out.set("seed.json", seedBytes);
    for (const [k, v] of board.live) out.set(k, v);
    for (const [k, v] of board.files) out.set(k, v);
    for (const a of agents) {
      if (a.live) out.set(stepsFile(a.i), a.live);
      for (const [k, v] of a.files) out.set(k, v);
    }
    return out;
  }

  function liveRunJson() {
    const { iface, harness, preset, provider } = settings;
    return { run_id: runId, iface, n_agents: n, rep: 0, harness, started, ...(ctf ? { ctf } : {}),
      params: { env: "cheerpx", topology: "board-split", provider: `${preset}:${provider?.model}`, cx_version: CX_VERSION,
        image, isolation: "weak-uid", preview: true, solve_verified: false, live: true } };
  }

  // §3 layout: <run_id>/<board files>, <run_id>/<agent files>, <run_id>/manifest.json. Complete only when the
  // board exported (its manifest exists), every agent's export says agent_stopped and the run ended done or
  // stopped.
  async function zip() {
    const files = [];
    for (const name of boardFiles) {
      // live mirror (or the seed as fetched) only if never exported
      const data = board.files.get(name) ?? board.live.get(name) ?? (name === "seed.json" ? seedBytes : null);
      if (data) files.push({ name, data });
    }
    for (const a of agents) {
      for (const name of agentFiles(a.i)) {
        const data = a.files.get(name) ?? (name === stepsFile(a.i) ? a.live : null);
        if (data) files.push({ name, data });
      }
    }
    const agentStopped = agents.map((a) => a.manifest?.agent_stopped === true);
    const manifest = { v: 1, run_id: runId, files: files.map((f) => f.name), agent_stopped: agentStopped,
      topology: "board-split" };
    files.push({ name: "manifest.json", data: enc.encode(JSON.stringify(manifest)) });
    const fatal = parseJson(board.files.get("run.json"))?.fatal;
    const complete = !!board.manifest && agentStopped.every(Boolean) && ["done", "stopped"].includes(outcome?.result);
    return { filename: `${runId}${zipSuffix(complete, fatal)}.zip`,
      blob: makeZip(files.map((f) => ({ name: `${runId}/${f.name}`, data: f.data }))) };
  }

  return {
    runId,
    run() { return (runPromise ||= run()); },
    stop() { beginStop("user"); },
    zip,
    liveFiles,
    outcome: () => outcome,
    // Test hooks (never rendered): a VM's console tail (its last 4 KiB as of the VM's end, PROTOCOL.md §8), a
    // file's text on a VM's export/outbox device (paths as §5.1; once the VM's frame is gone, export reads are
    // answered from what the page read at its end: status.json, manifest.json, run/<file>), and numbers-only
    // fabric/budget counters.
    consoleTail(vm) {
      const st = stateOf(vm);
      if (st?.vm) st.tail = st.vm.consoleTail();
      return st?.tail ?? "";
    },
    async readFile(vm, mount, path) {
      const st = stateOf(vm);
      if (!st) return null;
      if (!st.vm) return mount === "export" && st.cache.has(path) ? dec.decode(st.cache.get(path)) : null;
      const dev = { export: st.vm.exportDev, outbox: st.vm.outbox }[mount];
      const bytes = dev && real(await readDev(dev, path, timing.readMs, vm === BOARD_VM ? caps.board : caps.agentLog));
      return bytes ? dec.decode(bytes) : null;
    },
    stats: () => ({ fabric: fabric?.stats() ?? null, calls: budget?.calls ?? 0, tokens: budget?.tokens ?? 0,
      frames: frames.size }),
    // After run() settled (and the page stored the zip): deletes this run's VM databases and releases its Web
    // Locks. Resolves to the number of databases deleted. Export reads keep answering from page memory.
    dispose() {
      if (!runPromise) return Promise.resolve(0);
      return (disposed ||= runPromise.then(async () => {
        let deleted = 0;
        try { deleted = await deleteRunStorage(runId); } catch {}
        storage?.release();
        return deleted;
      }));
    },
  };
}
