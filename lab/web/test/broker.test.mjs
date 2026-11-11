// Unit tests for public/js/broker.js (PROTOCOL.md §3) with Map-backed fake devices and a stubbed fetch.
// Message content is a neutral placeholder.
//   cd lab/web && node --test test/broker.test.mjs
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac, randomBytes } from "node:crypto";
import { MAX_REQUEST_BYTES, createBudget, startBroker } from "../public/js/broker.js";
import { hmacKey, replyFile, requestFile } from "../public/js/protocol.js";
import { failOnUnhandledRejections, sleep } from "./helpers.mjs";

failOnUnhandledRejections();

const realFetch = globalThis.fetch;
const open = [];  // brokers to close even when a test fails, so the process can exit
afterEach(async () => {
  globalThis.fetch = realFetch;
  await Promise.all(open.splice(0).map((b) => b.close()));
});

async function until(fn, ms = 3000) {
  const t0 = Date.now();
  while (!fn()) {
    if (Date.now() - t0 > ms) throw new Error("timed out");
    await sleep(10);
  }
}

const OK_BODY = { choices: [{ message: { role: "assistant", content: "placeholder", extra: 1 }, finish_reason: "stop" }],
  usage: { prompt_tokens: 10, completion_tokens: 5 } };
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// One broker over fake devices. `agent` is its agent id (file names and payload.agent, §3); `budget` a shared
// createBudget() for the N-broker tests.
async function setup({ caps = { calls: 100, tokens: 1e9 }, timeoutMs, failWrites = 0, agent: id = 0, budget } = {}) {
  const keyHex = randomBytes(32).toString("hex");
  const out = new Map(), inn = new Map();
  const outbox = { async readFileAsBlob(p) { if (!out.has(p)) throw new Error("ENOENT"); return new Blob([out.get(p)]); } };
  const inbox = { async writeFile(p, text) { if (failWrites-- > 0) throw new Error("EIO"); inn.set(p, text); } };
  const calls = [], ignored = [];
  const broker = startBroker({ outbox, inbox, key: await hmacKey(keyHex), agent: id, timeoutMs, budget,
    provider: { baseUrl: "https://provider.test/v1", model: "page-model", apiKey: "sk-test" },
    harness: { max_tokens: 2048 }, caps, onCall: (c) => calls.push(c), onIgnored: (s) => ignored.push(s) });
  open.push(broker);
  const send = (seq, { agent = id, mac, v = 1, fileSeq = seq } = {}) => {
    const payload = JSON.stringify({ agent, seq, body: { model: "guest-model", messages: [{ role: "user", content: "x" }],
      temperature: 9, max_tokens: 99999 } });
    out.set(requestFile(id, fileSeq), JSON.stringify({ v, payload,
      mac: mac ?? createHmac("sha256", Buffer.from(keyHex, "hex")).update(payload).digest("hex") }));
  };
  const payloadFor = (seq) => JSON.stringify({ agent: id, seq, body: { messages: [{ role: "user", content: "x" }] } });
  const sendRaw = (seq, req) => out.set(requestFile(id, seq), JSON.stringify(req));
  const reply = (seq) => (inn.has(replyFile(id, seq)) ? JSON.parse(inn.get(replyFile(id, seq))) : null);
  return { broker, send, sendRaw, payloadFor, reply, calls, ignored, inn };
}

test("a signed request is answered with the page's model and clamped parameters", async () => {
  let seen;
  globalThis.fetch = async (url, init) => { seen = { url, init, body: JSON.parse(init.body) }; return json(200, OK_BODY); };
  const t = await setup();
  t.send(1);
  await until(() => t.reply(1));
  const r = t.reply(1);
  assert.equal(r.status, 200);
  assert.equal(r.seq, 1);
  assert.deepEqual(Object.keys(r.body.choices[0].message).sort(), ["content", "role"]);
  assert.equal(seen.url, "https://provider.test/v1/chat/completions");
  assert.deepEqual(Object.keys(seen.init.headers).sort(), ["Authorization", "Content-Type"]);
  assert.equal(seen.body.model, "page-model");
  assert.equal(seen.body.temperature, 2);
  assert.equal(seen.body.max_tokens, 2048);
  assert.equal(t.calls.at(-1).tokens, 15);
  assert.equal(t.broker.stats().next, 2);
  await t.broker.close();
});

