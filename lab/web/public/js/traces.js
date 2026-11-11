// Trace library actions of the lab page (docs/browser-swarm.md §7): ingest a zip or folder into the library and
// the player, the upload dialog (file pickers, drag and drop), download and delete. Messages are fixed text.
import { $, codeOf, fail, notice } from "./page.js";
import { ZIP_LIMITS, entriesFromFiles } from "./zip.js";
import { convertFiles, traceFromUpload } from "./playerhost.js";

// The upload dialog's message for an ingest failure.
function uploadMessage(e) {
  const code = codeOf(e);
  if (e?.name === "TraceError" && typeof e.message === "string" && e.message !== code) return e.message;  // fixed texts
  if (code.startsWith("E_ZIP_LIMIT") || code === "E_ZIP_SIZE") return `This upload is too large (${code}).`;
  if (code.startsWith("E_ZIP")) return `This is not a readable zip (${code}).`;
  if (code === "E_CONVERTER") return "The trace converter is not available in this build.";
  if (code === "E_TRACE_RUNNING") return "This trace belongs to the swarm that is running now.";
  if (code === "E_TRACE_EXISTS") return "A different trace with this run id is already stored; delete it first.";
  return `Could not convert this trace (${code}).`;
}

async function sameBytes(a, b) {
  if (!a || !b || a.size !== b.size) return false;
  const [x, y] = await Promise.all([a.arrayBuffer(), b.arrayBuffer()].map((p) => p.then((ab) => new Uint8Array(ab))));
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return false;
  return true;
}

// A folder upload's download name: its top directory, as a safe "<name>.zip".
const folderZipName = (entries) => `${(entries[0]?.name.split("/")[0] || "trace").replace(/[^\w.-]/g, "_").slice(0, 120)}.zip`;

// createTraceActions({lab, view, running, unsaved})
//   lab: {library, host} (filled in by app.js boot; null until then)
//   running(): the running swarm or null; unsaved(): {filename, blob} of a finished run not stored, or null
//   -> {ingest, uploadZip, uploadFolder, download, remove, openUpload, wireUploads}
export function createTraceActions({ lab, view, running, unsaved }) {
  // {blob | entries, name, source} -> stored, shown and selected trace; returns its id. Throws with a fixed message.
  async function ingest({ blob = null, entries = null, name, source }) {
    const { library, host } = lab;
    if (!host) throw fail("E_PLAYER");
    if (blob && blob.size > ZIP_LIMITS.maxTotalBytes) throw fail("E_ZIP_LIMIT");
    const t = await traceFromUpload({ blob, entries });
    // an upload must not replace the trace of the swarm that is running (its live trace, then its stored zip), nor
    // silently replace a stored trace: only the same bytes again (a re-upload) are accepted, never over a run's own
    if (source !== "run") {
      if (running() && t.runId === running().runId) throw fail("E_TRACE_RUNNING");
      const prev = await library.get(t.runId);
      if (prev) {
        if (!(await sameBytes(prev.blob, t.blob))) throw fail("E_TRACE_EXISTS");
        if (prev.source === "run") { await host.select(prev.id); return prev.id; }  // the same run's zip: keep its record
      }
    }
    const conv = await convertFiles(t.files, t.runId);
    const meta = await library.put({ id: t.runId, name, source, blob: t.blob, card: conv.card, nFiles: t.files.size,
      core: conv.core, detail: conv.detail });
    await host.addTrace(meta, conv);
    await host.select(meta.id);
    return meta.id;
  }

  // read(): the ingest input {blob | entries, name}; the result is shown in the upload dialog
  async function upload(read, { clearNotice = false } = {}) {
    $("uploadResult").textContent = "reading…";
    try {
      const id = await ingest({ ...(await read()), source: "upload" });
      $("uploadResult").textContent = "Added.";
      $("uploadDlg").close();
      if (clearNotice) notice("");
      return { ok: true, id };
    } catch (e) {
      $("uploadResult").textContent = uploadMessage(e);
      return { ok: false, code: codeOf(e) };
    }
  }
  const uploadZip = (file) => upload(async () => ({ blob: file, name: file.name }), { clearNotice: true });
  const uploadFolder = (files) => upload(async () => {
    const entries = await entriesFromFiles(files);
    return { entries, name: folderZipName(entries) };
  });

  async function download(id) {
    const rec = await lab.library.get(id);
    if (!rec?.blob) {
      // a finished run whose zip could not be stored is still in page memory
      const u = unsaved();
      if (u && u.filename.startsWith(id)) { view.save(u.filename, u.blob); return; }
      notice(lab.host?.isLive(id) ? "This run is still going; download it when it has ended." : "This trace has no stored zip.", true);
      return;
    }
    view.save(rec.name || `${id}.zip`, rec.blob);
  }

  async function remove(id) {
    if (running() && lab.host?.isLive(id)) { notice("Stop the running swarm before deleting its trace.", true); return; }
    if (!confirm("Delete this trace from this browser?")) return;
    await lab.library.delete(id);
    await lab.host.removeTrace(id);
    notice("");
  }

  function openUpload() {
    $("uploadResult").textContent = "";
    if (!$("uploadDlg").open) $("uploadDlg").showModal();
  }

  // the upload dialog's buttons and file inputs, and a zip dropped anywhere on the page
  function wireUploads() {
    $("emptyUpload").addEventListener("click", openUpload);
    $("closeUpload").addEventListener("click", () => $("uploadDlg").close());
    $("pickZip").addEventListener("click", () => $("zipInput").click());
    $("pickDir").addEventListener("click", () => $("dirInput").click());
    $("zipInput").addEventListener("change", (e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void uploadZip(f); });
    $("dirInput").addEventListener("change", (e) => { const fs = [...(e.target.files || [])]; e.target.value = ""; if (fs.length) void uploadFolder(fs); });
    document.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); });
    document.addEventListener("drop", (e) => {
      const f = e.dataTransfer?.files?.[0];
      if (!f) return;
      e.preventDefault();
      if (!/\.zip$/i.test(f.name)) { notice("Drop a trace zip (or use upload for a folder).", true); return; }
      void uploadZip(f).then((r) => { if (!r.ok) notice($("uploadResult").textContent, true); });
    });
  }

  return { ingest, uploadZip, uploadFolder, download, remove, openUpload, wireUploads };
}
