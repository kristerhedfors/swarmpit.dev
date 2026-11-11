#!/usr/bin/env node
// Tiny zero-dependency static server for local dev: node tools/serve.mjs [root=public] [port=8080]
// Correct MIME types for ES modules, no caching, path-traversal safe, binds 127.0.0.1 only.
// Single-range `Range: bytes=a-b` requests get 206 (narration mp3 seeking behaves like GitHub Pages).
import { createServer } from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { resolve, join, extname, sep } from 'node:path';

const root = resolve(process.argv[2] ?? 'public');
const port = Number(process.argv[3] ?? process.env.PORT ?? 8080);
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.webp': 'image/webp', '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2', '.map': 'application/json', '.mp3': 'audio/mpeg',
};

function fileFor(urlPath) {
  let p;
  try { p = decodeURIComponent(urlPath.split('?')[0].split('#')[0]); } catch { return null; }
  const abs = resolve(join(root, p));
  if (abs !== root && !abs.startsWith(root + sep)) return null;
  try {
    const st = statSync(abs);
    if (st.isDirectory()) { const idx = join(abs, 'index.html'); return statSync(idx).isFile() ? idx : null; }
    return st.isFile() ? abs : null;
  } catch { return null; }
}

createServer((req, res) => {
  if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405).end(); return; }
  let f = fileFor(req.url ?? '/');
  let status = 200;
  if (!f) { f = fileFor('/404.html'); status = 404; }
  if (!f) { res.writeHead(404, { 'content-type': 'text/plain' }).end('not found'); return; }
  const size = statSync(f).size;
  const head = { 'content-type': MIME[extname(f).toLowerCase()] ?? 'application/octet-stream',
    'cache-control': 'no-cache', 'x-content-type-options': 'nosniff', 'accept-ranges': 'bytes' };
  let range = null;
  const rh = status === 200 ? req.headers.range : undefined;
  if (rh) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(rh.trim());
    let a = m && m[1] !== '' ? Number(m[1]) : NaN, b = m && m[2] !== '' ? Number(m[2]) : NaN;
    if (m && m[1] === '' && m[2] !== '') { a = Math.max(0, size - b); b = size - 1; }   // suffix range: last N bytes
    else if (m && m[2] === '') b = size - 1;
    if (!m || !(a >= 0) || !(b >= a) || a >= size) {
      res.writeHead(416, { ...head, 'content-range': `bytes */${size}` }).end(); return;
    }
    range = { start: a, end: Math.min(b, size - 1) };
  }
  if (range) {
    res.writeHead(206, { ...head, 'content-range': `bytes ${range.start}-${range.end}/${size}`, 'content-length': range.end - range.start + 1 });
  } else res.writeHead(status, { ...head, 'content-length': size });
  if (req.method === 'HEAD') { res.end(); return; }
  createReadStream(f, range ?? undefined).pipe(res);
}).listen(port, '127.0.0.1', () => console.log(`serving ${root} at http://127.0.0.1:${port}/`));