test("a bad MAC or agent is ignored and reported once per seq; a valid rewrite is then accepted", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const t = await setup();
  t.send(1, { mac: "00".repeat(32) });
  await sleep(400);
  assert.deepEqual(t.ignored, [1]);
  assert.equal(t.reply(1), null);
  t.send(1, { agent: 1 });
  await sleep(200);
  assert.deepEqual(t.ignored, [1]);
  t.send(2, { fileSeq: 2, v: 2 });
  await sleep(200);
  assert.deepEqual(t.ignored, [1, 2]);
  t.send(1);
  await until(() => t.reply(1));
  assert.equal(t.reply(1).status, 200);
  await t.broker.close();
});

test("requests without a string payload and a hex MAC are ignored and never reach the provider", async () => {
  let fetches = 0;
  globalThis.fetch = async () => { fetches++; return json(200, OK_BODY); };
  const t = await setup();
  const forged = [
    { v: 1, payload: [t.payloadFor(1)], mac: "" },  // JSON.parse would stringify the array
    { v: 1, payload: JSON.parse(t.payloadFor(1)), mac: "" },
    { v: 1, payload: t.payloadFor(1) },
    { v: 1, payload: t.payloadFor(1), mac: "" },
    { v: 1, payload: t.payloadFor(1), mac: "0".repeat(63) },
    [{ v: 1, payload: t.payloadFor(1), mac: "" }],
    null,
  ];
  for (const req of forged) {
    t.sendRaw(1, req);
    await sleep(200);
    assert.equal(t.reply(1), null, JSON.stringify(req)?.slice(0, 40));
  }
  assert.deepEqual([fetches, t.ignored, t.broker.stats().next], [0, [1], 1]);
  await t.broker.close();
});

test("a payload seq that differs from the file's seq is ignored", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const t = await setup();
  t.send(5, { fileSeq: 1 });
  await sleep(300);
  assert.deepEqual(t.ignored, [1]);
  assert.equal(t.reply(1), null);
  await t.broker.close();
});

test("seq+1 is accepted when the harness gave up on a request; seq+2 waits", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const t = await setup();
  t.send(2);
  await until(() => t.reply(2));
  t.send(5);
  await sleep(300);
  assert.equal(t.reply(5), null);
  t.send(4);
  await until(() => t.reply(5));  // 4 is next+1; then 5 is next
  assert.equal(t.broker.stats().next, 6);
  await t.broker.close();
});

test("caps answer E_BUDGET without calling the provider", async () => {
  let n = 0;
  globalThis.fetch = async () => { n++; return json(200, OK_BODY); };
  const t = await setup({ caps: { calls: 1, tokens: 1e9 } });
  t.send(1);
  await until(() => t.reply(1));
  t.send(2);
  await until(() => t.reply(2));
  assert.deepEqual([t.reply(2).status, t.reply(2).code, n], [402, "E_BUDGET", 1]);
  const t2 = await setup({ caps: { calls: 100, tokens: 15 } });
  t2.send(1);
  await until(() => t2.reply(1));
  t2.send(2);
  await until(() => t2.reply(2));
  assert.equal(t2.reply(2).code, "E_BUDGET");
  await t.broker.close();
  await t2.broker.close();
});

test("stop() aborts the in-flight call and answers every later request with E_ABORT", async () => {
  let n = 0;
  globalThis.fetch = (url, init) => { n++; return new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason)); }); };
  const t = await setup();
  t.send(1);
  await until(() => n === 1);
  t.broker.stop();
  await until(() => t.reply(1));
  assert.deepEqual([t.reply(1).status, t.reply(1).code], [402, "E_ABORT"]);
  t.send(2);
  await until(() => t.reply(2));
  assert.deepEqual([t.reply(2).status, t.reply(2).code, n], [402, "E_ABORT", 1]);
  await t.broker.close();
});

