// T5 probe: a CheerpX VM inside a module Web Worker (no network interface; booting and running is the question).
const CX_VERSION = new URL(location.href).searchParams.get("cx") || "1.3.9";
const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin"], cwd: "/root", uid: 0, gid: 0 };
let vm = null;
(async () => {
  try {
    const m = await (await fetch("/vm/manifest.json")).json();
    const CheerpX = await import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);
    const t = performance.now();
    const disk = await CheerpX.HttpBytesDevice.create(new URL(`/vm/${m.filename}`, location.href).href);
    const overlay = await CheerpX.OverlayDevice.create(disk, await CheerpX.IDBDevice.create(`twovm-probe-W-${Date.now()}`));
    const cx = await CheerpX.Linux.create({ mounts: [{ type: "ext2", dev: overlay, path: "/" },
      { type: "devs", path: "/dev" }, { type: "proc", path: "/proc" }] });
    let chunks = [];
    const dec = new TextDecoder();
    cx.setCustomConsole((buf) => chunks.push(dec.decode(new Uint8Array(buf))), 200, 50);
    vm = { cx, get: () => chunks, reset: () => { chunks = []; } };
    postMessage({ type: "ready", bootMs: Math.round(performance.now() - t) });
  } catch (e) {
    postMessage({ type: "error", error: String(e && e.message || e).slice(0, 200) });
  }
})();
let q = Promise.resolve();
onmessage = (e) => {
  const { id, cmd } = e.data;
  q = q.then(async () => {
    vm.reset();
    const t = performance.now();
    const r = await vm.cx.run("/bin/bash", ["-c", cmd], ROOT_ENV);
    postMessage({ type: "result", id, r: { status: r.status, out: vm.get().join("").replace(/\r/g, ""), ms: Math.round(performance.now() - t) } });
  });
};
