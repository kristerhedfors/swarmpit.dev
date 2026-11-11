// Unit tests for public/js/swarm.js (docs/browser-swarm.md §2/§3/§6) in a fake world: fake iframes whose
// vmframe boot() returns Map-backed devices and a scripted stand-in for labctl --role board|agent that follows
// PROTOCOL.md §9 (unseals its spec, writes board-ready.json / mailbox requests / exports, waits for the stop
// file). The real fabric and brokers are used; the provider is a stubbed fetch. All payloads are neutral
// placeholders; nothing is printed.
//   cd lab/web && node --test test/swarm.test.mjs
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { BOOT_INPUTS, pageAgentMeta, seal } from "../public/js/protocol.js";
import { createSwarm } from "../public/js/swarm.js";
import { readZip } from "../public/js/zip.js";
import { failOnUnhandledRejections, sleep } from "./helpers.mjs";

failOnUnhandledRejections();

const enc = new TextEncoder(), dec = new TextDecoder();
const toBytes = (d) => (typeof d === "string" ? enc.encode(d) : new Uint8Array(d));
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; delete globalThis.document; });

const TIMING = { pollMs: 40, frameLoadMs: 2000, bootMs: 2000, boardReadyMs: 3000, staggerMs: 5, stopGraceMs: 400,
  boardEndMs: 3000, readMs: 1000, deadlineMarginMs: 60000 };
