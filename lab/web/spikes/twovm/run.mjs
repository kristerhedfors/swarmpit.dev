#!/usr/bin/env node
// Runs the two-VM spike (twovm.js) in a browser and prints PASS/FAIL with numeric metrics only.
//   cd lab/web && node spikes/twovm/run.mjs [--engine chromium|webkit|firefox] [--port 8790] [--base URL]
//       [--only REGEX] [--wedge] [--n 30] [--deadline-min 30]
// Serves this directory and the frozen image in /tmp/twovm/vm (server.mjs) unless --base is given. Samples
// the resident memory of the browser's process tree once a second (and, in Chromium, the page's JS heap over
// CDP) and reports the peak per spike phase. Exits 1 if any test failed or the page threw.
import { execFileSync } from "node:child_process";
import { parseArgs } from "node:util";
import { chromium, firefox, webkit } from "playwright";
import { createTwoVmServer } from "./server.mjs";

const { values: opt } = parseArgs({ options: {
  engine: { type: "string", default: "chromium" }, port: { type: "string", default: "8790" }, base: { type: "string" },
  only: { type: "string" }, wedge: { type: "boolean", default: false }, n: { type: "string", default: "30" },
  "deadline-min": { type: "string", default: "30" }, debug: { type: "boolean", default: false }, single: { type: "boolean", default: false }, idle: { type: "string" },
  limit: { type: "string" },
} });
let server = null, base = opt.base;
if (!base) { server = await createTwoVmServer({ port: Number(opt.port) }); base = server.url; }
const args = opt.engine === "chromium" ? ["--enable-precise-memory-info"] : [];
const browser = await { chromium, firefox, webkit }[opt.engine].launch({ args });
const page = await browser.newPage();
let pageErrors = 0;
page.on("pageerror", (e) => { pageErrors++; console.log("pageerror:", String(e).slice(0, 160)); });
const cdp = opt.engine === "chromium" ? await page.context().newCDPSession(page) : null;
if (cdp) await cdp.send("Performance.enable");

// resident set size (MB) of every descendant of this node process (the browser and its helpers)
function treeRssMB() {
  try {
    const rows = execFileSync("ps", ["-A", "-o", "pid=,ppid=,rss="], { encoding: "utf8" }).trim().split("\n")
      .map((l) => l.trim().split(/\s+/).map(Number));
    const kids = new Map();
    for (const [pid, ppid] of rows) kids.set(ppid, [...(kids.get(ppid) || []), pid]);
    const rss = new Map(rows.map(([pid, , r]) => [pid, r]));
    let total = 0;
    const stack = [...(kids.get(process.pid) || [])];
    while (stack.length) { const p = stack.pop(); total += rss.get(p) || 0; stack.push(...(kids.get(p) || [])); }
    return Math.round(total / 1024);
  } catch { return -1; }
}

const q = new URLSearchParams({ n: opt.n, ...(opt.debug ? { debug: "1", netdbg: "1" } : {}), ...(opt.single ? { single: "1" } : {}), ...(opt.idle ? { idle: opt.idle } : {}), ...(opt.limit ? { limit: opt.limit } : {}), ...(opt.only ? { only: opt.only } : {}), ...(opt.wedge ? { wedge: "1" } : {}) });
await page.goto(`${base}/?${q}`);
const peak = {};  // phase -> {rssMB, jsHeapMB}
let seen = 0;
const deadline = Date.now() + Number(opt["deadline-min"]) * 60000;
let s;
while (Date.now() < deadline) {
  s = await page.evaluate(() => ({ phase: window.__twovm?.phase, done: !!window.__twovm?.done, n: window.__twovm?.results.length || 0 }));
  const p = peak[s.phase] ||= { rssMB: 0, jsHeapMB: 0 };
  p.rssMB = Math.max(p.rssMB, treeRssMB());
  if (cdp) {
    const m = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((x) => [x.name, x.value]));
    p.jsHeapMB = Math.max(p.jsHeapMB, Math.round((m.JSHeapUsedSize || 0) / 1048576));
  }
  if (s.n > seen) {
    const rs = await page.evaluate((k) => window.__twovm.results.slice(k), seen);
    for (const t of rs) console.log(`${t.pass ? "PASS" : "FAIL"}  ${t.id}\n      ${t.metric}`);
    seen = s.n;
  }
  if (s.done) break;
  await new Promise((r) => setTimeout(r, 1000));
}
const r = await page.evaluate(() => ({ results: window.__twovm.results, error: window.__twovm.error, done: window.__twovm.done,
  mem: window.__twovm.mem, info: window.__twovm.info, pageErrors: window.__twovm.pageErrors }));
for (const t of r.results.slice(seen)) console.log(`${t.pass ? "PASS" : "FAIL"}  ${t.id}\n      ${t.metric}`);
console.log("info", JSON.stringify(r.info));
console.log("page-mem", JSON.stringify(r.mem));
console.log("peak-by-phase", JSON.stringify(peak));
if (server) console.log("server", JSON.stringify({ requests: server.stats.requests, rangeMB: Math.round(server.stats.rangeBytes / 1048576) }));
if (opt.debug) for (const l of (await page.evaluate(() => document.getElementById("log").textContent)).split("\n")) if (l.startsWith("debug")) console.log(l.slice(0, 900));
if (r.error) console.log("ERROR", r.error.split("\n")[0].slice(0, 200));
if (!r.done) console.log(`TIMEOUT after ${opt["deadline-min"]} min`);
console.log("pageErrors", pageErrors, r.pageErrors);
await browser.close();
await server?.close();
process.exit(r.done && !r.error && pageErrors === 0 && r.results.every((t) => t.pass) ? 0 : 1);
