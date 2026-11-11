// Swarm dev harness (local only, never deployed; docs/browser-swarm.md §6). Driven by spikes/swarm/run.mjs.
// Results go to window.__swarmdev.results as {id, pass, metric}; metrics are numbers, booleans and fixed
// strings from our own neutral commands. Nothing here reads, logs or renders run content.
//
//   ?mode=net&n=2      boot board + n agent frames through the production /vmframe.html on js/fabric.js and
//                      check cross-VM routing (a{i} reaches only board:9000+i), private port spaces, and cut()
//                      (both ends see EOF in real CheerpX; later connects refused). Measures idle memory.
//   ?mode=swarm&n=2    createSwarm() end to end with a mock provider through a page-level fetch shim (the lab
//                      page never has one). &overlay=1 copies the working tree's guest files (served by run.mjs
//                      at /dev-guest/) over /root/lab in every VM first, so the board-split labctl runs without
//                      an image rebuild.
import { createFabric, swarmRoutes } from "/js/fabric.js";
import { BOARD_VM, agentVm, dbRunId, makeRunId, swarmVms } from "/js/protocol.js";
import { createSwarm } from "/js/swarm.js";
import { readZip } from "/js/zip.js";

const params = new URLSearchParams(location.search);
const MODE = params.get("mode") || "net";
const N = Number(params.get("n") || 2);
const OVERLAY = params.get("overlay") === "1";
const R = window.__swarmdev = { done: false, results: [], error: null, phase: "init", mem: [], info: { mode: MODE, n: N } };
const logEl = document.getElementById("log");
const log = (s) => { logEl.textContent += s + "\n"; };
const ms = (t) => Math.round(performance.now() - t);
const sleep = (t) => new Promise((r) => setTimeout(r, t));
const dec = new TextDecoder();
const ROOT_ENV = { env: ["HOME=/root", "TERM=dumb", "LANG=C.UTF-8",
  "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"], cwd: "/root", uid: 0, gid: 0 };
addEventListener("error", () => { R.info.pageErrors = (R.info.pageErrors || 0) + 1; });
addEventListener("unhandledrejection", () => { R.info.pageErrors = (R.info.pageErrors || 0) + 1; });

// R.info.diag[k] = v (numbers and fixed tokens only)
const diag = (k, v) => { R.info.diag = { ...(R.info.diag || {}), [k]: v }; };

function result(id, pass, metric) {
  R.results.push({ id, pass: !!pass, metric: JSON.stringify(metric) });
  log(`${pass ? "PASS" : "FAIL"} ${id} ${JSON.stringify(metric)}`);
}

async function memory(phase) {
  if (window.gc) for (let k = 0; k < 6; k++) { window.gc(); await sleep(500); }  // run.mjs passes --js-flags=--expose-gc
  const m = { phase, t: Math.round(performance.now()) };
  if (performance.memory) m.jsHeapMB = Math.round(performance.memory.usedJSHeapSize / 1048576);
  if (performance.measureUserAgentSpecificMemory && self.crossOriginIsolated) {
    try {
      const r = await Promise.race([performance.measureUserAgentSpecificMemory(),
        new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 60000))]);
      m.uaMemMB = Math.round(r.bytes / 1048576);
    } catch (e) { m.uaMem = String(e?.name || "error").slice(0, 40); }
  }
  R.mem.push(m);
  R.phase = phase;
  log(`mem ${JSON.stringify(m)}`);
  await sleep(4000);  // let the runner's 1 s RSS sampler see this phase
}

async function readText(dev, path) {
  try {
    const blob = await dev.readFileAsBlob(path);
    return blob ? dec.decode(new Uint8Array(await blob.arrayBuffer())) : null;
  } catch { return null; }
}

async function cleanupDbs(runId) {
  for (const d of (await indexedDB.databases?.()) || []) {
    if (dbRunId(d.name) === runId) indexedDB.deleteDatabase(d.name);
  }
}

// ---- net mode ----