const OK_BODY = { choices: [{ message: { role: "assistant", content: "placeholder" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 10, completion_tokens: 5 } };
const CTF = { passphrase: "alpha-beta-gamma", key_hex: "0".repeat(16), flag: `FLAG{${"0".repeat(20)}}`, vault_id: "vlt-000000" };

class Dev {
  constructor(readDelayMs = 0) { this.m = new Map(); this.order = []; this.readDelayMs = readDelayMs; this.reads = 0; }
  async readFileAsBlob(p) {
    this.reads++;
    if (this.readDelayMs) await sleep(this.readDelayMs);
    if (!this.m.has(p)) throw new Error("ENOENT");
    return new Blob([this.m.get(p)]);
  }
  async writeFile(p, d) { if (this.m.has(p)) throw new Error("EEXIST"); this.m.set(p, toBytes(d)); this.order.push(p); }
  put(p, d) { this.m.set(p, toBytes(d)); }
  json(p) { return this.m.has(p) ? JSON.parse(dec.decode(this.m.get(p))) : null; }
}

// §2 seal is an XOR stream: sealing the ciphertext again under the same key and label yields the plaintext.
async function unseal(keyHex, label, text) {
  const ct = Uint8Array.from(atob(JSON.parse(text).data), (c) => c.charCodeAt(0));
  return Uint8Array.from(atob(JSON.parse(await seal(keyHex, label, ct)).data), (c) => c.charCodeAt(0));
}

// The fake world: frames, VMs and their scripted labctl. agentScript(i) -> {calls, wedge, exitError}.
// bootDelayMs: each boot() takes that long (W.maxBooting counts overlapping boots); boardDies: the board's labctl
// ends with E_BOARD once every agent has started its labctl.
// boardReadDelayMs: every read of the board's export device takes that long; boardReadyDelayMs: the board's
// labctl waits that long before board-ready.json; bootDelay(vm) overrides bootDelayMs per VM.
function world({ agentScript = () => ({ calls: 2 }), boardFails = false, bootDelayMs = 0, boardDies = false, bootFails = () => false,
  boardReadDelayMs = 0, boardReadyDelayMs = 0, bootDelay = () => bootDelayMs } = {}) {
  const W = { vms: new Map(), removed: [], booted: [], specs: new Map(), keys: new Map(), boardConns: [], metasAtStop: null,
    booting: 0, maxBooting: 0 };

  function makeVm(vm, networkInterface) {
    const inbox = new Dev(), outbox = new Dev(), exportDev = new Dev(vm === "board" ? boardReadDelayMs : 0);
    const h = { inbox, outbox, exportDev, consoleTail: () => "" };
    h.runLabctl = async (keyHex) => {
      W.keys.set(vm, keyHex);
      const spec = JSON.parse(dec.decode(await unseal(keyHex, "run.json", dec.decode(inbox.m.get(BOOT_INPUTS.spec.path)))));
      W.specs.set(vm, { spec, hasSeed: inbox.m.has(BOOT_INPUTS.seed.path) });
      return vm === "board" ? board(h, spec, keyHex, networkInterface) : agent(h, spec, keyHex, networkInterface);
    };
    W.vms.set(vm, h);
    return h;
  }

  async function board(h, spec, keyHex, net) {
    const seed = await unseal(keyHex, "seed.json", dec.decode(h.inbox.m.get(BOOT_INPUTS.seed.path)));
    assert.equal(dec.decode(seed), "{\"placeholder\":true}");
    h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "setup" }));
    if (boardFails) {
      h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "error", error: "E_BOARD" }));
      return 1;
    }
    for (let p = 0; p < spec.n_agents; p++) {  // the board's ports; count accepted relay connections and their EOF
      const srv = net.TCPServerSocket("0.0.0.0", { localPort: 9000 + p });
      const accepts = (await srv.opened).readable.getReader();
      (async () => {
        for (;;) {
          const { value } = await accepts.read();
          const s = await value.opened;
          const conn = { port: 9000 + p, eof: false };
          W.boardConns.push(conn);
          s.readable.getReader().read().then((r) => { conn.eof = r.done; });
        }
      })();
    }
    if (boardReadyDelayMs) await sleep(boardReadyDelayMs);
    h.exportDev.put("/board-ready.json", JSON.stringify({ v: 1, run_id: spec.run_id, ctf: CTF, started: "2026-10-24T00:00:00Z",
      ports: Array.from({ length: spec.n_agents }, (_, p) => 9000 + p) }));
    h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "running", events: 1 }));
    h.exportDev.put("/run/board_events.jsonl", "{\"kind\":\"placeholder\"}\n");
    if (boardDies) {
      while (W.specs.size < spec.n_agents + 1) await sleep(10);
      await sleep(50);
      h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "error", error: "E_BOARD" }));
      return 1;
    }
    while (!h.inbox.m.has("/stop")) await sleep(10);
    W.metasAtStop = h.inbox.order.filter((p) => p.startsWith("/agent-"));
    W.boardInbox = h.inbox;
    W.stopLast = h.inbox.order.at(-1) === "/stop";
    const metas = [];
    for (let i = 0; i < spec.n_agents; i++) metas.push(h.inbox.json(`/agent-${i}.meta.json`));
    const f = metas.findIndex((m) => m?.fatal);
    h.exportDev.put("/run/run.json", JSON.stringify({ run_id: spec.run_id, n_agents: spec.n_agents, ctf: CTF,
      ...(f >= 0 ? { fatal: { agent: f, status: metas[f].fatal.status } } : {}), params: { topology: "board-split" } }));
    for (const n of ["seed.json", "board.out", "board_events.jsonl"]) h.exportDev.put(`/run/${n}`, "placeholder\n");
    h.exportDev.put("/manifest.json", JSON.stringify({ v: 1, run_id: spec.run_id, agent_stopped: metas.map((m) => !!m?.agent_stopped) }));
    h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "done", ended: "finished" }));
    return 0;
  }

  async function agent(h, spec, keyHex, net) {
    const i = spec.agent_id, script = agentScript(i);
    h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "running", steps: 0 }));
    const relay = net.TCPSocket("127.0.0.1", spec.relay_host_port);  // held open like the relay's connection
    await relay.opened;
    let steps = JSON.stringify({ kind: "start" }) + "\n", fatal = null;
    h.exportDev.put(`/run/a${i}/steps.jsonl`, steps);
    for (let seq = 1; seq <= script.calls; seq++) {
      const payload = JSON.stringify({ agent: i, seq, body: { messages: [{ role: "user", content: "placeholder" }] } });
      h.outbox.put(`/a${i}-${seq}.req.json`, JSON.stringify({ v: 1, payload,
        mac: createHmac("sha256", Buffer.from(keyHex, "hex")).update(payload).digest("hex") }));
      let r;
      while (!(r = h.inbox.json(`/a${i}-${seq}.resp.json`))) await sleep(10);
      if (r.code === "E_ABORT" || r.code === "E_BUDGET") { fatal = { status: r.status }; break; }
      steps += JSON.stringify({ kind: "step", step: seq }) + "\n";
      h.exportDev.put(`/run/a${i}/steps.jsonl`, steps);
      if (script.shrink) {  // a transient shorter copy (export rebuild), then the full file again
        await sleep(120);
        h.exportDev.put(`/run/a${i}/steps.jsonl`, steps.slice(0, steps.indexOf("\n") + 1));
        await sleep(120);
        h.exportDev.put(`/run/a${i}/steps.jsonl`, steps);
      }
    }
    if (script.bigSteps) h.exportDev.put(`/run/a${i}/steps.jsonl`, steps + "x".repeat(script.bigSteps) + "\n");
    if (script.wedge) return new Promise(() => {});  // S2k: labctl never returns
    if (script.exitError) {
      h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "error", error: script.exitError }));
      return 1;
    }
    h.exportDev.put(`/run/cfg-a${i}.json`, "{}");
    h.exportDev.put(`/run/a${i}/harness.out`, "placeholder\n");
    h.exportDev.put(`/run/a${i}/agent-meta.json`, JSON.stringify({ v: 1, run_id: spec.run_id, agent: i, fatal,
      agent_stopped: true }));
    h.exportDev.put("/manifest.json", JSON.stringify({ v: 1, run_id: spec.run_id, agent_stopped: true, agent: i }));
    h.exportDev.put("/status.json", JSON.stringify({ v: 1, phase: "done", ended: fatal ? "fatal" : "finished" }));
    return 0;
  }

  globalThis.document = {
    createElement() {
      const listeners = {};
      const frame = {
        setAttribute() {}, addEventListener(ev, cb) { listeners[ev] = cb; },
        remove() { W.removed.push(frame.vm); },
        contentWindow: { __vmframe: { async boot({ vm, image, networkInterface }) {
          assert.equal(image, "img.ext2");
          frame.vm = vm;
          W.booted.push(vm);
          W.maxBooting = Math.max(W.maxBooting, ++W.booting);
          await sleep(bootDelay(vm));
          W.booting--;
          if (bootFails(vm)) throw new Error("E_PLACEHOLDER_BOOT");
          return makeVm(vm, networkInterface);
        } } },
      };
      frame.attach = () => setTimeout(() => listeners.load?.(), 1);
      return frame;
    },
    body: { append(f) { f.attach(); } },
  };
  return W;
}

