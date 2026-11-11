// LLM broker: answers the harness's mailbox requests with the visitor's key, which never enters the VM
// (PROTOCOL.md §3). The guest writes requests to the outbox (an IndexedDB mount the page reads); the page
// writes replies to the inbox (a DataDevice the guest reads). A request must carry a valid HMAC under the
// run's mailbox key (held by root in the guest, not readable by the agent user) and the expected seq;
// anything else is ignored. The guest cannot choose endpoint, model or headers.
import { bytesToHex, replyFile, requestFile } from "./protocol.js";

const POLL_MS = 150;
const FETCH_TIMEOUT_MS = 240000;  // below the harness's 300 s reply wait (§3)
const enc = new TextEncoder();
const MAC_RE = /^[0-9a-f]{64}$/;
// A request file larger than this is not read (the outbox is agent-writable; PROTOCOL.md §3): a harness request
// is the conversation so far (40 steps of <= 3000-char tool output plus the model turns), far below this.
export const MAX_REQUEST_BYTES = 8 << 20;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Constant-time comparison of two equal-length hex strings.
function sameMac(a, b) {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.min(a.length, b.length); i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function pickMessage(m = {}) {
  const out = { role: "assistant", content: m.content ?? null };
  if (Array.isArray(m.tool_calls)) {
    out.tool_calls = m.tool_calls.map((c) => ({ id: c.id, type: c.type || "function",
      function: { name: c.function?.name, arguments: c.function?.arguments } }));
  }
  if (m.reasoning != null) out.reasoning = m.reasoning;
  if (m.reasoning_content != null) out.reasoning_content = m.reasoning_content;
  return out;
}

function pickUsage(u = {}) {
  return { prompt_tokens: u.prompt_tokens || 0, completion_tokens: u.completion_tokens || 0,
    prompt_tokens_details: { cached_tokens: u.prompt_tokens_details?.cached_tokens || 0 } };
}

// Fixed replies (§3 Codes).
const ABORT = { status: 402, code: "E_ABORT" };
const BUDGET = { status: 402, code: "E_BUDGET" };
const UPSTREAM = { status: 502, code: "E_UPSTREAM" };

// Provider status -> reply code, so the unchanged harness retry logic does the right thing (§3 Codes).
function errorReply(status) {
  if (status === 401 || status === 403) return { status: 401, code: "E_AUTH" };
  if (status === 402) return BUDGET;
  if (status === 429) return { status: 429, code: "E_RATE" };
  if (status === 400 || status === 404 || status === 422) return { status: 400, code: "E_BAD_REQUEST" };
  return UPSTREAM;
}

// The call/token caps shared by every broker of a swarm (docs/browser-swarm.md §6): the swarm as a whole never
// exceeds what the visitor set. Each broker checks and counts against the same object; the check and the
// count are one synchronous step, so N brokers can never pass the call cap. The token cap is checked before
// each call, so the swarm can pass it by at most the tokens of the calls in flight (one per agent) when it is
// reached (PROTOCOL.md §3 E_BUDGET). stop() is the swarm's one Stop: it aborts every broker on the budget.
export function createBudget(caps) {
  const ok = (x) => Number.isSafeInteger(x) && x > 0;
  if (!caps || !ok(caps.calls) || !ok(caps.tokens)) throw new Error("E_CAPS");
  const stopper = new AbortController();
  return {
    caps: { calls: caps.calls, tokens: caps.tokens },
    calls: 0, tokens: 0,
    signal: stopper.signal,
    stop() { stopper.abort(); },
    stopped: () => stopper.signal.aborted,
  };
}

// provider {baseUrl, model, apiKey}; harness {max_tokens}; caps {calls, tokens} for a broker of its own, or
// budget (createBudget) shared with other brokers (then caps is ignored).
// onCall({seq, status, code?, ms?, usage?, calls, tokens, total: {calls, tokens}}) fires for every reply written
// (and with {code: "E_BROKER"} if the loop itself failed); calls/tokens are this broker's, total the budget's.
// onIgnored(seq) fires once per seq with an invalid request (including one signed for another agent id).
// stop() aborts this broker's in-flight call and answers every later valid request with E_ABORT (budget.stop()
// does the same for every broker on the budget); close() ends the loop.
export function startBroker({ outbox, inbox, key, agent = 0, provider, harness, caps, budget,
  onCall = () => {}, onIgnored = () => {}, timeoutMs = FETCH_TIMEOUT_MS }) {
  let next = 1, calls = 0, tokens = 0, opaqueStreak = 0, closed = false;
  const shared = budget || createBudget(caps);
  const own = new AbortController();
  const stopper = { signal: AbortSignal.any([own.signal, shared.signal]) };
  const ignored = new Set();
  const report = (fn, arg) => { try { fn(arg); } catch {} };
  const ignore = (s) => { if (!ignored.has(s)) { ignored.add(s); report(onIgnored, s); } };
  const totals = () => ({ calls, tokens, total: { calls: shared.calls, tokens: shared.tokens } });

  // A valid request for seq s as {seq, payload}, or null (missing, partial, or not from the harness).
  // The MAC is checked on the raw payload string before anything parses it (§3 Request).
  async function read(s) {
    let req;
    try {
      const blob = await outbox.readFileAsBlob(requestFile(agent, s));
      if (!blob) return null;
      if (blob.size > MAX_REQUEST_BYTES) { ignore(s); return null; }
      req = JSON.parse(await blob.text());
    } catch { return null; }  // missing, or still being written
    let payload = null;
    if (req && typeof req === "object" && req.v === 1 && typeof req.payload === "string"
        && typeof req.mac === "string" && MAC_RE.test(req.mac)) {
      const mac = bytesToHex(new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(req.payload))));
      if (sameMac(mac, req.mac)) {
        try { payload = JSON.parse(req.payload); } catch {}
      }
    }
    if (payload?.agent !== agent || payload.seq !== s) { ignore(s); return null; }
    return { seq: s, payload };
  }

  // The provider request: the page's model; only messages, tools and clamped parameters come from the guest.
  function upstreamBody(body) {
    return {
      model: provider.model,
      messages: body.messages,
      tools: body.tools,
      temperature: Math.min(Math.max(Number(body.temperature) || 0, 0), 2),
      max_tokens: Math.min(Number(body.max_tokens) || harness.max_tokens, harness.max_tokens),
    };
  }

  // The reply to a valid request (§3 Reply / Codes). The budget check and the call count are one synchronous step.
  async function handle({ payload }) {
    if (stopper.signal.aborted) return ABORT;
    if (shared.calls >= shared.caps.calls || shared.tokens >= shared.caps.tokens) return BUDGET;
    shared.calls++;
    calls++;
    const upstream = upstreamBody(payload.body || {});
    const signal = AbortSignal.any([stopper.signal, AbortSignal.timeout(timeoutMs)]);
    let data;
    try {
      const r = await fetch(`${provider.baseUrl}/chat/completions`, {
        method: "POST", signal,
        headers: { "Authorization": `Bearer ${provider.apiKey}`, "Content-Type": "application/json" },
        body: JSON.stringify(upstream),
      });
      opaqueStreak = 0;
      if (!r.ok) return errorReply(r.status);
      data = await r.json();
    } catch (e) {
      if (stopper.signal.aborted) return ABORT;
      if (e?.name === "TimeoutError" || signal.aborted) return UPSTREAM;
      if (e instanceof SyntaxError) return UPSTREAM;  // unparseable body
      opaqueStreak++;  // fetch rejected: network or CORS, the page cannot tell which
      return { status: opaqueStreak >= 3 ? 401 : 503, code: "E_OPAQUE" };
    }
    const choice = data?.choices?.[0];
    if (!choice) return UPSTREAM;
    const usage = pickUsage(data.usage);
    const used = usage.prompt_tokens + usage.completion_tokens;
    tokens += used;
    shared.tokens += used;
    return { status: 200, body: { choices: [{ message: pickMessage(choice.message), finish_reason: choice.finish_reason }], usage } };
  }

  const done = (async () => {
    while (!closed) {
      try {
        // the harness moves on to seq+1 if a request goes unanswered, so look one ahead too (§3 Seq)
        const req = await read(next) || await read(next + 1);
        if (!req) { await sleep(POLL_MS); continue; }
        next = req.seq + 1;
        const t0 = performance.now();
        const reply = await handle(req);
        if (closed) break;
        await inbox.writeFile(replyFile(agent, req.seq), JSON.stringify({ v: 1, seq: req.seq, ...reply }));
        report(onCall, { seq: req.seq, status: reply.status, code: reply.code, ms: performance.now() - t0,
          usage: reply.body?.usage, ...totals() });
      } catch {
        report(onCall, { code: "E_BROKER", ...totals() });
        await sleep(POLL_MS);
      }
    }
  })();

  return {
    stop() { own.abort(); },
    close() { closed = true; own.abort(); return done; },
    stats: () => ({ calls, tokens, next }),
  };
}
