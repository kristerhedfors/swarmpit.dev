// The "+" new-swarm dialog (docs/browser-swarm.md §7): the settings form (settings.js), why Start is disabled, and
// the memory warning above WARN_AGENTS agents. Starting itself is the run controller's (run.js).
import { $, notice } from "./page.js";
import { DEFAULT_AGENTS, HOST_BLOCKED, WARN_AGENTS, initSettingsForm, readAgents, readSettings } from "./settings.js";

export const NOT_ISOLATED = "This page is not cross-origin isolated; the VMs cannot start.";

// createNewSwarmDialog({isolated, running, onStart}) -> {open, close, update}
//   running(): whether a swarm is running; onStart(): the Start button
export function createNewSwarmDialog({ isolated, running, onStart }) {
  // why Start is disabled, or ""
  function blocker() {
    if (!isolated) return NOT_ISOLATED;
    if (running()) return "A swarm is already running.";
    const s = readSettings();
    if (s.invalid === "consent") return "Tick the cost consent to start.";
    if (s.invalid === "key") return "Enter your API key.";
    if (s.invalid === "provider") return "Choose a base URL and model.";
    if (s.invalid === "host") return `The base URL is blocked: ${HOST_BLOCKED}.`;
    return "";
  }
  function update() {
    const why = blocker();
    $("start").disabled = !!why;
    $("startNote").textContent = why;
    $("agentsWarn").hidden = readAgents() <= WARN_AGENTS;
  }
  function open() {
    if (running()) { notice("A swarm is already running; stop it first.", true); return; }
    update();
    if (!$("newDlg").open) $("newDlg").showModal();
  }
  function close() { if ($("newDlg").open) $("newDlg").close(); }

  initSettingsForm(update);
  $("nAgents").value = String(DEFAULT_AGENTS);
  $("start").addEventListener("click", onStart);
  $("cancelNew").addEventListener("click", close);
  $("emptyNew").addEventListener("click", open);
  return { open, close, update };
}
