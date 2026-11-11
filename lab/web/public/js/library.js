// Local trace library (docs/browser-swarm.md §7): IndexedDB "swarmlab-traces" holds each trace's raw zip blob (the
// download is exactly that blob) plus its converted card, so traces survive reloads. Nothing here leaves the browser.
// Two object stores keep the selector's list cheap: "traces" (small metadata + card, read by list()) and "blobs"
// (zip blob + optional cached core/detail bundles, read only by get()). Records are whitelisted field by field, so
// nothing else (in particular no API key or other run setting) can be persisted through put().
//
// Also here (pure, node-testable): traceFromEntries(), which finds the one run directory inside an uploaded zip or
// folder (a lab zip "<run_id>/...", a macbook "runs/<id>/..." tree, or bare run files) and rejects anything else.

export const DB_NAME = "swarmlab-traces";
const DB_VERSION = 1;
const META = "traces", DATA = "blobs";
// a trace id is also a zip directory name and a download file name: no leading dot (".", ".." would be zip-slip)
const ID_RE = /^[A-Za-z0-9][\w.-]{0,119}$/;
const SOURCES = new Set(["run", "upload"]);

export class TraceError extends Error {
  constructor(code, message) { super(message || code); this.name = "TraceError"; this.code = code; }
}

// ---- stores: {getAll(store), get(store, key), put(store, value), delete(store, key)}

const isBlob = (b) => typeof Blob !== "undefined" && b instanceof Blob;
const reqP = (r) => new Promise((ok, bad) => { r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error || new Error("E_IDB")); });

export function idbStore(idb = globalThis.indexedDB, name = DB_NAME) {
  if (!idb) return Promise.reject(new TraceError("E_IDB_UNAVAILABLE"));
  const open = idb.open(name, DB_VERSION);
  open.onupgradeneeded = () => {
    const db = open.result;
    if (!db.objectStoreNames.contains(META)) db.createObjectStore(META, { keyPath: "id" });
    if (!db.objectStoreNames.contains(DATA)) db.createObjectStore(DATA, { keyPath: "id" });
  };
  return reqP(open).then((db) => {
    // one transaction per call; resolves after the transaction commits (writes) or the request succeeds (reads)
    const run = (stores, mode, fn) => new Promise((ok, bad) => {
      const tx = db.transaction(stores, mode);
      let result;
      Promise.resolve(fn(tx)).then((r) => { result = r; }, bad);
      tx.oncomplete = () => ok(result);
      tx.onerror = () => bad(tx.error || new Error("E_IDB"));
      tx.onabort = () => bad(tx.error || new Error("E_IDB_ABORT"));
    });
    // The zip is stored as an ArrayBuffer and handed out as a Blob again: WebKit cannot store Blobs in IndexedDB in
    // ephemeral / private sessions (the put fails). Records written as Blobs read back unchanged.
    const toBlob = (v) => (v && v.blob instanceof ArrayBuffer ? { ...v, blob: new Blob([v.blob], { type: "application/zip" }) } : v);
    return {
      persistent: true,
      getAll: (s) => run([s], "readonly", (tx) => reqP(tx.objectStore(s).getAll())),
      get: (s, k) => run([s], "readonly", (tx) => reqP(tx.objectStore(s).get(k))).then((v) => (s === DATA ? toBlob(v) : v)),
      // meta + data written in one transaction, so a trace is never half-stored (the bytes are read before it opens:
      // an await inside would let the transaction commit early)
      putPair: async (meta, data) => {
        const d = isBlob(data.blob) ? { ...data, blob: await data.blob.arrayBuffer() } : data;
        return run([META, DATA], "readwrite", (tx) => { tx.objectStore(DATA).put(d); tx.objectStore(META).put(meta); });
      },
      put: (s, v) => run([s], "readwrite", (tx) => { tx.objectStore(s).put(v); }),
      deletePair: (k) => run([META, DATA], "readwrite", (tx) => { tx.objectStore(META).delete(k); tx.objectStore(DATA).delete(k); }),
      close: () => db.close(),
    };
  });
}

// In-memory fallback (no IndexedDB, e.g. some private modes) and unit tests: same interface, nothing persists.
export function memoryStore() {
  const m = { [META]: new Map(), [DATA]: new Map() };
  const clone = (v) => (v === undefined ? undefined : { ...v });
  return {
    persistent: false,
    getAll: async (s) => [...m[s].values()].map(clone),
    get: async (s, k) => clone(m[s].get(k)),
    putPair: async (meta, data) => { m[DATA].set(data.id, { ...data }); m[META].set(meta.id, { ...meta }); },
    put: async (s, v) => { m[s].set(v.id, { ...v }); },
    deletePair: async (k) => { m[META].delete(k); m[DATA].delete(k); },
    close: () => {},
  };
}

// ---- library

function checkCard(card, id) {
  if (!card || typeof card !== "object" || card.id !== id || typeof card.hash !== "string") throw new TraceError("E_TRACE_CARD");
}
// only partial: true is stored (a running swarm's snapshot); anything else leaves the field out
const partialFlag = (rec) => (rec.partial === true ? { partial: true } : {});
// the stored metadata fields of a get() result (the whitelist of the "traces" store)
const metaOf = (rec) => {
  const { id, name, source, created, updated, bytes, nFiles, card } = rec;
  return { id, name, source, created, updated, bytes, nFiles, card, ...partialFlag(rec) };
};

