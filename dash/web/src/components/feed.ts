// Live feed items: reverse-chronological stream of swarm events (SSE `event` payloads + lifecycle).
import { h } from '../dom';
import { agentColor, critical, cv, stepColor } from '../lib/colors';
import { clockT } from '../lib/format';
import type { LiveMessage } from '../lib/types';

export const HIGHLIGHT = new Set(['post', 'dm', 'first_contact', 'solve', 'secret_leak', 'persistence', 'swarm_onset']);
export const FEED_KINDS = ['post', 'dm', 'first_contact', 'solve', 'secret_leak', 'persistence', 'swarm_onset', 'step', 'req', 'upvote', 'irc_cmd', 'lifecycle', 'status'];

export function kindColor(kind: string): string {
  switch (kind) {
    case 'post': case 'dm': case 'upvote': case 'irc_cmd': return stepColor('write');
    case 'first_contact': case 'req': return stepColor('read');
    case 'solve': return stepColor('submit');
    case 'secret_leak': case 'persistence': return critical();
    case 'swarm_onset': return cv('--text-primary');
    default: return cv('--neutral-mark');
  }
}

export function feedKind(m: LiveMessage): string {
  if (m.event === 'event') return String(m.data.kind ?? 'event');
  if (m.event === 'run_updated' || m.event === 'metrics_update') return 'status';
  return 'lifecycle';
}

export function feedItem(m: LiveMessage, onOpen: (runId: string, t: number | null) => void): HTMLElement {
  const d = m.data as Record<string, unknown>;
  const runId = String(d.run_id ?? '');
  const kind = feedKind(m);
  const t = typeof d.t === 'number' ? d.t : null;
  const lane = typeof d.lane === 'number' ? d.lane : null;
  let label: string;
  if (m.event === 'event') label = String(d.label ?? '');
  else if (m.event === 'run_started') label = `run started · ${d.iface ?? ''} · ${d.n_agents ?? '?'} agents · ${d.model ?? ''}`;
  else if (m.event === 'run_finished') label = `run finished (${d.status ?? ''}${d.reconciled ? ', ' + d.reconciled : ''})`;
  else if (m.event === 'run_updated') label = `status ${d.status ?? ''} · v${d.data_version ?? ''}`;
  else if (m.event === 'metrics_update') { const mm = (d.metrics ?? {}) as Record<string, unknown>; label = `metrics · posts ${mm.total_posts ?? '–'} · discovery ${typeof mm.discovery_rate === 'number' ? Math.round(mm.discovery_rate * 100) + '%' : '–'} · live-live edges ${mm.live_live_edges ?? '–'}`; }
  else label = m.event;
  const crit = kind === 'secret_leak' || kind === 'persistence';
  return h('div', { class: 'item' + (crit ? ' crit' : HIGHLIGHT.has(kind) ? ' hl' : ''), onclick: () => onOpen(runId, t), title: 'Open run at this time' },
    h('span', { class: 'when' }, t != null ? clockT(t) : new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })),
    h('span', { class: 'what' },
      h('span', { class: 'row', style: { gap: '6px' } },
        h('span', { class: 'kindtag' }, h('i', { style: { background: kindColor(kind) } }), kind.replace('_', ' ')),
        lane != null && lane >= 0 ? h('span', { class: 'kindtag secondary' }, h('i', { style: { background: agentColor(lane) } }), 'a' + lane) : null,
        h('span', { class: 'muted mono', style: { fontSize: '11px' } }, runId),
        ...((d.flags as string[] | undefined) ?? []).map((f) => h('span', { class: 'flag ' + f }, f))),
      h('span', { class: 'lbl' }, label)));
}
