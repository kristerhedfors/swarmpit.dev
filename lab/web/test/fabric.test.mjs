// Unit tests for public/js/fabric.js (docs/browser-swarm.md §1, PROTOCOL.md §7): per-VM private loopnets,
// explicit board-split routes, and cut(). Drives sockets the way CheerpX does (locks held, close().catch()).
// Payloads are neutral placeholder bytes.
//   cd lab/web && node --test test/fabric.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createFabric, swarmRoutes } from "../public/js/fabric.js";
import { swarmVms } from "../public/js/protocol.js";
import { failOnUnhandledRejections } from "./helpers.mjs";

failOnUnhandledRejections();

const within = (p, ms) => Promise.race([p.then(() => true, () => true), new Promise((r) => setTimeout(() => r(false), ms))]);
const rejects = (p) => p.then(() => false, () => true);
const NAMES = ["board", "a0", "a1"];

async function listen(net, port) {
  const srv = net.TCPServerSocket("0.0.0.0", { localPort: port });
  const { readable } = await srv.opened;
  return { srv, port, accepts: readable.getReader() };
}

// connect from `net` to 127.0.0.1:port, accepted on listener `l`: both ends' stream objects
async function connect(net, l, host = "127.0.0.1") {
  const client = net.TCPSocket(host, l.port);
  const c = await client.opened;
  const server = (await l.accepts.read()).value;
  const s = await server.opened;
  return { client, server, c, s, cw: c.writable.getWriter(), cr: c.readable.getReader(),
    sw: s.writable.getWriter(), sr: s.readable.getReader() };
}

function swarm(n = 2, onEvent) {
  return createFabric({ names: swarmVms(n), routes: swarmRoutes(n), onEvent });
}

test("swarmRoutes maps a{i} to board port 9000+i", () => {
  assert.deepEqual(swarmRoutes(3), [{ from: "a0", port: 9000, to: "board" }, { from: "a1", port: 9001, to: "board" },
    { from: "a2", port: 9002, to: "board" }]);
});

test("a routed connect reaches the board listener; bytes flow both ways", async () => {
  const f = swarm();
  const board = f.iface("board"), a1 = f.iface("a1");
  const l = await listen(board, 9001);
  const k = await connect(a1, l, "127.0.0.1");
  await k.cw.write(new Uint8Array([1, 2, 3]));
  assert.deepEqual([...(await k.sr.read()).value], [1, 2, 3]);
  await k.sw.write(new Uint8Array([4]));
  assert.deepEqual([...(await k.cr.read()).value], [4]);
  assert.equal(f.stats().crossConnects, 1);
  assert.equal(f.stats().open, 1);
  await k.client.close();
  assert.ok(await within(k.server.closed, 100));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(f.stats().open, 0);
  await l.srv.close();
});

test("only the agent's own port is routed; other board ports and other VMs are unreachable", async () => {
  const f = swarm();
  const board = f.iface("board"), a0 = f.iface("a0"), a1 = f.iface("a1");
  await listen(board, 9000);
  await listen(board, 9001);
  await listen(board, 31337);
  await listen(a1, 7000);
  assert.ok(await rejects(a0.TCPSocket("127.0.0.1", 9001).opened), "a0 -> board:9001");
  assert.ok(await rejects(a0.TCPSocket("127.0.0.1", 31337).opened), "a0 -> board:31337");
  assert.ok(await rejects(a0.TCPSocket("127.0.0.1", 7000).opened), "a0 -> a1:7000");
  assert.ok(await rejects(board.TCPSocket("127.0.0.1", 7000).opened), "board -> a1:7000");
  assert.ok(await rejects(a0.TCPSocket("10.0.0.1", 9000).opened), "non-loopback");
  assert.ok(await rejects(a0.UDPSocket().opened), "udp");
  assert.ok(!(await rejects(a0.TCPSocket("127.0.0.1", 9000).opened)), "a0 -> board:9000 (its route)");
});

