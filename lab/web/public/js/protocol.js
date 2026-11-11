// Page side of the guest <-> page contract, lab/browser/PROTOCOL.md. Pure data and helpers (no DOM), so
// node tests import it too. The file lists mirror PROTOCOL.md §5.2 exactly (checked by test/protocol.test.mjs);
// the swarm part (board-split roles, PROTOCOL.md §9, docs/browser-swarm.md §1-§3) is at the end.

export const CX_VERSION = "1.3.9";

// Guest mount points (PROTOCOL.md §1). Device paths below are relative to the mount and start with "/".
export const MOUNTS = { root: "/", outbox: "/root/llmbox/out", inbox: "/root/llmbox/in", export: "/root/export" };

// The CheerpX mount list of one VM (§1), given its overlay root and its three mailbox/export devices.
export function cxMounts({ overlay, outbox, inbox, exportDev }) {
  return [
    { type: "ext2", dev: overlay, path: MOUNTS.root },
    { type: "dir", dev: outbox, path: MOUNTS.outbox },
    { type: "dir", dev: inbox, path: MOUNTS.inbox },
    { type: "dir", dev: exportDev, path: MOUNTS.export },
    { type: "devs", path: "/dev" }, { type: "devpts", path: "/dev/pts" },
    { type: "proc", path: "/proc" }, { type: "sys", path: "/sys" },
  ];
}

// The two sealed boot inputs the page writes into the inbox before starting labctl (§2): device path and
// seal label. The agent can read the inbox, so the plaintext never goes there.
export const BOOT_INPUTS = {
  spec: { path: "/run.json.sealed", label: "run.json" },
  seed: { path: "/seed.json.sealed", label: "seed.json" },
};

// Export device paths (§4, §5.1).
export const STATUS_PATH = "/status.json";
export const MANIFEST_PATH = "/manifest.json";
export const RUN_DIR = "/run";

// PROTOCOL.md §5.2, in block order. LIVE_FILES appear during the run, RUN_FILES only in the final export.
export const LIVE_FILES = ["a0/steps.jsonl"];
export const RUN_FILES = [
  "run.json", "cfg-a0.json", "seed.json", "board.out", "board_events.jsonl",
  "a0/steps.jsonl", "a0/summary.json", "a0/FATAL", "a0/harness.out", "a0/setup.out",
  "a0/vaultd.out", "a0/relay.jsonl", "a0/relay.out",
];

// Mailbox file names on the outbox / inbox devices (§3).
export const requestFile = (agent, seq) => `/a${agent}-${seq}.req.json`;
export const replyFile = (agent, seq) => `/a${agent}-${seq}.resp.json`;

