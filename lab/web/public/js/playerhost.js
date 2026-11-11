// Glue between the trace library, the swarmpit.dev replay player and the converter (docs/browser-swarm.md §5, §7).
// The player and its in-memory loader are the compiled site modules served under /player/js/ (build.sh copies
// site/public/js into dist/player/; dev-server.mjs serves it from site/public); the converter is module C's
// /player/js/convert/index.js, loaded on first use so the page still lists and downloads traces without it.
//
// Every trace the player shows is a card in the selector's 'Local' lane. Stored traces are converted lazily: the
// memory loader's resolver reads the zip blob from the library, unzips it (zip.js readZip), converts it and caches
// the bundles in memory (and in the library record, so the next reload skips the conversion).
import { makeZip, readZip } from "./zip.js";
import { traceFromEntries } from "./library.js";

const PLAYER_BASE = "/player/js/";
let convertMod = null;

// The converter module, or a fixed error the page can show.
export async function loadConverter(base = PLAYER_BASE) {
  if (!convertMod) {
    convertMod = import(`${base}convert/index.js`).catch((e) => { convertMod = null; throw Object.assign(new Error("E_CONVERTER"), { cause: e }); });
  }
  return convertMod;
}

// Card fixes for the lab: the library id is the card id, and local traces are always listed.
const localCard = (card, id) => ({ ...card, id, hidden: false });

// RunFiles (Map rel -> bytes) -> {card, core, detail} with the card normalised to `id`.
export async function convertFiles(files, id, opts = {}) {
  const { convertRun } = await loadConverter(opts.base);
  const c = await convertRun(files, { live: !!opts.live });
  return { card: localCard(c.card, id), core: c.core, detail: c.detail ?? null };
}

// A zip blob or folder entries -> {runId, files, entries, blob}: blob is the input zip as-is, or (folder upload)
// a stored zip of the normalised "<runId>/..." entries.
export async function traceFromUpload({ blob = null, entries = null }) {
  const raw = entries ?? await readZip(blob);
  const t = traceFromEntries(raw);
  return { ...t, blob: blob ?? makeZip(t.entries) };
}

// createPlayerHost({root, library, callbacks, base?}) -> host
//   callbacks: {onNew, onUpload, onDownload(id), onDelete(id)} (the selector action chips)
//   host: {player, loader, addTrace(meta, bundles?), showLive(conv), endLive(id), removeTrace(id), select(id),
//          ids(), isLive(id)}
export async function createPlayerHost({ root, library, callbacks = {}, base = PLAYER_BASE }) {
  const [{ createPlayer }, { createMemoryLoader, memoryIndex }, { detectEnv }, { localLabel }] = await Promise.all([
    import(`${base}player/player.js`), import(`${base}data/memoryLoader.js`), import(`${base}util/env.js`),
    import(`${base}ui/selector.js`)]);
  const live = new Set(), partial = new Set();
  const index = memoryIndex([]);

  // Lazy conversion of a stored trace (selector pick, reload). A converter newer than the stored card yields a new
  // hash: the record and the selector are updated, and this load retries once with the new card.
  // `signal` is set when a load asked (core/detail) and absent for a selector hover prefetch, which must not switch
  // the player to that trace.
  async function resolve(card, signal) {
    const rec = await library.get(card.id);
    if (!rec || !rec.blob) return null;
    let core = rec.core, detail = rec.detail, c = rec.card;
    if (!core) {
      const t = traceFromEntries(await readZip(rec.blob));
      ({ card: c, core, detail } = await convertFiles(t.files, card.id, { base }));
      await library.setCard(card.id, c, { core, detail }).catch(() => {});
      if (c.hash !== card.hash) queueMicrotask(() => { player.addRun(c); if (signal && !signal.aborted) void player.load(c.id); });
    }
    return { card: localCard(c, card.id), core, detail };
  }
  const loader = createMemoryLoader({ index, resolve });

  const env = detectEnv();
  const player = createPlayer({
    root, index, loader, env, url: {}, narration: false, urlKey: "trace", findings: false, graphKeyOpen: false,
    onNew: callbacks.onNew, onUpload: callbacks.onUpload, onDownload: callbacks.onDownload, onDelete: callbacks.onDelete,
    local: { match: () => true, title: "Traces",
      label: (c) => localLabel(c) + (live.has(c.id) ? " · live" : partial.has(c.id) ? " · partial" : "") },
  });

  const listed = (id) => index.runs.some((r) => r.id === id);
  return {
    player, loader,
    ids: () => index.runs.map((r) => r.id),
    isLive: (id) => live.has(id),
    // a library entry (list() item); bundles {core, detail} when already converted
    addTrace(meta, bundles) {
      const card = localCard(meta.card, meta.id);
      if (meta.partial) partial.add(meta.id); else partial.delete(meta.id);
      if (bundles?.core) loader.set({ card, core: bundles.core, detail: bundles.detail ?? null });
      else loader.delete(meta.id);
      if (listed(meta.id)) return player.updateRun(card);
      player.addRun(card);
      return Promise.resolve();
    },
    // a live conversion {card, core, detail} (card.id = the swarm's run id): added and selected on the first one,
    // then updated in place
    async showLive(conv) {
      const { id } = conv.card;
      const first = !live.has(id) || !listed(id);
      live.add(id);
      loader.set(conv);
      if (first) { player.addRun(conv.card); await player.load(id); }
      else await player.updateRun(conv.card);
    },
    endLive(id) { live.delete(id); const c = index.runs.find((r) => r.id === id); if (c) player.addRun(c); },
    async removeTrace(id) { live.delete(id); partial.delete(id); loader.delete(id); await player.removeRun(id); },
    select: (id) => player.load(id),
  };
}
