#!/usr/bin/env node
// Runs the CheerpX feasibility spikes (spikes/spike.js) in a browser and prints PASS/FAIL per test.
//   cd lab/web && node spikes/run.mjs [--base URL] [--cx 1.3.9] [--engine chromium|firefox|webkit]
//       [--net loop|none] [--only REGEX] [--deadline-min 40] [--verbose]
// Without --base it serves lab/web itself (dev-server.mjs). The full suite takes a few minutes; --only runs a
// subset (S2* tests that use the agent user need "S1e|S2a" too). A test whose command hangs fails after
// 3 min instead of stalling the run. Exits 1 if any test failed or the page threw. `out` (with --verbose)
// is the tail of the spike's own neutral test commands.
import { parseArgs } from "node:util";
import { chromium, firefox, webkit } from "playwright";
import { createLabServer } from "../dev-server.mjs";

const { values: opt } = parseArgs({ options: {
  base: { type: "string" }, cx: { type: "string", default: "1.3.9" }, engine: { type: "string", default: "chromium" },
  net: { type: "string", default: "loop" }, only: { type: "string" }, "deadline-min": { type: "string", default: "40" },
  verbose: { type: "boolean", default: false },
} });
let server = null, base = opt.base;
if (!base) { server = await createLabServer({ port: 0 }); base = server.url; }
const browser = await { chromium, firefox, webkit }[opt.engine].launch();
const page = await browser.newPage();
let pageErrors = 0;
// the message and the code locations of the top frames (page and CheerpX code only, no guest output)
page.on("pageerror", (e) => {
  pageErrors++;
  console.log("pageerror:", String(e).slice(0, 200), (e.stack || "").split("\n").slice(1, 4).map((l) => l.trim().slice(0, 160)).join(" | "));
});
const q = new URLSearchParams({ cx: opt.cx, net: opt.net, ...(opt.only ? { only: opt.only } : {}) });
await page.goto(`${base}/spikes/?${q}`);
let seen = 0;
const deadline = Date.now() + Number(opt["deadline-min"]) * 60000;
while (Date.now() < deadline) {
  const s = await page.evaluate(() => ({ log: document.getElementById("log")?.textContent || "", done: !!window.__spike?.done }));
  const lines = s.log.split("\n");
  for (const l of lines.slice(seen)) if (l) console.log("log:", l);
  seen = Math.max(seen, lines.length - 1);
  if (s.done) break;
  await new Promise((r) => setTimeout(r, 2000));
}
const r = await page.evaluate(() => window.__spike);
for (const t of r.results) console.log(`${t.pass ? "PASS" : "FAIL"}  ${t.id.padEnd(52)} ${t.ms ?? ""}${t.rtts ? " rtts=" + t.rtts : ""}`);
if (opt.verbose) for (const t of r.results) console.log(`--- ${t.id}\n${t.out}`);
if (r.error) console.log("ERROR", r.error.split("\n")[0].slice(0, 200));
if (!r.done) console.log(`TIMEOUT after ${opt["deadline-min"]} min`);
await browser.close();
await server?.close();
process.exit(r.done && !r.error && pageErrors === 0 && r.results.every((t) => t.pass) ? 0 : 1);
