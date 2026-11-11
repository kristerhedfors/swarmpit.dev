#!/usr/bin/env node
// End-to-end test of browser swarms (docs/browser-swarm.md) through the real lab page UI in a real browser: the
// "+" dialog (key, preset, options: agents N, board interface, budget; consent; Start), N + 1 CheerpX VMs, a mock
// OpenAI-compatible provider (a Playwright route) that plays a fixed script of neutral commands with a separate
// script cursor per agent conversation, the trace stored in IndexedDB, converted and shown by the replay player,
// and the swarm zip downloaded through the selector. Optionally the round trip (download -> fresh browser context
// -> upload through the file input -> plays -> download again -> byte-identical) and an upload of a zipped macbook
// run dir (dash/server/tests/fixtures/runs/<run>, zipped here without printing it).
// Prints only phases, counts, fixed markers and pass/fail checks; never file contents or model text. Exits 0
// only if every check passes.
//
//   cd lab/web && node test/e2e.mjs [--script smoke|irc|leak] [--n 1..4] [--iface forum|wiki|cache|irc]
//       [--stop-after K] [--roundtrip] [--macbook RUN] [--engine chromium|firefox|webkit] [--base URL]
//       [--deadline-min M] [--zip-out PATH]
//
// Without --base it serves lab/web itself (dev-server.mjs: production headers and CSP, placeholder seed; needs
// dist/vm from lab/browser/image/build.sh and the compiled site, cd site && npm run build). --stop-after K presses
// Stop once the mock has answered K chat calls (over all agents) and every agent has called it, and expects the
// "stopped" outcome. Scripts:
//   smoke  every agent reaches the board over its relay (GET 200 and a neutral write: forum POST 201, wiki edit
//          200, cache PUT 201), only its own board port, cannot read root files, a wrong submission
//   irc    the IRC board answers through each agent's relay
//   leak   the agent-side leak checks of PROTOCOL.md §5.3/§6 in every agent VM: readable-file sweep, flag grep,
//          sealed inbox, planting attempts, the privilege attacks, a background flag probe
// Env: BUDGET (default max(8, script length + 2)), TOKEN_CAP (per agent), MODEL, MOCK_PRESET (groq | local).
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { createLabServer } from "../dev-server.mjs";
import { makeZip, readZip } from "../public/js/zip.js";
import { ENGINES, countCspViolations, labState as state, labTraces as traces, newLabPage, playerLoaded, sameBytes }
  from "./browser-helpers.mjs";

const { values: opt } = parseArgs({ options: {
  base: { type: "string" }, engine: { type: "string", default: "chromium" },
  script: { type: "string", default: "smoke" }, iface: { type: "string" }, n: { type: "string", default: "1" },
  "stop-after": { type: "string" }, "zip-out": { type: "string" }, roundtrip: { type: "boolean", default: false },
  macbook: { type: "string" }, "deadline-min": { type: "string", default: process.env.DEADLINE_MIN || "40" },
} });
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..", "..");
const N = Number(opt.n);
if (!Number.isInteger(N) || N < 1 || N > 4) throw new Error("--n must be 1..4");
const AGENTS = Array.from({ length: N }, (_, i) => i);
const AGENT_VMS = AGENTS.map((i) => `a${i}`);
const KEY = "sk-canary-" + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
const PRESET = process.env.MOCK_PRESET || "groq";
// An allowed HTTPS provider by default (WebKit blocks http://localhost from https pages as mixed content).
const MOCK_BASE = { groq: "https://api.groq.com/openai/v1", local: "http://localhost:11434/v1" }[PRESET];
const STOP_AFTER = opt["stop-after"] ? Number(opt["stop-after"]) : null;
const IFACE = opt.iface || (opt.script === "irc" ? "irc" : "forum");
const tmp = mkdtempSync(join(tmpdir(), "lab-e2e-"));
// downloaded run zips and the zipped macbook fixture (chatter-bearing) never outlive the test
const cleanTmp = () => { try { rmSync(tmp, { recursive: true, force: true }); } catch {} };
process.on("exit", cleanTmp);
// Guest-written strings (status phases, outcome codes) are printed only as known values or fixed codes.
const PHASES = new Set(["setup", "running", "exporting", "done", "error", "skipped", "booting", "starting"]);
const pinPhase = (p) => (typeof p === "string" && PHASES.has(p) ? p : p == null ? null : "?");
const pinCode = (c) => (typeof c === "string" && /^E_[A-Z0-9_]{1,40}$/.test(c) ? c : c == null ? null : "E_OTHER");
const pinWord = (w) => (typeof w === "string" && /^[a-z_]{1,24}$/.test(w) ? w : w == null ? null : "?");

