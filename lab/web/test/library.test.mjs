// Unit tests for public/js/library.js (docs/browser-swarm.md §7): the trace library over the in-memory store and over
// a minimal fake IndexedDB (the real one is exercised in Chromium by test/ui.e2e.mjs), the stored-field whitelist
// (nothing like an API key can be persisted), and traceFromEntries (which run directory an upload holds).
// Placeholder contents only.
//   cd lab/web && node --test test/library.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { TraceError, createLibrary, idbStore, memoryStore, openLibrary, traceFromEntries } from "../public/js/library.js";

const enc = new TextEncoder();
const card = (id, hash = "0123abcd") => ({ id, hash, iface: "forum", pop: "multi", nAgents: 2, stats: { events: 3, nodes: 4 } });
const RUN = "browser-forum-multi2-20261024120000-ab12";
const blob = (s = "placeholder zip bytes") => new Blob([s], { type: "application/zip" });

// ---- a minimal IndexedDB: open/upgrade, keyPath stores, readonly/readwrite transactions, async request events
function fakeIDB() {
  const dbs = new Map();
  const later = (f) => setTimeout(f, 0);
  const request = (fn) => { const r = {}; later(() => { try { r.result = fn(); r.onsuccess?.(); } catch (e) { r.error = e; r.onerror?.(); } }); return r; };
  const clone = (v) => (v === undefined ? undefined : structuredClone(v));
  return {
    dbs,
    open(name, version) {
      const r = {};
      later(() => {
        let db = dbs.get(name);
        const fresh = !db;
        if (!db) {
          const stores = new Map();
          db = {
            stores, version,
            objectStoreNames: { contains: (n) => stores.has(n) },
            createObjectStore: (n, o) => { stores.set(n, { keyPath: o.keyPath, rows: new Map() }); },
            transaction(names, mode) {
              const tx = { pending: 0 };
              const done = () => later(() => { if (--tx.pending === 0) tx.oncomplete?.(); });
              tx.objectStore = (n) => {
                const s = stores.get(n);
                if (!names.includes(n) || !s) throw new Error("store not in transaction");
                const op = (fn, write) => {
                  if (write && mode !== "readwrite") throw new Error("readonly");
                  tx.pending++;
                  const req = request(fn);
                  const ok = () => { req.onsuccess0?.(); done(); };
                  Object.defineProperty(req, "onsuccess", { set(f) { req.onsuccess0 = f; }, get() { return ok; } });
                  return req;
                };
                return {
                  put: (v) => op(() => { s.rows.set(v[s.keyPath], clone(v)); }, true),
                  delete: (k) => op(() => { s.rows.delete(k); }, true),
                  get: (k) => op(() => clone(s.rows.get(k))),
                  getAll: () => op(() => [...s.rows.values()].map(clone)),
                };
              };
              return tx;
            },
            close() {},
          };
          dbs.set(name, db);
        }
        r.result = db;
        if (fresh) r.onupgradeneeded?.();
        r.onsuccess?.();
      });
      return r;
    },
  };
}

async function exercise(lib) {
  assert.deepEqual(await lib.list(), []);
  const a = await lib.put({ id: RUN, name: `${RUN}.zip`,
    source: "run", blob: blob(), card: card(RUN), nFiles: 9, core: { format: "x" }, detail: null });
  assert.equal(a.bytes, blob().size);
  await new Promise((r) => setTimeout(r, 5));
  await lib.put({ id: "up-1", source: "upload", blob: blob("other"), card: card("up-1") });
  const L = await lib.list();
  assert.deepEqual(L.map((m) => m.id), ["up-1", a.id]);           // newest first
  assert.equal(L[0].name, "up-1.zip");
  assert.ok(!("blob" in L[0]) && !("core" in L[0]));               // list() never carries blobs or bundles
  const g = await lib.get(a.id);
  assert.equal(await g.blob.text(), "placeholder zip bytes");
  assert.deepEqual(g.core, { format: "x" });
  await lib.setCard(a.id, card(a.id, "ffffffff"), { core: { format: "y" } });
  const g2 = await lib.get(a.id);
  assert.equal(g2.card.hash, "ffffffff");
  assert.equal(g2.created, g.created);
  assert.equal(await g2.blob.text(), "placeholder zip bytes");
  await lib.delete(a.id);
  assert.equal(await lib.get(a.id), null);
  assert.deepEqual((await lib.list()).map((m) => m.id), ["up-1"]);
}

test("library over the memory store", async () => { await exercise(createLibrary(memoryStore())); });

test("library over (fake) IndexedDB, persisting across opens", async () => {
  const idb = fakeIDB();
  const lib = createLibrary(await idbStore(idb, "swarmlab-traces"));
  await exercise(lib);
  const again = createLibrary(await idbStore(idb, "swarmlab-traces"));
  assert.deepEqual((await again.list()).map((m) => m.id), ["up-1"]);
  assert.equal(again.persistent, true);
});

test("openLibrary falls back to memory without IndexedDB", async () => {
  const lib = await openLibrary({ indexedDB: null });
  assert.equal(lib.persistent, false);
  assert.deepEqual(await lib.list(), []);
});

