#!/usr/bin/env node
// Local dev server for the in-browser lab: serves lab/web/public (and lab/web/spikes at /spikes/) with the
// production headers from public/_headers, and dist/vm at /vm/ with Range support. /player/js/ and /player/css/
// serve the compiled swarmpit.dev player from site/public (run `cd site && npm run build` first), as build.sh copies
// them into dist/player/; only js and css are mounted, never site/public/data.
//   node lab/web/dev-server.mjs [port]                     (SEED_FILE=<path> overrides the placeholder seed)
//   import { createLabServer } from "./dev-server.mjs"     (port 0 picks a free port; used by test/e2e.mjs)
import { createServer } from "node:http";
import { createReadStream, readFileSync, realpathSync, statSync } from "node:fs";
import { extname, join, normalize, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..");
const SITE = join(ROOT, "site", "public");
const MOUNTS = [["/vm/", join(ROOT, "dist", "vm")], ["/spikes/", join(HERE, "spikes")],
  ["/player/js/", join(SITE, "js")], ["/player/css/", join(SITE, "css")], ["/", join(HERE, "public")]];
// Local runs use a neutral placeholder seed; the real seed is only copied into dist by build.sh.
export const PLACEHOLDER_SEED = join(HERE, "spikes", "placeholder-seed.json");
// The production /* headers from public/_headers, so the CSP is exercised locally.
const PROD_HEADERS = Object.fromEntries(readFileSync(join(HERE, "public", "_headers"), "utf8")
  .split("\n").filter((l) => /^\s+\S+:/.test(l)).map((l) => { const i = l.indexOf(":"); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; }));
// The spikes are a local test harness that is never deployed; they get the isolation headers without the CSP
// so probe code can change freely without tracking the page's allowlist.
const { "Content-Security-Policy": _csp, ...SPIKE_HEADERS } = PROD_HEADERS;
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
  ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".wasm": "application/wasm" };

class BadPath extends Error {}

function resolve(urlPath, aliases) {
  if (aliases[urlPath]) return aliases[urlPath];
  for (const [prefix, dir] of MOUNTS) {
    if (!urlPath.startsWith(prefix)) continue;
    let rel;
    try { rel = normalize(decodeURIComponent(urlPath.slice(prefix.length))); } catch { throw new BadPath(); }
    if (rel.startsWith("..")) return null;
    if (rel === "." || rel === "" || rel.endsWith("/")) rel = join(rel, "index.html");
    return join(dir, rel);
  }
  return null;
}

// A Range header as [start, end] (inclusive), "unsatisfiable" (-> 416), or null when it is not a single
// bytes range (multi-range or malformed: ignored, the full file is served, RFC 9110 §14.2).
function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!m || (!m[1] && !m[2])) return null;
  let start, end;
  if (!m[1]) {  // suffix range: the last N bytes, clamped to the file
    const n = Number(m[2]);
    if (n === 0) return "unsatisfiable";
    [start, end] = [Math.max(0, size - n), size - 1];
  } else {
    [start, end] = [Number(m[1]), m[2] ? Math.min(Number(m[2]), size - 1) : size - 1];
  }
  return start > end || start >= size ? "unsatisfiable" : [start, end];
}

function handler(aliases) {
  return (req, res) => {
    const path = new URL(req.url, "http://x").pathname;
    const headers = { ...(path.startsWith("/spikes/") ? SPIKE_HEADERS : PROD_HEADERS), "Cache-Control": "no-store" };
    if (path.startsWith("/vm/")) headers["Cross-Origin-Resource-Policy"] = "cross-origin";
    let file;
    try { file = resolve(path, aliases); } catch { res.writeHead(400, headers); return res.end("bad request"); }
    let st;
    try { st = file && statSync(file); } catch { st = null; }
    // an extensionless path serves <path>.html, as Workers static assets do (html_handling auto-trailing-slash)
    if ((!st || !st.isFile()) && file && !extname(file)) {
      try { const html = statSync(file + ".html"); if (html.isFile()) { file += ".html"; st = html; } } catch {}
    }
    if (!st || !st.isFile()) { res.writeHead(404, headers); return res.end("not found"); }
    headers["Content-Type"] = TYPES[extname(file)] || "application/octet-stream";
    headers["Accept-Ranges"] = "bytes";
    headers["Last-Modified"] = st.mtime.toUTCString();  // HttpBytesDevice refuses images without a validator
    headers["ETag"] = `"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}"`;
    const range = req.headers.range === undefined ? null : parseRange(req.headers.range, st.size);
    if (range === "unsatisfiable") {
      res.writeHead(416, { ...headers, "Content-Range": `bytes */${st.size}` });
      return res.end();
    }
    if (range) {
      const [start, end] = range;
      res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${st.size}`, "Content-Length": end - start + 1 });
      return req.method === "HEAD" ? res.end() : createReadStream(file, { start, end }).pipe(res);
    }
    res.writeHead(200, { ...headers, "Content-Length": st.size });
    return req.method === "HEAD" ? res.end() : createReadStream(file).pipe(res);
  };
}

// Starts the server; resolves to {url, port, close()}.
export function createLabServer({ port = 8788, host = "127.0.0.1", seedFile = PLACEHOLDER_SEED } = {}) {
  const aliases = { "/data/seed.json": seedFile, "/vm-manifest.json": join(ROOT, "dist", "vm", "manifest.json") };
  const server = createServer(handler(aliases));
  return new Promise((ok, fail) => {
    server.once("error", fail);
    server.listen(port, host, () => {
      const p = server.address().port;
      ok({ url: `http://${host}:${p}`, port: p,
        close: () => new Promise((r) => { server.closeAllConnections?.(); server.close(() => r()); }) });
    });
  });
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { url } = await createLabServer({ port: Number(process.argv[2] || 8788), seedFile: process.env.SEED_FILE || PLACEHOLDER_SEED });
  console.log(`lab dev server on ${url}/`);
}
