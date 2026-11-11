// The swarm's page-owned network (docs/browser-swarm.md §1, PROTOCOL.md §7): one private loopnet.js per VM
// (unmodified; each VM's own loopback traffic stays in its own port space) plus explicit cross-VM routes.
// A connect from VM `from` to a loopback address on a routed port always goes to the route's target VM, even if
// `from` listens on that port itself (a root agent cannot shadow the board). Nothing else crosses VMs, and there
// is no shared port space: in board-split agent a{i} reaches only port 9000+i on the board (swarmRoutes), the
// browser analogue of the microsandbox egress rule allow@host:tcp:<19000+i>.
//
// cut(vm) is the end-of-life step for a VM whose iframe the page removes (docs/browser-two-vm.md §8 R1): it
// ends every open cross-VM connection touching `vm` on BOTH ends (each direction's stream ends, so the peer
// reads EOF and its writes are dropped; its socket's `closed` resolves) and refuses every later route from or
// to `vm`. Without it a removed agent's relay connection would stay half-open in the board forever.
import { createLoopNet } from "./loopnet.js";
import { BOARD_BASE_PORT, BOARD_VM, agentVm } from "./protocol.js";

const isLoopback = (host) => host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);

// A socket that never opened, shaped like loopnet's own refusals (opened pre-caught, close() async).
function refused() {
  const opened = Promise.reject(Object.assign(new Error("connection refused"), { name: "NetworkError" }));
  opened.catch(() => {});
  return { opened, closed: Promise.resolve(), async close() {} };
}

// Board-split routes for n agents: a{i} -> port 9000+i -> board (docs/browser-swarm.md §1).
export function swarmRoutes(n, basePort = BOARD_BASE_PORT) {
  return Array.from({ length: n }, (_, i) => ({ from: agentVm(i), port: basePort + i, to: BOARD_VM }));
}

// names: the VMs; routes: [{from, port, to}]. onEvent({vm, ev, port?}) sees only names, events and numbers
// (never payload bytes). Returns {iface(vm), route(from, port, to), cut(vm), isCut(vm), stats()}.
export function createFabric({ names, routes = [], onEvent = () => {} }) {
  const report = (e) => { try { onEvent(e); } catch {} };
  const nets = new Map(names.map((n) => [n, createLoopNet({ onEvent: (e) => report({ vm: n, ...e }) })]));
  const table = new Map(names.map((n) => [n, new Map()]));  // from -> port -> to
  const cutSet = new Set();
  const open = new Set();  // live cross-VM connections {from, to, port, client}
  const counters = { crossConnects: 0, refusedCut: 0, cutClosed: 0 };

  function route(from, port, to) {
    if (!nets.has(from) || !nets.has(to) || from === to) throw new Error("E_FABRIC_ROUTE");
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("E_FABRIC_ROUTE");
    table.get(from).set(port, to);
  }
  for (const r of routes) route(r.from, r.port, r.to);

  function iface(vm) {
    const own = nets.get(vm);
    if (!own) throw new Error("E_FABRIC_VM");
    return {
      TCPServerSocket: (addr, opts) => own.TCPServerSocket(addr, opts),
      UDPSocket: (...a) => own.UDPSocket(...a),
      TCPSocket(host, port) {
        const to = isLoopback(host) ? table.get(vm).get(port) : undefined;
        if (cutSet.has(vm) || (to && cutSet.has(to))) {
          counters.refusedCut++;
          report({ vm, ev: "refused-cut", port });
          return refused();
        }
        if (!to) return own.TCPSocket(host, port);
        // The target VM's loopnet creates the pair; we keep the client end. Closing it ends both directions,
        // which is what cut() needs (loopnet.js socket(): close() ends `out` and `inn`).
        const client = nets.get(to).TCPSocket("127.0.0.1", port);
        const conn = { from: vm, to, port, client };
        open.add(conn);
        client.opened.then(() => {
          counters.crossConnects++;
          report({ vm, ev: "cross-connect", port, to });
        }, () => open.delete(conn));
        client.closed.then(() => open.delete(conn));
        return client;
      },
    };
  }

  // Ends every cross-VM connection touching vm (both ends see EOF) and refuses all later routes from or to it.
  // Resolves once those connections report closed. Idempotent. The VM's private loopback connections are left
  // to die with its iframe: nothing outside the VM can see them.
  async function cut(vm) {
    if (cutSet.has(vm)) return;
    if (!nets.has(vm)) throw new Error("E_FABRIC_VM");
    cutSet.add(vm);
    const victims = [...open].filter((c) => c.from === vm || c.to === vm);
    for (const c of victims) open.delete(c);
    counters.cutClosed += victims.length;
    report({ vm, ev: "cut", port: victims.length });
    await Promise.all(victims.map((c) => c.client.close().catch(() => {})));
    // Drop the page's last reference to the VM's private network: its listeners' streams are read by the
    // removed frame's realm, and holding them would keep that realm (and its VM memory) alive.
    nets.delete(vm);
  }

  return {
    iface, route, cut,
    isCut: (vm) => cutSet.has(vm),
    stats: () => ({ ...counters, open: open.size }),
  };
}
