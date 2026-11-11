// Unit tests for the pure parts of public/js/settings.js: the provider allowlist matches the page's CSP
// connect-src (public/_headers), and the shared caps scale with the swarm size (also under a startSwarm override).
//   cd lab/web && node --test test/settings.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { HARNESS_DEFAULTS, MAX_AGENTS, PRESETS, PRESET_ORIGINS, callCap, providerAllowed, swarmSettings, tokenCap }
  from "../public/js/settings.js";

const headers = readFileSync(new URL("../public/_headers", import.meta.url), "utf8");
const csp = /Content-Security-Policy:\s*(.+)/.exec(headers)[1];
const connect = csp.split(";").map((d) => d.trim().split(/\s+/)).find((d) => d[0] === "connect-src").slice(1);

test("every preset origin is in the CSP connect-src, and every https CSP origin but the infra hosts is a preset", () => {
  for (const o of PRESET_ORIGINS) assert.ok(connect.includes(o), o);
  const infra = new Set(["'self'", "https://cxrtnc.leaningtech.com", "https://vm.swarmpit.dev", "http://localhost:*", "http://127.0.0.1:*"]);
  for (const o of connect) if (!infra.has(o)) assert.ok(PRESET_ORIGINS.includes(o), o);
  assert.ok(connect.includes("http://localhost:*") && connect.includes("http://127.0.0.1:*"));
});

test("providerAllowed: presets and localhost pass, custom hosts and credentials in the URL do not", () => {
  for (const p of PRESETS) assert.ok(providerAllowed(p.baseUrl), p.id);
  assert.ok(providerAllowed("http://127.0.0.1:8000/v1"));
  assert.ok(providerAllowed("http://localhost:1234/v1"));
  for (const u of ["https://my-vllm.example.test/v1", "http://api.openai.com/v1", "https://api.openai.com.example.test/v1",
    "https://user:pw@api.openai.com/v1", "not a url", "", "https://localhost/v1"]) assert.equal(providerAllowed(u), false, u);
});

test("the shared call and token caps scale with the agents", () => {
  assert.equal(callCap(4, 40), 260);
  assert.equal(tokenCap(4, 1000000), 4000000);
  assert.equal(tokenCap(1, 1000000), 1000000);
});

test("swarmSettings: an override's nAgents is clamped and the caps follow it unless the override sets caps", () => {
  const base = { harness: { ...HARNESS_DEFAULTS }, nAgents: 4, tokensPerAgent: 1000000, caps: { calls: 260, tokens: 4000000 } };
  assert.deepEqual(swarmSettings(base).caps, base.caps);
  const two = swarmSettings(base, { nAgents: 2 });
  assert.equal(two.nAgents, 2);
  assert.deepEqual(two.caps, { calls: callCap(2, 40), tokens: tokenCap(2, 1000000) });
  assert.equal(swarmSettings(base, { nAgents: 99 }).nAgents, MAX_AGENTS);
  assert.equal(swarmSettings(base, { nAgents: 0 }).nAgents, 4, "a missing or zero size falls back to the default");
  assert.deepEqual(swarmSettings(base, { nAgents: 1, caps: { calls: 7, tokens: 8 } }).caps, { calls: 7, tokens: 8 });
});
