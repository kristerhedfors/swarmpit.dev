// Two-VM spike (docs/browser-two-vm.md): two CheerpX Linux instances in one page, A = agent VM, B = control
// VM, each in its own same-origin iframe (vmframe.js), over the same image URL with separate IndexedDB
// overlays, wired by one fabric.js object owned by this page. Results go to
// window.__twovm.results as {id, pass, metric}; metrics are numbers and booleans from our own neutral test
// commands (no run content exists in this spike). Query: ?cx=1.3.9&only=<regex>&wedge=1&n=30
import { createFabric } from "./fabric.js";

const params = new URLSearchParams(location.search);
const CX_VERSION = params.get("cx") || "1.3.9";
const ONLY = params.get("only") ? new RegExp(params.get("only")) : null;
const WEDGE = params.get("wedge") === "1";
const N_EXEC = Number(params.get("n") || 30);
const SINGLE = params.get("single") === "1";
const IDLE = Number(params.get("idle") || 0);  // single mode: idle python servers started before the CPU timing  // baseline: boot only A, warm it, measure, stop
const LIMIT_MS = Number(params.get("limit") || 180000);
const DEBUG = params.get("debug") === "1";  // log the tail of our own T4 command output (spike scripts only)
const R = window.__twovm = { done: false, results: [], error: null, phase: "init", pageErrors: 0, mem: [], info: {} };
const logEl = document.getElementById("log");
const log = (s) => { logEl.textContent += s + "\n"; };
addEventListener("error", () => { R.pageErrors++; });
addEventListener("unhandledrejection", () => { R.pageErrors++; });
const want = (id) => !ONLY || ONLY.test(id);
function result(id, pass, metric) {
  R.results.push({ id, pass: !!pass, metric: typeof metric === "string" ? metric : JSON.stringify(metric) });
  log(`${pass ? "PASS" : "FAIL"} ${id} ${typeof metric === "string" ? metric : JSON.stringify(metric)}`);
}
const ms = (t) => Math.round(performance.now() - t);
const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor((s.length - 1) / 2)]; };
const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8",
  "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"], cwd: "/root", uid: 0, gid: 0 };
const hex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, "0")).join("");

async function memory(phase) {
  const m = { phase, t: Math.round(performance.now()) };
  if (performance.memory) m.jsHeapMB = Math.round(performance.memory.usedJSHeapSize / 1048576);
  if (performance.measureUserAgentSpecificMemory && self.crossOriginIsolated) {
    try {
      const r = await Promise.race([performance.measureUserAgentSpecificMemory(),
        new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 30000))]);
      m.uaMemMB = Math.round(r.bytes / 1048576);
    } catch (e) { m.uaMem = String(e.message || e).slice(0, 40); }
  }
  R.mem.push(m);
  R.phase = phase;
  log(`mem ${JSON.stringify(m)}`);
  await new Promise((r) => setTimeout(r, 3000));  // let the runner's 1 s RSS sampler see this phase
}

// Boots a VM in its own same-origin iframe (vmframe.js: one realm per VM) and copies the spike scripts into its
// /spike DataDevice. Resolves to the frame's {name, cx, data, sh, bootMs} plus the page-side boot time.
async function boot(name) {
  const t = performance.now();
  const f = document.createElement("iframe");
  f.src = `vmframe.html?name=${name}&cx=${CX_VERSION}`;
  document.body.append(f);
  await new Promise((r) => f.addEventListener("load", r, { once: true }));
  while (!f.contentWindow.__vm) {
    if (f.contentWindow.__vmError) throw new Error(`${name}: ${f.contentWindow.__vmError}`);
    if (performance.now() - t > 120000) throw new Error(`${name}: boot timeout`);
    await new Promise((r) => setTimeout(r, 100));
  }
  const vm = f.contentWindow.__vm;
  for (const file of ["execd.py", "execc.py", "lined.py", "linec.py", "rtt.py", "ticker.py"]) {
    await vm.data.writeFile(`/${file}`, await (await fetch(file)).text());
  }
  return { ...vm, sh: (c, o, l) => vm.sh(c, o, l), frameBootMs: ms(t), frame: f };
}

