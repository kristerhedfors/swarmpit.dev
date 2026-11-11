// Board state panel: threads/posts reconstructed client-side at the cursor (seed posts + live posts
// with t <= cursor, upvotes accumulated, posts newer than 30 s highlighted). Presentation follows
// the interface: forum threads, wiki pages, cache directory tree, IRC channel log.
import { h, replace } from '../dom';
import type { BoardPostAt, RunModel, ThreadAt } from '../lib/board';
import { agentColor } from '../lib/colors';
import { clockT, dur } from '../lib/format';
import { lastLEnum } from '../lib/time';

export class BoardState {
  el = h('div', { class: 'board' });
  private sig = '';
  private m: RunModel | null = null;
  private seedOpen = false;
  private headLabel: HTMLElement | null = null;
  private liveN = 0;

  setModel(m: RunModel) { this.m = m; this.sig = ''; this.el.className = 'board ' + m.iface; }

  update(t: number) {
    const m = this.m; if (!m) return;
    const n = lastLEnum(m.postT, t) + 1;
    let ups = 0; for (const u of m.upvotes) { if (u.t > t) break; ups++; }
    const newest = n ? (m.posts[n - 1].t ?? -1e9) : -1e9;
    const isNewWindow = Math.floor((t - newest) / 30) <= 0 ? 'n' : 'o';
    const sig = `${n}|${ups}|${isNewWindow}|${m.posts.length}|${this.seedOpen}`;
    const label = (live: number) => `${m.iface} · ${live} live post${live === 1 ? '' : 's'} at ${clockT(t)}`;
    // board content unchanged: only the cursor time in the header moves
    if (sig === this.sig) { if (this.headLabel) this.headLabel.textContent = label(this.liveN); return; }
    this.sig = sig;
    const threads = m.boardAt(t);
    const live = threads.reduce((s, th) => s + th.posts.filter((p) => !p.is_seed).length, 0);
    this.liveN = live;
    this.headLabel = h('span', { class: 'secondary' }, label(live));
    const head = h('div', { class: 'row', style: { marginBottom: '8px' } },
      this.headLabel,
      h('span', { class: 'grow' }),
      h('label', { class: 'muted', style: { fontSize: '11px' } },
        h('input', { type: 'checkbox', checked: this.seedOpen, onchange: (e: Event) => { this.seedOpen = (e.target as HTMLInputElement).checked; this.update(t); } }), ' expand seed posts'));
    const body = m.iface === 'irc' ? this.irc(threads, t) : m.iface === 'cache' ? this.cache(threads, t) : this.threads(threads, t, m.iface === 'wiki');
    replace(this.el, head, body);
    // scroll newest highlighted post into view
    const nw = this.el.querySelector('.post.new');
    if (nw) (nw as HTMLElement).scrollIntoView({ block: 'nearest' });
  }

  private author(p: BoardPostAt) {
    return h('span', { class: 'by' },
      p.is_seed ? h('i', { class: 'seedsw', title: 'seed persona' }) : h('i', { class: 'sw', style: { background: agentColor(p.agent_id ?? -1) } }),
      h('b', null, p.author ?? '?'),
      p.is_seed ? h('span', { class: 'muted' }, 'seed') : h('span', { class: 'muted' }, `a${p.agent_id}${p.phase ? ' · ' + p.phase : ''}`),
      h('span', { class: 'muted' }, p.t != null && p.t < 0 ? dur(-p.t) + ' before' : clockT(p.t)),
      p.upvotes ? h('span', { class: 'muted' }, `▲${p.upvotes}`) : null,
      ...p.flags.map((f) => h('span', { class: 'flag ' + f }, f.replace('_', ' '))));
  }

  private post(p: BoardPostAt) {
    const collapsed = p.is_seed && !this.seedOpen;
    return h('div', { class: 'post' + (p.is_seed ? ' seed' : '') + (p.isNew ? ' new' : '') },
      this.author(p), collapsed ? null : h('div', { class: 'body' }, p.body ?? ''));
  }

  private threads(threads: ThreadAt[], _t: number, wiki: boolean) {
    const sorted = [...threads].sort((a, b) => (a.is_seed === b.is_seed ? 0 : a.is_seed ? 1 : -1) || b.lastT - a.lastT);
    return h('div', null, ...sorted.map((th) => h('div', { class: 'thread' },
      h('div', { class: 'th' }, wiki ? camel(th.thread) : th.title, wiki ? null : h('span', { class: 'slug' }, th.thread), th.is_seed ? null : h('span', { class: 'flag' }, 'new thread')),
      ...th.posts.map((p) => this.post(p)))));
  }

  private irc(threads: ThreadAt[], _t: number) {
    return h('div', null, ...threads.map((th) => h('div', { class: 'thread' },
      h('div', { class: 'th' }, th.thread.startsWith('#') ? th.thread : '#' + th.thread),
      ...th.posts.map((p) => h('div', { class: 'post' + (p.is_seed ? ' seed' : '') + (p.isNew ? ' new' : '') },
        h('span', { class: 'muted' }, p.t != null && p.t < 0 ? '--:--' : clockT(p.t)),
        p.is_seed ? h('i', { class: 'seedsw' }) : h('i', { class: 'sw', style: { background: agentColor(p.agent_id ?? -1), width: '8px', height: '8px', borderRadius: '50%', display: 'inline-block', marginTop: '4px' } }),
        h('b', null, '<' + (p.author ?? '?') + '>'),
        h('span', { class: 'body' }, p.body ?? ''),
        ...p.flags.filter((f) => f === 'secret_leak').map((f) => h('span', { class: 'flag ' + f }, 'leak')))))));
  }

  private cache(threads: ThreadAt[], _t: number) {
    // directory per thread, file per post: NNNN-<author>.txt (seed) or the upload name
    return h('div', { class: 'tree' }, h('div', { class: 'muted' }, '/artifactory/agent-cache/'), ...threads.map((th) => h('div', { class: 'thread' },
      h('div', { class: 'th' }, '📁 ' + th.thread + '/'),
      ...th.posts.map((p) => h('div', { class: 'post' + (p.is_seed ? ' seed' : '') + (p.isNew ? ' new' : '') },
        h('div', { class: 'by' }, p.is_seed ? h('i', { class: 'seedsw' }) : h('i', { class: 'sw', style: { background: agentColor(p.agent_id ?? -1) } }),
          h('span', null, `${String(p.post_id ?? 0).padStart(4, '0')}-${slug(p.author ?? 'anon')}.txt`),
          h('span', { class: 'muted' }, `${(p.body ?? '').length} B · ${p.is_seed ? 'seed' : clockT(p.t)}`),
          ...p.flags.map((f) => h('span', { class: 'flag ' + f }, f.replace('_', ' ')))),
        this.seedOpen || !p.is_seed ? h('div', { class: 'body' }, decodeForm(p.body ?? '')) : null)))));
  }
}

function camel(s: string) { return s.split(/[-_]/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(''); }
function slug(s: string) { return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function decodeForm(s: string) {
  if (/^[\w.%+-]+=/.test(s) && /%[0-9A-F]{2}|\+/.test(s)) { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch { return s; } }
  return s;
}
