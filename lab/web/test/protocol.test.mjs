// The shared parts of PROTOCOL.md must match on both sides: the export file lists (§5.2, identical and in
// order in the doc, public/js/protocol.js and lab/browser/guest/labctl.py), the seal format (§2: what the
// page seals, labctl unseals) and the status error codes (§4).
//   cd lab/web && node --test 'test/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const LABCTL = join(REPO, "lab", "browser", "guest", "labctl.py");

// The {live, final} lists of the ```<tag> fenced block, or null if PROTOCOL.md has no such block.
function docBlock(tag) {
  const md = readFileSync(join(REPO, "lab", "browser", "PROTOCOL.md"), "utf8");
  const block = new RegExp("^```" + tag + "\\n([\\s\\S]*?)^```", "m").exec(md);
  if (!block) return null;
  const lists = { live: [], final: [] };
  for (const line of block[1].split("\n").filter((l) => l.trim())) {
    const m = /^(live|final): (\S+)$/.exec(line.trim());
    assert.ok(m, `unparseable ${tag} line: ${line}`);
    lists[m[1]].push(m[2]);
  }
  return lists;
}

function fromDoc() {
  const lists = docBlock("export-files");
  assert.ok(lists, "PROTOCOL.md has no export-files block");
  return { LIVE_FILES: lists.live, RUN_FILES: lists.final };
}

// Runs `body` with labctl.py imported as `m` (its main is guarded); returns its stdout parsed as JSON.
function withLabctl(body, input = "") {
  const py = `import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("labctl", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
${body}`;
  return JSON.parse(execFileSync("python3", ["-c", py, LABCTL], { encoding: "utf8", input }));
}

function fromLabctl() {
  return withLabctl(`print(json.dumps({"LIVE_FILES": list(m.LIVE_FILES), "RUN_FILES": list(m.RUN_FILES)}))`);
}

const doc = fromDoc();

test("PROTOCOL.md lists are well formed", () => {
  assert.ok(doc.LIVE_FILES.length > 0 && doc.RUN_FILES.length > 0);
  assert.equal(new Set(doc.RUN_FILES).size, doc.RUN_FILES.length, "duplicate RUN_FILES entry");
  for (const f of doc.LIVE_FILES) assert.ok(doc.RUN_FILES.includes(f), `live file ${f} is not a run file`);
});

test("protocol.js mirrors PROTOCOL.md", async () => {
  const js = await import("../public/js/protocol.js");
  assert.deepEqual([...js.LIVE_FILES], doc.LIVE_FILES);
  assert.deepEqual([...js.RUN_FILES], doc.RUN_FILES);
});

test("labctl.py mirrors PROTOCOL.md", () => {
  const py = fromLabctl();
  assert.deepEqual(py.LIVE_FILES, doc.LIVE_FILES);
  assert.deepEqual(py.RUN_FILES, doc.RUN_FILES);
});

test("labctl unseals what the page seals, and only under the same key and label", async () => {
  const js = await import("../public/js/protocol.js");
  const key = "ab".repeat(32), other = "cd".repeat(32);
  const data = new Uint8Array(1000).map((_, i) => (i * 7) & 255);  // non-UTF-8 bytes, several blocks
  const sealed = await js.seal(key, "seed.json", data);
  const got = withLabctl(`
box = sys.stdin.read()
def un(k, label):
    p = m.unseal(k, label, box)
    return None if p is None else p.hex()
print(json.dumps([un("${key}", "seed.json"), un("${other}", "seed.json"), un("${key}", "run.json")]))`, sealed);
  assert.deepEqual(got, [Buffer.from(data).toString("hex"), null, null]);
  assert.equal(await js.seal(key, "x", new Uint8Array()), await js.seal(key, "x", new Uint8Array()));
});