test("each VM has a private port space; a routed port cannot be shadowed by the source VM", async () => {
  const f = swarm();
  const board = f.iface("board"), a0 = f.iface("a0"), a1 = f.iface("a1");
  const la0 = await listen(a0, 8080), la1 = await listen(a1, 8080);  // same port in two VMs: no conflict
  const k0 = await connect(a0, la0), k1 = await connect(a1, la1);
  assert.ok(k0 && k1);
  const shadow = await listen(a0, 9000);  // a root agent listens on its own route port
  const lb = await listen(board, 9000);
  const client = a0.TCPSocket("127.0.0.1", 9000);
  await client.opened;
  const got = await Promise.race([lb.accepts.read().then(() => "board"), shadow.accepts.read().then(() => "shadow")]);
  assert.equal(got, "board");
  const lbo = await listen(board, 5000);
  const self = await connect(board, lbo);  // the board's own loopback still works
  assert.ok(self);
});

test("cut(a{i}) ends its open cross-VM connections on both ends and refuses later routes", async () => {
  const events = [];
  const f = swarm(2, (e) => events.push(e));
  const board = f.iface("board"), a0 = f.iface("a0"), a1 = f.iface("a1");
  const l0 = await listen(board, 9000), l1 = await listen(board, 9001);
  const k0 = await connect(a0, l0), k0b = await connect(a0, l0), k1 = await connect(a1, l1);
  assert.equal(f.stats().open, 3);
  // a pending board read and a pending agent read both settle with EOF
  const boardRead = k0.sr.read(), agentRead = k0.cr.read();
  await f.cut("a0");
  assert.equal((await boardRead).done, true, "board end reads EOF");
  assert.equal((await agentRead).done, true, "agent end reads EOF");
  assert.equal((await k0b.sr.read()).done, true);
  assert.ok(await within(k0.server.closed, 100), "board socket closed");
  assert.ok(await within(k0.client.closed, 100), "agent socket closed");
  await k0.sw.write(new Uint8Array(8));  // dropped, no throw
  // a1 is unaffected
  await k1.cw.write(new Uint8Array([9]));
  assert.deepEqual([...(await k1.sr.read()).value], [9]);
  assert.equal(f.stats().open, 1);
  assert.equal(f.stats().cutClosed, 2);
  // later routes from the cut VM fail fast, and so do its private connects
  assert.ok(await rejects(a0.TCPSocket("127.0.0.1", 9000).opened));
  assert.ok(await rejects(a0.TCPSocket("127.0.0.1", 1234).opened));
  assert.ok(f.isCut("a0") && !f.isCut("a1"));
  assert.ok(events.some((e) => e.vm === "a0" && e.ev === "cut" && e.port === 2));
  await f.cut("a0");  // idempotent
});

test("cut(board) ends every agent's connection to it and refuses routes into it", async () => {
  const f = swarm();
  const board = f.iface("board"), a0 = f.iface("a0"), a1 = f.iface("a1");
  const l0 = await listen(board, 9000), l1 = await listen(board, 9001);
  const k0 = await connect(a0, l0), k1 = await connect(a1, l1);
  await f.cut("board");
  assert.equal((await k0.cr.read()).done, true);
  assert.equal((await k1.cr.read()).done, true);
  assert.ok(await rejects(a1.TCPSocket("127.0.0.1", 9001).opened));
  assert.equal(f.stats().open, 0);
});

test("cut works while the guest holds the stream locks and never leaves an unhandled rejection", async () => {
  const f = swarm(1);
  const board = f.iface("board"), a0 = f.iface("a0");
  const l = await listen(board, 9000);
  const k = await connect(a0, l);
  const pend = [k.cr.read(), k.sr.read()];
  k.client.close().catch(() => {});  // the guest may close concurrently
  await f.cut("a0");
  for (const p of pend) assert.equal((await p).done, true);
});

test("routes are validated", () => {
  assert.throws(() => createFabric({ names: NAMES, routes: [{ from: "a0", port: 9000, to: "a9" }] }), /E_FABRIC_ROUTE/);
  assert.throws(() => createFabric({ names: NAMES, routes: [{ from: "a0", port: 0, to: "board" }] }), /E_FABRIC_ROUTE/);
  assert.throws(() => createFabric({ names: NAMES }).iface("a7"), /E_FABRIC_VM/);
});
