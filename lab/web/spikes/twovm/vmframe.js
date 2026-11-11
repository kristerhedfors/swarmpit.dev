// One CheerpX VM per same-origin iframe (vmframe.html?name=A|B): each iframe is its own JS realm, so it gets its
// own instance of the CheerpX module. Two Linux.create calls in ONE realm interfere in CheerpX 1.3.9 (the first
// VM's later disk reads hang or return wrong bytes; probe.js lazy=1), so the two-VM design needs one realm per
// VM. The network interface comes from the parent (parent.__fabric), so both VMs share one in-page network.
// Exposes window.__vm = {name, cx, data, sh(cmd, opts, limitMs), bootMs} to the parent.
const params = new URLSearchParams(location.search);
const NAME = params.get("name");
const CX_VERSION = params.get("cx") || "1.3.9";
const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8",
  "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"], cwd: "/root", uid: 0, gid: 0 };
const ms = (t) => Math.round(performance.now() - t);
window.__vmError = null;

(async () => {
  const m = await (await fetch("/vm/manifest.json")).json();
  const CheerpX = await import(`https://cxrtnc.leaningtech.com/${CX_VERSION}/cx.esm.js`);
  const t = performance.now();
  const disk = await CheerpX.HttpBytesDevice.create(new URL(`/vm/${m.filename}`, location.href).href);
  const overlay = await CheerpX.OverlayDevice.create(disk, await CheerpX.IDBDevice.create(`twovm-${NAME}-${Date.now()}`));
  const data = await CheerpX.DataDevice.create();
  const cx = await CheerpX.Linux.create({ networkInterface: parent.__fabric.iface(NAME), mounts: [
    { type: "ext2", dev: overlay, path: "/" },
    { type: "dir", dev: data, path: "/spike" },
    { type: "devs", path: "/dev" }, { type: "devpts", path: "/dev/pts" },
    { type: "proc", path: "/proc" }, { type: "sys", path: "/sys" },
  ] });
  const bootMs = ms(t);
  let chunks = [];
  const dec = new TextDecoder();
  cx.setCustomConsole((buf) => chunks.push(dec.decode(new Uint8Array(buf))), 200, 50);
  // One command at a time (one console per VM); a command that has not returned after limitMs rejects.
  let queue = Promise.resolve();
  function sh(cmd, opts = ROOT_ENV, limitMs = 180000) {
    const run = async () => {
      chunks = [];
      const s = performance.now();
      let timer;
      const r = await Promise.race([cx.run("/bin/bash", ["-c", cmd], opts), new Promise((_, rej) => {
        timer = setTimeout(() => rej(new Error(`${NAME}: no result after ${limitMs / 1000} s`)), limitMs);
      })]).finally(() => clearTimeout(timer));
      return { status: r.status, out: chunks.join("").replace(/\r/g, ""), ms: ms(s) };
    };
    const p = queue.then(run, run);
    queue = p.catch(() => {});
    return p;
  }
  window.__vm = { name: NAME, cx, data, sh, bootMs };
})().catch((e) => { window.__vmError = String(e && e.message || e).slice(0, 300); });