test("put whitelists fields: extra keys (e.g. an API key) are never stored", async () => {
  const idb = fakeIDB();
  const lib = createLibrary(await idbStore(idb, "t"));
  await lib.put({ id: "r1", source: "run", blob: blob(), card: card("r1"), apiKey: "sk-placeholder-canary", settings: { x: 1 } });
  const db = idb.dbs.get("t");
  const dump = JSON.stringify([...db.stores.values()].map((s) => [...s.rows.values()].map((r) => ({ ...r, blob: undefined }))));
  assert.ok(!dump.includes("canary") && !dump.includes("settings"));
  const g = await lib.get("r1");
  assert.ok(!("apiKey" in g) && !("settings" in g));
});

test("put validates id, source, blob and card", async () => {
  const lib = createLibrary(memoryStore());
  const ok = { id: "r1", source: "run", blob: blob(), card: card("r1") };
  const bad = async (patch, c) => assert.rejects(lib.put({ ...ok, ...patch }), (e) => e instanceof TraceError && e.code === c);
  await bad({ id: "../x" }, "E_TRACE_ID");
  await bad({ id: "" }, "E_TRACE_ID");
  await bad({ source: "web" }, "E_TRACE_SOURCE");
  await bad({ blob: "bytes" }, "E_TRACE_BLOB");
  await bad({ card: card("other") }, "E_TRACE_CARD");
  await bad({ card: null }, "E_TRACE_CARD");
  // a bad name falls back to <id>.zip
  assert.equal((await lib.put({ ...ok, name: "../evil name.zip" })).name, "r1.zip");
});

const E = (name, s = "{}") => ({ name, data: enc.encode(s) });

test("traceFromEntries: lab zip, macbook runs/<id>/ tree, bare files, junk", () => {
  const lab = traceFromEntries([E(`${RUN}/run.json`, JSON.stringify({ run_id: RUN })),
    E(`${RUN}/a0/steps.jsonl`, ""), E("__MACOSX/._x"), E(`${RUN}/.DS_Store`)]);
  assert.equal(lab.runId, RUN);
  assert.deepEqual([...lab.files.keys()].sort(), ["a0/steps.jsonl", "run.json"]);
  assert.deepEqual(lab.entries.map((e) => e.name).sort(), [`${RUN}/a0/steps.jsonl`, `${RUN}/run.json`]);

  const mac = traceFromEntries([E("runs/forum-multi4-r1/run.json", '{"placeholder": 1}'), E("runs/forum-multi4-r1/a3/steps.jsonl", ""),
    E("runs/forum-multi4-r1/board_events.jsonl", "")]);
  assert.equal(mac.runId, "forum-multi4-r1");
  assert.ok(mac.files.has("a3/steps.jsonl") && mac.files.has("board_events.jsonl"));

  // a run directory with only agent logs (incomplete), named by its directory
  assert.equal(traceFromEntries([E("my-run/a0/steps.jsonl", "")]).runId, "my-run");
  // bare files: the id must come from run.json
  assert.equal(traceFromEntries([E("run.json", '{"run_id": "r-bare"}'), E("a0/steps.jsonl", "")]).runId, "r-bare");
  assert.throws(() => traceFromEntries([E("run.json", "{}")]), (e) => e.code === "E_RUN_ID");
  // a unsafe run_id in run.json is ignored in favour of the directory name
  assert.equal(traceFromEntries([E("dir-1/run.json", '{"run_id": "../../x"}')]).runId, "dir-1");
});

test("traceFromEntries: non-run and multi-run uploads are rejected with fixed messages", () => {
  assert.throws(() => traceFromEntries([E("photos/a.jpg"), E("notes.txt")]), (e) => e.code === "E_NOT_A_RUN" && /swarm trace/.test(e.message));
  assert.throws(() => traceFromEntries([]), (e) => e.code === "E_NOT_A_RUN");
  assert.throws(() => traceFromEntries([E("runs/r1/run.json"), E("runs/r2/run.json")]), (e) => e.code === "E_MANY_RUNS");
});

test("trace ids that are all dots (zip-slip as a directory name) are refused", async () => {
  // run.json run_id ".." falls back to the directory name; a bare ".." id is refused
  assert.equal(traceFromEntries([E("dir-2/run.json", '{"run_id": ".."}')]).runId, "dir-2");
  assert.throws(() => traceFromEntries([E("run.json", '{"run_id": ".."}')]), (e) => e.code === "E_RUN_ID");
  assert.throws(() => traceFromEntries([E("run.json", '{"run_id": ".hidden"}')]), (e) => e.code === "E_RUN_ID");
  const lib = createLibrary(memoryStore());
  await assert.rejects(lib.put({ id: "..", source: "upload", blob: blob(), card: card("..") }), (e) => e.code === "E_TRACE_ID");
});

test("a partial snapshot is flagged, and the final put of the same id clears the flag (keeps created)", async () => {
  const lib = createLibrary(memoryStore());
  const m1 = await lib.put({ id: "run-partial-0001", source: "run", partial: true, blob: blob(), card: card("run-partial-0001") });
  assert.equal(m1.partial, true);
  assert.equal((await lib.list())[0].partial, true);
  const m2 = await lib.put({ id: "run-partial-0001", source: "run", blob: blob("final placeholder"), card: card("run-partial-0001", "ffff0000") });
  assert.equal(m2.partial, undefined);
  assert.equal(m2.created, m1.created);
  assert.equal((await lib.get("run-partial-0001")).partial, undefined);
  await lib.put({ id: "run-partial-0002", source: "run", partial: "yes", blob: blob(), card: card("run-partial-0002") });
  assert.equal((await lib.get("run-partial-0002")).partial, undefined, "only partial: true is stored");
});