// Neutral test programs. srv.py: the board side, one listener per port; logs "conn|eof <port> <id>".
// cli.py: "probe" connects, reads the banner and closes; "hold" also blocks until EOF and logs it.
const SRV_PY = `import queue, socket, sys, threading
# One writer (the main thread): appends to the export mount from several threads lost the file's content in
# CheerpX 1.3.9 (dev-harness finding), so the threads only queue their events.
events = queue.Queue()
nid = [0]
lock = threading.Lock()
def handle(c, port, k):
    try:
        c.sendall(b"board %d\\n" % port)
        while c.recv(4096):
            pass
    except OSError:
        pass
    events.put("eof %d %d" % (port, k))
    c.close()
def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(16)
    events.put("listening %d" % port)
    while True:
        c, _ = s.accept()
        with lock:
            nid[0] += 1
            k = nid[0]
        events.put("conn %d %d" % (port, k))
        threading.Thread(target=handle, args=(c, port, k), daemon=True).start()
for p in sys.argv[2].split(","):
    threading.Thread(target=serve, args=(int(p),), daemon=True).start()
while True:
    line = events.get()
    with open(sys.argv[1], "a") as log:
        log.write(line + "\\n")
`;
const CLI_PY = `import errno, socket, sys, time
class out:
    def write(s):
        with open(sys.argv[1], "a") as f:
            f.write(s)
mode, port = sys.argv[2], int(sys.argv[3])
t = time.time()
try:
    s = socket.create_connection(("127.0.0.1", port))
    banner = s.recv(64).decode().strip().replace(" ", "_")
except OSError as e:
    out.write("%s %d fail %s %d\\n" % (mode, port, errno.errorcode.get(e.errno or 0, type(e).__name__), int((time.time() - t) * 1000)))
    sys.exit(0)
out.write("%s %d ok %s %d\\n" % (mode, port, banner, int((time.time() - t) * 1000)))
if mode == "hold":
    try:
        d = s.recv(1)
        out.write("hold %d %s\\n" % (port, "data" if d else "eof"))
    except OSError as e:
        out.write("hold %d err %s\\n" % (port, type(e).__name__))
s.close()
`;

async function bootFrame(runId, vm, image, fabric) {
  const t = performance.now();
  const frame = document.createElement("iframe");
  frame.width = "0";
  frame.height = "0";
  frame.src = `/vmframe.html?vm=${vm}`;
  const loaded = new Promise((r) => frame.addEventListener("load", r, { once: true }));
  document.getElementById("frames").append(frame);
  await loaded;
  while (!frame.contentWindow.__vmframe) {
    if (performance.now() - t > 30000) throw new Error(`${vm}: no __vmframe (CSP or frame-ancestors?)`);
    await sleep(50);
  }
  const h = await frame.contentWindow.__vmframe.boot({ runId, vm, image, networkInterface: fabric.iface(vm) });
  return { frame, h, bootMs: ms(t) };
}

// A file's text: export-device paths ("/x.log") are read directly; guest paths under /tmp (written by the
// threaded srv.py, whose appends on the export mount were lost in CheerpX 1.3.9) are first copied to a fresh
// export name by a foreground cp.
let snaps = 0;
async function readGuest(h, file) {
  if (!file.startsWith("/tmp/")) return readText(h.exportDev, file);
  const snap = `/snap-${++snaps}.txt`;
  await sh(h, `cp ${file} /root/export${snap} 2>/dev/null || : > /root/export${snap}`);
  return readText(h.exportDev, snap);
}

// Waits until `file` has a line matching re (count >= k); resolves to the matching lines.
async function waitLines(h, file, re, k = 1, limitMs = 60000) {
  const t = performance.now();
  for (;;) {
    const lines = ((await readGuest(h, file)) || "").split("\n").filter((l) => re.test(l));
    if (lines.length >= k || performance.now() - t > limitMs) return lines;
    await sleep(200);
  }
}

// our own log lines, reduced to their fixed tokens (words, numbers)
const shape = (l) => l.replace(/[^\w .-]/g, "?").slice(0, 60);
const sh = (h, cmd) => h.run("/bin/bash", ["-c", cmd], ROOT_ENV);

