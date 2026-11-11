// Step inspector: reasoning, content, tool calls, command, full output (with a reconstructed marker
// of what the model actually saw: head/tail halves of harness.output_chars), exit, duration,
// submissions, linked board events and relay bytes. Data: /agents/{a}/steps (whole agent, cached).
import { api } from '../api';
import { h, replace } from '../dom';
import { agentColor, stepColor } from '../lib/colors';
import { stepClassOf } from '../lib/board';
import { clockT, dur, num } from '../lib/format';
import type { StepRow, StepsResponse } from '../lib/types';

export class StepInspector {
  el = h('div', { class: 'insp' });
  private cache = new Map<string, Promise<StepsResponse>>();
  private runId = '';
  private agent: number | null = null;
  private stepIdx: number | null = null;
  private outputChars = 3000;
  private dataVersion = 0;

  constructor(private onNav: (agent: number, stepIdx: number) => void) {
    replace(this.el, h('div', { class: 'empty' }, 'Click a step in the lanes (or press 1–9 then [ / ]) to inspect it.'));
  }

  setRun(runId: string, outputChars: number | undefined, dataVersion: number) {
    if (runId !== this.runId || dataVersion !== this.dataVersion) this.cache.clear();
    this.runId = runId; this.outputChars = outputChars ?? 3000; this.dataVersion = dataVersion;
  }

  private load(agent: number) {
    const k = `${this.runId}:${agent}`;
    let p = this.cache.get(k);
    if (!p) { p = api.steps(this.runId, agent); this.cache.set(k, p); p.catch(() => this.cache.delete(k)); }
    return p;
  }

  /** Show the step at index stepIdx (index in the agent's step list, as in the lanes). */
  async show(agent: number, stepIdx: number | null) {
    this.agent = agent; this.stepIdx = stepIdx;
    if (stepIdx == null) { replace(this.el, h('div', { class: 'empty' }, `a${agent} selected — click a step or press [ / ].`)); return; }
    let data: StepsResponse;
    try { data = await this.load(agent); } catch (e) { replace(this.el, h('div', { class: 'err' }, String(e))); return; }
    if (this.agent !== agent || this.stepIdx !== stepIdx) return; // superseded
    const st = data.steps[stepIdx];
    if (!st) { replace(this.el, h('div', { class: 'empty' }, 'step not loaded yet')); return; }
    this.render(agent, stepIdx, st, data);
  }

