// One run (one per page load): boots the VM, hands labctl its inputs, brokers model calls, follows the live
// export, and decides the outcome. Protocol details are in lab/browser/PROTOCOL.md.
import { BOOT_INPUTS, CX_VERSION, MANIFEST_PATH, RUN_DIR, RUN_FILES, STATUS_PATH, hmacKey, seal } from "./protocol.js";
import { startBroker } from "./broker.js";
import { bootVm, claimRunStorage, deleteStaleRunStorage, readFile } from "./vm.js";
import { makeZip } from "./zip.js";

const POLL_MS = 2000;
const dec = new TextDecoder();
const randHex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, "0")).join("");
const utcStamp = () => new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");

async function fetchBytes(path, init) {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`E_ASSET ${path} HTTP ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

const parseJson = (bytes) => { try { return bytes ? JSON.parse(dec.decode(bytes)) : null; } catch { return null; } };

export function createSession(settings, view) {
  const runId = `browser-${settings.iface}-solo-${utcStamp()}-${randHex(2)}`;  // PROTOCOL.md §2
  let vm = null, broker = null, outcome = null, stopping = false;
  let consumed = 0, steps = 0;  // steps.jsonl bytes consumed; step records seen

  const devices = () => ({ export: vm?.exportDev, outbox: vm?.outbox });

  const readJson = async (path) => parseJson(vm && await readFile(vm.exportDev, path));
  const readStatus = () => readJson(STATUS_PATH);  // display only, except after labctl exited (§4)

  // One poll of the export: status and new complete lines of steps.jsonl.
  async function refresh() {
    const st = await readStatus();
    if (st) view.status(st, stopping);
    // The file can vanish while labctl rebuilds the export (§5.3); only lines ending in "\n" are complete.
    const bytes = await readFile(vm.exportDev, `${RUN_DIR}/a0/steps.jsonl`);
    if (!bytes || bytes.length <= consumed) return;
    const end = bytes.lastIndexOf(10) + 1;
    if (end <= consumed) return;
    const lines = dec.decode(bytes.subarray(consumed, end)).split("\n").filter(Boolean);
    consumed = end;
    for (const line of lines) {
      let rec;
      try { rec = JSON.parse(line); } catch { continue; }
      if (rec?.kind === "step") view.steps(++steps);
      view.record(rec);
    }
  }

  // Polls never overlap; errors only affect the display.
  let polling = Promise.resolve();
  const poll = () => (polling = polling.then(refresh).catch(() => {}));

  async function run() {
    outcome = { runId, result: "running" };
    view.started(runId);
    try {
      view.phase("loading VM");
      await claimRunStorage(runId);
      await deleteStaleRunStorage();
      const manifest = JSON.parse(dec.decode(await fetchBytes("/vm-manifest.json", { cache: "no-store" })));
      const seed = await fetchBytes("/data/seed.json");
      vm = await bootVm({ runId, image: manifest.filename });
      const { preset, iface, harness, wire, provider, caps } = settings;
      const spec = JSON.stringify({ run_id: runId, iface, rep: 0, harness, wire,
        provider_label: `${preset}:${provider.model}`, cx_version: CX_VERSION, image: manifest.filename });
      // The agent can read the inbox, so the boot inputs are sealed under the mailbox key (§2).
      const keyHex = randHex(32);
      for (const [input, data] of [[BOOT_INPUTS.spec, new TextEncoder().encode(spec)], [BOOT_INPUTS.seed, seed]]) {
        await vm.inbox.writeFile(input.path, await seal(keyHex, input.label, data));
      }

      broker = startBroker({ outbox: vm.outbox, inbox: vm.inbox, key: await hmacKey(keyHex), agent: 0,
        provider, harness, caps, onCall: view.calls, onIgnored: view.ignored });
      if (stopping) broker.stop();
      view.phase(stopping ? "stopping" : "starting");
      const timer = setInterval(poll, POLL_MS);
      let exit, st;
      try {
        exit = await vm.runLabctl(keyHex);
        await poll();
        st = await readStatus();  // the only status that decides: after cx.run no agent process is alive (§4)
      } finally {
        clearInterval(timer);
        broker.close();
      }
      // labctl exits 0 exactly when it ends in phase done (§4); anything else is an error.
      if (!st || exit !== 0 || st.phase !== "done") {
        const error = !st ? "E_NO_STATUS" : st.phase === "error" ? st.error : exit !== 0 ? `E_EXIT_${exit}` : "E_STATUS";
        outcome = { runId, result: "error", error };
      } else if (stopping && st.ended === "fatal") {
        outcome = { runId, result: "stopped" };  // Stop ends the harness through FATAL (E_ABORT, §3)
      } else {
        outcome = { runId, result: "done" };
      }
    } catch (e) {
      outcome = { runId, result: "error", error: String(e?.message || e).slice(0, 200) };
    }
    view.ended(outcome, !!vm);
    return outcome;
  }

  return {
    runId,
    run,
    outcome: () => outcome,
    stop() {
      if (stopping || (outcome && outcome.result !== "running")) return;
      stopping = true;
      broker?.stop();
      view.phase("stopping");
    },
    // A file's text on the "export" or "outbox" device (paths as in PROTOCOL.md §5.1), or null.
    async readFile(mount, path) {
      const dev = devices()[mount];
      const bytes = dev && await readFile(dev, path);
      return bytes ? dec.decode(bytes) : null;
    },
    // The download (§5.1): {filename, blob}. Complete only when labctl committed the export with every agent
    // process stopped and the run ended done or stopped; a run with a FATAL marker is named aborted.
    async zip() {
      const files = [];
      for (const name of RUN_FILES) {
        const data = await readFile(vm.exportDev, `${RUN_DIR}/${name}`);
        if (data) files.push({ name: `${runId}/${name}`, data });
      }
      const manifest = await readJson(MANIFEST_PATH);
      const fatal = (await readJson(`${RUN_DIR}/run.json`))?.fatal;
      const complete = manifest?.agent_stopped === true && ["done", "stopped"].includes(outcome?.result);
      const suffix = !complete ? "-incomplete" : fatal ? `-aborted-${String(fatal.status).replace(/[^\w]/g, "")}` : "";
      return { filename: `${runId}${suffix}.zip`, blob: makeZip(files) };
    },
    consoleTail: () => vm?.consoleTail() ?? "",
  };
}