async function netMode() {
  await memory("before-boot");
  const manifest = await (await fetch("/vm-manifest.json", { cache: "no-store" })).json();
  R.info.image = manifest.sha12;
  R.info.crossOriginIsolated = self.crossOriginIsolated;
  const runId = makeRunId("forum", N);
  R.info.runIdShape = runId.replace(/\d{14}-[0-9a-f]{4}$/, "<stamp>");
  const names = swarmVms(N);
  const fabric = createFabric({ names, routes: swarmRoutes(N) });
  const vms = {};
  try {
    for (const vm of names) {
      vms[vm] = await bootFrame(runId, vm, manifest.filename, fabric);
      const st = await sh(vms[vm].h, "echo ok >/dev/null");
      log(`booted ${vm} in ${vms[vm].bootMs} ms (status ${st})`);
    }
    result("N1 board + agents boot in their own frames", true, Object.fromEntries(names.map((v) => [v, vms[v].bootMs])));
    await memory(`idle board+${N}`);

    const B = vms.board.h;
    const timePy = async (h) => { const t = performance.now(); const st = await sh(h, "python3 -c pass && echo x >> /root/export/tp.txt"); return { st, ms: ms(t), file: ((await readText(h.exportDev, "/tp.txt")) || "").length }; };
    diag("pyIdle", { board: await timePy(B), a0: await timePy(vms.a0.h) });
    for (const vm of names) {
      await vms[vm].h.inbox.writeFile("/srv.py", SRV_PY);
      await vms[vm].h.inbox.writeFile("/cli.py", CLI_PY);
    }
    const ports = Array.from({ length: N }, (_, i) => 9000 + i);
    await sh(B, `nohup python3 /root/llmbox/in/srv.py /tmp/srv.log ${ports.join(",")},31337 >/dev/null 2>&1 &`);
    const listening = await waitLines(B, "/tmp/srv.log", /^listening/, N + 1);
    diag("pySrv", { board: await timePy(B), a0: await timePy(vms.a0.h) });
    result("N2 board listens on 9000..9000+N-1 (+31337)", listening.length === N + 1, { listening: listening.length });

    // N3: each agent reaches exactly its own board port; other board ports, unrouted ports refused
    const reach = [];
    for (let i = 0; i < N; i++) {
      const A = vms[agentVm(i)].h;
      const t = performance.now();
      await sh(A, ports.concat([31337]).map((p) => `python3 /root/llmbox/in/cli.py /root/export/cli.log probe ${p}`).join("; "));
      const lines = await waitLines(A, "/cli.log", /^probe/, N + 1, 10000);
      const ok = lines.filter((l) => / ok /.test(l)).map((l) => l.split(" ")[1]);
      const own = lines.find((l) => l.startsWith(`probe ${9000 + i} ok board_${9000 + i}`));
      reach.push({ i, ok, own: !!own, ms: ms(t), connectMs: Number((own || "").split(" ").pop()) });
    }
    result("N3 a{i} reaches only board:9000+i", reach.every((r) => r.own && r.ok.length === 1),
      reach.map((r) => ({ i: r.i, okPorts: r.ok, connectMs: r.connectMs, totalMs: r.ms })));

    diag("afterN3", { board: await timePy(B), a0: await timePy(vms.a0.h), srvLen: ((await readGuest(B, "/tmp/srv.log")) || "").length });
    // N4: private port spaces: every agent can listen on the same port, and its own connect stays inside it
    const priv = [];
    for (let i = 0; i < N; i++) {
      const A = vms[agentVm(i)].h;
      await sh(A, "nohup python3 /root/llmbox/in/srv.py /tmp/own.log 7000 >/dev/null 2>&1 &");
      const ownListen = await waitLines(A, "/tmp/own.log", /^listening 7000/, 1, 30000);
      diag(`own7000-listen-a${i}`, ownListen.length);
      await sh(A, "python3 /root/llmbox/in/cli.py /root/export/own-cli.log probe 7000");
      const l = await waitLines(A, "/own-cli.log", /^probe 7000/, 1, 15000);
      priv.push(l.some((x) => / ok board_7000 /.test(x)));
      diag(`own7000-a${i}`, l.map(shape));
    }
    const boardTo7000 = await (async () => {
      await sh(B, "python3 /root/llmbox/in/cli.py /root/export/b-cli.log probe 7000");
      const l = await waitLines(B, "/b-cli.log", /^probe 7000/, 1, 10000);
      diag("board7000", l.map(shape));
      return l.some((x) => / fail /.test(x));
    })();
    result("N4 private port spaces (agents' 7000s are separate; the board cannot reach them)", priv.every(Boolean) && boardTo7000,
      { agentsOwn7000: priv, boardRefused7000: boardTo7000 });

    // N5: a held connection per agent, then cut. Agent a{N-1}: cut while its frame is alive -> both ends EOF.
    // Agent a0 (production order): remove the frame, then cut -> the board end EOF.
    for (let i = 0; i < N; i++) {
      await sh(vms[agentVm(i)].h, `nohup python3 /root/llmbox/in/cli.py /root/export/hold.log hold ${9000 + i} >/dev/null 2>&1 &`);
    }
    for (let i = 0; i < N; i++) await waitLines(vms[agentVm(i)].h, "/hold.log", /^hold \d+ ok/, 1, 15000);
    const openOf = (lines, port) => {
      const c = lines.filter((l) => l.startsWith(`conn ${port} `)).length, e = lines.filter((l) => l.startsWith(`eof ${port} `)).length;
      return c - e;
    };
    const srvLines = async () => ((await readGuest(B, "/tmp/srv.log")) || "").split("\n");
    const before = await srvLines();
    diag("srvBefore", before.filter(Boolean).map(shape).slice(-40));
    diag("hold", await Promise.all(Array.from({ length: N }, async (_, i) => ((await readText(vms[agentVm(i)].h.exportDev, "/hold.log")) || "").split("\n").filter(Boolean).map(shape))));
    const openBefore = ports.map((p) => openOf(before, p));
    const last = N - 1, lastPort = 9000 + last;
    let t = performance.now();
    await fabric.cut(agentVm(last));
    const agentEof = (await waitLines(vms[agentVm(last)].h, "/hold.log", new RegExp(`^hold ${lastPort} eof`), 1, 15000)).length > 0;
    const agentEofMs = ms(t);
    let after = await srvLines();
    for (let k = 0; k < 50 && openOf(after, lastPort) > 0; k++) { await sleep(200); after = await srvLines(); }
    const boardEofMs = ms(t);
    await sh(vms[agentVm(last)].h, `python3 /root/llmbox/in/cli.py /root/export/cut-cli.log probe ${lastPort}`);
    const refusedAfterCut = (await waitLines(vms[agentVm(last)].h, "/cut-cli.log", /^probe/, 1, 10000)).some((l) => / fail /.test(l));
    result("N5 cut(a{N-1}) with its frame alive: agent end EOF, board end EOF, later connect refused",
      agentEof && openOf(after, lastPort) === 0 && openBefore[last] === 1 && refusedAfterCut,
      { openBefore: openBefore[last], openAfter: openOf(after, lastPort), agentEof, agentEofMs, boardEofMs, refusedAfterCut });
    vms[agentVm(last)].frame.remove();

    if (N >= 2) {
      t = performance.now();
      vms.a0.frame.remove();
      const removeMs = ms(t);
      await sleep(2000);
      const mid = await srvLines();
      const openAfterRemoveOnly = openOf(mid, 9000);
      t = performance.now();
      await fabric.cut("a0");
      let fin = await srvLines();
      for (let k = 0; k < 50 && openOf(fin, 9000) > 0; k++) { await sleep(200); fin = await srvLines(); }
      result("N6 remove a0's frame then cut(a0): the board end sees EOF (R1)", openOf(fin, 9000) === 0 && openBefore[0] === 1,
        { removeMs, openAfterRemoveOnly, openAfterCut: openOf(fin, 9000), boardEofMs: ms(t) });
      // the other agents keep working
      const others = [];
      for (let i = 1; i < last; i++) {
        await sh(vms[agentVm(i)].h, `python3 /root/llmbox/in/cli.py /root/export/post.log probe ${9000 + i}`);
        others.push((await waitLines(vms[agentVm(i)].h, "/post.log", /^probe/, 1, 10000)).some((l) => / ok /.test(l)));
      }
      const bAlive = await sh(B, "true") === 0;
      result("N7 the board and the remaining agents keep running", bAlive && others.every(Boolean), { bAlive, others });
    }
    await memory("after-cut");
    R.info.fabric = fabric.stats();
  } finally {
    for (const v of Object.values(vms)) v.frame.remove();
    await cleanupDbs(runId);
  }
  for (const k of Object.keys(vms)) delete vms[k];
  await memory("all-removed");
}

