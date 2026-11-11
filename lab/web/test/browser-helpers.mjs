// Shared Playwright helpers of the lab page browser checks (test/ui.e2e.mjs, test/e2e.mjs). Everything here reports
// counts, booleans and the page's test-hook results (PROTOCOL.md §8), never page or run content.
import { chromium, firefox, webkit } from "playwright";

export const ENGINES = { chromium, firefox, webkit };

// Init script: counts CSP violation events in window.__csp, and in window.__cspKinds by directive + our own source
// file name and line (never a sample of the blocked content).
export function countCspViolations() {
  window.__csp = 0;
  window.__cspKinds = {};
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__csp++;
    const k = `${e.effectiveDirective} ${String(e.sourceFile || "").split("/").pop()}:${e.lineNumber} ${String(e.blockedURI).slice(0, 16)}`;
    window.__cspKinds[k] = (window.__cspKinds[k] || 0) + 1;
  });
}

// A 1400x900 page of ctx that counts page errors, console errors and CSP console messages (matching cspConsole)
// into counters {pageErrors, consoleErrors, consoleCsp}, and accepts every dialog (the delete confirm()).
export async function newLabPage(ctx, counters, cspConsole = /Content Security Policy|Refused to/i) {
  const page = await ctx.newPage();
  await page.setViewportSize({ width: 1400, height: 900 });
  page.on("pageerror", () => { counters.pageErrors++; });
  page.on("console", (m) => { if (m.type() === "error") { counters.consoleErrors++; if (cspConsole.test(m.text())) counters.consoleCsp++; } });
  page.on("dialog", (d) => { void d.accept(); });
  return page;
}

export const labState = (page) => page.evaluate(() => window.__lab.state());
export const labTraces = (page) => page.evaluate(() => window.__lab.traces());
// the player has a run loaded (its root carries .loaded)
export const playerLoaded = (page, timeout) =>
  page.waitForFunction(() => document.getElementById("player").classList.contains("loaded"), null, { timeout });

export const sameBytes = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
