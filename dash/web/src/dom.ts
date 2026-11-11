// Tiny DOM helper. All text goes through createTextNode (labels are untrusted data).
type Child = Node | string | number | null | undefined | false | Child[];
type Attrs = Record<string, unknown> | null | undefined;

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs?: Attrs, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (attrs) applyAttrs(el, attrs);
  append(el, children);
  return el;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function s<K extends keyof SVGElementTagNameMap>(tag: K, attrs?: Attrs, ...children: Child[]): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v as EventListener);
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

function applyAttrs(el: HTMLElement, attrs: Record<string, unknown>) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = String(v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset' && typeof v === 'object') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v as EventListener);
    else if (k in el && typeof v !== 'string') (el as unknown as Record<string, unknown>)[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

export function append(el: Node, children: Child[]) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

export function clear(el: Element) { while (el.firstChild) el.removeChild(el.firstChild); }

export function replace(el: Element, ...children: Child[]) { clear(el); append(el, children); }

/** Canvas sized to its CSS box at devicePixelRatio. Returns 2D ctx already scaled. */
export function fitCanvas(c: HTMLCanvasElement, w: number, hgt: number): CanvasRenderingContext2D {
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(hgt * dpr));
  if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  c.style.width = w + 'px'; c.style.height = hgt + 'px';
  const ctx = c.getContext('2d')!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

/** Shared floating tooltip (one per page). */
let tipEl: HTMLDivElement | null = null;
export function tooltip(): { show(x: number, y: number, ...content: Child[]): void; hide(): void } {
  if (!tipEl) { tipEl = h('div', { class: 'tip', role: 'tooltip' }); document.body.appendChild(tipEl); }
  const el = tipEl;
  return {
    show(x, y, ...content) {
      replace(el, ...content);
      el.style.display = 'block';
      const r = el.getBoundingClientRect();
      let left = x + 14, top = y + 14;
      if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
      if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
      el.style.left = Math.max(4, left) + 'px'; el.style.top = Math.max(4, top) + 'px';
    },
    hide() { el.style.display = 'none'; },
  };
}

export function onResize(el: Element, fn: (w: number, h: number) => void): () => void {
  let raf = 0;
  const ro = new ResizeObserver((entries) => {
    const r = entries[0].contentRect;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => fn(r.width, r.height));
  });
  ro.observe(el);
  return () => { ro.disconnect(); cancelAnimationFrame(raf); };
}

export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}
