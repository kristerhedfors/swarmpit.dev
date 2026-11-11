// Facet bar: one row of filter chips above everything it scopes. Dimensions come from /api/facets
// (param.* and harness.* dims appear automatically). Filters serialise exactly like API params.
import { h } from '../dom';
import { ifaceColor, popColor } from '../lib/colors';
import { shortModel } from '../lib/format';
import type { Facets, Filters } from '../lib/types';

const HIDE = new Set(['status']);
// Core dims are always visible; every other discovered dim (param.*, harness.*, condition...) sits behind "more filters".
const CORE = new Set(['model', 'iface', 'pop', 'n_agents', 'param.goal', 'tag', 'status', 'harness.budget', 'param.code_version']);
let moreOpen = false;
const LABEL: Record<string, string> = { model: 'model', iface: 'interface', pop: 'population', n_agents: 'agents', 'param.goal': 'goal', tag: 'tag', status: 'status' };

export function facetBar(facets: Facets, filters: Filters, onChange: (f: Filters) => void, opts: { showStatus?: boolean; extra?: Node[] } = {}): HTMLElement {
  const bar = h('div', { class: 'facetbar', role: 'toolbar', 'aria-label': 'Filters' });
  const toggle = (key: string, v: string) => {
    const f: Filters = { ...filters };
    const cur = new Set(f[key] ?? []);
    if (cur.has(v)) cur.delete(v); else cur.add(v);
    if (cur.size) f[key] = [...cur]; else delete f[key];
    onChange(f);
  };
  const more = h('div', { class: 'facetbar-more', style: { display: 'contents' } });
  let nMore = 0;
  for (const d of facets.dims) {
    if (HIDE.has(d.key) && !opts.showStatus) continue;
    if (d.values.length < 2 && !(filters[d.key]?.length)) continue;
    const isCore = CORE.has(d.key) || !!filters[d.key]?.length;
    const target: HTMLElement = isCore ? bar : more;
    if (!isCore) nMore++;
    const sel = new Set(filters[d.key] ?? []);
    const label = LABEL[d.key] ?? d.key.replace(/^param\./, '').replace(/^harness\./, 'harness.');
    target.appendChild(h('div', { class: 'facet' }, h('span', { class: 'k' }, label),
      ...d.values.slice(0, 12).map((v) => {
        const sv = v.value == null ? '' : String(v.value);
        const sw = d.key === 'iface' ? ifaceColor(sv) : d.key === 'pop' ? popColor(sv) : null;
        return h('button', { class: 'chip' + (sel.has(sv) ? ' on' : ''), 'aria-pressed': String(sel.has(sv)), onclick: () => toggle(d.key, sv), title: `${label} = ${sv || '(none)'}` },
          sw ? h('i', { class: 'sw', style: { background: sw } }) : null, sv === '' ? '(untagged)' : d.key === 'model' ? shortModel(sv) : sv, h('span', { class: 'n' }, String(v.n_runs)));
      })));
  }
  if (nMore) {
    more.style.display = moreOpen ? 'contents' : 'none';
    const btn = h('button', { class: 'btn', 'aria-expanded': String(moreOpen), onclick: () => { moreOpen = !moreOpen; more.style.display = moreOpen ? 'contents' : 'none'; btn.textContent = moreOpen ? 'fewer filters' : `more filters (${nMore})`; btn.setAttribute('aria-expanded', String(moreOpen)); } }, moreOpen ? 'fewer filters' : `more filters (${nMore})`);
    bar.appendChild(btn);
    bar.appendChild(more);
  }
  const inc = filters.include_tagged?.[0] === '1';
  bar.appendChild(h('label', { class: 'facet', title: 'Include smoke / calibration runs (tag != "")' },
    h('input', { type: 'checkbox', checked: inc, onchange: () => { const f = { ...filters }; if (inc) delete f.include_tagged; else f.include_tagged = ['1']; onChange(f); } }),
    h('span', { class: 'secondary' }, 'include tagged runs')));
  const n = Object.keys(filters).length;
  if (n) bar.appendChild(h('button', { class: 'btn', onclick: () => onChange({}) }, 'clear filters'));
  if (opts.extra) for (const x of opts.extra) bar.appendChild(x);
  return bar;
}
