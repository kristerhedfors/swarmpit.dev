// swarmpit lab page (docs/browser-swarm.md §7): the swarmpit.dev replay player over the local trace library, a
// "+" dialog that starts a browser swarm (js/swarm.js createSwarm, §6) with the visitor's key, a live panel with one
// lane per VM, periodic live re-conversion of the partial trace, and upload / download / delete of trace zips.
// This file composes the page modules and boots the library and player; the guest contract is lab/browser/PROTOCOL.md.
//   newswarm.js  the "+" dialog          run.js     the live run controller
//   traces.js    library actions/uploads  view.js    the live panel
//
// The API key lives only in the dialog's input and in the settings object handed to createSwarm (page memory). It is
// never put in IndexedDB, localStorage, the URL, a VM or a trace (library.js whitelists what it stores).
import * as protocol from "./protocol.js";
import { $, codeOf, notice } from "./page.js";
import { createView } from "./view.js";
import { openLibrary } from "./library.js";
import { createPlayerHost } from "./playerhost.js";
import { NOT_ISOLATED, createNewSwarmDialog } from "./newswarm.js";
import { createTraceActions } from "./traces.js";
import { createRunController } from "./run.js";

const isolated = self.crossOriginIsolated;  // CheerpX needs SharedArrayBuffer
const lab = { library: null, host: null };  // set by boot()
let hostError = null;
let persisted = null;  // navigator.storage.persist() result (null: not asked / unsupported)

// The three page modules call each other only through these closures (run is defined last), never while constructed.
const view = createView();
const traceActions = createTraceActions({ lab, view, running: () => run.running(), unsaved: () => run.unsaved() });
const dialog = createNewSwarmDialog({ isolated, running: () => !!run.running(), onStart: () => { void run.start(); } });
const run = createRunController({ lab, view, isolated, ingest: traceActions.ingest, onStarted: dialog.close,
  onBusyChange: dialog.update });

// ---- wiring

$("isolation").textContent = isolated ? "" : NOT_ISOLATED;
$("stop").addEventListener("click", run.stop);
$("saveRaw").addEventListener("click", run.saveUnsaved);
traceActions.wireUploads();
// a running swarm lives only in this tab: leaving asks first (its partial trace is saved periodically, run.js)
addEventListener("beforeunload", (e) => { if (run.running()) { e.preventDefault(); e.returnValue = ""; } });
dialog.update();

// ---- boot: library, player, storage persistence, stored traces

async function boot() {
  lab.library = await openLibrary();
  try {
    lab.host = await createPlayerHost({ root: $("player"), library: lab.library,
      callbacks: { onNew: dialog.open, onUpload: traceActions.openUpload, onDownload: (id) => void traceActions.download(id),
        onDelete: (id) => void traceActions.remove(id) } });
  } catch (e) {
    hostError = codeOf(e);
    notice("The trace player could not be loaded.", true);
    return;
  }
  // stable ids for the e2e driver (PROTOCOL.md §8 lists "download")
  const dl = document.querySelector('.sel-act[data-act="download"]');
  if (dl) dl.id = "download";
  if (!lab.library.persistent) notice("This browser does not allow local storage here; traces last until the tab is closed.");
  await askPersistence();
  await restoreTraces();
}

// best-effort storage can be evicted as a whole under storage pressure (traces included): ask to keep it
async function askPersistence() {
  try { persisted = navigator.storage?.persist ? await navigator.storage.persist() : null; } catch { persisted = null; }
  $("storageNote").textContent = !lab.library.persistent ? "" : persisted ? "Storage here is persistent."
    : "Storage here is best-effort: the browser may evict stored traces under storage pressure; download the ones you keep.";
}

// every stored trace into the selector; selects #trace=<id> if stored, else the newest
async function restoreTraces() {
  const list = await lab.library.list();
  for (const meta of list) await lab.host.addTrace(meta);
  const want = new URLSearchParams(location.hash.slice(1)).get("trace");
  const first = list.find((m) => m.id === want) ?? list[0];
  if (first) await lab.host.select(first.id);
}

const booted = boot().catch((e) => { hostError = codeOf(e); notice("The lab could not start.", true); });

// ---- test hooks (PROTOCOL.md §8, docs/browser-swarm.md §7). They return ids, counts and fixed codes only.

function state() {
  const { library, host } = lab, swarm = run.running(), outcome = run.outcome(), player = host?.player;
  return {
    isolated, hostError, running: !!swarm, runId: swarm?.runId ?? outcome?.runId ?? null, result: outcome?.result ?? null,
    persistent: library?.persistent ?? null, persisted, traces: host ? host.ids().length : 0,
    selected: player?.model?.card.id ?? null, live: host ? host.ids().filter((id) => host.isLive(id)) : [],
    // the replay clock (numbers only): playback position and length in ms, and whether it is playing
    clock: player?.model ? { t: Math.round(player.clock.t), duration: Math.round(player.clock.duration),
      playing: !!player.clock.playing } : null,
    ...run.stats(), empty: $("player").classList.contains("pl-empty"),
    dialog: $("newDlg").open ? "new" : $("uploadDlg").open ? "upload" : null,
  };
}

window.__lab = {
  RUN_FILES: protocol.RUN_FILES,
  ready: () => booted.then(() => !hostError),
  outcome: () => run.outcome(),
  // forwarded as-is to the swarm (§6 runtime: readFile(vm, mount, path), consoleTail(vm)); the two-argument form
  // readFile(mount, path) addresses the board VM
  readFile: async (...a) => {
    const s = run.lastSwarm();
    if (!s?.readFile) return null;
    return a.length === 2 && (a[0] === "export" || a[0] === "outbox") ? s.readFile("board", ...a) : s.readFile(...a);
  },
  consoleTail: (vm = "board") => { const s = run.lastSwarm(); return s?.consoleTail ? s.consoleTail(vm) : ""; },
  async traces() {
    await booted;
    if (!lab.library) return [];
    return (await lab.library.list()).map((m) => ({ id: m.id, source: m.source, bytes: m.bytes, nFiles: m.nFiles,
      nAgents: m.card?.nAgents ?? null, events: m.card?.stats?.events ?? null, nodes: m.card?.stats?.nodes ?? null }));
  },
  async loadTrace(blob) {
    await booted;
    try { return { ok: true, id: await traceActions.ingest({ blob, name: blob?.name, source: "upload" }) }; }
    catch (e) { return { ok: false, code: codeOf(e) }; }
  },
  startSwarm: (override) => run.start(override),
  // e2e: keep the next finished run's VM databases (so storage can be scanned), then free them on request
  retainVmStorage: (on = true) => run.retainVmStorage(on),
  disposeVmStorage: async () => { const s = run.lastSwarm(); return s?.dispose ? s.dispose() : 0; },
  stopSwarm: () => run.stop(),
  refreshLive: () => run.refreshLive(),
  state,
};