// fetch stub: the two assets and the provider. provider(url, init) -> Response (default: always OK_BODY).
function stubFetch(provider = async () => json(200, OK_BODY)) {
  const stats = { provider: 0 };
  globalThis.fetch = async (url, init) => {
    if (url === "/vm-manifest.json") return json(200, { filename: "img.ext2" });
    if (url === "/data/seed.json") return new Response("{\"placeholder\":true}");
    stats.provider++;
    return provider(url, init);
  };
  return stats;
}
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
// Provider stubs: one that never answers until its call is aborted (Stop, deadline), and one that answers OK after
// ms unless aborted first.
const untilAbort = (url, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(init.signal.reason)));
const okAfter = (ms, init) => new Promise((ok, rej) => {
  const tm = setTimeout(() => ok(json(200, OK_BODY)), ms);
  init.signal.addEventListener("abort", () => { clearTimeout(tm); rej(init.signal.reason); });
});

function settings(nAgents, over = {}) {
  return { preset: "groq", iface: "forum", nAgents, provider: { baseUrl: "https://provider.test/v1", model: "m", apiKey: "sk-test" },
    harness: { budget: 40, temperature: 0.7, max_tokens: 2048, output_chars: 3000, run_timeout_s: 5400, model: "m" },
    wire: {}, caps: { calls: 100, tokens: 1e9 }, ...over };
}

// The stored zip's entry names -> bytes, read back with the page's own reader (CRC-checked).
const unzip = async (blob) => new Map((await readZip(blob)).map(({ name, data }) => [name, data]));
const zipJson = (files, runId, name) => JSON.parse(dec.decode(files.get(`${runId}/${name}`)));

