// CheerpX VM for one run: devices and mounts (PROTOCOL.md §1), the labctl command (§2), and the run's
// IndexedDB storage, owned through a Web Lock so a second tab never deletes a live run.
import { CX_VERSION, DB_KINDS, LEGACY_DB_PREFIX, cxMounts, dbName, labctlCommand, runLock } from "./protocol.js";
import { createLoopNet } from "./loopnet.js";

const VM_BASE = location.hostname === "lab.swarmpit.dev" ? "https://vm.swarmpit.dev/vm/" : "/vm/";
const CONSOLE_TAIL = 4096;  // bytes of guest console kept for the test hook (PROTOCOL.md §8); never rendered
const LEGACY_MAX_AGE_MS = 24 * 3600 * 1000;

// Boots the image with the run's mounts. runLabctl(keyHex) resolves to labctl's exit status once it ends;
// consoleTail() is the end of labctl's own stdout/stderr (its children log to files).
export async function bootVm({ runId, image }) {
  const CheerpX = await import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);
  const disk = await CheerpX.HttpBytesDevice.create(VM_BASE + image);
  const overlay = await CheerpX.OverlayDevice.create(disk, await CheerpX.IDBDevice.create(dbName("overlay", runId)));
  const outbox = await CheerpX.IDBDevice.create(dbName("out", runId));
  const exportDev = await CheerpX.IDBDevice.create(dbName("export", runId));
  const inbox = await CheerpX.DataDevice.create();
  const cx = await CheerpX.Linux.create({
    networkInterface: createLoopNet(),
    mounts: cxMounts({ overlay, outbox, inbox, exportDev }),
  });
  let tail = "";
  const dec = new TextDecoder();
  cx.setCustomConsole((buf) => { tail = (tail + dec.decode(new Uint8Array(buf))).slice(-CONSOLE_TAIL); }, 80, 24);
  return {
    inbox, outbox, exportDev,
    consoleTail: () => tail,
    async runLabctl(keyHex) {
      const { argv: [path, ...args], opts } = labctlCommand(keyHex);
      return (await cx.run(path, args, opts)).status;
    },
  };
}

// A file's bytes on a CheerpX device, or null if it does not exist (or vanished while being read).
export async function readFile(dev, path) {
  try {
    const blob = await dev.readFileAsBlob(path);
    return blob ? new Uint8Array(await blob.arrayBuffer()) : null;
  } catch { return null; }
}

// Holds the run's Web Lock for the rest of this tab's life (§1). Resolves once the lock is held, or at once
// when the browser has no Web Locks (then nothing is ever deleted either; see below).
export function claimRunStorage(runId) {
  if (!navigator.locks) return Promise.resolve(false);
  return new Promise((held) => {
    navigator.locks.request(runLock(runId), () => { held(true); return new Promise(() => {}); })
      .catch(() => held(false));
  });
}

// Deletes the databases of runs no tab owns: a run's databases go only if its lock is free right now.
// Does nothing without navigator.locks or indexedDB.databases(), so it can never delete a live run.
// Databases of the first deployed version (LEGACY_DB_PREFIX) have no lock; they go once their run is a day old.
export async function deleteStaleRunStorage() {
  if (!navigator.locks || !indexedDB.databases) return;
  const pattern = new RegExp(`^${dbName("(" + DB_KINDS.join("|") + ")", "(.+)")}$`);
  const legacy = new RegExp(`^${LEGACY_DB_PREFIX}(${DB_KINDS.join("|")})-(.+)$`);
  const runs = new Map(), old = [];  // runId -> [db names]; legacy db names
  for (const { name } of await indexedDB.databases()) {
    const m = pattern.exec(name || ""), l = legacy.exec(name || "");
    if (m) runs.set(m[2], [...(runs.get(m[2]) || []), name]);
    else if (l && Date.now() - runStartMs(l[2]) > LEGACY_MAX_AGE_MS) old.push(name);
  }
  await deleteDatabases(old);
  for (const [runId, names] of runs) {
    await navigator.locks.request(runLock(runId), { ifAvailable: true }, async (lock) => {
      if (lock) await deleteDatabases(names);
    });
  }
}

// The start time in a run id (PROTOCOL.md §2: ...-<UTC YYYYMMDDHHMMSS>-<4 hex>), or Infinity if absent.
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
