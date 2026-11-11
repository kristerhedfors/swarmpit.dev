#!/usr/bin/env node
// Two-VM spike server (docs/browser-two-vm.md). Serves this directory at / and a frozen copy of the VM image
// at /vm/ (default /tmp/twovm/vm, so an image rebuild in dist/vm cannot change the spike mid-run), with the
// isolation headers CheerpX needs (COOP/COEP/CORP), Range, ETag and Last-Modified.
//   node lab/web/spikes/twovm/server.mjs [port]          (default 8790; TWOVM_VM_DIR overrides the image dir)
//   import { createTwoVmServer } from "./server.mjs"
import { createServer } from "node:http";
import { createReadStream, realpathSync, statSync } from "node:fs";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HEADERS = { "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp",
  "Cross-Origin-Resource-Policy": "same-origin", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store" };
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
  ".json": "application/json", ".py": "text/plain; charset=utf-8" };

function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!m || (!m[1] && !m[2])) return null;
  let start, end;
  if (!m[1]) { const n = Number(m[2]); if (n === 0) return "bad"; [start, end] = [Math.max(0, size - n), size - 1]; }
  else [start, end] = [Number(m[1]), m[2] ? Math.min(Number(m[2]), size - 1) : size - 1];
  return start > end || start >= size ? "bad" : [start, end];
}

export function createTwoVmServer({ port = 8790, host = "127.0.0.1", vmDir = process.env.TWOVM_VM_DIR || "/tmp/twovm/vm" } = {}) {
  const mounts = [["/vm/", vmDir], ["/", HERE]];
  const stats = { requests: 0, rangeBytes: 0 };
  const server = createServer((req, res) => {
    stats.requests++;
    const path = new URL(req.url, "http://x").pathname;
    const headers = { ...HEADERS };
    let file = null;
    for (const [prefix, dir] of mounts) {
      if (!path.startsWith(prefix)) continue;
      let rel;
      try { rel = normalize(decodeURIComponent(path.slice(prefix.length))); } catch { break; }
      if (rel.startsWith("..")) break;
      if (rel === "." || rel === "" || rel.endsWith("/")) rel = join(rel, "index.html");
      file = join(dir, rel);
      break;
    }
    let st = null;
    try { st = file && statSync(file); } catch {}
    if (!st || !st.isFile()) { res.writeHead(404, headers); return res.end("not found"); }
    headers["Content-Type"] = TYPES[extname(file)] || "application/octet-stream";
    headers["Accept-Ranges"] = "bytes";
    headers["Last-Modified"] = st.mtime.toUTCString();
    headers["ETag"] = `"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}"`;
    const range = req.headers.range === undefined ? null : parseRange(req.headers.range, st.size);
    if (range === "bad") { res.writeHead(416, { ...headers, "Content-Range": `bytes */${st.size}` }); return res.end(); }
    if (range) {
      const [start, end] = range;
      stats.rangeBytes += end - start + 1;
      res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${st.size}`, "Content-Length": end - start + 1 });
      return req.method === "HEAD" ? res.end() : createReadStream(file, { start, end }).pipe(res);
    }
    res.writeHead(200, { ...headers, "Content-Length": st.size });
    return req.method === "HEAD" ? res.end() : createReadStream(file).pipe(res);
  });
  return new Promise((ok, fail) => {
    server.once("error", fail);
    server.listen(port, host, () => {
      const p = server.address().port;
      ok({ url: `http://${host}:${p}`, port: p, stats,
        close: () => new Promise((r) => { server.closeAllConnections?.(); server.close(() => r()); }) });
    });
  });
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { url } = await createTwoVmServer({ port: Number(process.argv[2] || 8790) });
  console.log(`two-vm spike server on ${url}/`);
}