test("N = 2: board then agents, shared ctf, per-VM keys, metas then stop, every frame removed, §3 zip", async () => {
  const W = world();
  const stats = stubFetch();
  const seen = { status: 0, records: [0, 0], calls: [0, 0], last: null };
  const s = createSwarm(settings(2), {
    onStatus: (st) => { seen.status++; seen.last = st; },
    onRecord: (i) => { seen.records[i]++; },
    onCall: (i) => { seen.calls[i]++; },
  }, { timing: TIMING });
  assert.match(s.runId, /^browser-forum-multi2-\d{14}-[0-9a-f]{4}$/);
  const out = await s.run();
  assert.deepEqual(out, { result: "done" });
  assert.deepEqual(W.booted, ["board", "a0", "a1"]);
  assert.deepEqual(W.removed.sort(), ["a0", "a1", "board"]);
  const b = W.specs.get("board"), a0 = W.specs.get("a0"), a1 = W.specs.get("a1");
  assert.deepEqual([b.spec.role, b.spec.n_agents, b.hasSeed], ["board", 2, true]);
  assert.deepEqual([a1.spec.role, a1.spec.agent_id, a1.spec.n_agents, a1.spec.relay_host_port, a1.hasSeed], ["agent", 1, 2, 9001, false]);
  assert.deepEqual(a0.spec.ctf, CTF);
  assert.deepEqual(a1.spec.ctf, CTF);
  assert.equal(new Set(W.keys.values()).size, 3, "one mailbox key per VM");
  assert.ok(![...W.specs.values()].some((x) => JSON.stringify(x.spec).includes("sk-test")), "the API key never reaches a VM");
  assert.deepEqual(W.metasAtStop, ["/agent-0.meta.json", "/agent-1.meta.json"]);
  assert.ok(W.stopLast);
  assert.equal(stats.provider, 4);
  assert.deepEqual(seen.records, [3, 3]);  // a start record + 2 steps each
  assert.deepEqual(seen.calls, [2, 2]);
  assert.equal(seen.last.phase, "done");
  assert.deepEqual(seen.last.agents.map((a) => [a.i, a.ended, a.steps, a.calls]), [[0, true, 2, 2], [1, true, 2, 2]]);
  // every relay connection the board accepted was ended by fabric.cut when its agent was removed
  assert.equal(W.boardConns.length, 2);
  assert.ok(W.boardConns.every((c) => c.eof));
  assert.equal(s.stats().fabric.open, 0);
  assert.equal(s.stats().frames, 0);
  const z = await s.zip();
  assert.equal(z.filename, `${s.runId}.zip`);
  const files = await unzip(z.blob);
  const names = [...files.keys()].map((n) => n.slice(s.runId.length + 1));
  assert.ok([...files.keys()].every((n) => n.startsWith(`${s.runId}/`)));
  for (const n of ["run.json", "seed.json", "board.out", "board_events.jsonl", "cfg-a0.json", "a0/steps.jsonl",
    "a0/agent-meta.json", "a1/steps.jsonl", "a1/harness.out", "manifest.json"]) assert.ok(names.includes(n), n);
  assert.ok(!names.some((n) => /board-ready|status\.json/.test(n)));
  const manifest = zipJson(files, s.runId, "manifest.json");
  assert.deepEqual([manifest.v, manifest.run_id, manifest.topology, manifest.agent_stopped], [1, s.runId, "board-split", [true, true]]);
  assert.equal(manifest.files.length, names.length - 1);
  const live = await s.liveFiles();
  assert.ok(live.has("run.json") && live.has("a0/steps.jsonl") && live.has("board_events.jsonl"));
});

test("the call cap is shared by every agent's broker; a fatal agent names the zip aborted", async () => {
  world({ agentScript: () => ({ calls: 4 }) });
  const stats = stubFetch();
  const s = createSwarm(settings(3, { caps: { calls: 5, tokens: 1e9 } }), {}, { timing: TIMING });
  const out = await s.run();
  assert.equal(out.result, "done");
  assert.equal(stats.provider, 5);
  assert.equal(s.stats().calls, 5);
  const z = await s.zip();
  assert.equal(z.filename, `${s.runId}-aborted-402.zip`);
});

test("Stop aborts every broker (E_ABORT); the agents export and the run ends stopped", async () => {
  const W = world({ agentScript: () => ({ calls: 50 }) });
  stubFetch(untilAbort);
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const p = s.run();
  while (W.booted.length < 3 || !W.specs.has("a1")) await sleep(10);
  await sleep(100);
  s.stop();
  const out = await p;
  assert.deepEqual(out, { result: "stopped" });
  const z = await s.zip();
  assert.equal(z.filename, `${s.runId}-aborted-402.zip`);
  assert.deepEqual(W.removed.sort(), ["a0", "a1", "board"]);
});

