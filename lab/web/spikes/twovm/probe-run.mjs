#!/usr/bin/env node
// Runs probe.js for one or more modes and engines; prints each step's metrics (numbers/booleans only).
//   cd lab/web && node spikes/twovm/probe-run.mjs --engine chromium --modes same,diffurl,iframe,worker
import { parseArgs } from "node:util";
import { chromium, firefox, webkit } from "playwright";
import { createTwoVmServer } from "./server.mjs";

const { values: opt } = parseArgs({ options: { engine: { type: "string", default: "chromium" },
  modes: { type: "string", default: "same,diffurl,iframe,worker" }, extra: { type: "string", default: "" }, port: { type: "string", default: "8790" } } });
const server = await createTwoVmServer({ port: Number(opt.port) });
let fails = 0;
for (const mode of opt.modes.split(",")) {
  const browser = await { chromium, firefox, webkit }[opt.engine].launch();
  const page = await browser.newPage();
  let pageErrors = 0;
  page.on("pageerror", (e) => { pageErrors++; if (pageErrors <= 3) console.log(`  pageerror: ${String(e).slice(0, 140)}`); });
  await page.goto(`${server.url}/probe.html?mode=${mode}${opt.extra ? "&" + opt.extra : ""}`);
  const deadline = Date.now() + 8 * 60000;
  let r;
  while (Date.now() < deadline) {
    r = await page.evaluate(() => window.__probe);
    if (r?.done) break;
    await new Promise((s) => setTimeout(s, 1000));
  }
  const ok = r?.done && !r.error && r.steps.every((s) => s.healthy !== false && s.separate !== false);
  fails += !ok;
  console.log(`${ok ? "PASS" : "FAIL"} ${opt.engine} mode=${mode} ${opt.extra}${r?.error ? " error=" + r.error.slice(0, 160) : ""}${r?.done ? "" : " TIMEOUT"} pageErrors=${pageErrors}`);
  for (const s of r?.steps || []) console.log(`  ${JSON.stringify(s)}`);
  await browser.close();
}
await server.close();
process.exit(fails ? 1 : 0);