// waits (up to ~30 s) in `vm` until url answers 200; resolves to the last HTTP code
const httpWait = (url) => `for i in $(seq 60); do c=$(curl -s -o /dev/null -w '%{http_code}' ${url}); [ "$c" = 200 ] && break; sleep 0.5; done; echo; echo $c`;
const httpOnce = (url) => `curl -s -m 10 -o /dev/null -w '%{http_code}' ${url}; echo`;
const lastLine = (r) => r.out.trim().split("\n").pop().trim();
const resultLine = (r) => { const m = /RESULT (\{.*\})/.exec(r.out); return m ? JSON.parse(m[1]) : null; };
const waitListen = (file) => `for i in $(seq 100); do grep -q listening ${file} 2>/dev/null && break; sleep 0.2; done; grep -c listening ${file}`;

async function main() {
  await memory("before-boot");
  const manifest = await (await fetch("/vm/manifest.json")).json();
  R.info.image = manifest.sha12;
  R.info.crossOriginIsolated = self.crossOriginIsolated;
  log(`image ${manifest.sha12}, CheerpX ${CX_VERSION}, crossOriginIsolated=${self.crossOriginIsolated}`);
  const fabric = createFabric({ onEvent: () => {} });
  window.__fabric = fabric;  // the VM frames take their network interface from here

  // ---- T1: two instances, separate overlays and consoles, concurrent cx.run ----
  const A = await boot("A");
  const a1 = await A.sh("echo hello-A; nproc; awk '/MemTotal/{print $2}' /proc/meminfo");
  await memory("after-A");
  if (SINGLE) {
    const w = await A.sh("python3 /spike/rtt.py cpu 3000000; curl --version >/dev/null && echo warm");
    await memory("single-A-warm");
    const cpuRuns = [];
    for (let i = 0; i < 2; i++) cpuRuns.push(Number((/cpu ms=(\d+)/.exec((await A.sh("python3 /spike/rtt.py cpu 3000000")).out) || [])[1]));
    for (let i = 0; i < IDLE; i++) await A.sh(`nohup python3 -m http.server --bind 127.0.0.1 ${9500 + i} >/dev/null 2>&1 & sleep 2`);
    const idleRuns = [];
    for (let i = 0; i < (IDLE ? 3 : 0); i++) idleRuns.push(Number((/cpu ms=(\d+)/.exec((await A.sh("python3 /spike/rtt.py cpu 3000000")).out) || [])[1]));
    result("T3s single-VM baseline", w.out.includes("warm"), { bootA_ms: A.bootMs, frameBootA_ms: A.frameBootMs, cpu_ms: cpuRuns, idleServers: IDLE, cpuWithIdle_ms: idleRuns });
    return;
  }
  let B = null, t1err = null;
  try {
    B = await boot("B");
  } catch (e) { t1err = String(e).slice(0, 200); }
  R.info.cxMethods = Object.getOwnPropertyNames(Object.getPrototypeOf(A.cx)).filter((k) => k !== "constructor").sort().join(",");
  if (!B) {
    result("T1 second Linux.create in the same page", false, { error: t1err });
    return;
  }
  const b1 = await B.sh("echo hello-B; nproc; awk '/MemTotal/{print $2}' /proc/meminfo");
  await memory("after-B");
  await A.sh("echo only-in-A > /tmp/a-only");
  const sep = await B.sh("test -e /tmp/a-only && echo SHARED || echo SEPARATE");
  const dbs = indexedDB.databases ? (await indexedDB.databases()).map((d) => d.name).filter((n) => n?.startsWith("twovm-")) : [];
  const consolesOk = a1.out.includes("hello-A") && !a1.out.includes("hello-B") && b1.out.includes("hello-B") && !b1.out.includes("hello-A");
  const cpu = "python3 /spike/rtt.py cpu 3000000";
  const cpuMs = (r) => Number((/cpu ms=(\d+)/.exec(r.out) || [])[1]);
  const aloneA = await A.sh(cpu), aloneB = await B.sh(cpu);
  let t = performance.now();
  const [ca, cb] = await Promise.all([A.sh(cpu), B.sh(cpu)]);
  const bothWall = ms(t);
  const memA = Number(a1.out.trim().split("\n").pop()), memB = Number(b1.out.trim().split("\n").pop());
  result("T1 two Linux.create in one page (one iframe per VM), both boot and run", consolesOk && sep.out.includes("SEPARATE") && a1.status === 0 && b1.status === 0,
    { bootA_ms: A.bootMs, bootB_ms: B.bootMs, frameBootA_ms: A.frameBootMs, frameBootB_ms: B.frameBootMs, separateOverlay: sep.out.includes("SEPARATE"), consolesSeparate: consolesOk,
      idbDbs: dbs.length, guestMemTotalKB: [memA, memB] });
  result("T1b concurrent cx.run in both VMs", ca.status === 0 && cb.status === 0,
    { aloneA_ms: aloneA.ms, aloneB_ms: aloneB.ms, bothWall_ms: bothWall, guestCpuA_ms: [cpuMs(aloneA), cpuMs(ca)],
      guestCpuB_ms: [cpuMs(aloneB), cpuMs(cb)], overlapRatio: +(bothWall / (aloneA.ms + aloneB.ms)).toFixed(2) });

  // ---- T2: ONE shared loopnet for both VMs ----
  if (want("T2")) {
    fabric.setMode("shared");
    await A.sh("echo '127.0.0.3\tcontrol.internal' >> /etc/hosts");
    await B.sh("echo '127.0.0.4\tagentvm.internal' >> /etc/hosts");
    const bsrv = await B.sh(`nohup python3 -m http.server --bind 127.0.0.1 9200 >/tmp/hs.out 2>&1 & ${httpWait("http://127.0.0.1:9200/")}`);
    const a2b = await A.sh(`${httpOnce("http://127.0.0.1:9200/")}; ${httpOnce("http://control.internal:9200/")}`);
    result("T2a shared: A -> B http (127.0.0.1, hosts name)", lastLine(bsrv) === "200" && /^200\n200$/m.test(a2b.out.trim()),
      { codes: a2b.out.trim().split("\n").join(","), ms: a2b.ms });
    await B.sh("nohup python3 /spike/lined.py 31337 B >/tmp/lined.out 2>&1 & sleep 1");
    const ll = await A.sh("python3 /spike/linec.py 127.0.0.1 31337; python3 /spike/linec.py control.internal 31337");
    result("T2b shared: A -> B line protocol on 31337", (ll.out.match(/lined ok \d+ banner=B/g) || []).length === 2,
      { lines: (ll.out.match(/lined ok/g) || []).length, ms: (ll.out.match(/lined ok (\d+)/g) || []).map((s) => +s.split(" ")[2]) });
    const asrv = await A.sh(`nohup python3 -m http.server --bind 127.0.0.1 9201 >/tmp/hs.out 2>&1 & ${httpWait("http://127.0.0.1:9201/")}`);
    const b2a = await B.sh(`${httpOnce("http://127.0.0.1:9201/")}; ${httpOnce("http://agentvm.internal:9201/")}`);
    result("T2c shared: B -> A http (127.0.0.1, hosts name)", lastLine(asrv) === "200" && /^200\n200$/m.test(b2a.out.trim()),
      { codes: b2a.out.trim().split("\n").join(",") });
    const ref = await A.sh("python3 /spike/linec.py 127.0.0.1 9299; python3 /spike/linec.py 10.0.0.1 80");
    result("T2d shared: unknown port and non-loopback refused", (ref.out.match(/lined fail ConnectionRefusedError/g) || []).length === 2,
      { refused: (ref.out.match(/ConnectionRefusedError/g) || []).length, ms: (ref.out.match(/ConnectionRefusedError (\d+)/g) || []).map((s) => +s.split(" ")[1]) });
  }

  // ---- T2e-T2h: namespaced fabric (private port space per VM + explicit routes) ----
  const ROUTES = { routes: { A: { 31338: "B", 8080: "B" }, B: { 7070: "A", 7071: "A" } }, exportOnly: { A: [7070] } };
  fabric.setMode("fabric", ROUTES);
  if (want("T2")) {
    await B.sh("nohup python3 /spike/lined.py 31338 B >/tmp/lined2.out 2>&1 & sleep 1");
    const r1 = await A.sh("python3 /spike/linec.py 127.0.0.1 31338; python3 /spike/linec.py control.internal 31338");
    result("T2e fabric: routed port A -> B", (r1.out.match(/lined ok \d+ banner=B/g) || []).length === 2, { ok: (r1.out.match(/lined ok/g) || []).length });
    await B.sh(`nohup python3 -m http.server --bind 127.0.0.1 9202 >/tmp/hs2.out 2>&1 & ${httpWait("http://127.0.0.1:9202/")}`);
    await A.sh(`nohup python3 -m http.server --bind 127.0.0.1 9203 >/tmp/hs3.out 2>&1 & ${httpWait("http://127.0.0.1:9203/")}`);
    const pa = await A.sh(`${httpOnce("http://127.0.0.1:9202/")}; ${httpOnce("http://127.0.0.1:9203/")}`);
    const pb = await B.sh(`${httpOnce("http://127.0.0.1:9203/")}; ${httpOnce("http://127.0.0.1:9202/")}`);
    const [aToB, aToA] = pa.out.trim().split("\n"), [bToA, bToB] = pb.out.trim().split("\n");
    result("T2f fabric: unrouted ports private to their VM, own loopback works", aToB !== "200" && aToA === "200" && bToA !== "200" && bToB === "200",
      { aToBprivate: aToB, aToAown: aToA, bToAprivate: bToA, bToBown: bToB });
    // a root agent listening on a routed port in its own VM does not capture the route
    await A.sh("nohup python3 /spike/lined.py 31338 A >/tmp/lined3.out 2>&1 & sleep 1");
    const sh3 = await A.sh("python3 /spike/linec.py 127.0.0.1 31338");
    result("T2g fabric: routed port cannot be shadowed from A", /banner=B/.test(sh3.out), { banner: (/banner=(\w+)/.exec(sh3.out) || [])[1] || "none" });
    const ref = await A.sh("python3 /spike/linec.py 127.0.0.1 9299");
    result("T2h fabric: unknown port refused", ref.out.includes("ConnectionRefusedError"), { ms: +((/ConnectionRefusedError (\d+)/.exec(ref.out) || [])[1] || -1) });
  }

  // ---- T3: resource cost and CPU contention ----
  if (want("T3")) {
    await memory("T3-idle");
    await B.sh("nohup python3 /spike/rtt.py serve 9210 >/dev/null 2>&1 & sleep 1");
    await A.sh("nohup python3 /spike/rtt.py serve 7071 >/dev/null 2>&1 & sleep 1");
    const rttRe = /rtt median=([\d.]+) p90=([\d.]+) max=([\d.]+)/;
    const rtt = (r) => { const m = rttRe.exec(r.out); return m ? { med: +m[1], p90: +m[2], max: +m[3] } : null; };
    const baseLocal = rtt(await B.sh("python3 /spike/rtt.py client 127.0.0.1 9210 50"));
    const baseCross = rtt(await B.sh("python3 /spike/rtt.py client 127.0.0.1 7071 50"));
    const baseCpu = cpuMs(await B.sh(cpu));
    t = performance.now();
    const busy = A.sh("python3 /spike/rtt.py cpu 60000000");  // ~tens of seconds of pure CPU in A
    await new Promise((r) => setTimeout(r, 1500));
    await memory("T3-A-busy");
    const busyLocal = rtt(await B.sh("python3 /spike/rtt.py client 127.0.0.1 9210 50"));
    const busyCross = rtt(await B.sh("python3 /spike/rtt.py client 127.0.0.1 7071 50"));
    const busyCpu = cpuMs(await B.sh(cpu));
    const tPageCheck = performance.now();
    await new Promise((r) => setTimeout(r, 0));
    const pageLatency = ms(tPageCheck);
    const busyR = await busy;
    result("T3 CPU contention: B while A runs a busy loop", !!(baseLocal && busyLocal && baseCross && busyCross),
      { bLocalRttMed_ms: [baseLocal?.med, busyLocal?.med], bLocalRttP90_ms: [baseLocal?.p90, busyLocal?.p90],
        crossRttMed_ms: [baseCross?.med, busyCross?.med], crossRttP90_ms: [baseCross?.p90, busyCross?.p90],
        bCpu_ms: [baseCpu, busyCpu], aBusy_ms: busyR.ms, pageTick_ms: pageLatency });
    await memory("T3-after");
  }

  // ---- T4: cross-VM exec daemon (A runs commands for B) ----
  if (want("T4")) {
    const key = hex(32);
    const keyEnv = { ...ROOT_ENV, env: [...ROOT_ENV.env, `EXECD_KEY=${key}`, ...(DEBUG ? ["EXECD_DEBUG=1"] : [])] };
    const u = await A.sh("id -u agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent; id -u agent; id -g agent");
    const [uid, gid] = u.out.trim().split("\n").slice(-2).map(Number);
    await A.sh(`nohup python3 /spike/execd.py 7070 ${uid} >/root/execd.out 2>&1 & echo $! > /root/execd.pid`, keyEnv);
    const up = await A.sh(waitListen("/root/execd.out"));
    if (DEBUG) log(`debug uid=${uid} gid=${gid} up=${JSON.stringify(up.out.slice(-200))} execd.out=${JSON.stringify((await A.sh("tail -c 300 /root/execd.out; ss -ltn 2>/dev/null | head -5")).out)}`);
    await B.sh("umask 077; echo placeholder-secret > /root/b-secret; echo x > /tmp/b-only");
    const direct = [];
    for (let i = 0; i < 10; i++) direct.push((await A.sh("echo x")).ms);
    const execc = async (mode, extra = "", limit = LIMIT_MS) => {
      const r = await B.sh(`python3 /spike/execc.py 127.0.0.1 7070 ${uid} ${gid} ${mode} ${extra} 2>&1`, keyEnv, limit)
        .catch(async (e) => { if (DEBUG) log(`debug ${mode} ${e} replies=${JSON.stringify((await A.sh("grep -c replied /root/execd.out; grep -c eof /root/execd.out", ROOT_ENV, 20000).catch(() => ({ out: "A hang" }))).out)} prog=${JSON.stringify((await B.sh("grep -c got /tmp/execc.prog; tail -n 3 /tmp/execc.prog", ROOT_ENV, 20000).catch(() => ({ out: "B hang" }))).out)}`); throw e; });
      if (DEBUG) log(`debug ${mode} status=${r.status} ms=${r.ms} out=${JSON.stringify(r.out.slice(-400))}`);
      return r;
    };
    const bench = resultLine(await execc("bench", String(N_EXEC)));
    result(`T4a exec daemon latency over ${N_EXEC} commands`, lastLine(up) === "1" && bench?.ok === N_EXEC,
      { ...(bench || { missing: true }), directCxRunMedian_ms: median(direct) });
    const to = resultLine(await execc("timeout", "3"));
    result("T4b timeout kills the command and its children (/proc walk), earlier background job survives",
      to && to.timed_out && to.cmd_children_stopped && to.earlier_bg_survives, to || { missing: true });
    const iso = resultLine(await execc("isolation", "9202"));
    result("T4c agent in A sees nothing of B; execd port and B-private port closed to A; uid 0 refused",
      iso && iso.agent_uid && !iso.b_files_visible && !iso.b_private_port_open && !iso.execd_port_open_from_a && iso.root_refused, iso || { missing: true });
    const fg = resultLine(await execc("forge"));
    result("T4d bad MAC and replayed seq refused", fg && fg.bad_mac_refused && fg.replay_refused, fg || { missing: true });
    // What a root agent in A can do: read execd's memory (to learn the key)? replace execd (fake responses)?
    const mem = await A.sh("p=$(cat /root/execd.pid); echo pid=${p:-none}; python3 -c \"import sys\np=sys.argv[1]\nfor f in ('mem','maps','environ','cmdline'):\n  try:\n    d=open(f'/proc/{p}/'+f,'rb').read(64); print(f, 'readable', len(d))\n  except Exception as e: print(f, type(e).__name__)\" $p");
    const memInfo = Object.fromEntries(mem.out.trim().split("\n").slice(1).map((l) => l.split(" ")).map(([f, ...v]) => [f, v.join(" ")]));
    result("T4e (finding) root in A: execd /proc entries", true, memInfo);
    await A.sh("kill -KILL $(cat /root/execd.pid); sleep 0.5");  // never pkill -f here: it would match this shell
    t = performance.now();
    const gone = await execc("bench", "1", 60000).catch((e) => ({ out: String(e) }));
    const goneMs = ms(t);
    await A.sh(`nohup python3 /spike/execd.py 7070 ${uid} >/root/execd2.out 2>&1 & echo $! > /root/execd.pid; ${waitListen("/root/execd2.out")}`,
      { ...ROOT_ENV, env: [...ROOT_ENV.env, `EXECD_KEY=${hex(32)}`] });
    const fake = resultLine(await execc("bench", "3"));
    result("T4f dead execd fails fast; impostor without the key is detected by MAC",
      !resultLine(gone) && goneMs < 10000 && fake && fake.ok === 0,
      { deadDaemonFail_ms: goneMs, impostorRepliesAccepted: fake ? fake.ok : "no-result" });
  }

  // ---- T8: does a client's close() wait for the peer to close? (within one VM and across VMs) ----
  if (want("T8")) {
    const HOLD = "import socket,sys,time\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(4)\nkeep=[]\nwhile True:\n    c,_=s.accept(); keep.append(c)\n";
    const CLOSE = "import socket,sys,time\nc=socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=10); c.sendall(b'x'); t=time.time(); c.close(); print('closed', round((time.time()-t)*1000))\n";
    await A.data.writeFile("/hold.py", HOLD); await A.data.writeFile("/close.py", CLOSE);
    await B.data.writeFile("/hold.py", HOLD); await B.data.writeFile("/close.py", CLOSE);
    fabric.setMode("fabric", { ...ROUTES, routes: { ...ROUTES.routes, B: { ...ROUTES.routes.B, 7072: "A" } } });
    await A.sh("nohup python3 /spike/hold.py 7072 >/dev/null 2>&1 & sleep 1");  // holds every connection open, reads nothing
    await B.sh("nohup python3 /spike/hold.py 9220 >/dev/null 2>&1 & sleep 1");
    const intra = await B.sh("python3 /spike/close.py 9220", ROOT_ENV, 30000).then((r) => (/closed (\d+)/.exec(r.out) || [, "noresult"])[1], () => "hang>30s");
    const cross = await B.sh("python3 /spike/close.py 7072", ROOT_ENV, 30000).then((r) => (/closed (\d+)/.exec(r.out) || [, "noresult"])[1], () => "hang>30s");
    // server side: B accepts, writes a line and closes, while a client in A holds the connection (reads nothing)
    const SRV = "import socket,sys,time\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(4)\nc,_=s.accept(); c.sendall(b'bye\\n'); t=time.time(); c.close(); print('srvclosed', round((time.time()-t)*1000), flush=True)\n";
    const HOLDC = "import socket,sys,time\nc=socket.create_connection(('127.0.0.1', int(sys.argv[1]))); time.sleep(40)\n";
    await B.data.writeFile("/srv.py", SRV); await A.data.writeFile("/holdc.py", HOLDC);
    fabric.setMode("fabric", { ...ROUTES, routes: { ...ROUTES.routes, A: { ...ROUTES.routes.A, 31340: "B" } } });
    await B.sh("rm -f /tmp/srv.out; nohup python3 /spike/srv.py 31340 >/tmp/srv.out 2>&1 & sleep 1");
    await A.sh("nohup python3 /spike/holdc.py 31340 >/dev/null 2>&1 & sleep 1");
    const srv = await B.sh("for i in $(seq 20); do grep -q srvclosed /tmp/srv.out && break; sleep 1; done; cat /tmp/srv.out", ROOT_ENV, 60000);
    const srvClose = (/srvclosed (\d+)/.exec(srv.out) || [, "pending>20s"])[1];
    fabric.setMode("fabric", ROUTES);
    if (DEBUG) log(`debug nettrace ${JSON.stringify((globalThis.__netTrace || []).filter((l) => /^(9220|7072|31340)#/.test(l)))}`);
    result("T8 (finding) close() while the peer keeps the connection open", true, { intraVm_clientCloseMs: intra, crossVm_clientCloseMs: cross, crossVm_serverCloseMs: srvClose });
  }

  if (WEDGE) {
    // S2k in A (README: a SIGKILL to an exited orphan never returns in CheerpX 1.3.9): does it stall B too?
    // the exact S2k recipe of lab/web/spikes/spike.js (harness-style kill of the shell, then SIGKILL every agent pid)
    const KILL_PY = `import os,signal,subprocess,sys,time
tick='/tmp/spike-tick'
t=time.time()
p=subprocess.Popen(['bash','-c',sys.argv[1]],user='agent',group='agent',extra_groups=[],cwd='/tmp',stdin=subprocess.DEVNULL,start_new_session=True)
try:
    p.wait(timeout=3); print('exited early')
except subprocess.TimeoutExpired:
    try:
        os.killpg(p.pid,signal.SIGKILL); print('killpg ok')
    except ProcessLookupError:
        print('killpg ProcessLookupError')
        print('pkill-s rc',subprocess.run(['/usr/bin/pkill','-KILL','-s',str(p.pid)]).returncode)
        try: os.kill(p.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    for _ in range(100):
        if p.poll() is not None: break
        time.sleep(0.1)
print('waited' if p.poll() is not None else 'wait-timeout','elapsed',round(time.time()-t,1))
a=os.path.getsize(tick); time.sleep(1.5); b=os.path.getsize(tick)
print('alive' if b>a else 'stopped')
`;
    const wa = A.sh(`rm -f /tmp/spike-tick; echo ${btoa(KILL_PY)} | base64 -d > /tmp/kill.py; python3 /tmp/kill.py 'while :; do echo . >> /tmp/spike-tick; sleep 0.2; done'; sleep 1; for p in $(pgrep -u agent); do kill -KILL $p; done; echo a-end`, ROOT_ENV, 60000)
      .then((r) => ({ hung: !r.out.includes("a-end") }), () => ({ hung: true }));
    await new Promise((r) => setTimeout(r, 8000));
    const bAlive = await B.sh("echo b-alive", ROOT_ENV, 30000).then((r) => r.out.includes("b-alive"), () => false);
    const aState = await wa;
    result("T6 (finding) S2k wedge in A leaves B running", bAlive, { aHung: aState.hung, bAlive, pageErrors: R.pageErrors });
  }
  // ---- T7: end-of-run kill by removing the agent VM's iframe (no SIGKILL inside CheerpX at all) ----
  if (want("T7")) {
    await memory("before-A-removed");
    const t7 = performance.now();
    A.frame.remove();
    window.__fabric.remove("A");
    const removeMs = ms(t7);
    await new Promise((r) => setTimeout(r, 3000));
    const b7 = await B.sh("echo b-ok; python3 -c 'print(\"py-ok\")'", ROOT_ENV, 60000).catch(() => ({ out: "" }));
    // A's routes now lead nowhere: B's connect to A's execd port must fail fast, not hang
    const r7 = await B.sh("python3 /spike/linec.py 127.0.0.1 7070", ROOT_ENV, 60000).catch(() => ({ out: "hang" }));
    await memory("after-A-removed");
    result("T7 removing A's iframe ends A; B keeps running", b7.out.includes("b-ok") && b7.out.includes("py-ok"),
      { removeMs, bAfter_ms: b7.ms, connectToRemovedA: (/lined (ok|fail \w+)/.exec(r7.out) || ["hang"])[0] });
  }
  await memory("end");
}

main().catch((e) => { R.error = `${e && e.message || e} | ${String(e && e.stack || "").slice(0, 400)}`; log("ERROR " + e); })
  .finally(async () => {
    try {
      for (const d of (await indexedDB.databases?.()) || []) if (d.name?.startsWith("twovm-")) indexedDB.deleteDatabase(d.name);
    } catch {}
    R.done = true; log("done");
  });