// ---- swarm mode ----

const MOCK_BASE = "https://mock.invalid/v1";
// The mock agent's commands; each prints only a fixed marker or a number, and check() looks for that marker.
const SCRIPT = [
  { id: "echo", cmd: "echo step-ok", check: (t) => /step-ok/.test(t) },
  { id: "board-200", cmd: "curl -s -m 20 -o /dev/null -w '%{http_code}\\n' http://board.internal:8080/", check: (t) => /\b200\b/.test(t) },
  { id: "reach-1", cmd: "python3 -c \"import socket\nn = 0\nfor p in range(9000, 9004):\n    try:\n        socket.create_connection(('127.0.0.1', p)).close()\n        n += 1\n    except OSError:\n        pass\nprint('reach', n)\"",
    check: (t) => /reach 1\b/.test(t) },
];

function installMockProvider() {
  const real = window.fetch.bind(window);
  R.checks = Object.fromEntries(SCRIPT.map((s) => [s.id, 0]));
  R.mockCalls = 0;
  window.fetch = async (url, init) => {
    if (typeof url !== "string" || !url.startsWith(MOCK_BASE)) return real(url, init);
    R.mockCalls++;
    const body = JSON.parse(init.body);
    const i = body.messages.filter((m) => m.role === "assistant").length;  // which step of this agent's script
    if (i > 0 && i <= SCRIPT.length) {
      const tool = [...body.messages].reverse().find((m) => m.role === "tool");
      if (SCRIPT[i - 1].check(String(tool?.content || ""))) R.checks[SCRIPT[i - 1].id]++;
    }
    const message = i < SCRIPT.length
      ? { role: "assistant", content: "", tool_calls: [{ id: `c${i}`, type: "function", function: { name: "bash", arguments: JSON.stringify({ command: SCRIPT[i].cmd }) } }] }
      : { role: "assistant", content: "done" };
    await sleep(50);
    return new Response(JSON.stringify({ choices: [{ message, finish_reason: "stop" }],
      usage: { prompt_tokens: 100 * body.messages.length, completion_tokens: 20 } }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
}

// dev only: the working tree's guest files over /root/lab (run.mjs serves them; the lab page never does this)
async function overlay(vm, h) {
  const t = performance.now();
  const files = await (await fetch("/dev-guest/files.json")).json();
  for (const f of files) {
    const data = await (await fetch(`/dev-guest/${f}`)).text();  // DataDevice.writeFile takes text (see swarm.js)
    await h.inbox.writeFile(`/dev-${f.replaceAll("/", "_")}`, data);
  }
  const cmd = files.map((f) => `rm -f /root/lab/${f} && cp /root/llmbox/in/dev-${f.replaceAll("/", "_")} /root/lab/${f}`).join(" && ") + " && chmod -R go-rwx /root/lab";
  const st = await sh(h, cmd);
  await sh(h, "grep -c 'def main_board' /root/lab/labctl.py > /root/export/dev-overlay-check.txt; md5sum /root/lab/labctl.py /root/llmbox/in/dev-labctl.py | cut -c1-32 >> /root/export/dev-overlay-check.txt");
  const check = ((await readText(h.exportDev, "/dev-overlay-check.txt")) || "").trim().split("\n");
  R.info.overlay = { ...(R.info.overlay || {}), [vm]: { status: st, files: files.length, ms: ms(t), mainBoard: check[0], sameMd5: check[1] === check[2] } };
  if (st !== 0) throw new Error("E_DEV_OVERLAY");
}

const unzipNames = async (blob) => new Map((await readZip(blob)).map(({ name, data }) => [name, data]));

async function swarmMode() {
  installMockProvider();
  await memory("before-swarm");
  const settings = { preset: "mock", iface: params.get("iface") || "forum", nAgents: N,
    provider: { baseUrl: MOCK_BASE, model: "mock", apiKey: "sk-dev-placeholder" },
    harness: { budget: 8, temperature: 0.7, max_tokens: 2048, output_chars: 3000, run_timeout_s: 1200, model: "mock" },
    wire: {}, caps: { calls: 8 * N, tokens: 600000 } };
  let ignored = 0, last = null, peakRunning = 0;
  let s = createSwarm(settings, {
    onStatus: (st) => {
      last = st;
      R.phase = `swarm:${st.phase}`;
      peakRunning = Math.max(peakRunning, st.agents.filter((a) => a.phase === "running").length);
    },
    onIgnored: () => { ignored++; },
  }, { container: document.getElementById("frames"), prepareVm: OVERLAY ? overlay : undefined,
    timing: { boardReadyMs: Number(params.get("readyMs") || 150000), boardEndMs: 90000 } });
  window.__swarm = s;
  // fixed fields of every VM's status.json (phase, ended, error code, exception class) and the exception class
  // names at the end of labctl's console; never the console text itself
  const vmStatus = {};
  const watch = setInterval(async () => {
    for (const vm of swarmVms(N)) {
      try {
        const st = JSON.parse(await s.readFile(vm, "export", "/status.json") || "null");
        const exc = (s.consoleTail(vm).match(/\b[A-Z]\w*(Error|Exception)\b/g) || []).slice(-2);
        // fixed-code fields only: anything that is not a short code-shaped string is reported as "?"
        const code = (v) => (v == null ? v : typeof v === "string" && /^[\w.:-]{1,64}$/.test(v) ? v : "?");
        if (st || exc.length) vmStatus[vm] = { phase: code(st?.phase), ended: code(st?.ended), error: code(st?.error), detail: code(st?.detail), exc };
      } catch {}
    }
    R.info.vmStatus = vmStatus;
    try {
      const text = await s.readFile(BOARD_VM, "export", "/board-ready.json");
      let ready = null;
      try { ready = JSON.parse(text); } catch {}
      R.info.boardReady = text == null ? "absent" : { bytes: text.length, parses: !!ready, keys: ready && Object.keys(ready).sort(),
        v: ready?.v, runIdMatches: ready?.run_id === s.runId, ctfKeys: ready?.ctf && Object.keys(ready.ctf).sort() };
    } catch {}
  }, 3000);
  R.info.runIdShape = s.runId.replace(/\d{14}-[0-9a-f]{4}$/, "<stamp>");
  const stopAfter = Number(params.get("stop") || 0);
  if (stopAfter) (async () => { while (R.mockCalls < stopAfter) await sleep(200); s.stop(); })();
  const t = performance.now();
  const out = await s.run();
  clearInterval(watch);
  const z = await s.zip();
  const files = await unzipNames(z.blob);
  const rel = [...files.keys()].map((k) => k.slice(s.runId.length + 1)).sort();
  const manifest = JSON.parse(dec.decode(files.get(`${s.runId}/manifest.json`) || new Uint8Array()) || "null");
  const run = files.get(`${s.runId}/run.json`) ? JSON.parse(dec.decode(files.get(`${s.runId}/run.json`))) : null;
  const summary = {
    outcome: out.result, error: out.error || null, detail: out.detail || null, ms: ms(t),
    zipSuffix: z.filename.slice(s.runId.length), zipEntries: rel.length, names: rel,
    agentStopped: manifest?.agent_stopped, mockCalls: R.mockCalls, checks: R.checks, ignored, peakRunning,
    agents: last?.agents.map((a) => ({ i: a.i, phase: a.phase, steps: a.steps, calls: a.calls })),
    run: run && { n_agents: run.n_agents, topology: run.params?.topology, hasCtf: !!run.ctf, timedOut: run.timed_out,
      fatalStatus: run.fatal?.status ?? null, agentStopped: run.params?.agent_stopped, agentsStopped: run.params?.agents_stopped,
      stopReason: run.params?.stop_reason ?? null, exposure: run.params?.secret_exposure ?? null },
    stats: s.stats(),
  };
  const want = stopAfter ? "stopped" : "done";
  result(`S1 createSwarm n=${N} ends ${want} with a complete §3 zip`, out.result === want && summary.zipSuffix !== "-incomplete.zip"
    && rel.includes("run.json") && rel.includes("manifest.json") && (manifest?.agent_stopped || []).length === N, summary);
  if (!stopAfter) {
    result(`S2 every agent reached the board through its relay and only its own port`,
      R.checks["board-200"] === N && R.checks["reach-1"] === N, R.checks);
  }
  await memory("after-swarm");
  const runId = s.runId;
  window.__swarm = null;
  s = null;
  await memory("after-release");
  await cleanupDbs(runId);
}

(MODE === "swarm" ? swarmMode() : netMode())
  .catch((e) => { R.error = String(e?.message || e).slice(0, 300); log("ERROR " + R.error); })
  .finally(() => { R.done = true; log("done"); });
