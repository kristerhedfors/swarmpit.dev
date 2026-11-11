// One CheerpX VM per same-origin iframe (vmframe.html, docs/browser-swarm.md §1). Two Linux.create calls in one
// JS realm corrupt each other in CheerpX 1.3.9 (docs/browser-two-vm.md T1/T5), so the swarm page boots every VM
// (board, a0..a{N-1}) in a frame of its own, each with its own instance of the CheerpX module. The frame does
// nothing until the parent calls window.__vmframe.boot(); the network interface comes from the parent's fabric
// (js/fabric.js), so all VMs share one page-owned network with explicit routes.
//
// boot() is vm.js bootVm generalised to one VM of a run: the devices and mounts of PROTOCOL.md §1 with the
// IndexedDB names of this VM's storage id (dbName(kind, <run_id>-<vm>), protocol.js vmStorageId), and the
// labctl command of §2. The Web Lock and the stale-storage cleanup stay in the parent (swarm.js), which outlives
// the frames. Removing the iframe is how the page ends a VM (no SIGKILL inside CheerpX, two-vm T7).
import { CX_VERSION, cxMounts, dbName, labctlCommand, vmStorageId } from "./protocol.js";

const VM_BASE = location.hostname === "lab.swarmpit.dev" ? "https://vm.swarmpit.dev/vm/" : "/vm/";
const CONSOLE_TAIL = 4096;  // bytes of guest console kept for the test hook (PROTOCOL.md §8); never rendered

let booted = false;

// opts {runId, vm: "board" | "a<i>", image, networkInterface}. Resolves to the VM handle:
// {inbox, outbox, exportDev, consoleTail(), runLabctl(keyHex) -> exit status, run(path, args, opts) -> status}.
// One boot per frame: a second call rejects (E_FRAME_BOOTED).
async function boot({ runId, vm, image, networkInterface }) {
  if (booted) throw new Error("E_FRAME_BOOTED");
  booted = true;
  if (!/^(board|a\d)$/.test(vm) || typeof image !== "string" || !/^[\w.-]+$/.test(image)) throw new Error("E_FRAME_ARGS");
  const id = vmStorageId(runId, vm);
  const CheerpX = await import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);
  const disk = await CheerpX.HttpBytesDevice.create(new URL(VM_BASE + image, location.href).href);
  const overlay = await CheerpX.OverlayDevice.create(disk, await CheerpX.IDBDevice.create(dbName("overlay", id)));
  const outbox = await CheerpX.IDBDevice.create(dbName("out", id));
  const exportDev = await CheerpX.IDBDevice.create(dbName("export", id));
  const inbox = await CheerpX.DataDevice.create();
  const cx = await CheerpX.Linux.create({
    networkInterface,
    mounts: cxMounts({ overlay, outbox, inbox, exportDev }),
  });
  let tail = "";
  const dec = new TextDecoder();
  cx.setCustomConsole((buf) => { tail = (tail + dec.decode(new Uint8Array(buf))).slice(-CONSOLE_TAIL); }, 80, 24);
  const run = async (path, args, opts) => (await cx.run(path, args, opts)).status;
  return {
    inbox, outbox, exportDev,
    consoleTail: () => tail,
    async runLabctl(keyHex) {
      const { argv: [path, ...args], opts } = labctlCommand(keyHex);
      return run(path, args, opts);
    },
    // Any guest command (dev harness and spikes only; the page runs nothing but labctl).
    run,
  };
}

window.__vmframe = { boot };
