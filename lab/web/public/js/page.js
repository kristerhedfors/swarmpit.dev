// Small page helpers shared by the lab page modules: DOM lookup, the notice line, and fixed error codes.
// Error text shown on the page is fixed text plus our own E_ codes, never archive, board or model content.

export const $ = (id) => document.getElementById(id);

// A fixed error code (E_ + upper-case words), as thrown by the page modules and swarm.js.
export const isCode = (c) => typeof c === "string" && /^E_[A-Z0-9_]{1,40}$/.test(c);
// An error's code (its `code`, else its message when that is a code), or E_UNKNOWN.
export const codeOf = (e) => (isCode(e?.code) ? e.code : isCode(e?.message) ? e.message : "E_UNKNOWN");
// An Error carrying `code` as message and code.
export const fail = (code) => Object.assign(new Error(code), { code });

// p, or a rejection with `code` after ms (p keeps running; its late result is dropped)
export function within(p, ms, code) {
  let timer;
  return Promise.race([p, new Promise((_, bad) => { timer = setTimeout(() => bad(fail(code)), ms); })]).finally(() => clearTimeout(timer));
}

// The page notice under the player (fixed text only); an empty text hides it.
export function notice(text, bad = false) {
  const n = $("notice");
  n.hidden = !text;
  n.textContent = text || "";
  n.classList.toggle("bad", !!bad);
}
