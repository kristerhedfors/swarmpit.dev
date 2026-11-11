#!/usr/bin/env node
// Browser check of the lab page UI (docs/browser-swarm.md §7) under the production CSP from dev-server.mjs, without
// booting any VM or calling any model: the page loads with zero CSP violations, shows the empty state, the trace
// library (real IndexedDB) stores an uploaded synthetic zip that the replay player renders, the trace survives a
// reload, a non-run zip is refused, "+" opens the new-swarm dialog, download returns the stored bytes, delete empties
// the player, and the API key typed into the dialog never reaches localStorage or IndexedDB.
//
//   cd lab/web && node test/ui.e2e.mjs [--engine chromium|firefox|webkit] [--real-convert] [--headed] [--shot PREFIX]
//   (--shot writes PREFIX-empty.png, PREFIX-dialog.png and PREFIX-trace.png of the synthetic trace)
//
// The uploaded zip has placeholder content only. Unless --real-convert is given, /player/js/convert/index.js is
// served by a stub that returns the deterministic synthetic bundle of site/tools/synth.py (seeded random words, no
// run data), so the check does not depend on module C. Needs the site compiled (cd site && npm run build).
// Prints only check names, pass/fail and counts.
import { parseArgs } from "node:util";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createLabServer } from "../dev-server.mjs";
import { makeZip } from "../public/js/zip.js";
import { ENGINES, countCspViolations, labState, labTraces, newLabPage, playerLoaded, sameBytes } from "./browser-helpers.mjs";

const { values: opt } = parseArgs({ options: {
  engine: { type: "string", default: "chromium" }, "real-convert": { type: "boolean", default: false },
  headed: { type: "boolean", default: false }, shot: { type: "string" },
} });
const HERE = dirname(fileURLToPath(import.meta.url));
const SITE_TOOLS = join(HERE, "..", "..", "..", "site", "tools");
const RUN_ID = "browser-forum-multi2-20261024120000-ab12";
const CANARY = "sk-canary-" + Math.random().toString(36).slice(2);

let failures = 0, passes = 0;
function check(name, ok) {
  if (ok) passes++; else failures++;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}`);
}

// The synthetic stress bundle (seeded, placeholder words) as a stub converter module; the JSON never gets printed.
function stubConverter() {
  const py = [
    "import json, sys",
    `sys.path.insert(0, ${JSON.stringify(SITE_TOOLS)})`,
    "import export, synth",
    "from redact import Redactor",
    "core, detail = synth.make_synth({'obsSchema': 1, 'obsDerive': 0})",
    "core, detail, cj, dj = export.finalize(core, detail, Redactor(deny=[]))",
    "card = export.make_card(core, len(cj), len(dj), False, {})",
    "json.dump({'card': card, 'core': core, 'detail': detail}, sys.stdout)",
  ].join("\n");
  const json = execFileSync("python3", ["-c", py], { encoding: "utf8", maxBuffer: 64 << 20 });
  return `const B = ${json};