test("labctl's status error codes are exactly the PROTOCOL.md §4 list", () => {
  const md = readFileSync(join(REPO, "lab", "browser", "PROTOCOL.md"), "utf8");
  const line = /^- `error` \(only with `error`\): ([^.]*)\./m.exec(md);  // the list ends at the first "."
  assert.ok(line, "PROTOCOL.md §4 has no error list");
  const doc = [...line[1].matchAll(/`(E_[A-Z_]+)`/g)].map((m) => m[1]).sort();
  const src = readFileSync(LABCTL, "utf8");
  const py = [...new Set([...src.matchAll(/"(E_[A-Z_]+)"/g)].map((m) => m[1]))].sort();
  assert.deepEqual(py, doc);
});

// ---- swarm roles (PROTOCOL.md §9, docs/browser-swarm.md §1-§3) ----
// The §9.4 blocks are compared with protocol.js and with labctl's role-dependent lists (run_files() /
// live_files() with _role and _agent set, as labctl's main sets them from the verified spec).

const boardDoc = docBlock("export-files-board"), agentDoc = docBlock("export-files-agent");
const swarmDoc = { skip: !boardDoc || !agentDoc ? "PROTOCOL.md has no §9.4 export-files-board/-agent blocks" : false };

function labctlRoleLists(role, agent) {
  return withLabctl(`m._role = ${role === null ? "None" : JSON.stringify(role)}
m._agent = ${agent}
print(json.dumps({"live": list(m.live_files()), "final": list(m.run_files())}))`);
}

test("protocol.js board and agent lists mirror PROTOCOL.md §9.4", swarmDoc, async () => {
  const js = await import("../public/js/protocol.js");
  assert.deepEqual(js.boardFiles, boardDoc.final);
  assert.deepEqual(js.BOARD_LIVE_FILES, boardDoc.live);
  assert.deepEqual(js.AGENT_FILE_TEMPLATES.final, agentDoc.final);
  assert.deepEqual(js.AGENT_FILE_TEMPLATES.live, agentDoc.live);
  for (const i of [0, 1, 3]) {
    const sub = (l) => l.map((x) => x.replaceAll("{i}", String(i)));
    assert.deepEqual(js.agentFiles(i), sub(agentDoc.final));
    assert.deepEqual(js.agentLiveFiles(i), sub(agentDoc.live));
    assert.ok(js.agentFiles(i).includes(js.agentMetaFile(i)));
  }
  for (const f of boardDoc.live) assert.ok(boardDoc.final.includes(f));
  for (const f of agentDoc.live) assert.ok(agentDoc.final.includes(f));
  // agent 0's files are the solo a0 names (§9.4) apart from the board-side ones and the meta
  const solo = new Set(doc.RUN_FILES);
  for (const f of js.agentFiles(0)) assert.ok(solo.has(f) || f === js.agentMetaFile(0), f);
  const all = js.runFiles(4);
  assert.equal(new Set(all).size, all.length, "duplicate run file across roles");
});

test("labctl's role lists mirror PROTOCOL.md §9.4", swarmDoc, (t) => {
  const probe = withLabctl(`print(json.dumps(hasattr(m, "run_files") and hasattr(m, "live_files")))`);
  if (!probe) return t.skip("labctl.py has no role-dependent run_files()/live_files()");
  assert.deepEqual(labctlRoleLists("board", 0), boardDoc);
  for (const i of [0, 2, 3]) {
    const sub = (l) => l.map((x) => x.replaceAll("{i}", String(i)));
    assert.deepEqual(labctlRoleLists("agent", i), { live: sub(agentDoc.live), final: sub(agentDoc.final) });
  }
  assert.deepEqual(labctlRoleLists(null, 0), { live: doc.LIVE_FILES, final: doc.RUN_FILES });
});

