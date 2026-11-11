// Setup form: provider presets, the key check, and reading the form into run settings.
// The API key is read from its input when needed and lives only in page memory.
import { $ } from "./page.js";

// Every baseUrl host here must also appear in the connect-src of public/_headers, or the browser blocks it.
export const PRESETS = [
  { id: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { id: "openrouter", label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1" },
  { id: "groq", label: "Groq", baseUrl: "https://api.groq.com/openai/v1" },
  { id: "cerebras", label: "Cerebras", baseUrl: "https://api.cerebras.ai/v1", model: "qwen-3.8-27b" },
  { id: "berget", label: "Berget", baseUrl: "https://api.berget.ai/v1", wire: { sanitize_tool_args: true } },
  { id: "together", label: "Together", baseUrl: "https://api.together.xyz/v1" },
  { id: "mistral", label: "Mistral", baseUrl: "https://api.mistral.ai/v1" },
  { id: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com/v1" },
  { id: "local", label: "Local (Ollama / LM Studio)", baseUrl: "http://localhost:11434/v1" },
];
export const HARNESS_DEFAULTS = { budget: 40, temperature: 0.7, max_tokens: 2048, output_chars: 3000, run_timeout_s: 5400 };
// Swarm size (docs/browser-swarm.md §1): 1..4 agent VMs next to the board VM; above WARN_AGENTS the page warns
// about memory (about 690 MB for the first VM plus 200-530 MB per extra VM, §8).
export const MAX_AGENTS = 4, DEFAULT_AGENTS = 4, WARN_AGENTS = 2;
// The shared model-call cap (§6: one budget for all brokers): each agent's tool-call budget plus 25 calls of slack
// (the solo lab's cap), times the agents.
export const callCap = (nAgents, budget) => nAgents * (budget + 25);
// The shared token cap: the per-agent cap from the form (#tokenCap) times the agents, as the call cap scales. A
// harness resends its whole history on every call, so one 40-step agent can use about 0.8M tokens.
export const DEFAULT_TOKENS_PER_AGENT = 1000000;
export const tokenCap = (nAgents, perAgent) => nAgents * perAgent;
const sharedCaps = (nAgents, budget, perAgent) => ({ calls: callCap(nAgents, budget), tokens: tokenCap(nAgents, perAgent) });

// The origins the page's CSP (public/_headers connect-src) lets a provider fetch reach: every preset's origin
// plus http://localhost:<any port> and http://127.0.0.1:<any port>. test/settings.test.mjs checks the CSP
// against this list, so a custom base URL the browser would block is refused in the form instead.
export const PRESET_ORIGINS = [...new Set(PRESETS.filter((p) => !/^http:\/\/localhost/.test(p.baseUrl)).map((p) => new URL(p.baseUrl).origin))];
export function providerAllowed(baseUrl) {
  let u;
  try { u = new URL(baseUrl); } catch { return false; }
  if (u.username || u.password) return false;
  if (u.protocol === "http:" && (u.hostname === "localhost" || u.hostname === "127.0.0.1")) return true;
  return u.protocol === "https:" && PRESET_ORIGINS.includes(u.origin);
}

export const HOST_BLOCKED = "custom hosts are not allowed by this page's CSP; use a preset or localhost";
// Compatibility checkboxes carry data-wire="<harness wire option>" (PROTOCOL.md §2 `wire`).
const wireBoxes = () => [...document.querySelectorAll("input[data-wire]")];
const preset = () => PRESETS.find((p) => p.id === $("preset").value);

function readProvider() {
  return { baseUrl: $("baseUrl").value.trim().replace(/\/+$/, ""), model: $("model").value.trim(), apiKey: $("apiKey").value };
}

function applyPreset() {
  const p = preset();
  $("baseUrl").value = p.baseUrl;
  $("model").value = p.model || "";
  for (const box of wireBoxes()) box.checked = !!p.wire?.[box.dataset.wire];
  $("checkResult").textContent = "";
}

// Fills the preset list and wires the form; onChange runs after any edit that may change readSettings().
export function initSettingsForm(onChange) {
  for (const p of PRESETS) $("preset").append(new Option(p.label, p.id));
  $("preset").addEventListener("change", () => { applyPreset(); onChange(); });
  for (const id of ["apiKey", "consent", "model", "baseUrl", "iface", "budget", "tokenCap", "nAgents"]) $(id)?.addEventListener("input", onChange);
  $("check").addEventListener("click", checkKey);
  applyPreset();
}

// Agents in the swarm (the #nAgents select; 1..MAX_AGENTS, default DEFAULT_AGENTS).
export function readAgents() {
  const n = Math.round(Number($("nAgents")?.value));
  return Number.isFinite(n) && n >= 1 ? Math.min(MAX_AGENTS, n) : DEFAULT_AGENTS;
}

// The run settings, or {invalid: reason} while the form cannot start a run. nAgents is the swarm size; the call
// and token caps cover every agent (they are shared by all brokers, §6): the form's token cap is per agent.
export function readSettings() {
  const provider = readProvider();
  if (!$("consent").checked) return { invalid: "consent" };
  if (!provider.baseUrl || !provider.model) return { invalid: "provider" };
  if (!providerAllowed(provider.baseUrl)) return { invalid: "host" };
  if (!provider.apiKey && $("preset").value !== "local") return { invalid: "key" };
  const harness = { ...HARNESS_DEFAULTS, model: provider.model,
    budget: Math.min(40, Math.max(5, Number($("budget").value) || HARNESS_DEFAULTS.budget)) };
  const wire = {};
  for (const box of wireBoxes()) if (box.checked) wire[box.dataset.wire] = true;
  const nAgents = readAgents();
  const per = Math.round(Number($("tokenCap").value));
  const tokensPerAgent = Number.isSafeInteger(per) && per >= 10000 ? per : DEFAULT_TOKENS_PER_AGENT;
  return {
    preset: $("preset").value, iface: $("iface").value, provider, harness, wire, nAgents, tokensPerAgent,
    caps: sharedCaps(nAgents, harness.budget, tokensPerAgent),
  };
}

// The settings a swarm starts with: readSettings() (valid) with `override` merged over it (PROTOCOL.md §8
// startSwarm). nAgents is clamped to 1..MAX_AGENTS, and the shared caps follow the swarm size actually started
// unless the override sets caps.
export function swarmSettings(base, override = null) {
  const settings = { ...base, ...(override || {}) };
  settings.nAgents = Math.min(MAX_AGENTS, Math.max(1, Math.round(Number(settings.nAgents) || DEFAULT_AGENTS)));
  if (!override?.caps) settings.caps = sharedCaps(settings.nAgents, settings.harness.budget, settings.tokensPerAgent);
  return settings;
}

// "Check key": lists the provider's models into the model datalist.
export async function checkKey() {
  const { baseUrl, apiKey } = readProvider();
  const out = $("checkResult");
  if (!providerAllowed(baseUrl)) { out.textContent = HOST_BLOCKED; return; }
  out.textContent = "checking…";
  try {
    const r = await fetch(`${baseUrl}/models`, { headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {} });
    if (!r.ok) { out.textContent = r.status === 401 || r.status === 403 ? "key rejected" : `HTTP ${r.status}`; return; }
    const ids = ((await r.json()).data || []).map((m) => m?.id).filter((x) => typeof x === "string").sort();
    $("models").replaceChildren(...ids.slice(0, 500).map((id) => new Option(id)));
    out.textContent = `ok — ${ids.length} models`;
  } catch {
    out.textContent = "unreachable (network or CORS)";
  }
}