test("an agent whose labctl never returns after Stop is removed after the grace; the zip is incomplete", async () => {
  const W = world({ agentScript: (i) => ({ calls: i === 1 ? 50 : 1, wedge: i === 1 }) });
  stubFetch((url, init) => (init.signal && JSON.parse(init.body).messages ? okAfter(30, init) : json(200, OK_BODY)));
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const p = s.run();
  while (!W.specs.has("a1")) await sleep(10);
  await sleep(150);
  s.stop();
  const out = await p;
  assert.equal(out.result, "stopped");
  assert.ok(W.removed.includes("a1"));
  assert.ok(W.boardConns.every((c) => c.eof), "the wedged agent's relay connection was cut");
  const z = await s.zip();
  assert.equal(z.filename, `${s.runId}-incomplete.zip`);
  const files = await unzip(z.blob);
  const manifest = zipJson(files, s.runId, "manifest.json");
  assert.deepEqual(manifest.agent_stopped, [true, false]);
  assert.ok(files.has(`${s.runId}/a1/steps.jsonl`), "the wedged agent's live steps are kept");
  assert.ok(!files.has(`${s.runId}/cfg-a1.json`));
});

test("an agent's labctl error makes the run an error, but the board still exports", async () => {
  world({ agentScript: (i) => ({ calls: 1, exitError: i === 0 ? "E_AGENT_ALIVE" : null }) });
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const out = await s.run();
  assert.deepEqual([out.result, out.error, out.detail], ["error", "E_AGENT_ALIVE", "a0:E_AGENT_ALIVE"]);
  assert.equal((await s.zip()).filename, `${s.runId}-incomplete.zip`);
});

test("a board that fails before board-ready ends the run with its error and boots no agent", async () => {
  const W = world({ boardFails: true });
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const out = await s.run();
  assert.deepEqual([out.result, out.error], ["error", "E_BOARD"]);
  assert.deepEqual(W.booted, ["board"]);
  assert.deepEqual(W.removed, ["board"]);
  assert.match((await s.zip()).filename, /-incomplete\.zip$/);
});

test("the page deadline stops the swarm like Stop", async () => {
  world({ agentScript: () => ({ calls: 50 }) });
  stubFetch(untilAbort);
  const st = settings(1);
  st.harness = { ...st.harness, run_timeout_s: 0 };
  const s = createSwarm(st, {}, { timing: { ...TIMING, deadlineMarginMs: 300 } });
  assert.match(s.runId, /-solo-/);
  const out = await s.run();
  assert.deepEqual(out, { result: "stopped", detail: "deadline" });
});

test("Stop before the run starts boots nothing and ends the run stopped", async () => {
  const W = world();
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  s.stop();
  const out = await s.run();
  assert.equal(out.result, "stopped");
  assert.deepEqual(W.booted, []);
  assert.equal(s.stats().frames, 0);
});

test("Stop while the board boots abandons the boot: no labctl, no agents, ended at once", async () => {
  const W = world({ bootDelayMs: 1500 });
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const p = s.run();
  while (!W.booted.length) await sleep(5);
  const t = performance.now();
  s.stop();
  const out = await p;
  assert.equal(out.result, "stopped");
  assert.ok(performance.now() - t < 500, "did not wait for the boot");
  assert.deepEqual(W.booted, ["board"]);
  assert.equal(W.specs.size, 0, "no labctl started");
  assert.deepEqual(W.removed, ["board"]);
});

test("Stop after the board's labctl started but before board-ready: stop file, short board end", async () => {
  const W = world({ boardReadyDelayMs: 300 });
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: { ...TIMING, boardEndMs: 60000, boardEarlyEndMs: 2000 } });
  const p = s.run();
  while (!W.specs.has("board")) await sleep(5);
  s.stop();
  const out = await p;
  assert.equal(out.result, "stopped");
  assert.deepEqual(W.booted, ["board"]);
  assert.ok(W.stopLast);
});

test("a slow board export does not build a poll backlog (ticks are skipped, the end is not delayed)", async () => {
  const W = world({ boardReadDelayMs: 150, agentScript: () => ({ calls: 2 }) });
  stubFetch();
  const s = createSwarm(settings(1), {}, { timing: { ...TIMING, pollMs: 20 } });
  const t = performance.now();
  assert.deepEqual(await s.run(), { result: "done" });
  const ms = performance.now() - t;
  assert.ok(ms < 6000, `ended in ${Math.round(ms)} ms`);
  // reads are bounded by wall time (one in flight per caller), not by the number of ticks
  assert.ok(W.vms.get("board").exportDev.reads <= ms / 150 * 3 + 20);
});

