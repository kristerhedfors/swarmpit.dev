// SPIKE DEBUG COPY of loopnet.js (records which stream operations CheerpX performs on each socket end into
// globalThis.__netTrace). Not used unless twovm.js is loaded with ?netdbg=1.
// In-page loopback network for CheerpX (passed as Linux.create({networkInterface}); PROTOCOL.md §7).
// CheerpX calls TCPServerSocket(addr, {localPort}) for listen() and TCPSocket(host, port) for connect(),
// expecting Direct Sockets-shaped objects ({opened, closed, close()}); it calls close().catch(), so every
// close() is async. Only loopback is routed: a connect to 127.0.0.0/8 or localhost reaches the guest listener
// on that port; anything else (and all UDP) is refused, so the guest has no network beyond its own processes.
// Listeners are keyed by port because CheerpX always binds "0.0.0.0".
const HIGH_WATER = 64 * 1024;  // bytes buffered per direction before the writer waits

export function createLoopNet({ onEvent = () => {} } = {}) {
  const listeners = new Map();  // port -> listener
  let ephemeral = 40000;
  const isLoopback = (host) => host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);

  function TCPServerSocket(localAddress, { localPort: port } = {}) {
    if (listeners.has(port)) return failed(`port ${port} in use`);
    // Connections wait in `backlog` until the guest reads the accept stream; on shutdown the unaccepted ones
    // are closed so their clients see EOF instead of hanging. After shutdown a pending accept never settles:
    // CheerpX 1.3.9 reads `.remoteAddress` of whatever an accept read yields, so ending the stream (a done
    // read with no value) throws inside CheerpX, and erroring it would be an unhandled rejection there.
    const backlog = [];
    let wake = () => {}, done = false, resolveClosed;
    const closed = new Promise((r) => { resolveClosed = r; });
    const accept = new ReadableStream({
      async pull(c) {
        while (!backlog.length && !done) await new Promise((r) => { wake = r; });
        if (done) return new Promise(() => {});
        c.enqueue(backlog.shift());
      },
      cancel: () => shut(),
    }, { highWaterMark: 0 });
    const self = { connect(sock) { backlog.push(sock); wake(); } };
    // Ends the listener whether the guest called close() or cancelled the accept stream. The port is freed
    // only if it still maps to this listener.
    function shut() {
      if (done) return;
      done = true;
      if (listeners.get(port) === self) listeners.delete(port);
      for (const sock of backlog.splice(0)) sock.close();
      wake();
      onEvent({ ev: "unlisten", port });
      resolveClosed();
    }
    listeners.set(port, self);
    onEvent({ ev: "listen", port });
    return {
      opened: Promise.resolve({ readable: accept, localAddress: localAddress || "0.0.0.0", localPort: port }),
      closed,
      async close() { shut(); },
    };
  }

  function TCPSocket(remoteAddress, remotePort) {
    const listener = isLoopback(remoteAddress) ? listeners.get(remotePort) : undefined;
    if (!listener) {
      onEvent({ ev: "refused", host: isLoopback(remoteAddress) ? remoteAddress : "non-loopback", port: remotePort });
      return failed("connection refused");
    }
    const localPort = ephemeral = ephemeral >= 60999 ? 40000 : ephemeral + 1;
    const cid = (globalThis.__netConn = (globalThis.__netConn || 0) + 1);
    const tr = (ev) => (globalThis.__netTrace ||= []).push(`${remotePort}#${cid} ${ev}`);
    const up = channel((w) => tr(`up:${w}`)), down = channel((w) => tr(`down:${w}`));  // client -> server, server -> client
    listener.connect(socket(down, up, { remoteAddress: "127.0.0.1", remotePort: localPort,
      localAddress: remoteAddress, localPort: remotePort }, (w) => tr(`server:${w}`)));
    onEvent({ ev: "connect", port: remotePort });
    return socket(up, down, { remoteAddress, remotePort, localAddress: "127.0.0.1", localPort }, (w) => tr(`client:${w}`));
  }

  // No UDP: DNS and anything else fail fast (names resolve through /etc/hosts only).
  function UDPSocket() {
    onEvent({ ev: "udp-refused" });
    return failed("udp unavailable");
  }

  return { TCPSocket, TCPServerSocket, UDPSocket };
}

// One socket end: it writes into `out` and reads from `inn`. closed resolves once both directions have ended,
// whether the guest closed or cancelled the streams itself or called close(); close() works while CheerpX
// holds the stream locks.
function socket(out, inn, info, tr = () => {}) {
  const closed = Promise.all([out.ended, inn.ended]).then(() => tr("closed-resolved"));
  return {
    opened: Promise.resolve({ readable: inn.readable, writable: out.writable, ...info }),
    get closed() { tr("closed-read"); return closed; },
    async close() { tr("close()"); out.end(); inn.end(); await closed; },
  };
}

// One direction of a connection: a byte-sized queue of at most HIGH_WATER bytes (the writer waits for the
// reader beyond that). It ends exactly once: when the writer closes or aborts, the reader cancels, or a
// socket is closed. Queued bytes can still be read after the end (then EOF); later writes are dropped.
function channel(tr = () => {}) {
  let queue, wake = () => {}, done = false, resolveEnded;
  const ended = new Promise((r) => { resolveEnded = r; });
  function end() {
    if (done) return;
    done = true;
    try { queue.close(); } catch {}  // throws if the reader already cancelled
    wake();
    resolveEnded();
  }
  const readable = new ReadableStream({ start(c) { queue = c; }, pull: () => wake(), cancel: () => { tr("reader-cancel"); end(); } },
    { highWaterMark: HIGH_WATER, size: (chunk) => chunk.byteLength });
  const writable = new WritableStream({
    async write(chunk) {
      if (done) return;
      queue.enqueue(chunk);
      while (!done && queue.desiredSize <= 0) await new Promise((r) => { wake = r; });
    },
    close: () => { tr("writer-close"); end(); },
    abort: () => { tr("writer-abort"); end(); },
  });
  return { readable, writable, ended, end };
}

// A socket that never opened (refused, port in use, UDP): opened rejects (pre-caught, so an unobserved
// rejection is not reported), closed is resolved, and close() returns a Promise like the real ones.
function failed(message) {
  const opened = Promise.reject(Object.assign(new Error(message), { name: "NetworkError" }));
  opened.catch(() => {});
  return { opened, closed: Promise.resolve(), async close() {} };
}
