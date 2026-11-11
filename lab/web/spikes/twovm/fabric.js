// Two-VM network for the spike (docs/browser-two-vm.md). Each VM gets a networkInterface. Two modes:
//  - "shared": both VMs use ONE loopnet instance, so every guest listener is reachable from both VMs
//    (one global port space; the simplest possible wiring).
//  - "fabric": each VM has its own loopnet (a private port space for its own loopback traffic) and only the
//    listed cross-VM routes exist: routes[vm][port] = target VM. A connect to a routed port always goes to the
//    target VM, even if the source VM listens on that port itself (no shadowing by a root agent). Ports in
//    exportOnly[vm] can be reached only from other VMs, not from the VM's own processes.
// remove(vm) refuses every later route into a VM whose frame was removed. The mode can be switched at run time;
// it applies to new listeners and connects. loopnet.js is an unmodified copy of lab/web/public/js/loopnet.js.
import { createLoopNet as createLoopNetPlain } from "./loopnet.js";
import { createLoopNet as createLoopNetDebug } from "./loopnet-dbg.js";

const createLoopNet = new URLSearchParams(location.search).get("netdbg") === "1" ? createLoopNetDebug : createLoopNetPlain;

export function createFabric({ names = ["A", "B"], onEvent = () => {} } = {}) {
  const shared = createLoopNet({ onEvent: (e) => onEvent({ vm: "*", ...e }) });
  const own = Object.fromEntries(names.map((n) => [n, createLoopNet({ onEvent: (e) => onEvent({ vm: n, ...e }) })]));
  const state = { mode: "shared", routes: {}, exportOnly: {} };
  const removed = new Set();  // VMs whose frame is gone: routes to them are refused (their listeners never accept)
  const counters = { crossConnects: 0, refusedPolicy: 0 };

  function refused() {
    const opened = Promise.reject(Object.assign(new Error("connection refused"), { name: "NetworkError" }));
    opened.catch(() => {});
    return { opened, closed: Promise.resolve(), async close() {} };
  }

  function iface(vm) {
    function TCPServerSocket(addr, opts) {
      return (state.mode === "shared" ? shared : own[vm]).TCPServerSocket(addr, opts);
    }
    function TCPSocket(host, port) {
      if (state.mode === "shared") return shared.TCPSocket(host, port);
      const loop = host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);
      const target = loop ? state.routes[vm]?.[port] : undefined;
      if (target && removed.has(target)) { onEvent({ vm, ev: "refused-removed", port }); return refused(); }
      if (target) { counters.crossConnects++; return own[target].TCPSocket("127.0.0.1", port); }
      if (loop && state.exportOnly[vm]?.includes(port)) { counters.refusedPolicy++; onEvent({ vm, ev: "refused-export-only", port }); return refused(); }
      return own[vm].TCPSocket(host, port);
    }
    function UDPSocket(...a) { return shared.UDPSocket(...a); }
    return { TCPSocket, TCPServerSocket, UDPSocket };
  }

  return {
    iface, counters,
    remove(vm) { removed.add(vm); },
    setMode(mode, { routes = {}, exportOnly = {} } = {}) { Object.assign(state, { mode, routes, exportOnly }); },
  };
}