test("an agent-written status error that is not a fixed code is replaced by E_STATUS", async () => {
  world({ agentScript: (i) => ({ calls: 1, exitError: i === 0 ? "placeholder text, not a code" : null }) });
  stubFetch();
  const s = createSwarm(settings(1), {}, { timing: TIMING });
  const out = await s.run();
  assert.deepEqual([out.result, out.error, out.detail], ["error", "E_STATUS", "a0:E_STATUS"]);
});

test("an agent file over its read cap is not read; the agent ends E_EXPORT_SIZE", async () => {
  world({ agentScript: () => ({ calls: 1, bigSteps: 3 << 20 }) });
  stubFetch();
  const s = createSwarm(settings(1), {}, { timing: TIMING, readCaps: { agentLog: 1 << 20 } });
  const out = await s.run();
  assert.deepEqual([out.result, out.error], ["error", "E_EXPORT_SIZE"]);
  const files = await unzip((await s.zip()).blob);
  assert.ok(![...files.values()].some((b) => b.length > 1 << 20), "no oversize file in the zip");
});

test("each agent's deadline runs from its own labctl start; only the late agent is aborted", async () => {
  const W = world({ agentScript: (i) => ({ calls: i === 0 ? 1 : 50 }), bootDelay: (vm) => (vm === "a1" ? 300 : 0) });
  stubFetch((url, init) => (JSON.parse(init.body).messages.length && init.signal ? okAfter(50, init) : json(200, OK_BODY)));
  const st = settings(2);
  st.harness = { ...st.harness, run_timeout_s: 0 };
  const s = createSwarm(st, {}, { timing: { ...TIMING, deadlineMarginMs: 250 } });
  const out = await s.run();
  assert.deepEqual(out, { result: "stopped", detail: "deadline" });
  const files = await unzip((await s.zip()).blob);
  const meta = (i) => zipJson(files, s.runId, `a${i}/agent-meta.json`);
  assert.equal(meta(0).fatal, null, "a0 ended on its own");
  assert.equal(meta(1).fatal?.status, 402, "a1 was aborted at its deadline");
  assert.ok(W.specs.has("a1"));
});

test("the agents' deadlines are capped by the board's own deadline", async () => {
  world({ agentScript: () => ({ calls: 50 }) });
  stubFetch(untilAbort);
  const st = settings(1);
  st.harness = { ...st.harness, run_timeout_s: 0 };
  const s = createSwarm(st, {}, { timing: { ...TIMING, deadlineMarginMs: 600000, boardMarginMs: 400, boardReserveMs: 100 } });
  const t = performance.now();
  const out = await s.run();
  assert.deepEqual(out, { result: "stopped", detail: "deadline" });
  assert.ok(performance.now() - t < 5000);
});

test("Stop while an agent boots abandons that boot (skipped, not lost); the running agents export", async () => {
  const W = world({ agentScript: () => ({ calls: 50 }), bootDelay: (vm) => (vm === "a1" ? 3000 : 0) });
  stubFetch(untilAbort);
  const seen = { last: null };
  const s = createSwarm(settings(2), { onStatus: (st) => { seen.last = st; } }, { timing: TIMING });
  const p = s.run();
  while (!W.booted.includes("a1")) await sleep(5);
  const t = performance.now();
  s.stop();
  const out = await p;
  assert.deepEqual(out, { result: "stopped" });
  assert.ok(performance.now() - t < 2500, "did not wait for a1's boot");
  assert.ok(!W.specs.has("a1"));
  assert.ok(W.removed.includes("a1"));
  assert.equal(seen.last.agents[1].phase, "skipped");
  assert.equal(W.boardInbox.m.has("/agent-1.meta.json"), false, "no page meta for a skipped agent");
});

test("Stop during the ending phase does not turn a finished run into stopped", async () => {
  world();
  stubFetch();
  let s;
  s = createSwarm(settings(1), { onStatus: (st) => { if (st.phase === "ending") s.stop(); } }, { timing: TIMING });
  assert.deepEqual(await s.run(), { result: "done" });
});

test("dispose() after run settles (no IndexedDB / locks here: nothing to delete)", async () => {
  world();
  stubFetch();
  const s = createSwarm(settings(1), {}, { timing: TIMING });
  assert.equal(await s.dispose(), 0);
  await s.run();
  assert.equal(await s.dispose(), 0);
});

