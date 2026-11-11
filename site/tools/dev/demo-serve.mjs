#!/usr/bin/env node
// Dev-only server for the engine test harness (never deployed): node tools/dev/demo-serve.mjs [port=8081]
// /            -> tools/dev/engine-demo.html
// /js/**       -> .cache/dev/js (tsc -p tsconfig.dev.json: full src incl. src/engine/dev)
// everything else (data/, css/, favicon) -> public/
import { createServer } from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { resolve, join, extname, sep, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const port = Number(process.argv[2] ?? process.env.PORT ?? 8081);
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' };

function under(root, p) {
  const abs = resolve(join(root, p));
  if (abs !== root && !abs.startsWith(root + sep)) return null;
  try { return statSync(abs).isFile() ? abs : null; } catch { return null; }
}

createServer((req, res) => {
  let p;
  try { p = decodeURIComponent((req.url ?? '/').split('?')[0].split('#')[0]); } catch { res.writeHead(400).end(); return; }
  let f;
  if (p === '/' || p === '/engine-demo.html') f = join(site, 'tools', 'dev', 'engine-demo.html');
  else if (p.startsWith('/js/')) f = under(resolve(site, '.cache', 'dev', 'js'), p.slice(4));
  else f = under(resolve(site, 'public'), p);
  if (!f) { res.writeHead(404, { 'content-type': 'text/plain' }).end('not found'); return; }
  res.writeHead(200, { 'content-type': MIME[extname(f)] ?? 'application/octet-stream', 'cache-control': 'no-cache' });
  createReadStream(f).pipe(res);
}).listen(port, '127.0.0.1', () => console.log(`engine demo at http://127.0.0.1:${port}/`));