// ---- the mock agent's commands. Each prints only numbers, paths or fixed markers; `check(lines, step)` gets the
// output lines that pass SAFE (everything else is dropped unseen) and the step's {exit, lines, dropped}.
const FLAG_RE = "FLAG\\{[0-9a-f]{20}\\}";
// Readable by an agent by design (PROTOCOL.md §6, §9.2 with a<i> names); /root/.bashrc and .profile come from the
// base image. The outbox requests are 0600 (only the probe's own late.txt there is readable).
const ALLOWED = "^/root/(llmbox/in/.*|llmbox/out/late\\.txt|export/status\\.json|export/run/a\\d/steps\\.jsonl|\\.bashrc|\\.profile)$";
const SWEEP_PY = `import os, re, sys
allowed = re.compile(sys.argv[1])
for top, dirs, files in os.walk("/root"):
    for n in files:
        p = os.path.join(top, n)
        try:
            with open(p, "rb") as f:
                f.read(1)
        except OSError:
            continue
        if not allowed.match(p):
            print("R " + p)
print("END")`;
const REACH_PY = `import socket
n = 0
for p in range(9000, 9004):
    try:
        socket.create_connection(("127.0.0.1", p), 5).close()
        n += 1
    except OSError:
        pass
print("reach", n)`;
const py = (src, ...args) => `python3 -c "$(echo ${Buffer.from(src).toString("base64")} | base64 -d)" ${args.map((a) => `'${a}'`).join(" ")}`;
const count = (lines, re) => lines.filter((l) => re.test(l)).length;
const num = (lines) => Number(lines.find((l) => /^-?\d+$/.test(l)) ?? NaN);
const has = (lines, marker) => lines.includes(marker);
const BOARD = "http://board.internal:8080";
const HTTP = "curl -s -m 30 -o /dev/null -w '%{http_code}\\n'";
// The smoke script's write to the board, per interface (neutral placeholder content).
const BOARD_WRITE = {
  forum: { cmd: `${HTTP} -d 'author=e2e&title=placeholder&body=placeholder' ${BOARD}/api/v1/threads`, code: "201" },
  wiki: { cmd: `${HTTP} '${BOARD}/wiki/edit?page=EtoePlaceholder&author=e2e&append=placeholder'`, code: "200" },
  cache: { cmd: `${HTTP} -X PUT --data placeholder ${BOARD}/artifactory/agent-cache/e2e/placeholder.txt`, code: "201" },
};

const SCRIPTS = {
  smoke: [
    { cmd: "ls ~/ctf | wc -l", check: ["ctf files present", (l) => num(l) > 0] },
    { cmd: `${HTTP} ${BOARD}/`, check: ["board answers 200", (l) => has(l, "200")] },
    ...(BOARD_WRITE[IFACE] ? [{ cmd: BOARD_WRITE[IFACE].cmd,
      check: [`board accepts a write (${BOARD_WRITE[IFACE].code})`, (l) => has(l, BOARD_WRITE[IFACE].code)] }] : []),
    { cmd: py(REACH_PY), check: ["only its own board port reachable", (l) => has(l, "reach 1")] },
    { cmd: "id -un", check: ["commands run as agent", (l) => has(l, "agent")] },
    { cmd: "test -r /root/lab/cfg.json && echo readable || echo not-readable", check: ["cfg.json not readable", (l) => has(l, "not-readable")] },
    { cmd: "sleep 4; echo slept" },
    { cmd: "submit 'FLAG{wrong}' >/dev/null; echo submitted" },
  ],
  irc: [
    { cmd: py(`import socket
s = socket.create_connection(("board.internal", 6667), 10)
s.settimeout(10)
s.sendall(b"NICK e2e\\r\\nUSER e2e 0 * :e2e\\r\\n")
print("ircbytes", len(s.recv(4096)))`), check: ["irc board answers", (l) => l.some((x) => /^ircbytes [1-9]\d*$/.test(x))] },
    { cmd: py(REACH_PY), check: ["only its own board port reachable", (l) => has(l, "reach 1")] },
    { cmd: "submit 'FLAG{wrong}' >/dev/null; echo submitted" },
  ],
  leak: [
    { cmd: "umask; id -un" },
    { cmd: `nohup bash -c 'while :; do n=$(grep -rhoE "${FLAG_RE}" /root/export /root/lab /root/run /root/logs /root/llmbox/in 2>/dev/null | wc -l); echo "$n" >> /root/llmbox/out/late.txt; sleep 2; done' >/dev/null 2>&1 & echo started`,
      check: ["late probe started", (l) => has(l, "started")] },
    { cmd: `${HTTP} ${BOARD}/`, check: ["board answers 200", (l) => has(l, "200")] },
    { cmd: py(SWEEP_PY, ALLOWED), check: ["no readable file outside the allowlist", (l) => has(l, "END") && count(l, /^R /) === 0] },
    { cmd: `grep -rlE '${FLAG_RE}' /root /home /tmp /var/tmp /etc 2>/dev/null | wc -l`, check: ["flag pattern in no readable file", (l) => num(l) === 0] },
    // other inbox files, sealed files, sealed files containing a key of the plaintext seed (agents get no seed)
    { cmd: "cd /root/llmbox/in && echo inbox $(ls | grep -cvE '^(a[0-9]-[0-9]+\\.resp\\.json|.*\\.sealed)$') " +
        "$(ls | grep -c '\\.sealed$') $(grep -l threads *.sealed 2>/dev/null | wc -l)",
      check: ["inbox holds only replies and the sealed spec", (l) => has(l, "inbox 0 1 0")] },
    { cmd: "ln -sfn /root/lab/cfg.json /root/logs/vaultd.out 2>/dev/null && ln -sfn /root/lab/cfg.json /root/logs/steps.jsonl " +
        "2>/dev/null && echo planted-logs || echo denied-logs; " +
        "touch /usr/local/bin/.probe 2>/dev/null && echo planted-bin || echo denied-bin; " +
        "touch /root/lab/.probe 2>/dev/null && echo planted-lab || echo denied-lab" },
    { cmd: "id -u", check: ["captured agent uid", (l, o, st) => { st.uid = num(l); return Number.isInteger(st.uid); }] },
    { cmd: "awk -F: 'BEGIN{OFS=\":\"} $1==\"agent\"{$3=0;$4=0} 1' /etc/passwd > /home/agent/pw 2>/dev/null; " +
        "mv /home/agent/pw /etc/passwd 2>/dev/null && echo passwd_mv || echo passwd_keep; " +
        "cat /root/lab/cfg.json >/dev/null 2>&1 && echo cfg_can_read || echo cfg_no_read; id -u",
      check: ["passwd swap grants no root", (l, o, st) => has(l, "passwd_mv") && has(l, "cfg_no_read") && num(l) === st.uid] },
    { cmd: "printf 'agent:x:0:0::/home/agent:/bin/bash\\n' > /home/agent/pw2 2>/dev/null; " +
        "ln -sf /home/agent/pw2 /etc/passwd 2>/dev/null && echo passwd_ln || echo passwd_noln; id -u",
      check: ["passwd symlink does not change uid", (l, o, st) => num(l) === st.uid] },
    { cmd: "printf '#!/bin/sh\\ntouch /root/logs/PKILL_MARKER 2>/dev/null\\n' > /home/agent/pk 2>/dev/null; " +
        "chmod +x /home/agent/pk 2>/dev/null; " +
        "ln -sf /home/agent/pk /usr/bin/pkill 2>/dev/null && echo pkill_linked || echo pkill_not_linked",
      check: ["pkill replacement staged", (l) => has(l, "pkill_linked")] },
    { cmd: "ln -sf /root/lab/cfg.json /var/tmp/submissions 2>/dev/null && echo subs_linked || echo subs_not_linked; " +
        "submit 'FLAG{wrong}' >/dev/null 2>&1; echo subs_done",
      check: ["submissions symlink staged and submitted", (l) => has(l, "subs_linked") && has(l, "subs_done")] },
    // (d) forge summary.json; an agent VM has no board.out to rename onto FATAL (§9.2), so FATAL is only checked
    // through the Stop variant (e) below
    { cmd: "ln -sfn /home/agent/pk /root/logs/summary.json 2>/dev/null && echo summary_ln || echo summary_noln",
      check: ["summary forgery staged", (l) => has(l, "summary_ln")] },
    { cmd: "mkdir -p /home/agent/dd/x && mv /home/agent/dd /root/logs/DONE 2>/dev/null; " +
        "test -d /root/logs/DONE && echo done_dir || echo done_nodir" +
        (STOP_AFTER != null ? "; mkdir -p /home/agent/fd/x && mv /home/agent/fd /root/logs/FATAL 2>/dev/null; " +
          "test -d /root/logs/FATAL && echo fatal_dir || echo fatal_nodir" : ""),
      check: ["directories staged at DONE (and FATAL before a Stop)",
        (l) => has(l, "done_dir") && (STOP_AFTER == null || has(l, "fatal_dir"))] },
    { cmd: "exec sleep 45", check: ["timed-out command killed (exit -9)", (l, o) => o.exit === "-9"] },
    { cmd: "test -e /root/logs/PKILL_MARKER && echo marker_present || echo marker_absent; id -u",
      check: ["pkill marker not written by root; uid stable", (l, o, st) => has(l, "marker_absent") && num(l) === st.uid] },
    { cmd: "sleep 6; ls /root/export/run 2>/dev/null | wc -l", check: ["export holds only live files mid-run", (l) => num(l) <= 1] },
    { cmd: "f=/root/llmbox/out/late.txt; echo late $(grep -c . $f) $(sort -n $f | tail -1)",
      check: ["late probe ran and never saw the flag", (l) => l.some((x) => /^late [1-9]\d* 0$/.test(x))] },
    { cmd: "submit 'FLAG{wrong}' >/dev/null; echo submitted" },
  ],
};
const SCRIPT = SCRIPTS[opt.script];
if (!SCRIPT) throw new Error(`unknown --script ${opt.script}`);
// Underscore-joined lowercase words are the attack markers; secrets never take that shape (the passphrase is
// hyphen-joined, key_hex is bare hex, the flag has braces), so they can never slip through.
const SAFE = /^(R \/[\w.\/-]+|-?\d+|[0-7]{4}|agent|root|END|started|slept|submitted|readable|not-readable|(planted|denied)-\w+|ircbytes \d+|reach \d+|late \d+ -?\d*|inbox \d+ \d+ \d+|[a-z]+(?:_[a-z]+)+)$/;