// createLibrary(store) -> {persistent, list(), get(id), put(rec), setCard(id, card, bundles?), delete(id)}
//   list(): [{id, name, source, created, updated, bytes, nFiles, card, partial?}] newest first (no blobs)
//   get(id): the list entry + {blob, core, detail} (core/detail null unless cached), or null
//   put({id, name?, source, blob, card, nFiles?, core?, detail?, partial?}): stores (replaces) a trace; returns the
//     list entry. partial: true marks a running swarm's periodic snapshot (run.js), replaced by its final zip.
export function createLibrary(store) {
  async function put(rec) {
    if (!rec || typeof rec !== "object") throw new TraceError("E_TRACE_RECORD");
    const { id } = rec;
    if (typeof id !== "string" || !ID_RE.test(id)) throw new TraceError("E_TRACE_ID");
    if (!isBlob(rec.blob)) throw new TraceError("E_TRACE_BLOB");
    if (!SOURCES.has(rec.source)) throw new TraceError("E_TRACE_SOURCE");
    checkCard(rec.card, id);
    const prev = await store.get(META, id);
    const now = Date.now();
    const name = typeof rec.name === "string" && /^[\w.-]{1,160}\.zip$/.test(rec.name) ? rec.name : `${id}.zip`;
    const meta = { id, name, source: rec.source, created: prev?.created ?? now, updated: now, bytes: rec.blob.size,
      nFiles: Number.isInteger(rec.nFiles) ? rec.nFiles : null, card: rec.card, ...partialFlag(rec) };
    const data = { id, blob: rec.blob, core: rec.core ?? null, detail: rec.detail ?? null };
    await store.putPair(meta, data);
    return meta;
  }
  return {
    get persistent() { return store.persistent; },
    async list() {
      const all = await store.getAll(META);
      return all.sort((a, b) => (b.created - a.created) || (a.id < b.id ? 1 : -1));
    },
    async get(id) {
      const meta = await store.get(META, id);
      if (!meta) return null;
      const data = (await store.get(DATA, id)) || {};
      return { ...meta, blob: data.blob ?? null, core: data.core ?? null, detail: data.detail ?? null };
    },
    put,
    // a re-conversion (new converter version): replace card and cached bundles, keep blob and timestamps
    async setCard(id, card, bundles = {}) {
      const cur = await this.get(id);
      if (!cur) throw new TraceError("E_TRACE_MISSING");
      checkCard(card, id);
      await store.putPair({ ...metaOf(cur), card, updated: Date.now() },
        { id, blob: cur.blob, core: bundles.core ?? null, detail: bundles.detail ?? null });
    },
    delete: (id) => store.deletePair(id),
  };
}

// IndexedDB library, falling back to memory (persistent = false) when IndexedDB cannot be opened.
export async function openLibrary({ indexedDB = globalThis.indexedDB, name = DB_NAME } = {}) {
  try { return createLibrary(await idbStore(indexedDB, name)); } catch { return createLibrary(memoryStore()); }
}

// ---- uploads: entries -> one run directory

const JUNK = (n) => n.startsWith("__MACOSX/") || /(^|\/)\.DS_Store$/.test(n) || /(^|\/)\._[^/]*$/.test(n);
const RUN_MARKERS = [/^(.*\/)?run\.json$/, /^(.*\/)?manifest\.json$/, /^(.*\/)?a\d{1,2}\/steps\.jsonl$/, /^(.*\/)?board_events\.jsonl$/];
const dec = new TextDecoder();

function prefixOf(name) {
  for (const re of RUN_MARKERS) { const m = re.exec(name); if (m) return m[1] || ""; }
  return null;
}
const depth = (p) => (p ? p.split("/").length - 1 : 0);

// The run id from run.json / manifest.json `run_id`, else the run directory's name; null if neither is a safe id.
function runIdOf(files, prefix) {
  for (const f of ["run.json", "manifest.json"]) {
    const b = files.get(f);
    if (!b) continue;
    try { const j = JSON.parse(dec.decode(b)); if (typeof j?.run_id === "string" && ID_RE.test(j.run_id)) return j.run_id; } catch { /* not JSON */ }
  }
  const seg = prefix.replace(/\/$/, "").split("/").pop();
  return seg && ID_RE.test(seg) && seg !== "runs" ? seg : null;
}

// traceFromEntries([{name, data}]) -> {runId, files: Map(rel -> Uint8Array), entries: [{name: "<runId>/<rel>", data}]}
// Throws TraceError E_NOT_A_RUN (no run.json, manifest.json, a<i>/steps.jsonl or board_events.jsonl), E_MANY_RUNS
// (several run directories) or E_RUN_ID. Messages are fixed text for the page.
export function traceFromEntries(entries) {
  const live = entries.filter((e) => !JUNK(e.name));
  const prefixes = new Set();
  for (const e of live) { const p = prefixOf(e.name); if (p !== null) prefixes.add(p); }
  if (!prefixes.size) throw new TraceError("E_NOT_A_RUN", "This does not look like a swarm trace: no run.json or agent steps found.");
  // the shallowest prefixes are run directories (a<i>/steps.jsonl under a run dir maps to the same prefix)
  const min = Math.min(...[...prefixes].map(depth));
  const top = [...prefixes].filter((p) => depth(p) === min);
  if (top.length > 1) throw new TraceError("E_MANY_RUNS", "This upload holds several runs; upload one run at a time.");
  const prefix = top[0];
  const files = new Map();
  for (const e of live) if (e.name.startsWith(prefix) && e.name.length > prefix.length) files.set(e.name.slice(prefix.length), e.data);
  const runId = runIdOf(files, prefix);
  if (!runId) throw new TraceError("E_RUN_ID", "This trace has no usable run id.");
  return { runId, files, entries: [...files].map(([rel, data]) => ({ name: `${runId}/${rel}`, data })) };
}