test("a provider timeout answers E_UPSTREAM", async () => {
  globalThis.fetch = (url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason)); });
  const t = await setup({ timeoutMs: 50 });
  t.send(1);
  await until(() => t.reply(1));
  assert.deepEqual([t.reply(1).status, t.reply(1).code], [502, "E_UPSTREAM"]);
  await t.broker.close();
});

test("provider statuses map to reply codes", async () => {
  const cases = [[401, 401, "E_AUTH"], [403, 401, "E_AUTH"], [402, 402, "E_BUDGET"], [429, 429, "E_RATE"],
    [400, 400, "E_BAD_REQUEST"], [404, 400, "E_BAD_REQUEST"], [422, 400, "E_BAD_REQUEST"], [500, 502, "E_UPSTREAM"]];
  let i = 0;
  globalThis.fetch = async () => json(cases[i][0], { error: {} });
  const t = await setup();
  for (; i < cases.length; i++) {
    t.send(i + 1);
    await until(() => t.reply(i + 1));
    assert.deepEqual([t.reply(i + 1).status, t.reply(i + 1).code], cases[i].slice(1), `provider ${cases[i][0]}`);
  }
  globalThis.fetch = async () => new Response("not json", { status: 200 });
  t.send(i + 1);
  await until(() => t.reply(i + 1));
  assert.equal(t.reply(i + 1).code, "E_UPSTREAM");
  globalThis.fetch = async () => json(200, { choices: [] });
  t.send(i + 2);
  await until(() => t.reply(i + 2));
  assert.equal(t.reply(i + 2).code, "E_UPSTREAM");
  assert.equal(t.calls.length, i + 2);
  await t.broker.close();
});

test("rejected fetches answer E_OPAQUE 503, then 401 from the third in a row", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  const t = await setup();
  const got = [];
  for (let s = 1; s <= 4; s++) {
    t.send(s);
    await until(() => t.reply(s));
    got.push(`${t.reply(s).status} ${t.reply(s).code}`);
  }
  assert.deepEqual(got, ["503 E_OPAQUE", "503 E_OPAQUE", "401 E_OPAQUE", "401 E_OPAQUE"]);
  globalThis.fetch = async () => json(200, OK_BODY);
  t.send(5);
  await until(() => t.reply(5));
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  t.send(6);
  await until(() => t.reply(6));
  assert.equal(t.reply(6).status, 503);
  await t.broker.close();
});

test("a loop error is reported and does not kill the broker", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const t = await setup({ failWrites: 1 });
  t.send(1);
  await until(() => t.calls.some((c) => c.code === "E_BROKER"));
  t.send(2);
  await until(() => t.reply(2));
  assert.equal(t.reply(2).status, 200);
  await t.broker.close();
});

// ---- swarm: N brokers (one per agent VM) on one shared budget (docs/browser-swarm.md §6) ----

test("N brokers on one budget never pass the aggregate call cap", async () => {
  let n = 0;
  globalThis.fetch = async () => { n++; await sleep(20); return json(200, OK_BODY); };
  const budget = createBudget({ calls: 5, tokens: 1e9 });
  const ts = await Promise.all([0, 1, 2].map((agent) => setup({ agent, budget })));
  for (let seq = 1; seq <= 3; seq++) {
    for (const t of ts) t.send(seq);
    await until(() => ts.every((t) => t.reply(seq)));
  }
  const codes = ts.flatMap((t) => [1, 2, 3].map((s) => t.reply(s).code || "ok"));
  assert.equal(codes.filter((c) => c === "ok").length, 5);
  assert.equal(codes.filter((c) => c === "E_BUDGET").length, 4);
  assert.equal(n, 5);
  assert.equal(budget.calls, 5);
  assert.equal(ts.reduce((k, t) => k + t.broker.stats().calls, 0), 5);
  const last = ts.flatMap((t) => t.calls).at(-1);
  assert.equal(last.total.calls, 5);
});

test("the token cap is shared: once the swarm's total reaches it every broker answers E_BUDGET", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);  // 15 tokens per call
  const budget = createBudget({ calls: 100, tokens: 30 });
  const [a, b] = await Promise.all([0, 1].map((agent) => setup({ agent, budget })));
  a.send(1);
  await until(() => a.reply(1));
  b.send(1);
  await until(() => b.reply(1));
  assert.equal(budget.tokens, 30);
  a.send(2);
  b.send(2);
  await until(() => a.reply(2) && b.reply(2));
  assert.deepEqual([a.reply(2).code, b.reply(2).code], ["E_BUDGET", "E_BUDGET"]);
  assert.deepEqual([a.broker.stats().tokens, b.broker.stats().tokens], [15, 15]);
  assert.deepEqual(b.calls.at(-1).total, { calls: 2, tokens: 30 });
});

