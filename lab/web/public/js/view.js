// Live run panel (docs/browser-swarm.md §7): run id, swarm totals, one lane per VM (board + a0..a{N-1}: phase,
// steps, calls, tokens) and a short event log. Everything from the run (records, phases, error codes) is rendered
// with textContent only, so board or model text cannot inject markup into the page. DOM ids kept for the e2e
// driver (PROTOCOL.md §8): runPanel runId phase steps calls tokens elapsed error stop log.
import { $, isCode } from "./page.js";

const LOG_MAX = 200;
const PHASE_LABELS = { boot: "booting", booting: "booting", setup: "setting up", ready: "ready", running: "running",
  exporting: "exporting", stopping: "stopping", done: "done", stopped: "stopped", error: "error", ended: "ended" };
const label = (p) => (typeof p === "string" ? PHASE_LABELS[p] || p.slice(0, 24) : "—");
const num = (n) => (Number.isFinite(n) ? n : 0);
const fmtTok = (t) => num(t).toLocaleString();
const secs = (ms) => `${Math.round(ms / 1000)}s`;
// Outcome error codes are shown only as fixed E_ codes (swarm.js already maps anything else; this is the backstop).
const code = (c) => (isCode(c) ? c : "E_OTHER");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export function createView() {
  let timer = null, t0 = 0;
  const lanes = new Map();  // "board" | i -> {li, ph, n}

  // the lane of "board" or agent i, created on first use
  function lane(key) {
    let l = lanes.get(key);
    if (l) return l;
    const board = key === "board";
    const li = el("li", `lv-lane ${board ? "board" : "a" + (key % 8)}`);
    const ph = el("span", "lv-ph", "—"), n = el("span", "lv-n", "");
    li.append(el("span", "lv-i", board ? "board" : "a" + key), ph, n);
    $("lanes").append(li);
    l = { li, ph, n };
    lanes.set(key, l);
    return l;
  }

  function log(kind, text, extra) {
    const li = el("li");
    li.append(el("span", "k", kind + " "), document.createTextNode(String(text)));
    if (extra) li.append(el("span", "x", " " + extra));
    const box = $("log");
    box.prepend(li);
    while (box.childElementCount > LOG_MAX) box.lastElementChild.remove();
  }

  function error(msg) {
    $("error").hidden = !msg;
    $("error").textContent = msg || "";
  }

  return {
    error,
    started(runId, nAgents) {
      $("runPanel").hidden = false;
      $("runId").textContent = runId;
      $("log").replaceChildren();
      $("lanes").replaceChildren();
      lanes.clear();
      error("");
      $("phase").textContent = "starting";
      for (const id of ["steps", "calls", "tokens"]) $(id).textContent = "0";
      lane("board");
      for (let i = 0; i < nAgents; i++) lane(i);
      $("stop").disabled = false;
      t0 = Date.now();
      clearInterval(timer);
      timer = setInterval(() => { $("elapsed").textContent = secs(Date.now() - t0); }, 1000);
    },
    phase(text) { $("phase").textContent = text; },
    // createSwarm hooks.onStatus (§6): {phase, elapsedMs, board: {phase}, agents: [{i, phase, steps, calls, tokens, ended}]}
    status(st, stopping) {
      if (!st || typeof st !== "object") return;
      $("phase").textContent = (stopping ? "stopping: " : "") + label(st.phase);
      if (Number.isFinite(st.elapsedMs)) $("elapsed").textContent = secs(st.elapsedMs);
      if (st.board) lane("board").ph.textContent = label(st.board.phase);
      let steps = 0, calls = 0, tokens = 0;
      for (const a of Array.isArray(st.agents) ? st.agents : []) {
        if (!Number.isInteger(a?.i) || a.i < 0 || a.i > 15) continue;
        const l = lane(a.i);
        l.ph.textContent = label(a.phase) + (a.ended ? " · ended" : "");
        l.li.classList.toggle("ended", !!a.ended);
        l.n.textContent = `${num(a.steps)} steps · ${num(a.calls)} calls · ${fmtTok(a.tokens)} tok`;
        steps += num(a.steps); calls += num(a.calls); tokens += num(a.tokens);
      }
      $("steps").textContent = String(steps);
      $("calls").textContent = String(calls);
      $("tokens").textContent = fmtTok(tokens);
    },
    // hooks.onCall(i, info): failures only go to the log (totals come from onStatus)
    call(i, c) {
      if (!c) return;
      if (c.code === "E_BROKER") log(`a${i} broker`, "mailbox error; retrying");
      else if (c.status !== undefined && c.status !== 200) log(`a${i} model`, `call ${num(c.seq)} failed`, `${String(c.code ?? "")} (HTTP ${num(c.status)})`);
    },
    ignored(i, seq) { log(`a${i} broker`, `ignored an unauthenticated request (seq ${num(seq)})`); },
    // hooks.onRecord(i, rec): one steps.jsonl record (harness format), page memory only
    record(i, rec) {
      if (!rec || typeof rec !== "object") return;
      const k = `a${i}`;
      if (rec.kind === "step") {
        log(`${k} #${num(rec.step)}`, rec.command == null ? "(no tool call)" : `$ ${String(rec.command)}`.slice(0, 300),
          rec.exit == null ? "" : `exit ${num(rec.exit)}${rec.phase !== rec.phase_before ? " — flag accepted" : ""}`);
      } else if (rec.kind === "end") {
        log(`${k} end`, `${String(rec.ended ?? "")}, solved: ${!!rec.solved}`, `steps ${num(rec.steps_used)}, model calls ${num(rec.model_calls)}`);
      } else if (rec.kind === "crash") {
        log(`${k} crash`, "the harness stopped with an error");
      }
    },
    liveNote(text) { $("liveNote").textContent = text || ""; },
    // outcome = {result: "done"|"stopped"|"error", error?, detail?} (§6)
    ended(outcome) {
      clearInterval(timer);
      const r = outcome?.result ?? "error";
      $("phase").textContent = r + (outcome?.error ? ` (${code(outcome.error)})` : "");
      if (r === "error" && !$("error").textContent) error(`The run failed: ${outcome?.error ? code(outcome.error) : "unknown error"}.`);
      $("stop").disabled = true;
    },
    save(filename, blob) {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    },
  };
}