test("createSwarm validates nAgents", () => {
  assert.throws(() => createSwarm(settings(0)), /E_N_AGENTS/);
  assert.throws(() => createSwarm(settings(5)), /E_N_AGENTS/);
});

test("the readFile test hook answers export reads from page memory once a VM's frame is gone", async () => {
  world();
  stubFetch();
  const s = createSwarm(settings(1), {}, { timing: TIMING });
  await s.run();
  assert.equal(JSON.parse(await s.readFile("a0", "export", "/status.json")).phase, "done");
  assert.equal(JSON.parse(await s.readFile("board", "export", "/manifest.json")).v, 1);
  assert.ok((await s.readFile("a0", "export", "/run/a0/steps.jsonl")).length > 0);
  assert.equal(await s.readFile("a0", "outbox", "/a0-1.req.json"), null);
  assert.equal(await s.readFile("a7", "export", "/status.json"), null);
  assert.equal(typeof s.consoleTail("board"), "string");
});

test("agent VMs boot one at a time (the next boot waits for the previous one)", async () => {
  const W = world({ bootDelayMs: 80 });
  stubFetch();
  const s = createSwarm(settings(3), {}, { timing: TIMING });
  assert.deepEqual(await s.run(), { result: "done" });
  assert.deepEqual(W.booted, ["board", "a0", "a1", "a2"]);
  assert.equal(W.maxBooting, 1);
});

test("a board whose labctl dies while agents run stops the swarm; the run is the board's error", async () => {
  const W = world({ boardDies: true, agentScript: () => ({ calls: 50 }) });
  stubFetch(untilAbort);
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const t = performance.now();
  const out = await s.run();
  assert.deepEqual([out.result, out.error, out.detail], ["error", "E_BOARD", "board"]);
  assert.ok(performance.now() - t < 5000, "ended without waiting for the agents' run_timeout");
  assert.deepEqual(W.removed.sort(), ["a0", "a1", "board"]);
  assert.match((await s.zip()).filename, /-incomplete\.zip$/);
});

test("a transiently shorter live steps.jsonl does not replay records", async () => {
  world({ agentScript: () => ({ calls: 2, shrink: true }) });
  stubFetch();
  const records = [0];
  const s = createSwarm(settings(1), { onRecord: (i) => { records[i]++; } }, { timing: TIMING });
  assert.deepEqual(await s.run(), { result: "done" });
  assert.deepEqual(records, [3]);
});

test("an agent VM that fails to boot is removed at once; the others run and the run is an error", async () => {
  const W = world({ bootFails: (vm) => vm === "a0" });
  stubFetch();
  const s = createSwarm(settings(2), {}, { timing: TIMING });
  const out = await s.run();
  assert.deepEqual([out.result, out.error], ["error", "E_PLACEHOLDER_BOOT"]);
  assert.deepEqual(W.booted, ["board", "a0", "a1"]);
  assert.equal(W.removed[0], "a0");
  assert.ok(W.specs.has("a1") && !W.specs.has("a0"));
  assert.equal(s.stats().frames, 0);
  // the page-made meta stub (protocol.js pageAgentMeta) carries the page-side error code to the board
  const stub = W.boardInbox.json("/agent-0.meta.json");
  assert.deepEqual([stub.v, stub.run_id, stub.agent, stub.page, stub.error, stub.timed_out, stub.agent_stopped, stub.fatal],
    [1, s.runId, 0, true, "E_PLACEHOLDER_BOOT", true, false, null]);
  assert.ok(W.metasAtStop.includes("/agent-0.meta.json"));
});

test("pageAgentMeta has the exact shape lab/tests/test_labctl.py feeds labctl's meta_ok", () => {
  assert.deepEqual(pageAgentMeta("run-x-0000", 1, "E_AGENT_LOST", 1.5, 2.5), { v: 1, run_id: "run-x-0000", agent: 1, page: true,
    timed_out: true, agent_stopped: false, solved: false, solve_step: null, started: 1.5, finished: 2.5, fatal: null,
    stop_reason: null, error: "E_AGENT_LOST", secret_exposure: null, secret_exposure_logs: null });
  assert.equal(pageAgentMeta("r", 0, "E_EXIT_1", 0, 0).error, "E_PAGE", "labctl codes have no digits");
});