test("budget.stop() is one Stop for every broker: in-flight calls abort, later requests get E_ABORT", async () => {
  let n = 0;
  globalThis.fetch = (url, init) => { n++; return new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason)); }); };
  const budget = createBudget({ calls: 100, tokens: 1e9 });
  const ts = await Promise.all([0, 1].map((agent) => setup({ agent, budget })));
  for (const t of ts) t.send(1);
  await until(() => n === 2);
  budget.stop();
  await until(() => ts.every((t) => t.reply(1)));
  for (const t of ts) assert.deepEqual([t.reply(1).status, t.reply(1).code], [402, "E_ABORT"]);
  for (const t of ts) t.send(2);
  await until(() => ts.every((t) => t.reply(2)));
  for (const t of ts) assert.equal(t.reply(2).code, "E_ABORT");
  assert.equal(n, 2);
  assert.ok(budget.stopped());
});

test("one broker's stop() leaves the others on the budget running", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const budget = createBudget({ calls: 100, tokens: 1e9 });
  const [a, b] = await Promise.all([0, 1].map((agent) => setup({ agent, budget })));
  a.broker.stop();
  a.send(1);
  b.send(1);
  await until(() => a.reply(1) && b.reply(1));
  assert.deepEqual([a.reply(1).code, b.reply(1).status], ["E_ABORT", 200]);
  assert.ok(!budget.stopped());
});

test("a request whose payload.agent is another agent's id is ignored and costs nothing", async () => {
  let n = 0;
  globalThis.fetch = async () => { n++; return json(200, OK_BODY); };
  const budget = createBudget({ calls: 100, tokens: 1e9 });
  const t = await setup({ agent: 2, budget });
  t.send(1, { agent: 0 });  // signed with the right key, in a2's file, but claims agent 0
  await sleep(400);
  t.send(2, { agent: 3 });
  await sleep(300);
  assert.deepEqual(t.ignored, [1, 2]);
  assert.equal(t.reply(1), null);
  assert.deepEqual([n, budget.calls], [0, 0]);
  t.send(1);  // the genuine request for a2 is then answered
  await until(() => t.reply(1));
  assert.deepEqual([t.reply(1).status, n, budget.calls], [200, 1, 1]);
});

test("a broker without a budget keeps its own caps (single-broker API)", async () => {
  globalThis.fetch = async () => json(200, OK_BODY);
  const a = await setup({ caps: { calls: 1, tokens: 1e9 } });
  const b = await setup({ caps: { calls: 1, tokens: 1e9 } });
  a.send(1);
  b.send(1);
  await until(() => a.reply(1) && b.reply(1));
  assert.deepEqual([a.reply(1).status, b.reply(1).status], [200, 200]);
});

test("createBudget refuses caps that are not positive integers (E_CAPS)", () => {
  for (const caps of [undefined, {}, { calls: 5 }, { calls: 0, tokens: 10 }, { calls: 5, tokens: Infinity }, { calls: 1.5, tokens: 10 }])
    assert.throws(() => createBudget(caps), /E_CAPS/);
  assert.equal(createBudget({ calls: 5, tokens: 10 }).caps.calls, 5);
});

test("a request file over MAX_REQUEST_BYTES is ignored unread", async () => {
  let fetches = 0;
  globalThis.fetch = async () => { fetches++; return json(200, OK_BODY); };
  const t = await setup();
  t.sendRaw(1, { v: 1, payload: "x".repeat(MAX_REQUEST_BYTES + 1), mac: "0".repeat(64) });
  await until(() => t.ignored.includes(1));
  assert.equal(t.reply(1), null);
  assert.equal(fetches, 0);
  t.send(2);
  await until(() => t.reply(2));
  assert.equal(t.reply(2).status, 200);
});