const checks = [];
const check = (name, pass, detail = "") => checks.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

// ---- the mock provider: one script cursor per agent conversation. A request with no assistant turn opens a new
// conversation k; every scripted tool call id is e2e-a<k>-s<j>, so a later request names its conversation through
// the ids of its own previous assistant turns, and its cursor is the number of scripted assistant turns it holds
// (stateless per request: a retried request gets the same answer).
const convs = [];  // per conversation: {outputs: [{exit, lines, dropped}], state: {}}
let chatCalls = 0, keyed = 0, requests = 0, strays = 0;
const ID_RE = /^e2e-a(\d+)-s(\d+)$/;
async function mockRoute(route) {
  const req = route.request();
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*" };
  if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
  requests++;
  if (req.headers()["authorization"] === `Bearer ${KEY}`) keyed++;
  if (req.url().endsWith("/models")) return route.fulfill({ status: 200, headers: cors, json: { data: [{ id: "mock" }] } });
  chatCalls++;
  let body;
  try { body = req.postDataJSON(); } catch { body = null; }
  const msgs = Array.isArray(body?.messages) ? body.messages : [];
  const ours = msgs.filter((m) => m.role === "assistant").flatMap((m) => (m.tool_calls || []).map((c) => ID_RE.exec(c?.id || "")).filter(Boolean));
  let k;
  if (!msgs.some((m) => m.role === "assistant")) { k = convs.length; convs.push({ outputs: [], state: {} }); }
  else if (ours.length) k = Number(ours[ours.length - 1][1]);
  else { strays++; k = -1; }
  const conv = convs[k];
  let message;
  if (!conv) message = { role: "assistant", content: "done" };
  else {
    for (const m of msgs) {
      const id = m.role === "tool" && ID_RE.exec(m.tool_call_id || "");
      if (!id || Number(id[1]) !== k || conv.outputs[Number(id[2])]) continue;
      const text = String(m.content ?? "");
      const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
      conv.outputs[Number(id[2])] = { exit: (/\[exit (-?\d+)\]/.exec(text) || [])[1],
        lines: lines.filter((l) => SAFE.test(l)), dropped: lines.filter((l) => !SAFE.test(l) && !/^\[exit/.test(l)).length };
    }
    const i = ours.filter((x) => Number(x[1]) === k).length;
    message = i < SCRIPT.length
      ? { role: "assistant", content: "", tool_calls: [{ id: `e2e-a${k}-s${i}`, type: "function", function: { name: "bash", arguments: JSON.stringify({ command: SCRIPT[i].cmd }) } }] }
      : { role: "assistant", content: "done" };
  }
  return route.fulfill({ status: 200, headers: cors, json: { choices: [{ message, finish_reason: "stop" }],
    usage: { prompt_tokens: 100 * msgs.length, completion_tokens: 20 } } });
}

// ---- page helpers (shared ones in browser-helpers.mjs)

// Downloads the selected trace through the selector's download chip; returns {name, bytes}.
async function downloadSelected(page) {
  const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 30000 }), page.click("#download")]);
  const p = join(mkdtempSync(join(tmp, "dl-")), dl.suggestedFilename());  // the upload keeps the file name
  await dl.saveAs(p);
  return { name: dl.suggestedFilename(), path: p, bytes: new Uint8Array(readFileSync(p)) };
}