// How the page starts labctl (§2). keyHex is the per-run mailbox key: 32 random bytes as lowercase hex.
export function labctlCommand(keyHex) {
  if (!/^[0-9a-f]{64}$/.test(keyHex)) throw new Error("E_KEY");
  return {
    argv: ["/usr/local/bin/python3", "/root/lab/labctl.py"],
    opts: {
      env: ["HOME=/root", "LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        `LAB_MAILBOX_KEY=${keyHex}`],
      cwd: "/root/lab", uid: 0, gid: 0,
    },
  };
}

// §2 seal: the text of a sealed inbox file holding `data` (Uint8Array) under the mailbox key and a label.
// Keystream block i = HMAC(key, "seal-stream\0" label "\0" u32be(i)); tag = HMAC(key, "seal-tag\0" label "\0" ct).
// The key is per run and each label is sealed once, so no nonce is needed.
export async function seal(keyHex, label, data) {
  const key = await hmacKey(keyHex);
  const mac = async (prefix, bytes) => new Uint8Array(await crypto.subtle.sign("HMAC", key,
    concat(new TextEncoder().encode(`${prefix}\0${label}\0`), bytes)));
  const ct = new Uint8Array(data.length);
  for (let i = 0; i < data.length; i += 32) {
    const block = await mac("seal-stream", u32be(i / 32));
    for (let j = 0; j < 32 && i + j < data.length; j++) ct[i + j] = data[i + j] ^ block[j];
  }
  const tag = await mac("seal-tag", ct);
  return JSON.stringify({ v: 1, tag: bytesToHex(tag), data: bytesToBase64(ct) });
}

// The HMAC-SHA-256 signing key of a mailbox key (the seal above, the broker's request MACs, §3).
export async function hmacKey(keyHex) {
  return crypto.subtle.importKey("raw", hexToBytes(keyHex), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

export const hexToBytes = (hex) => new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
export const bytesToHex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
const u32be = (n) => new Uint8Array([n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]);
function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}
function bytesToBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// The run's IndexedDB databases (§1); a tab owns them while it holds the Web Lock runLock(runId).
// LEGACY_DB_PREFIX is the name prefix of the first deployed version, whose tabs take no lock (vm.js).
export const DB_KINDS = ["overlay", "out", "export"];
export const dbName = (kind, runId) => `swarmlab-${kind}-${runId}`;
export const runLock = (runId) => `swarmlab-run-${runId}`;
export const LEGACY_DB_PREFIX = "swarmpit-";

// ---- Swarm: board-split roles (PROTOCOL.md §9, docs/browser-swarm.md §1-§3) ----
// One VM per role: "board" runs labctl --role board (the board on ports 9000..9000+N-1), "a{i}" runs
// labctl --role agent for agent i (its relay reaches the board on 9000+i through the fabric, js/fabric.js).

export const MAX_AGENTS = 4;
export const BOARD_BASE_PORT = 9000;
export const agentPort = (i) => BOARD_BASE_PORT + i;
export const BOARD_VM = "board";
export const agentVm = (i) => `a${i}`;
// Every VM of an N-agent swarm, board first (the boot order).
export const swarmVms = (n) => [BOARD_VM, ...Array.from({ length: n }, (_, i) => agentVm(i))];

// docs/browser-swarm.md §1: browser-<iface>-(solo|multi<N>)-<UTC YYYYMMDDHHMMSS>-<4 hex>, solo iff N = 1.
// `date` and `hex4` are parameters so tests can pin them.
export const IFACES = ["forum", "wiki", "cache", "irc"];
export function makeRunId(iface, nAgents, date = new Date(), hex4) {
  if (!IFACES.includes(iface)) throw new Error("E_IFACE");
  if (!Number.isInteger(nAgents) || nAgents < 1 || nAgents > MAX_AGENTS) throw new Error("E_N_AGENTS");
  const stamp = date.toISOString().slice(0, 19).replace(/[-:T]/g, "");
  const rand = hex4 ?? bytesToHex(crypto.getRandomValues(new Uint8Array(2)));
  if (!/^[0-9a-f]{4}$/.test(rand)) throw new Error("E_RUN_ID");
  return `browser-${iface}-${nAgents === 1 ? "solo" : `multi${nAgents}`}-${stamp}-${rand}`;
}
export const RUN_ID_RE = /^browser-(forum|wiki|cache|irc)-(solo|multi[2-4])-\d{14}-[0-9a-f]{4}$/;

// Each VM has its own three databases (§1) under the storage id <run_id>-<vm>, so dbName(kind, storageId)
// is e.g. swarmlab-overlay-<run_id>-a1. The tab holds runLock(run_id) AND runLock(storageId) for every VM, so
// the solo page's stale cleanup (vm.js, which reads the whole tail after the kind as a run id) also sees a
// held lock and never deletes a live swarm VM's databases.
export const vmStorageId = (runId, vm) => `${runId}-${vm}`;
// CheerpX (1.3.x) keeps an IDBDevice created as <name> in the IndexedDB database "cjFS_/<name>/", which is the
// name indexedDB.databases() lists (found by test/e2e.mjs): listed names are unwrapped before they are matched.
export const unwrapDbName = (name) => { const m = /^cjFS_\/(.+)\/$/.exec(name || ""); return m ? m[1] : name || ""; };
// The run id that owns a database (solo: <run_id>; swarm: <run_id>-<vm>; as created or as CheerpX lists it), or
// null if the name is not ours. Run ids end in -<4 hex> (§2), which is what tells a VM suffix from the id itself
// (a solo id may end -a123).
export function dbRunId(name) {
  const m = new RegExp(`^swarmlab-(?:${DB_KINDS.join("|")})-(.+-[0-9a-f]{4})(?:-(?:board|a\\d))?$`).exec(unwrapDbName(name));
  return m ? m[1] : null;
}

// Board export (§9.1): /root/export/board-ready.json once every board port listens and the CTF is drawn:
// {"v": 1, "run_id", "ctf": {passphrase, key_hex, flag, vault_id}, "started": "<UTC ...Z>", "ports": [...]}.
// The page hands the ctf only to the (sealed) agent specs and never renders it. status.json, manifest.json and
// run/<file> are as §4/§5.1 (the board's status has `events` instead of `steps`).
export const BOARD_READY_PATH = "/board-ready.json";

// PROTOCOL.md §9.4 export-files-board (same order): the board's run files, the zip's top level
// (docs/browser-swarm.md §3). The content sink board_events.content.chatter.json holds the post bodies, is
// exported only if non-empty and never mirrored live; it is chatter-bearing (never printed).
export const boardFiles = ["run.json", "seed.json", "board.out", "board_events.jsonl", "board_events.content.chatter.json"];
export const BOARD_LIVE_FILES = ["board_events.jsonl"];

// PROTOCOL.md §9.4 export-files-agent (same order), {i} = agent id: agent i's run files as its VM exports them
// under /root/export/run/ and as they appear in the zip (cfg-a{i}.json without the mailbox section;
// a{i}/agent-meta.json, §9.2, is the browser extra the board merges, written last).
const AGENT_FILE_TEMPLATE = ["cfg-a{i}.json", "a{i}/steps.jsonl", "a{i}/summary.json", "a{i}/FATAL",
  "a{i}/harness.out", "a{i}/setup.out", "a{i}/vaultd.out", "a{i}/relay.jsonl", "a{i}/relay.out", "a{i}/agent-meta.json"];
const AGENT_LIVE_TEMPLATE = ["a{i}/steps.jsonl"];
const expand = (template, i) => template.map((n) => n.replaceAll("{i}", String(i)));
export const agentFiles = (i) => expand(AGENT_FILE_TEMPLATE, i);
export const agentLiveFiles = (i) => expand(AGENT_LIVE_TEMPLATE, i);
// The unexpanded §9.4 templates, for test/protocol.test.mjs.
export const AGENT_FILE_TEMPLATES = { live: AGENT_LIVE_TEMPLATE, final: AGENT_FILE_TEMPLATE };
export const agentMetaFile = (i) => `a${i}/agent-meta.json`;
// Every file of an N-agent run dir (without the page's manifest.json), board files first.
export const runFiles = (n) => [...boardFiles, ...Array.from({ length: n }, (_, i) => agentFiles(i)).flat()];

// What the page writes into the board's inbox at the end (§9.1; plain, unsealed files: the board VM runs no
// agent): one agent-<i>.meta.json per agent that exported one (its a{i}/agent-meta.json, byte for byte), then
// stop. Each is written once (DataDevice cannot rewrite, §1).
export const BOARD_INBOX = { agentMeta: (i) => `/agent-${i}.meta.json`, stop: "/stop" };

// §9.1: the page-made agent-<i>.meta.json for an agent that started (its boot began) but exported no meta of its
// own (frame or boot failure, removed after the grace): the shape labctl's meta_ok accepts, with the page-side
// error code in `error` (labctl CODE_RE, E_[A-Z_]{1,32}; others become E_PAGE), so run.json's
// params.agent_errors names it. It counts as timed out and not stopped, as a missing meta does (orchestrate's
// straggler semantics); the secret counts are unknown (null). started / finished are epoch seconds.
const LABCTL_CODE_RE = /^E_[A-Z_]{1,32}$/;
export function pageAgentMeta(runId, i, code, started, finished) {
  return { v: 1, run_id: runId, agent: i, page: true, timed_out: true, agent_stopped: false, solved: false,
    solve_step: null, started, finished, fatal: null, stop_reason: null,
    error: LABCTL_CODE_RE.test(code) ? code : "E_PAGE", secret_exposure: null, secret_exposure_logs: null };
}

// Which sealed boot inputs (§2, BOOT_INPUTS) each role gets: the board the spec and the seed, an agent only its
// spec (§9: no seed). The labels stay "run.json" / "seed.json" in every VM: each VM has its own mailbox key.
export const ROLE_INPUTS = { board: ["spec", "seed"], agent: ["spec"] };

// The sealed run spec for one VM (§2 + the §9 role fields). base = {run_id, iface, rep, harness, wire,
// provider_label, cx_version, image}. Agent specs carry the shared ctf from board-ready.json unchanged.
export function boardSpec(base, nAgents) {
  return { ...base, role: "board", n_agents: nAgents };
}
export function agentSpec(base, nAgents, i, ctf) {
  return { ...base, role: "agent", agent_id: i, n_agents: nAgents, ctf, relay_host_port: agentPort(i) };
}