  private render(agent: number, idx: number, st: StepRow, data: StepsResponse) {
    const cls = stepClassOf({ t: st.t ?? 0, lane: agent, kind: st.idle ? 'idle' : 'step', cat: st.cat, is_board: st.is_board, step: st.step, label: st.command ?? '', ref: '' },
      new Set((st.board ?? []).some((b) => b.target_kind === 'write' || b.kind === 'post') ? [agent + ':' + st.step] : []));
    const m = st.model;
    const nav = h('div', { class: 'row' },
      h('button', { class: 'btn icon', disabled: idx <= 0, title: 'Previous step ([)', onclick: () => this.onNav(agent, idx - 1) }, '‹'),
      h('button', { class: 'btn icon', disabled: idx >= data.steps.length - 1, title: 'Next step (])', onclick: () => this.onNav(agent, idx + 1) }, '›'));
    const retries = data.retries.filter((r) => st.t != null && r.after_t <= st.t && (idx === 0 || (data.steps[idx - 1].t ?? 0) < r.after_t));
    replace(this.el,
      h('div', { class: 'hd' },
        h('i', { style: { width: '10px', height: '10px', borderRadius: '50%', background: agentColor(agent), display: 'inline-block' } }),
        h('b', null, `a${agent} · step ${st.step}`),
        h('span', { class: 'legend' }, h('span', { class: 'it' }, cls === 'idle' ? h('i', { class: 'hollow' }) : h('i', { class: 'sw', style: { background: stepColor(cls) } }), cls === 'cmd' ? (st.cat ?? 'cmd') : cls)),
        st.is_solve ? h('span', { class: 'flag correct' }, 'solve') : null,
        st.persistence ? h('span', { class: 'flag secret_leak' }, 'persistence') : null,
        h('span', { class: 'grow' }), nav),
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 't'), h('span', { class: 'tnum' }, `${clockT(st.t)} → ${clockT(st.t_end)} (${dur(st.dur_s)})`),
        h('span', { class: 'k' }, 'phase'), h('span', null, st.phase_before === st.phase ? (st.phase ?? '–') : `${st.phase_before} → ${st.phase}`),
        h('span', { class: 'k' }, 'exit'), h('span', { class: 'tnum' }, st.exit == null ? '–' : String(st.exit)),
        st.is_board ? h('span', { class: 'k' }, 'board via') : null, st.is_board ? h('span', null, st.board_via ?? '–') : null,
        m ? h('span', { class: 'k' }, 'model call') : null,
        m ? h('span', { class: 'tnum' }, `#${m.call} · latency ${dur(m.latency_s)} · ${num(m.usage?.prompt_tokens)} prompt / ${num(m.usage?.completion_tokens)} completion / ${num(m.usage?.reasoning_tokens)} reasoning tok`) : null),
      retries.length ? h('div', { class: 'warnbadge' }, `API retry before this call: ${retries.map((r) => `${r.detail} (wait ${dur(r.wait)})`).join(', ')}`) : null,
      m?.reasoning ? h('details', { open: true }, h('summary', null, 'Reasoning'), h('pre', null, m.reasoning)) : null,
      m?.content && m.content.trim() ? h('details', { open: true }, h('summary', null, 'Content'), h('pre', null, m.content)) : null,
      m?.tool_calls && m.tool_calls.length > 1 ? h('details', null, h('summary', null, `Tool calls in this turn (${m.tool_calls.length})`),
        ...m.tool_calls.map((tc) => h('pre', null, `[${tc.idx}] ${tc.name}${tc.args_valid ? '' : ' (invalid arguments)'}\n${tc.command ?? tc.arguments ?? ''}`))) : null,
      h('div', { class: 'sec' }, st.idle ? 'Idle turn (no tool call)' : 'Command'),
      st.idle ? null : h('pre', null, st.command ?? ''),
      st.idle ? null : h('div', { class: 'sec' }, `Output · ${num(st.output_len)} chars`),
      st.idle ? null : this.output(st.output ?? '', st.output_len ?? (st.output ?? '').length),
      st.submissions.length ? h('div', { class: 'sec' }, 'Submissions') : null,
      ...st.submissions.map((sb) => h('div', null, h('code', null, sb.value), sb.correct ? h('span', { class: 'flag correct' }, 'correct') : sb.correct === false ? h('span', { class: 'flag' }, 'incorrect') : null)),
      st.board?.length ? h('div', { class: 'sec' }, `Board events (${st.board.length})`) : null,
      ...(st.board ?? []).map((b) => h('div', { class: 'mono', style: { fontSize: '11px' } }, `${clockT(b.t)} ${b.kind} ${b.method ?? ''} ${b.path ?? ''} ${b.status ?? ''} ${b.target_kind ? '· ' + b.target_kind : ''}`, b.body ? h('div', { class: 'secondary' }, b.body) : null)),
      st.relay ? h('div', { class: 'muted', style: { marginTop: '4px', fontSize: '11px' } }, `relay: ${st.relay.conns} conn · ${num(st.relay.bytes_up)} B up · ${num(st.relay.bytes_down)} B down`) : null,
    );
  }

  /** Full output with the part the model did NOT see dimmed (reconstructed: head/tail halves of output_chars). */
  private output(out: string, len: number) {
    const lim = this.outputChars;
    if (len <= lim || out.length <= lim) return h('pre', null, out || '(empty)');
    const half = Math.floor(lim / 2);
    const hidden = out.length - 2 * half;
    return h('pre', null, out.slice(0, half),
      h('span', { class: 'cut' }, `▼ ${num(hidden)} chars not shown to the model (reconstructed from harness.output_chars=${lim}) ▼`),
      h('span', { class: 'hidden' }, out.slice(half, out.length - half)),
      h('span', { class: 'cut' }, '▲ end of hidden middle ▲'),
      out.slice(out.length - half));
  }
}