export function runFilesFromEntries(entries) { return new Map(entries.map((e) => [e.name.replace(/^[^/]+\\//, ""), e.data])); }
export async function convertRun(files, opts = {}) {
  if (!(files instanceof Map) || !files.size) throw new Error("E_CONVERT_EMPTY");
  const b = structuredClone(B);
  b.card.hidden = false;
  return b;
}`;
}

const enc = new TextEncoder();
const zipBytes = async (files) => new Uint8Array(await makeZip(files).arrayBuffer());
// window.__lab.loadTrace of these zip bytes: {ok, id} or {ok: false, code}
const loadTrace = (page, bytes, type) => page.evaluate(async ([b, t]) =>
  window.__lab.loadTrace(new Blob([new Uint8Array(b)], t ? { type: t } : undefined)), [[...bytes], type]);

function neutralZip(runId = RUN_ID) {
  const step = (i) => JSON.stringify({ kind: "step", step: i, command: "echo placeholder", exit: 0 });
  return makeZip([
    { name: `${runId}/run.json`, data: enc.encode(JSON.stringify({ run_id: runId, placeholder: true })) },
    { name: `${runId}/manifest.json`, data: enc.encode(JSON.stringify({ v: 1, run_id: runId, files: [], topology: "board-split" })) },
    { name: `${runId}/a0/steps.jsonl`, data: enc.encode([1, 2, 3].map(step).join("\n") + "\n") },
    { name: `${runId}/a1/steps.jsonl`, data: enc.encode([1, 2].map(step).join("\n") + "\n") },
  ]);
}

// A stand-in for js/swarm.js (§6 contract) so the live panel, live re-conversion and end-of-run storage can be
// checked without VMs: two agents, one status update, one placeholder record, ends on Stop with a neutral zip.
const LIVE_ID = "browser-forum-multi2-20261024130000-cd34";
const FAKE_SWARM = `import { makeZip } from "/js/zip.js";
const enc = new TextEncoder();
export function createSwarm(settings, hooks) {
  const runId = ${JSON.stringify(LIVE_ID)};
  let stopped; const ended = new Promise((r) => { stopped = r; });
  const agents = (ended) => [0, 1].map((i) => ({ i, phase: ended ? "done" : "running", steps: 2 + i, calls: 3, tokens: 1000, ended }));
  window.__fakeSwarm = { nAgents: settings.nAgents, calls: settings.caps?.calls, tokens: settings.caps?.tokens, disposed: 0,
    hasKey: typeof settings.provider?.apiKey === "string" && settings.provider.apiKey.length > 0 };
  return {
    runId,
    async run() {
      hooks.onStatus({ phase: "running", elapsedMs: 1000, board: { phase: "running" }, agents: agents(false) });
      hooks.onRecord(0, { kind: "step", step: 1, command: "echo placeholder", exit: 0, phase: "task", phase_before: "task" });
      hooks.onCall(1, { seq: 1, status: 500, code: "E_HTTP" });
      await ended;
      hooks.onStatus({ phase: "done", elapsedMs: 2000, board: { phase: "done" }, agents: agents(true) });
      return { result: "stopped" };
    },
    stop() { stopped(); },
    async dispose() { window.__fakeSwarm.disposed++; return 0; },
    async liveFiles() { return new Map([["a0/steps.jsonl", enc.encode("{}\\n")]]); },
    async zip() {
      const f = (n, s) => ({ name: runId + "/" + n, data: enc.encode(s) });
      return { filename: runId + "-incomplete.zip", blob: makeZip([f("run.json", JSON.stringify({ run_id: runId })), f("a0/steps.jsonl", "{}\\n"), f("a1/steps.jsonl", "{}\\n")]) };
    },
  };
}`;

const server = await createLabServer({ port: 0 });
const browser = await ENGINES[opt.engine].launch({ headless: !opt.headed });
try {
  const ctx = await browser.newContext({ acceptDownloads: true });
  if (!opt["real-convert"]) {
    const src = stubConverter();
    await ctx.route("**/player/js/convert/index.js", (r) => r.fulfill({ status: 200, contentType: "text/javascript", body: src }));
  }
  await ctx.route("**/js/swarm.js", (r) => r.fulfill({ status: 200, contentType: "text/javascript", body: FAKE_SWARM }));
  // count CSP violations and console errors (counts and fixed categories only)
  await ctx.addInitScript(countCspViolations);
  const counters = { pageErrors: 0, consoleErrors: 0, consoleCsp: 0 };
  const page = await newLabPage(ctx, counters, /Content Security Policy/i);

  await page.goto(server.url + "/");
  check("page boots (player host loaded)", await page.evaluate(() => window.__lab.ready()));
  let st = await labState(page);
  check("cross-origin isolated", st.isolated === true);
  check("library is persistent (IndexedDB)", st.persistent === true);
  check("empty state shown with no traces", st.empty && st.traces === 0 && await page.isVisible("#empty"));
  if (opt.shot) await page.screenshot({ path: `${opt.shot}-empty.png` });
  check("selector action chips rendered", (await page.locator(".sel-act").count()) === 4);

  // "+" opens the dialog; Start stays disabled without consent; the memory warning shows for 4 agents
  await page.click('.sel-act[data-act="new"]');
  st = await labState(page);
  check("+ opens the new-swarm dialog", st.dialog === "new");
  check("start disabled without consent", await page.isDisabled("#start"));
  await page.click("#options summary");
  check("memory warning above 2 agents", await page.isVisible("#agentsWarn"));
  await page.selectOption("#nAgents", "2");
  check("no memory warning at 2 agents", !(await page.isVisible("#agentsWarn")));
  await page.fill("#apiKey", CANARY);
  await page.fill("#model", "placeholder-model");
  await page.check("#consent");
  check("start enabled with key + consent", !(await page.isDisabled("#start")));
  const presetUrl = await page.inputValue("#baseUrl");
  await page.fill("#baseUrl", "https://custom-host.example.test/v1");
  check("a base URL the CSP blocks is refused in the form", await page.isDisabled("#start") && /CSP/.test(await page.textContent("#startNote")));
  await page.fill("#baseUrl", presetUrl);
  check("the preset base URL is accepted again", !(await page.isDisabled("#start")));
  if (opt.shot) await page.screenshot({ path: `${opt.shot}-dialog.png` });
  await page.click("#cancelNew");

  // upload a synthetic neutral zip through the test hook (same path as the upload dialog)
  const stored = new Uint8Array(await neutralZip().arrayBuffer());
  const up = await loadTrace(page, stored, "application/zip");
  check("upload accepted", up.ok === true && up.id === RUN_ID);
  await playerLoaded(page, 15000);
  st = await labState(page);
  check("player renders the uploaded trace", st.selected === RUN_ID && !st.empty && st.traces === 1);
  if (opt.shot) { await page.waitForTimeout(1500); await page.screenshot({ path: `${opt.shot}-trace.png` }); }
  check("player has a canvas", (await page.locator(".stage-gl canvas").count()) >= 1);
  check("Local lane lists the trace", (await page.locator(".sel-local .chip").count()) === 1);
  check("url hash scoped to #trace=", new URL(page.url()).hash === `#trace=${RUN_ID}`);
  check("no narration button", await page.evaluate(() => [...document.querySelectorAll(".tl-btn")].every((b) => !/narrat/i.test(b.getAttribute("aria-label") || "") || b.hidden)));

  // the trace survives a reload (IndexedDB) and is selected again
  await page.reload();
  await page.evaluate(() => window.__lab.ready());
  await playerLoaded(page, 15000);
  const traces = await labTraces(page);
  check("trace persisted across reload", traces.length === 1 && traces[0].id === RUN_ID && traces[0].nFiles === 4);
  st = await labState(page);
  check("persisted trace re-selected", st.selected === RUN_ID);
  check("storage persistence asked and reported", typeof st.persisted === "boolean" && (await page.textContent("#storageNote")).length > 0);

  // the same zip again is accepted (same id); other bytes under a stored run id are refused, not swapped in
  const again = await loadTrace(page, stored);
  check("re-upload of the same bytes accepted", again.ok === true && again.id === RUN_ID);
  const other = await zipBytes([{ name: `${RUN_ID}/run.json`, data: enc.encode(JSON.stringify({ run_id: RUN_ID, other: 1 })) },
    { name: `${RUN_ID}/a0/steps.jsonl`, data: enc.encode("{}\n") }]);
  const swapped = await loadTrace(page, other);
  check("upload of other bytes under a stored run id refused", swapped.ok === false && swapped.code === "E_TRACE_EXISTS");
  check("stored trace unchanged", (await labTraces(page)).length === 1);

  // a zip without a run is refused with a fixed code
  const junk = await zipBytes([{ name: "photos/placeholder.txt", data: enc.encode("placeholder") }]);
  const bad = await loadTrace(page, junk);
  check("non-run zip rejected", bad.ok === false && bad.code === "E_NOT_A_RUN");
  const notZip = await page.evaluate(async () => window.__lab.loadTrace(new Blob(["placeholder, not a zip"])));
  check("non-zip rejected", notZip.ok === false && notZip.code === "E_ZIP_FORMAT");

  // download returns exactly the stored bytes
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#download")]);
  const path = await dl.path();
  const got = path ? new Uint8Array(readFileSync(path)) : new Uint8Array(0);
  check("download is the stored zip", dl.suggestedFilename() === `${RUN_ID}.zip` && sameBytes(got, stored));

  // the key typed earlier is not in localStorage or IndexedDB (boolean only; nothing printed)
  await page.click('.sel-act[data-act="new"]');
  await page.fill("#apiKey", CANARY);
  await page.click("#cancelNew");
  const leaked = await page.evaluate(async (canary) => {
    let s = "";
    for (let i = 0; i < localStorage.length; i++) s += localStorage.key(i) + localStorage.getItem(localStorage.key(i));
    for (let i = 0; i < sessionStorage.length; i++) s += sessionStorage.key(i) + sessionStorage.getItem(sessionStorage.key(i));
    const db = await new Promise((ok, bad) => { const r = indexedDB.open("swarmlab-traces"); r.onsuccess = () => ok(r.result); r.onerror = bad; });
    for (const name of db.objectStoreNames) {
      const rows = await new Promise((ok) => { const r = db.transaction(name).objectStore(name).getAll(); r.onsuccess = () => ok(r.result); });
      for (const row of rows) {
        s += JSON.stringify({ ...row, blob: undefined });
        if (row.blob) s += row.blob instanceof Blob ? await row.blob.text() : new TextDecoder().decode(row.blob);  // ArrayBuffer (library.js)
      }
    }
    db.close();
    return s.includes(canary) || location.href.includes(canary);
  }, CANARY);
  check("API key not in storage or URL", leaked === false);

  // a (fake) swarm: live panel lanes, a live re-conversion shown in the player, Stop, the trace stored at the end
  await page.click('.sel-act[data-act="new"]');
  await page.fill("#model", "placeholder-model");
  await page.fill("#apiKey", CANARY);
  await page.check("#consent");
  await page.click("#options summary").catch(() => {});
  await page.selectOption("#nAgents", "2");
  await page.click("#start");
  await page.waitForFunction(() => window.__lab.state().running, null, { timeout: 10000 });
  st = await labState(page);
  check("swarm started with the dialog settings", st.runId === LIVE_ID && st.dialog === null
    && await page.evaluate(() => window.__fakeSwarm.nAgents === 2 && window.__fakeSwarm.hasKey));
  check("shared call cap covers both agents", await page.evaluate(() => window.__fakeSwarm.calls === 2 * (40 + 25)));
  check("shared token cap is per agent times agents", await page.evaluate(() => window.__fakeSwarm.tokens === 2 * 1000000));
  const clashed = await loadTrace(page, new Uint8Array(await neutralZip(LIVE_ID).arrayBuffer()));
  check("upload of the running swarm's id refused", clashed.ok === false && clashed.code === "E_TRACE_RUNNING");
  check("live panel: board + 2 agent lanes", await page.isVisible("#runPanel") && (await page.locator("#lanes li").count()) === 3);
  check("live panel totals from onStatus", (await page.textContent("#steps")) === "5" && (await page.textContent("#calls")) === "6");
  await page.evaluate(() => window.__lab.refreshLive());
  st = await labState(page);
  check("live trace shown in the player", st.live.includes(LIVE_ID) && st.selected === LIVE_ID && st.liveConversions === 1);
  check("live trace marked in the selector", (await page.locator(".sel-local .chip", { hasText: "live" }).count()) === 1);
  check("live trace not yet in the library", !(await labTraces(page)).some((t) => t.id === LIVE_ID));
  await page.click("#stop");
  await page.waitForFunction(() => !window.__lab.state().running, null, { timeout: 15000 });
  await page.waitForFunction((id) => window.__lab.traces().then((t) => t.some((x) => x.id === id)), LIVE_ID, { timeout: 15000 });
  st = await labState(page);
  check("ended run stored, converted and selected", st.result === "stopped" && st.live.length === 0 && st.selected === LIVE_ID && st.traces === 2);
  check("live marker cleared", (await page.locator(".sel-local .chip", { hasText: "live" }).count()) === 0);
  await page.waitForFunction(() => window.__fakeSwarm.disposed === 1, null, { timeout: 10000 }).catch(() => {});
  check("the run's VM storage is disposed once the trace is stored", await page.evaluate(() => window.__fakeSwarm.disposed === 1));
  await page.click(`.sel-local .chip:not([aria-checked="true"])`);
  await page.waitForFunction((id) => window.__lab.state().selected === id, RUN_ID, { timeout: 10000 });
  check("selector switches between local traces", true);
  await page.click('.sel-act[data-act="delete"]');
  await page.waitForFunction(() => window.__lab.state().traces === 1, null, { timeout: 10000 });

  // delete empties the player
  await page.click('.sel-act[data-act="delete"]');
  await page.waitForFunction(() => document.getElementById("player").classList.contains("pl-empty"), null, { timeout: 10000 });
  st = await labState(page);
  check("delete removes the trace and shows the empty state", st.traces === 0 && st.empty && (await labTraces(page)).length === 0);

  const csp = await page.evaluate(() => window.__csp);
  if (csp) for (const [k, n] of Object.entries(await page.evaluate(() => window.__cspKinds))) console.log(`  csp ${n}x ${k}`);
  check(`zero CSP violations (events ${csp}, console ${counters.consoleCsp})`, csp === 0 && counters.consoleCsp === 0);
  check(`no page errors (${counters.pageErrors}) or console errors (${counters.consoleErrors})`,
    counters.pageErrors === 0 && counters.consoleErrors === 0);
} finally {
  await browser.close();
  await server.close();
}
console.log(`ui e2e: ${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