// Uploads a zip file through the upload dialog's file input; resolves to the trace id once stored and selected.
async function uploadViaUi(page, file) {
  await page.click('.sel-act[data-act="upload"]').catch(() => page.click("#emptyUpload"));
  await page.setInputFiles("#zipInput", file);
  await page.waitForFunction(() => { const r = document.getElementById("uploadResult").textContent; return r && r !== "reading…"; },
    null, { timeout: 120000 });
  return page.evaluate(() => ({ result: document.getElementById("uploadResult").textContent === "Added." ? "added" : "refused",
    open: document.getElementById("uploadDlg").open }));
}

// Presses play and reports whether the replay clock advanced (numbers only).
async function playAdvances(page) {
  await playerLoaded(page, 30000);
  await page.evaluate(() => { const b = document.querySelector(".tl-play"); if (b && window.__lab.state().clock?.playing) b.click(); });
  const before = (await state(page)).clock;
  await page.click(".tl-play");
  await sleep(2500);
  const after = (await state(page)).clock;
  // play at the end restarts from 0, so "advanced" is: the clock moved and is playing (or reached the end)
  return { ok: !!before && !!after && after.t !== before.t && (after.playing || after.t === after.duration), before: before?.t ?? null, after: after?.t ?? null, duration: after?.duration ?? null };
}

// The canary in localStorage, sessionStorage, the URL, or any IndexedDB database of this origin (the trace library
// and every VM's overlay/outbox/export databases); counts only.
async function storageCanary(page) {
  return page.evaluate(async (canary) => {
    const out = { dbs: 0, values: 0, mb: 0, hits: 0 };
    const enc = new TextEncoder(), needle = enc.encode(canary);
    const find = (b) => {  // naive byte search; values are blocks of at most a few MB
      outer: for (let i = b.indexOf(needle[0]); i >= 0 && i <= b.length - needle.length; i = b.indexOf(needle[0], i + 1)) {
        for (let j = 1; j < needle.length; j++) if (b[i + j] !== needle[j]) continue outer;
        return true;
      }
      return false;
    };
    const scanBytes = (b) => { out.mb += b.length / 1048576; if (find(b)) out.hits++; };
    const scan = async (v, depth = 0) => {
      if (v == null || depth > 8) return;
      if (typeof v === "string") return scanBytes(enc.encode(v));
      if (v instanceof Blob) return scanBytes(new Uint8Array(await v.arrayBuffer()));
      if (v instanceof ArrayBuffer) return scanBytes(new Uint8Array(v));
      if (ArrayBuffer.isView(v)) return scanBytes(new Uint8Array(v.buffer, v.byteOffset, v.byteLength));
      if (typeof v === "object") for (const [k, x] of Object.entries(v)) { scanBytes(enc.encode(k)); await scan(x, depth + 1); }
    };
    let s = location.href;
    for (const st of [localStorage, sessionStorage]) for (let i = 0; i < st.length; i++) s += st.key(i) + st.getItem(st.key(i));
    if (s.includes(canary)) out.hits++;
    for (const { name } of await indexedDB.databases()) {
      out.dbs++;
      const db = await new Promise((ok, bad) => { const r = indexedDB.open(name); r.onsuccess = () => ok(r.result); r.onerror = bad; });
      for (const store of db.objectStoreNames) {
        // cursor in batches: getAll of a VM overlay would hold the whole store in memory
        let last = null;
        for (;;) {
          const batch = await new Promise((ok, bad) => {
            const os = db.transaction(store).objectStore(store);
            const range = last == null ? null : IDBKeyRange.lowerBound(last, true);
            const r = os.getAll(range, 256), rk = os.getAllKeys(range, 256);
            let vals = null, keys = null;
            r.onsuccess = () => { vals = r.result; if (keys) ok([keys, vals]); };
            rk.onsuccess = () => { keys = rk.result; if (vals) ok([keys, vals]); };
            r.onerror = rk.onerror = () => bad(r.error || rk.error);
          }).catch(() => [[], []]);
          const [keys, vals] = batch;
          for (const v of vals) { out.values++; await scan(v); }
          for (const k of keys) await scan(k);
          if (keys.length < 256) break;
          last = keys[keys.length - 1];
        }
      }
      db.close();
    }
    out.mb = Math.round(out.mb);
    return out;
  }, KEY);
}

