// T1/T5 probe (docs/browser-two-vm.md): how can a page host two CheerpX VMs? Modes (?mode=):
//   same     second Linux.create in the same realm, same image URL
//   diffurl  second Linux.create in the same realm, image URL with a different query string
//   iframe   second VM in a same-origin iframe (its own JS realm, so its own CheerpX module instance)
//   worker   second VM in a module Web Worker
// Each VM runs `echo` and a check of an executable after both VMs exist. Results -> window.__probe.
import { createFabric } from "./fabric.js";

const params = new URLSearchParams(location.search);
const ROLE = params.get("role") || "top";
const MODE = params.get("mode") || "same";
const DATA = params.get("data") === "1";  // write the spike scripts into each VM's DataDevice, as twovm.js does
const MEM = params.get("mem") === "1";
const LAZY = params.get("lazy") === "1";  // A runs only a bash builtin before B boots (no binary read from disk yet)    // call performance.measureUserAgentSpecificMemory between boots
const CX_VERSION = params.get("cx") || "1.3.9";
const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin"], cwd: "/root", uid: 0, gid: 0 };
const P = self.__probe = { done: false, steps: [], error: null };
const step = (id, v) => { P.steps.push({ id, ...v }); };
const ms = (t) => Math.round(performance.now() - t);

export async function bootVm(CheerpX, name, net, url) {
  const t = performance.now();
  const disk = await CheerpX.HttpBytesDevice.create(url);
  const overlay = await CheerpX.OverlayDevice.create(disk, await CheerpX.IDBDevice.create(`twovm-probe-${name}-${Date.now()}`));
  const data = await CheerpX.DataDevice.create();
  const cx = await CheerpX.Linux.create({ ...(net ? { networkInterface: net } : {}), mounts: [
    { type: "ext2", dev: overlay, path: "/" }, { type: "dir", dev: data, path: "/spike" },
    { type: "devs", path: "/dev" }, { type: "devpts", path: "/dev/pts" }, { type: "proc", path: "/proc" }, { type: "sys", path: "/sys" }] });
  const bootMs = ms(t);
  if (DATA) for (const f of ["execd.py", "execc.py", "lined.py", "linec.py", "rtt.py", "ticker.py"]) await data.writeFile(`/${f}`, await (await fetch(f)).text());
  let chunks = [];
  const dec = new TextDecoder();
  cx.setCustomConsole((buf) => chunks.push(dec.decode(new Uint8Array(buf))), 200, 50);
  let q = Promise.resolve();
  const sh = (cmd, opts = ROOT_ENV, limit = 120000) => {
    const run = async () => {
      chunks = [];
      const s = performance.now();
      let timer;
      const r = await Promise.race([cx.run("/bin/bash", ["-c", cmd], opts),
        new Promise((_, rej) => { timer = setTimeout(() => rej(new Error("hang")), limit); })]).finally(() => clearTimeout(timer));
      return { status: r.status, out: chunks.join("").replace(/\r/g, ""), ms: ms(s) };
    };
    const p = q.then(run, run); q = p.catch(() => {}); return p;
  };
  return { cx, data, sh, bootMs };
}

// a VM is healthy if bash runs a builtin, an ELF binary and python
const HEALTH = "echo hi-$0; /usr/bin/curl --version >/dev/null 2>&1 && echo curl-ok; python3 -c 'print(\"py-ok\")'";
const healthy = (r, tag) => r.out.includes("curl-ok") && r.out.includes("py-ok") && r.status === 0;

async function imageUrl() {
  const m = await (await fetch("/vm/manifest.json")).json();
  return new URL(`/vm/${m.filename}`, location.href).href;
}
const loadCx = () => import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);

async function top() {
  const url = await imageUrl();
  const CheerpX = await loadCx();
  const fabric = createFabric();
  self.__fabric = fabric;
  fabric.setMode("shared");
  const A = await bootVm(CheerpX, "A", fabric.iface("A"), url);
  step("A boot", { ms: A.bootMs, ...(LAZY ? { echo: (await A.sh("echo x")).out.includes("x") } : { healthy: healthy(await A.sh(HEALTH)) }) });
  if (MEM && performance.measureUserAgentSpecificMemory) {
    const t = performance.now();
    try { await performance.measureUserAgentSpecificMemory(); step("uaMemory", { ms: ms(t) }); } catch (e) { step("uaMemory", { error: e.name }); }
  }
  let B;  // {sh} in the top realm, or a remote handle
  if (MODE === "same" || MODE === "diffurl") {
    const b = await bootVm(CheerpX, "B", fabric.iface("B"), MODE === "same" ? url : url + "?vm=B");
    step("B boot", { ms: b.bootMs, sameObject: b.cx === A.cx });
    B = { sh: (c) => b.sh(c) };
  } else if (MODE === "iframe") {
    const f = document.createElement("iframe");
    const t = performance.now();
    f.src = `probe.html?role=frame&cx=${CX_VERSION}`;
    document.body.append(f);
    await new Promise((r) => f.addEventListener("load", r, { once: true }));
    while (!f.contentWindow.__probeVm) {
      if (f.contentWindow.__probe?.error) throw new Error("frame: " + f.contentWindow.__probe.error);
      await new Promise((r) => setTimeout(r, 200));
    }
    step("B boot (iframe)", { ms: ms(t), vmBootMs: f.contentWindow.__probeVm.bootMs, frameIsolated: f.contentWindow.crossOriginIsolated });
    B = { sh: (c) => f.contentWindow.__probeVm.sh(c) };
  } else if (MODE === "worker") {
    const w = new Worker(`probe-worker.js?cx=${CX_VERSION}`, { type: "module" });
    const t = performance.now();
    let id = 0;
    const pending = new Map();
    let ready, failed;
    const readyP = new Promise((r, f) => { ready = r; failed = f; });
    w.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ready") ready(m);
      else if (m.type === "error") failed(new Error("worker: " + m.error));
      else if (m.type === "result") pending.get(m.id)(m.r);
    };
    w.onerror = (e) => failed(new Error("worker onerror: " + (e.message || "").slice(0, 120)));
    const info = await Promise.race([readyP, new Promise((_, r) => setTimeout(() => r(new Error("worker boot timeout")), 120000))]);
    step("B boot (worker)", { ms: ms(t), vmBootMs: info.bootMs });
    B = { sh: (c) => new Promise((r) => { pending.set(++id, r); w.postMessage({ type: "sh", id, cmd: c }); }) };
  }
  const [ra, rb] = [await A.sh(HEALTH), await B.sh(HEALTH)];
  step("after both: A healthy", { healthy: healthy(ra), status: ra.status, ms: ra.ms });
  step("after both: B healthy", { healthy: healthy(rb), status: rb.status, ms: rb.ms });
  const [ca, cb] = await Promise.all([A.sh(HEALTH), B.sh(HEALTH)]);
  step("concurrent: both healthy", { healthy: healthy(ca) && healthy(cb) });
  await A.sh("echo a > /tmp/a-only");
  const sep = await B.sh("test -e /tmp/a-only && echo SHARED || echo SEPARATE");
  step("overlays separate", { separate: sep.out.includes("SEPARATE") });
}

async function frame() {
  // same-origin iframe: borrow the parent's fabric so both VMs share one network object graph
  const url = await imageUrl();
  const CheerpX = await loadCx();
  const net = parent.__fabric ? parent.__fabric.iface("B") : undefined;
  self.__probeVm = await bootVm(CheerpX, "B", net, url);
}

(ROLE === "frame" ? frame() : top())
  .catch((e) => { P.error = String(e && e.message || e).slice(0, 300); })
  .finally(() => { P.done = true; });
