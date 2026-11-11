#!/usr/bin/env node
// Runs the swarm dev harness (spikes/swarm/swarm-dev.js) in a real browser and prints PASS/FAIL with numeric
// metrics and fixed strings only (never run content).
//   cd lab/web && node spikes/swarm/run.mjs [--mode net|swarm] [--n 2] [--overlay] [--stop K] [--engine chromium]
//       [--base URL] [--deadline-min 30]
// Serves lab/web with dev-server.mjs (production headers on /vmframe.html, placeholder seed, dist/vm) unless
// --base is given. --overlay serves the working tree's guest files (lab/browser/guest/labctl.py, lab/agent/
// {harness,setup_vm}.py, lab/board/*.py: the image's /root/lab allowlist, lab/browser/image/build.sh) at
// /dev-guest/ through a Playwright route, so the harness can copy them over /root/lab before labctl starts.
// Samples the browser process tree's RSS once a second and reports the peak per harness phase.
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { chromium, firefox, webkit } from "playwright";
import { createLabServer } from "../../dev-server.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const { values: opt } = parseArgs({ options: {
  mode: { type: "string", default: "net" }, n: { type: "string", default: "2" }, overlay: { type: "boolean", default: false },
  stop: { type: "string" }, engine: { type: "string", default: "chromium" }, base: { type: "string" },
  "deadline-min": { type: "string", default: "30" }, iface: { type: "string" }, "ready-ms": { type: "string" },
} });
let server = null, base = opt.base;
if (!base) { server = await createLabServer({ port: 0 }); base = server.url; }
const browser = await { chromium, firefox, webkit }[opt.engine].launch({ args: opt.engine === "chromium" ? ["--enable-precise-memory-info", "--js-flags=--expose-gc"] : [] });
const page = await browser.newPage();
let pageErrors = 0, consoleErrors = 0, cspViolations = 0;
page.on("pageerror", () => { pageErrors++; });
page.on("console", (m) => { if (m.type() === "error") { consoleErrors++; if (/Content Security Policy|frame-ancestors|Refused/.test(m.text())) cspViolations++; } });

if (opt.overlay) {
  const files = { "labctl.py": join(REPO, "lab/browser/guest/labctl.py"), "harness.py": join(REPO, "lab/agent/harness.py"),
    "setup_vm.py": join(REPO, "lab/agent/setup_vm.py") };
  for (const f of readdirSync(join(REPO, "lab/board"))) if (f.endsWith(".py")) files[`board/${f}`] = join(REPO, "lab/board", f);
  await page.route("**/dev-guest/**", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.slice("/dev-guest/".length));
    if (name === "files.json") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(Object.keys(files)) });
    if (!files[name]) return route.fulfill({ status: 404, body: "" });
    return route.fulfill({ status: 200, contentType: "text/plain", body: readFileSync(files[name]) });
  });
}

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

const q = new URLSearchParams({ mode: opt.mode, n: opt.n, ...(opt.overlay ? { overlay: "1" } : {}), ...(opt.stop ? { stop: opt.stop } : {}),
  ...(opt.iface ? { iface: opt.iface } : {}), ...(opt["ready-ms"] ? { readyMs: opt["ready-ms"] } : {}) });
await page.goto(`${base}/spikes/swarm/?${q}`);
const peak = {};
let seen = 0, s;
const deadline = Date.now() + Number(opt["deadline-min"]) * 60000;
while (Date.now() < deadline) {
  s = await page.evaluate(() => ({ phase: window.__swarmdev?.phase, done: !!window.__swarmdev?.done, n: window.__swarmdev?.results.length || 0 }));
  const p = peak[s.phase] ||= { rssMB: 0 };
  p.rssMB = Math.max(p.rssMB, treeRssMB());
  if (s.n > seen) {
    for (const t of await page.evaluate((k) => window.__swarmdev.results.slice(k), seen)) console.log(`${t.pass ? "PASS" : "FAIL"}  ${t.id}\n      ${t.metric}`);
    seen = s.n;
  }
  if (s.done) break;
  await new Promise((r) => setTimeout(r, 1000));
}
const r = await page.evaluate(() => ({ results: window.__swarmdev.results, error: window.__swarmdev.error, done: window.__swarmdev.done,
  mem: window.__swarmdev.mem, info: window.__swarmdev.info }));
for (const t of r.results.slice(seen)) console.log(`${t.pass ? "PASS" : "FAIL"}  ${t.id}\n      ${t.metric}`);
console.log("info", JSON.stringify(r.info));
if (opt.engine === "chromium") {
  try {
    const cdp = await page.context().newCDPSession(page);
    console.log("dom-counters", JSON.stringify(await cdp.send("Memory.getDOMCounters")));
    for (let k = 0; k < 3; k++) { await cdp.send("HeapProfiler.collectGarbage"); await new Promise((r) => setTimeout(r, 1000)); }
    const m = Object.fromEntries((await (await cdp.send("Performance.enable"), cdp.send("Performance.getMetrics"))).metrics.map((x) => [x.name, x.value]));
    console.log("after-cdp-gc", JSON.stringify({ ...(await cdp.send("Memory.getDOMCounters")), jsHeapMB: Math.round((m.JSHeapUsedSize || 0) / 1048576), rssMB: treeRssMB() }));
  } catch {}
}
console.log("page-mem", JSON.stringify(r.mem));
console.log("peak-rss-by-phase", JSON.stringify(peak));
if (r.error) console.log("ERROR", r.error.split("\n")[0].slice(0, 200));
if (!r.done) console.log(`TIMEOUT after ${opt["deadline-min"]} min`);
console.log("pageErrors", pageErrors, "consoleErrors", consoleErrors, "cspViolations", cspViolations);
await browser.close();
await server?.close();
process.exit(r.done && !r.error && pageErrors === 0 && cspViolations === 0 && r.results.length && r.results.every((t) => t.pass) ? 0 : 1);
