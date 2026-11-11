// Unit tests for public/js/loopnet.js (PROTOCOL.md §7). Drives the sockets the way CheerpX does: it holds
// reader/writer locks for the socket's lifetime and calls close().catch().
//   cd lab/web && node --test test/loopnet.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createLoopNet } from "../public/js/loopnet.js";
import { failOnUnhandledRejections } from "./helpers.mjs";

failOnUnhandledRejections();

const within = (p, ms) => Promise.race([p.then(() => true), new Promise((r) => setTimeout(() => r(false), ms))]);
let nextPort = 9100;

async function listen(net, port = nextPort++) {
  const srv = net.TCPServerSocket("0.0.0.0", { localPort: port });
  const { readable } = await srv.opened;
  return { srv, port, accepts: readable.getReader() };
}

async function connect(net, l) {
  const client = net.TCPSocket("127.0.0.1", l.port);
  const c = await client.opened;
  const server = (await l.accepts.read()).value;
  const s = await server.opened;
  return { client, server, c, s };
}

async function readAll(readable) {
  const r = readable.getReader(), parts = [];
  let n = 0;
  for (;;) {
    const { value, done } = await r.read();
    if (done) break;
    parts.push(value);
    n += value.byteLength;
  }
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.byteLength; }
  return out;
}

test("connect then close before any write resolves closed on both ends quickly", async () => {
  const net = createLoopNet(), l = await listen(net);
  const { client, server, c } = await connect(net, l);
  const w = c.writable.getWriter(), r = c.readable.getReader();
  const t0 = performance.now();
  await w.close();
  await r.cancel();
  assert.ok(await within(client.closed, 100), "client closed");
  assert.ok(await within(server.closed, 100), "server closed");
  assert.ok(performance.now() - t0 < 100);
  await l.srv.close();
});

test("close() works while the stream locks are held, and the peer sees EOF", async () => {
  const net = createLoopNet(), l = await listen(net);
  const { client, server, c, s } = await connect(net, l);
  c.writable.getWriter();
  c.readable.getReader();
  const p = client.close();
  assert.ok(p instanceof Promise);
  assert.ok(await within(p, 100), "close() resolves");
  assert.ok(await within(client.closed, 100));
  assert.ok(await within(server.closed, 100));
  assert.equal((await s.readable.getReader().read()).done, true);
  await s.writable.getWriter().write(new Uint8Array(4));  // dropped, no throw
  await l.srv.close();
});

test("every stub socket's close() returns a Promise", async () => {
  const net = createLoopNet(), l = await listen(net);
  const stubs = [
    net.TCPSocket("127.0.0.1", 1),  // refused
    net.TCPSocket("10.0.0.1", l.port),  // non-loopback
    net.TCPServerSocket("0.0.0.0", { localPort: l.port }),  // in use
    net.UDPSocket(),
  ];
  for (const s of stubs) {
    const p = s.close();
    assert.ok(p instanceof Promise);
    p.catch(() => {});
    await p;
    await assert.rejects(s.opened, { name: "NetworkError" });
    assert.ok(await within(s.closed, 50));
  }
  await l.srv.close();
});

test("a cancelled listener frees its port; a later connect is refused without throwing", async () => {
  const net = createLoopNet(), l = await listen(net);
  await l.accepts.cancel();
  assert.ok(await within(l.srv.closed, 100), "listener closed");
  let sock;
  assert.doesNotThrow(() => { sock = net.TCPSocket("127.0.0.1", l.port); });
  await assert.rejects(sock.opened, { name: "NetworkError" });
  const again = net.TCPServerSocket("0.0.0.0", { localPort: l.port });
  await again.opened;
  await again.close();
  // closing an old listener must not free a port that a newer listener holds
  const l2 = await listen(net, l.port);
  await l.srv.close();
  const c = net.TCPSocket("127.0.0.1", l.port);
  await c.opened;
  await c.close();
  await l2.srv.close();
});

test("closing a listener ends its unaccepted connections", async () => {
  const net = createLoopNet();
  const srv = net.TCPServerSocket("0.0.0.0", { localPort: nextPort });
  await srv.opened;
  const c = net.TCPSocket("127.0.0.1", nextPort++);
  await c.opened;
  await srv.close();
  assert.ok(await within(c.closed, 100));
});

test("closing an idle listener leaves a pending accept unsettled (CheerpX dereferences its value)", async () => {
  const net = createLoopNet(), l = await listen(net);
  let settled = false;
  l.accepts.read().then(() => { settled = true; }, () => { settled = true; });
  await new Promise((r) => setTimeout(r, 10));
  await l.srv.close();
  assert.ok(await within(l.srv.closed, 100), "listener closed");
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(settled, false);
  const again = await listen(net, l.port);  // the port is free again
  await again.srv.close();
});

test("400 KiB pass through intact in both directions", async () => {
  const net = createLoopNet(), l = await listen(net);
  const { client, c, s } = await connect(net, l);
  const data = new Uint8Array(400 * 1024);
  for (let i = 0; i < data.length; i++) data[i] = (i * 31 + (i >> 8)) & 0xff;
  const send = async (writable) => {
    const w = writable.getWriter();
    for (let o = 0; o < data.length; o += 1500) await w.write(data.slice(o, o + 1500));
    await w.close();
  };
  const [up, down] = await Promise.all([readAll(s.readable), readAll(c.readable), send(c.writable), send(s.writable)]);
  assert.deepEqual(up, data);
  assert.deepEqual(down, data);
  assert.ok(await within(client.closed, 100));
  await l.srv.close();
});

test("half-close: the peer reads EOF and can still reply", async () => {
  const net = createLoopNet(), l = await listen(net);
  const { client, server, c, s } = await connect(net, l);
  const cw = c.writable.getWriter();
  await cw.write(new Uint8Array([1, 2, 3]));
  await cw.close();
  assert.deepEqual(await readAll(s.readable), new Uint8Array([1, 2, 3]));
  assert.equal(await within(client.closed, 30), false, "still open in the other direction");
  const sw = s.writable.getWriter();
  await sw.write(new Uint8Array([9]));
  await sw.close();
  assert.deepEqual(await readAll(c.readable), new Uint8Array([9]));
  assert.ok(await within(client.closed, 100));
  assert.ok(await within(server.closed, 100));
  await l.srv.close();
});

test("buffering per direction is bounded when nobody reads", async () => {
  const net = createLoopNet(), l = await listen(net);
  const { client, c } = await connect(net, l);
  const w = c.writable.getWriter();
  let written = 0, stop = false;
  const writer = (async () => { while (!stop) { await w.write(new Uint8Array(4096)); written += 4096; } })();
  await new Promise((r) => setTimeout(r, 50));
  assert.ok(written <= 64 * 1024 + 4096, `buffered ${written} bytes`);
  stop = true;
  await client.close();  // releases the blocked writer; later writes are dropped
  await writer;
  await l.srv.close();
});