test("the board inbox control names and board-ready path match labctl", swarmDoc, async (t) => {
  const js = await import("../public/js/protocol.js");
  const py = withLabctl(`print(json.dumps({k: getattr(m, k, None) for k in ["STOP_FILE", "BOARD_READY", "AGENT_META", "BOARD_BASE_PORT", "BOARD_PORT"]}))`);
  if (!py.STOP_FILE || !py.BOARD_READY) return t.skip("labctl.py has no STOP_FILE/BOARD_READY");
  assert.equal(js.BOARD_INBOX.stop, `/${py.STOP_FILE}`);
  assert.equal(js.BOARD_READY_PATH, `/${py.BOARD_READY}`);
  assert.equal(js.agentMetaFile(2), `a2/${py.AGENT_META}`);
  assert.equal(js.BOARD_INBOX.agentMeta(2), "/agent-2.meta.json");  // §9.1
  assert.equal(js.agentPort(0), py.BOARD_BASE_PORT ?? py.BOARD_PORT);
});

test("run ids: solo iff one agent, multi<N> otherwise; labctl accepts their character set", async () => {
  const js = await import("../public/js/protocol.js");
  const d = new Date(Date.UTC(2026, 9, 24, 1, 2, 3));
  assert.equal(js.makeRunId("forum", 1, d, "0a1b"), "browser-forum-solo-20261024010203-0a1b");
  assert.equal(js.makeRunId("irc", 4, d, "ffff"), "browser-irc-multi4-20261024010203-ffff");
  assert.match(js.makeRunId("wiki", 3), js.RUN_ID_RE);
  for (const bad of [["forum", 0], ["forum", 5], ["forum", 1.5], ["nope", 2]]) assert.throws(() => js.makeRunId(...bad));
  assert.match(js.makeRunId("cache", 2), /^[A-Za-z0-9._-]{1,128}$/);  // §2 labctl check
});

test("per-VM database names map back to their run id; a solo id ending -a<digits> is not split", async () => {
  const js = await import("../public/js/protocol.js");
  const id = "browser-forum-multi3-20261024010203-0a1b", solo = "browser-forum-solo-20261024010203-a123";
  for (const vm of ["board", "a0", "a3"]) {
    for (const kind of js.DB_KINDS) assert.equal(js.dbRunId(js.dbName(kind, js.vmStorageId(id, vm))), id);
  }
  assert.equal(js.dbRunId(js.dbName("overlay", solo)), solo);
  assert.equal(js.dbRunId(js.dbName("overlay", id)), id);
  assert.equal(js.dbRunId("swarmlab-traces"), null);
  // as CheerpX lists an IDBDevice's database: "cjFS_/<name>/"
  for (const vm of ["board", "a0", "a3"]) for (const kind of js.DB_KINDS)
    assert.equal(js.dbRunId(`cjFS_/${js.dbName(kind, js.vmStorageId(id, vm))}/`), id);
  assert.equal(js.dbRunId(`cjFS_/${js.dbName("overlay", solo)}/`), solo);
  assert.equal(js.unwrapDbName("cjFS_/swarmpit-overlay-x/"), "swarmpit-overlay-x");
  assert.equal(js.unwrapDbName("swarmlab-traces"), "swarmlab-traces");
  assert.equal(js.dbRunId("cjFS_/swarmlab-traces/"), null);
  assert.equal(js.dbRunId("swarmpit-overlay-" + id), null);
});

test("swarm VM names: the board first, then a0..a{N-1}", async () => {
  const js = await import("../public/js/protocol.js");
  assert.deepEqual(js.swarmVms(3), ["board", "a0", "a1", "a2"]);
  assert.deepEqual(js.swarmVms(1), [js.BOARD_VM, js.agentVm(0)]);
});

test("role specs carry exactly the §9 role fields", async () => {
  const js = await import("../public/js/protocol.js");
  const base = { run_id: "r", iface: "forum", rep: 0, harness: {}, wire: {}, provider_label: "p:m", cx_version: "x", image: "i" };
  assert.deepEqual(js.boardSpec(base, 3), { ...base, role: "board", n_agents: 3 });
  const ctf = { passphrase: "p", key_hex: "k", flag: "f", vault_id: "v" };
  assert.deepEqual(js.agentSpec(base, 3, 2, ctf), { ...base, role: "agent", agent_id: 2, n_agents: 3, ctf, relay_host_port: 9002 });
  assert.deepEqual(js.ROLE_INPUTS, { board: ["spec", "seed"], agent: ["spec"] });
});
