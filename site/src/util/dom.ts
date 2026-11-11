type Attrs = Record<string, string | number | boolean | null | undefined | EventListener>;
type Child = Node | string | number | null | undefined | false | Child[];

function add(el: Node, c: Child): void {
  if (c === null || c === undefined || c === false) return;
  if (Array.isArray(c)) { for (const x of c) add(el, x); return; }
  el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
}

function apply(el: Element, a: Attrs | null | undefined): void {
  if (!a) return;
  for (const k in a) {
    const v = a[k];
    if (v === null || v === undefined || v === false) continue;
    if (typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.setAttribute('class', String(v));
    else if (k === 'html') el.innerHTML = String(v);
    // inline styles through the CSSOM, not the style attribute: a strict CSP (style-src without 'unsafe-inline', as on
    // lab.swarmpit.dev) blocks style attributes but not CSSOM writes
    else if (k === 'style') (el as HTMLElement).style.cssText = String(v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

/** Hyperscript: h('div.a.b', { 'aria-label': 'x', onclick: fn }, child...). Event keys start with 'on'. */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K | `${K}.${string}`, a?: Attrs | null, ...kids: Child[]): HTMLElementTagNameMap[K] {
  const [t, ...cls] = tag.split('.');
  const el = document.createElement(t as K);
  if (cls.length) el.className = cls.join(' ');
  apply(el, a);
  add(el, kids);
  return el;
}

export function s<K extends keyof SVGElementTagNameMap>(tag: K, a?: Attrs | null, ...kids: Child[]): SVGElementTagNameMap[K] {
  const el = document.createElementNS('http\x3a//www.w3.org/2000/svg', tag);
  apply(el, a);
  add(el, kids);
  return el;
}

export const $ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document): T | null => root.querySelector<T>(sel);

export function clear(el: Element): void { while (el.firstChild) el.removeChild(el.firstChild); }

/** Set textContent only if changed (cheap per-frame updates). */
export function text(el: Element, v: string): void { if (el.textContent !== v) el.textContent = v; }

export function toggle(el: Element, cls: string, on: boolean): void { if (el.classList.contains(cls) !== on) el.classList.toggle(cls, on); }

export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export const agentVar = (a: number): string => (a < 0 ? 'var(--text-muted)' : `var(--series-${(a % 8) + 1})`);