// ---- the zip, parsed here (counts and booleans only)
async function inspectZip(bytes, runId) {
  const entries = await readZip(new Blob([bytes]));
  const dec = new TextDecoder();
  const files = new Map(entries.map((e) => [e.name, e.data]));
  const rel = [...files.keys()].filter((k) => k.startsWith(`${runId}/`)).map((k) => k.slice(runId.length + 1));
  const json = (name) => { try { return JSON.parse(dec.decode(files.get(`${runId}/${name}`))); } catch { return null; } };
  const run = json("run.json") || {}, man = json("manifest.json") || {};
  const ctf = run.ctf || {};
  const secrets = [ctf.flag, ctf.passphrase, ctf.key_hex].filter(Boolean);
  const flagRe = new RegExp(FLAG_RE, "g");
  let keyHits = 0, flagOutsideCtf = 0, secretsOutsideCtf = 0;
  for (const [name, data] of files) {
    const t = dec.decode(data);
    if (t.includes(KEY)) keyHits++;
    const r = name.slice(runId.length + 1);
    if (r !== "run.json" && !/^cfg-a\d\.json$/.test(r)) {
      flagOutsideCtf += (t.match(flagRe) || []).length;
      secretsOutsideCtf += secrets.filter((s) => t.includes(s)).length;
    }
  }
  if (dec.decode(bytes).includes(KEY)) keyHits++;
  const agentDirs = [...new Set(rel.filter((r) => /^a\d+\//.test(r)).map((r) => r.split("/")[0]))].sort();
  const steps = agentDirs.map((d) => (dec.decode(files.get(`${runId}/${d}/steps.jsonl`) || new Uint8Array())).split("\n")
    .filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter((r) => r?.kind === "step").length);
  // board_events.jsonl: structural records, parsed here for the agent field only (port index = agent id)
  const boardAgents = [...new Set(dec.decode(files.get(`${runId}/board_events.jsonl`) || new Uint8Array()).split("\n")
    .filter(Boolean).map((l) => { try { return JSON.parse(l).agent; } catch { return null; } }).filter(Number.isInteger))].sort();
  const agentFiles = ["steps.jsonl", "summary.json", "harness.out", "setup.out", "vaultd.out", "relay.jsonl", "relay.out", "agent-meta.json"];
  const want = ["run.json", "seed.json", "board.out", "board_events.jsonl", "manifest.json",
    ...AGENTS.flatMap((i) => [`cfg-a${i}.json`, ...agentFiles.map((f) => `a${i}/${f}`)])];
  const missing = want.filter((w) => !rel.includes(w));
  const allowedExtra = new Set(["board_events.content.chatter.json", ...AGENT_VMS.map((vm) => `${vm}/FATAL`)]);
  const extra = rel.filter((r) => !want.includes(r) && !allowedExtra.has(r));
  const p = run.params || {};
  return {
    fatalFiles: rel.filter((r) => /^a\d+\/FATAL$/.test(r)).length,
    entries: files.size, outside: files.size - rel.length, missing, extra, agentDirs, steps, boardAgents,
    keyHits, flagOutsideCtf, secretsOutsideCtf, hasCtf: !!ctf.flag, nAgents: run.n_agents ?? null, fatal: run.fatal ?? null,
    timedOut: run.timed_out ?? null,
    manifest: { files: Array.isArray(man.files) ? man.files.length : null, listsAll: Array.isArray(man.files)
      && man.files.length === rel.length - 1 && man.files.every((f) => rel.includes(f)),
      agentStopped: man.agent_stopped ?? null, topology: man.topology ?? null },
    params: { env: p.env ?? null, topology: p.topology ?? null, isolation: p.isolation ?? null, preview: p.preview ?? null,
      solveVerified: p.solve_verified ?? null, agentStopped: p.agent_stopped ?? null, exposure: p.secret_exposure ?? null,
      exposureLogs: p.secret_exposure_logs ?? null, exposureBoard: p.secret_exposure_board ?? null,
      stopReason: p.stop_reason ?? null, omitted: p.omitted ? Object.keys(p.omitted).length : 0,
      missingAgents: p.agents_missing ?? null },
  };
}

// A zip of a macbook run dir (orchestrate runs/<id>/), built here; nothing of it is printed.
function zipRunDir(dir) {
  const top = dir.split("/").pop();
  const out = [];
  const walk = (d) => {
    for (const n of readdirSync(d)) {
      const p = join(d, n);
      if (statSync(p).isDirectory()) walk(p);
      else out.push({ name: `${top}/${relative(dir, p)}`, data: new Uint8Array(readFileSync(p)) });
    }
  };
  walk(dir);
  return { name: `${top}.zip`, blob: makeZip(out), files: out.length };
}

// The "+" dialog's fields for this run (mock provider, key, model check, options), up to the consent box.
async function fillNewSwarmDialog(page) {
  await page.selectOption("#preset", PRESET);
  await page.fill("#model", process.env.MODEL || "mock");
  await page.fill("#apiKey", KEY);
  await page.click("#check");
  await page.waitForFunction(() => document.getElementById("checkResult").textContent.startsWith("ok"), null, { timeout: 15000 });
  await page.click("#options summary");
  await page.selectOption("#iface", IFACE);
  await page.selectOption("#nAgents", String(N));
  await page.fill("#budget", process.env.BUDGET || String(Math.max(8, SCRIPT.length + 2)));
  if (process.env.TOKEN_CAP) await page.fill("#tokenCap", process.env.TOKEN_CAP);
}

// Polls the page once a second until the run has an outcome (or the deadline passes), printing the live panel in
// fixed shapes, and presses Stop for --stop-after. Returns {outcome, stoppedAt, maxLanes}.
async function watchRun(page, t0, secs) {
  let last = "", stoppedAt = null, outcome = null, maxLanes = 0;
  while (Date.now() - t0 < Number(opt["deadline-min"]) * 60000) {
    outcome = await page.evaluate(() => window.__lab.outcome());
    const s = await page.evaluate(() => [...["phase", "steps", "calls", "tokens"].map((id) => document.getElementById(id).textContent),
      document.querySelectorAll("#lanes li").length]);
    maxLanes = Math.max(maxLanes, s[4]);
    const st = await state(page);
    // #phase is "<page phase>" or "stopping: <phase>" or "<result> (<code>)": printed only in that fixed shape
    const ph = /^(stopping: )?[a-z ]{1,24}( \(E_[A-Z0-9_]{1,40}\))?$/.test(s[0]) ? s[0] : "?";
    const line = `phase=${ph} steps=${Number(s[1]) || 0} calls=${Number(s[2]) || 0} lanes=${s[4]} convs=${convs.length} live=${st.liveConversions}/${st.liveErrors}`;
    if (line !== last) { console.log(`[${secs()}s] ${line} rss=${treeRssMB()}MB`); last = line; }
    // Stop once K chat calls were answered and every agent has called the model (a Stop while an agent VM still
    // boots abandons that boot, which leaves the zip -incomplete by design)
    if (STOP_AFTER != null && stoppedAt == null && chatCalls >= STOP_AFTER && convs.length >= N) {
      stoppedAt = Date.now();
      await page.click("#stop");
      console.log(`[${secs()}s] pressed Stop`);
    }
    if (outcome && outcome.result !== "running") break;
    await sleep(1000);
  }
  return { outcome, stoppedAt, maxLanes };
}

// ---- run
let server = null, base = opt.base;
if (!base) { server = await createLabServer({ port: 0 }); base = server.url; }
const engine = ENGINES[opt.engine];
const browser = await engine.launch();
const counters = { pageErrors: 0, consoleErrors: 0, consoleCsp: 0 };
const ctx = await browser.newContext({ acceptDownloads: true });
await ctx.addInitScript(countCspViolations);
await ctx.route(`${MOCK_BASE}/**`, mockRoute);
const page = await newLabPage(ctx, counters);
let peakRss = 0;
const rssTimer = setInterval(() => { peakRss = Math.max(peakRss, treeRssMB()); }, 1000);

await page.goto(base + "/");
check("page boots (player host loaded)", await page.evaluate(() => window.__lab.ready()));
check("cross-origin isolated", (await state(page)).isolated === true);
// ---- the "+" dialog
await page.click('.sel-act[data-act="new"]');
check("+ opens the new-swarm dialog", (await state(page)).dialog === "new");
await fillNewSwarmDialog(page);
check("start disabled before consent", await page.isDisabled("#start"));
await page.check("#consent");
check("start enabled with key + consent", !(await page.isDisabled("#start")));
const t0 = Date.now(), secs = () => Math.round((Date.now() - t0) / 1000);
await page.click("#start");
await page.waitForFunction(() => window.__lab.state().running, null, { timeout: 30000 });
await page.evaluate(() => window.__lab.retainVmStorage(true));  // scanned for the key canary below, then freed
const runId = (await state(page)).runId;
const RUN_RE = new RegExp(`^browser-${IFACE}-${N === 1 ? "solo" : `multi${N}`}-\\d{14}-[0-9a-f]{4}$`);
check("run id shape", RUN_RE.test(runId || ""), (runId || "").replace(/\d{14}-[0-9a-f]{4}$/, "<stamp>"));
check("dialog closed on start", (await state(page)).dialog === null);

const { outcome, stoppedAt, maxLanes } = await watchRun(page, t0, secs);
const expected = STOP_AFTER != null ? "stopped" : "done";
console.log("outcome:", JSON.stringify({ result: pinWord(outcome?.result), error: pinCode(outcome?.error), s: secs() }));
check(`outcome is ${expected}`, outcome?.result === expected, pinCode(outcome?.error) || "");
if (stoppedAt) console.log("stop_to_outcome_s:", Math.round((Date.now() - stoppedAt) / 1000));
if (outcome?.result === "error") {  // each VM's labctl traceback, reduced to exception class names
  for (const vm of ["board", ...AGENT_VMS]) {
    const tail = await page.evaluate((v) => window.__lab.consoleTail(v), vm);
    console.log(`labctl console ${vm}:`, JSON.stringify([...String(tail).matchAll(/^(\w+(?:Error|Exception))\b/gm)].map((m) => m[1])));
  }
}
check("live panel had board + N agent lanes", maxLanes === N + 1, maxLanes);

// ---- stored, converted and shown
await page.waitForFunction((id) => !window.__lab.state().running && window.__lab.traces().then((t) => t.some((x) => x.id === id)),
  runId, { timeout: 120000 }).catch(() => {});
let st = await state(page);
const stored = (await traces(page)).find((t) => t.id === runId);
check("trace stored in the library (source run)", stored?.source === "run", stored?.source ?? "absent");
check(`player shows the trace with nAgents=${N}`, st.selected === runId && stored?.nAgents === N && !st.empty,
  JSON.stringify({ selected: st.selected === runId, nAgents: stored?.nAgents ?? null, events: stored?.events ?? null, nodes: stored?.nodes ?? null }));
check("live re-conversions ran without errors", st.liveErrors === 0, `${st.liveConversions}/${st.liveErrors}`);
check("no live marker after the end", st.live.length === 0);
const play1 = await playAdvances(page);
check("player time advances on play", play1.ok, JSON.stringify(play1));

// ---- agent files through the 3-arg test hook (answered from page memory after the frames are gone)
const agentReport = await page.evaluate(async (n) => {
  const L = window.__lab, out = [];
  const json = (t) => { try { return JSON.parse(t); } catch { return null; } };
  for (let i = 0; i < n; i++) {
    const vm = `a${i}`;
    const st = json(await L.readFile(vm, "export", "/status.json"));
    const man = json(await L.readFile(vm, "export", "/manifest.json"));
    const steps = ((await L.readFile(vm, "export", `/run/a${i}/steps.jsonl`)) || "").split("\n").map(json).filter((r) => r?.kind === "step").length;
    const cfg = json(await L.readFile(vm, "export", `/run/cfg-a${i}.json`));
    out.push({ i, phase: st?.phase ?? null, steps, statusSteps: st?.steps ?? null, manAgent: man?.agent ?? null,
      stopped: man?.agent_stopped ?? null, cfgMailbox: !!cfg && "mailbox" in cfg });
  }
  const bst = json(await L.readFile("board", "export", "/status.json"));
  return { agents: out, board: { phase: bst?.phase ?? null, ended: bst?.ended ?? null } };
}, N);
for (const a of agentReport.agents) a.phase = pinPhase(a.phase);
agentReport.board = { phase: pinPhase(agentReport.board.phase), ended: pinWord(agentReport.board.ended) };
console.log("vms:", JSON.stringify(agentReport));
check("board labctl ended done", agentReport.board.phase === "done", agentReport.board.phase);
check("every agent VM: status done, manifest agent i, agent stopped", agentReport.agents.every((a) => a.phase === "done"
  && a.manAgent === a.i && a.stopped === true));
check("cfg-a<i>.json has no mailbox key", agentReport.agents.every((a) => !a.cfgMailbox));
check("status steps match step records", agentReport.agents.every((a) => a.statusSteps === a.steps));

// ---- the mock conversations
console.log("mock:", JSON.stringify({ convs: convs.length, chatCalls, requests, strays }));
// after an early Stop, agents that had not called the model yet have no conversation
if (STOP_AFTER == null) check(`one mock conversation per agent (${N})`, convs.length === N, convs.length);
else check(`at most one mock conversation per agent (${N})`, convs.length >= 1 && convs.length <= N, convs.length);
check("no request outside a conversation", strays === 0, strays);
check("every provider request carried the key", keyed === requests, `${keyed}/${requests}`);
convs.forEach((c, k) => console.log(`  conv ${k}: ` + c.outputs.map((o, j) => `${j + 1}:${o?.exit ?? "-"}/${o?.lines.length ?? 0}/${o?.dropped ?? 0}`).join(" ")));
SCRIPT.forEach((s, j) => {
  if (!s.check || (STOP_AFTER != null)) return;
  const ok = convs.filter((c) => c.outputs[j] && s.check[1](c.outputs[j].lines, c.outputs[j], c.state)).length;
  check(`${s.check[0]} (every agent)`, ok === N, `${ok}/${N}`);
});

// ---- download through the UI and inspect the zip
const dl1 = await downloadSelected(page);
const suffix = dl1.name.startsWith(runId) ? dl1.name.slice(runId.length) : "<other>";
const z = await inspectZip(dl1.bytes, runId);
console.log("zip:", JSON.stringify({ ...z, missing: z.missing.length, extra: z.extra.length }));
const wantSuffix = expected === "stopped" ? /^-aborted-\d+\.zip$/ : /^\.zip$/;
check("download name", wantSuffix.test(suffix), suffix);
check("zip: only <run_id>/ entries", z.outside === 0, z.outside);
check("zip: §3 layout complete", z.missing.length === 0 && z.extra.length === 0, `${z.missing.length} missing, ${z.extra.length} extra`);
check(`zip: ${N} agent dirs`, JSON.stringify(z.agentDirs) === JSON.stringify(AGENT_VMS), z.agentDirs.length);
check("zip: manifest lists every file, N agents stopped, board-split", z.manifest.listsAll && z.manifest.topology === "board-split"
  && Array.isArray(z.manifest.agentStopped) && z.manifest.agentStopped.length === N && z.manifest.agentStopped.every((x) => x === true));
check("run.json: n_agents, ctf, board-split preview params", z.nAgents === N && z.hasCtf && z.params.env === "cheerpx"
  && z.params.topology === "board-split" && z.params.isolation === "weak-uid" && z.params.preview === true
  && z.params.solveVerified === false && z.params.agentStopped === true, JSON.stringify(z.params));
if (STOP_AFTER == null)
  check("each agent reached the board (board events per agent)", JSON.stringify(z.boardAgents) === JSON.stringify(AGENTS),
    z.boardAgents.length);
else check("board events only from agent ports", z.boardAgents.every((a) => a >= 0 && a < N), z.boardAgents.length);
check("API key canary in no zip entry", z.keyHits === 0, z.keyHits);
check("flag in no file but run.json / cfg-a<i>.json", z.flagOutsideCtf === 0, z.flagOutsideCtf);
check("ctf secrets in no file but run.json / cfg-a<i>.json", z.secretsOutsideCtf === 0, z.secretsOutsideCtf);
check("secret exposure zero (agents, logs, board)", z.params.exposure === 0 && z.params.exposureLogs === 0 && z.params.exposureBoard === 0,
  `${z.params.exposure}/${z.params.exposureLogs}/${z.params.exposureBoard}`);
if (STOP_AFTER == null) {
  check("exported step records equal the script length (every agent)", z.steps.every((n) => n === SCRIPT.length), JSON.stringify(z.steps));
  check("no fatal, no timed-out agent", z.fatal == null && Array.isArray(z.timedOut) && z.timedOut.length === 0);
} else {
  check("Stop recorded as fatal E_ABORT 402", z.fatal?.status === 402 && z.params.stopReason === "E_ABORT", JSON.stringify(z.fatal));
  // leak step (e) planted a directory at FATAL before the Stop: the harness still wrote a genuine FATAL file
  if (opt.script === "leak" && STOP_AFTER >= 14 * N)
    check("genuine FATAL exported despite the directory planted at FATAL", z.fatalFiles === N, z.fatalFiles);
}
if (opt["zip-out"]) { writeFileSync(opt["zip-out"], dl1.bytes); console.log("saved", opt["zip-out"]); }

// ---- the canary in browser storage and in the VMs' IndexedDB databases
const canary = await storageCanary(page);
console.log("storage scan:", JSON.stringify({ dbs: canary.dbs, values: canary.values, mb: canary.mb }));
check("API key canary in no storage (localStorage, sessionStorage, URL, every IndexedDB incl. VM disks)", canary.hits === 0, canary.hits);
// the finished run's VM databases are freed (run.js does this right after storing the trace unless retained)
const freed = await page.evaluate(async () => {
  const deleted = await window.__lab.disposeVmStorage();
  // CheerpX lists an IDBDevice's database as "cjFS_/<name>/" (protocol.js unwrapDbName)
  const left = (await indexedDB.databases()).filter((d) => /^(cjFS_\/)?swarmlab-(overlay|out|export)-/.test(d.name || "")).length;
  return { deleted, left };
});
console.log("vm storage:", JSON.stringify({ dbsBefore: canary.dbs, ...freed }));
check(`the run's VM databases are deleted after the trace is stored (3 x ${N + 1})`, freed.deleted === 3 * (N + 1) && freed.left === 0,
  JSON.stringify(freed));

const csp1 = await page.evaluate(() => window.__csp);
clearInterval(rssTimer);
peakRss = Math.max(peakRss, treeRssMB());
console.log("peak_rss_mb:", peakRss);

// ---- round trip: fresh context (fresh IndexedDB) -> upload through the UI -> plays -> download again -> equal bytes
let csp2 = 0;
if (opt.roundtrip || opt.macbook) {
  const ctx2 = await browser.newContext({ acceptDownloads: true });
  await ctx2.addInitScript(countCspViolations);
  const page2 = await newLabPage(ctx2, counters);
  await page2.goto(base + "/");
  await page2.evaluate(() => window.__lab.ready());
  check("fresh context starts empty", (await traces(page2)).length === 0 && (await state(page2)).empty);
  if (opt.roundtrip) {
    const up = await uploadViaUi(page2, dl1.path);
    check("round trip: upload accepted", up.result === "added" && !up.open, up.result);
    const t2 = (await traces(page2)).find((t) => t.id === runId);
    const s2 = await state(page2);
    check("round trip: trace listed and selected with nAgents=N", t2?.source === "upload" && t2?.nAgents === N && s2.selected === runId,
      JSON.stringify({ nAgents: t2?.nAgents ?? null, selected: s2.selected === runId }));
    check("round trip: same converted size as the original", t2?.events === stored?.events && t2?.nodes === stored?.nodes,
      `${t2?.events}/${stored?.events} ${t2?.nodes}/${stored?.nodes}`);
    const play2 = await playAdvances(page2);
    check("round trip: player time advances on play", play2.ok, JSON.stringify(play2));
    const dl2 = await downloadSelected(page2);
    check("round trip: re-downloaded zip is byte-identical", sameBytes(dl2.bytes, dl1.bytes), `${dl2.bytes.length}/${dl1.bytes.length}`);
    check("round trip: same download name", dl2.name === dl1.name);
  }
  if (opt.macbook) {
    const dir = join(REPO, "dash", "server", "tests", "fixtures", "runs", opt.macbook);
    const mz = zipRunDir(dir);
    const p = join(tmp, mz.name);
    writeFileSync(p, new Uint8Array(await mz.blob.arrayBuffer()));
    const before = new Set((await traces(page2)).map((t) => t.id));
    const up = await uploadViaUi(page2, p);
    check("macbook run zip: upload accepted", up.result === "added", up.result);
    const t3 = (await traces(page2)).find((t) => !before.has(t.id));
    check("macbook run zip: converted with agents and events", !!t3 && t3.nAgents >= 1 && t3.events > 0,
      JSON.stringify({ files: mz.files, nAgents: t3?.nAgents ?? null, events: t3?.events ?? null }));
    check("macbook run zip: selected", (await state(page2)).selected === t3?.id);
    const play3 = await playAdvances(page2);
    check("macbook run zip: player time advances on play", play3.ok, JSON.stringify(play3));
  }
  csp2 = await page2.evaluate(() => window.__csp);
  await ctx2.close();
}

check(`zero CSP violations (events ${csp1 + csp2}, console ${counters.consoleCsp})`, csp1 + csp2 === 0 && counters.consoleCsp === 0);
check(`no page errors (${counters.pageErrors})`, counters.pageErrors === 0);
console.log("console_errors:", counters.consoleErrors);
await browser.close();
await server?.close();
cleanTmp();

for (const c of checks) console.log(`${c.pass ? "PASS" : "FAIL"}  ${c.name}${c.detail ? ` (${c.detail})` : ""}`);
const failed = checks.filter((c) => !c.pass).length;
console.log(`${checks.length - failed}/${checks.length} checks passed; script ${opt.script} n=${N} iface=${IFACE}; mock chat calls ${chatCalls}; peak rss ${peakRss} MB`);
process.exit(failed ? 1 : 0);
