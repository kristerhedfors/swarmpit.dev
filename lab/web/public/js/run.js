// The live run controller (docs/browser-swarm.md §6, §7): starts a browser swarm (js/swarm.js createSwarm) with the
// dialog's settings, drives the live panel (view.js), re-converts the partial trace every LIVE_MS while it runs,
// saves it as a partial library entry at most every PARTIAL_MS, and stores the final zip when the run settles.
//
// Lifecycle of one run: start() -> live (refreshLive every LIVE_MS) -> settle (live closed, final zip ingested,
// selector live marker cleared) -> the run's VM storage freed. Only one run at a time; `swarm` is set from start()
// until the final trace is stored (or failed to store).
import { $, codeOf, notice, within } from "./page.js";
import { makeZip } from "./zip.js";
import { convertFiles } from "./playerhost.js";
import { readSettings, swarmSettings } from "./settings.js";

const LIVE_MS = 10000;      // live re-conversion period (§7)
const LIVE_WAIT_MS = 30000; // one live re-conversion (liveFiles + convert) may take this long before it is dropped
const PARTIAL_MS = 60000;   // a running swarm's partial trace is saved to the library at most this often (crash recovery)

// createRunController({lab, view, isolated, ingest, onStarted, onBusyChange})
//   lab: {library, host}; ingest: traces.js ingest; onStarted(): the dialog closes; onBusyChange(): running() changed
//   -> {start(override?), stop(), refreshLive(), running(), lastSwarm(), outcome(), unsaved(), saveUnsaved(),
//       retainVmStorage(on), stats()}
export function createRunController({ lab, view, isolated, ingest, onStarted, onBusyChange }) {
  let swarm = null;      // the running swarm (null when idle)
  let lastSwarm = null;  // the most recent swarm, for the readFile / consoleTail test hooks after it ended
  let outcome = null;    // {runId, result, error?} of the latest swarm (PROTOCOL.md §8 outcome())
  let stopping = false;
  let unsaved = null;    // {filename, blob} of a finished run that could not be stored (offered as a plain download)
  let retainVm = false;  // e2e: keep the finished run's VM databases until disposeVmStorage()
  // live re-conversion: allowed while liveOpen (false once run() settled: the final trace must not be overwritten)
  let liveOpen = false, liveTimer = 0, liveBusy = null, liveConversions = 0, liveErrors = 0;
  let partialAt = 0, partialP = null;  // last partial save (performance.now()) and the one in flight

  const isCurrent = (s) => s === swarm && liveOpen;

  // One live re-conversion of the running swarm's files, shown in the player; also triggers the periodic partial save.
  async function refreshLive() {
    if (liveBusy || !swarm || !lab.host || !liveOpen) return;
    const s = swarm;
    liveBusy = within(convertLive(s), LIVE_WAIT_MS, "E_LIVE_TIMEOUT")
      .catch(() => { liveErrors++; }).finally(() => { liveBusy = null; });
    return liveBusy;
  }
  async function convertLive(s) {
    const files = await s.liveFiles();
    if (!files || !files.size || !isCurrent(s)) return;
    const conv = await convertFiles(files, s.runId, { live: true });
    if (!isCurrent(s)) return;
    await lab.host.showLive(conv);
    liveConversions++;
    view.liveNote(`live trace updated ${new Date().toLocaleTimeString()}`);
    if (performance.now() - partialAt >= PARTIAL_MS && !partialP) {
      partialAt = performance.now();
      partialP = savePartial(s, files, conv).finally(() => { partialP = null; });
    }
  }

  // The running swarm's partial trace (its live files as a zip, flagged partial) in the library, so a tab crash or
  // reload keeps what the run produced so far. The final zip replaces it (same id) when the run ends.
  async function savePartial(s, files, conv) {
    try {
      const entries = [...files].map(([rel, data]) => ({ name: `${s.runId}/${rel}`, data }));
      const blob = makeZip(entries);
      if (!isCurrent(s)) return;
      await lab.library.put({ id: s.runId, name: `${s.runId}-partial.zip`, source: "run", partial: true, blob, card: conv.card,
        nFiles: files.size });
    } catch { /* best effort: quota, or the run ended meanwhile */ }
  }

  // createSwarm's hooks (§6) -> the live panel
  const hooks = {
    onStatus: (st) => view.status(st, stopping),
    onRecord: (i, rec) => view.record(i, rec),
    onCall: (i, info) => view.call(i, info),
    onIgnored: (i, seq) => view.ignored(i, seq),
  };

  // settings override (e2e): merged over the form's readSettings() result; the form must still be valid.
  async function start(override = null) {
    const base = readSettings();
    if (!isolated) return { error: "E_NOT_ISOLATED" };
    if (swarm) return { error: "E_RUNNING" };
    if (base.invalid) return { error: `E_SETTINGS_${String(base.invalid).toUpperCase()}` };
    const settings = swarmSettings(base, override);
    let mod;
    try { mod = await import("./swarm.js"); } catch { notice("The swarm runtime could not be loaded.", true); return { error: "E_RUNTIME" }; }
    let s;
    try { s = mod.createSwarm(settings, hooks); } catch (e) {
      notice(`The swarm could not be created (${codeOf(e)}).`, true);
      return { error: codeOf(e) };
    }
    begin(s, settings.nAgents);
    void settle(s);
    return { runId: s.runId };
  }

  // phase 1: the run is live
  function begin(s, nAgents) {
    onStarted();
    swarm = s; lastSwarm = s; stopping = false; liveOpen = true;
    outcome = { runId: s.runId, result: "running" };
    liveConversions = 0; liveErrors = 0; partialAt = performance.now();
    view.started(s.runId, nAgents);
    unsaved = null;
    $("saveRaw").hidden = true;
    notice("");
    onBusyChange();
    liveTimer = setInterval(() => { void refreshLive(); }, LIVE_MS);
  }

  // phase 2: run() settled (it never rejects, §6): close the live view, store the final trace, free the VMs
  async function settle(s) {
    const out = await s.run();
    await closeLive();
    outcome = { runId: s.runId, result: out?.result ?? "error", ...(out?.error ? { error: out.error } : {}) };
    view.ended(out);
    await storeFinal(s);
    // The run's VM databases (overlay, outbox, export) are no longer needed: everything was read into page memory
    // and the zip is stored (or offered as unsaved). Free them and the run's locks now, not at the next start.
    if (!retainVm && typeof s.dispose === "function") { try { await s.dispose(); } catch {} }
  }
  async function closeLive() {
    clearInterval(liveTimer);
    liveOpen = false;
    await liveBusy;  // bounded by LIVE_WAIT_MS
    await partialP;  // a partial save must not land after the final trace
  }
  async function storeFinal(s) {
    let z = null;
    try {
      z = await s.zip();
      const id = await ingest({ blob: z.blob, name: z.filename, source: "run" });
      if (id !== s.runId && lab.host?.ids().includes(s.runId)) await lab.host.removeTrace(s.runId);
      view.liveNote("trace saved in this browser");
    } catch (e) {
      view.error(`The trace could not be stored (${codeOf(e)}).`);
      if (z?.blob) { unsaved = z; $("saveRaw").hidden = false; }
    } finally {
      lab.host?.endLive(s.runId);
      swarm = null;
      onBusyChange();
    }
  }

  function stop() {
    if (!swarm) return;
    stopping = true;
    $("stop").disabled = true;
    view.phase("stopping");
    swarm.stop();
  }

  return {
    start, stop, refreshLive,
    running: () => swarm,
    lastSwarm: () => lastSwarm,
    outcome: () => outcome,
    unsaved: () => unsaved,
    saveUnsaved() { if (unsaved) view.save(unsaved.filename, unsaved.blob); },
    retainVmStorage(on) { retainVm = !!on; },
    stats: () => ({ liveConversions, liveErrors }),
  };
}
