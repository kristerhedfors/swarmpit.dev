// Port of dash/server/obs/parse.py: run_id grammar, JSONL line splitter, record normalizers (CONTRACT 3.2).
//
// Every normalizer takes a decoded record plus its identity (agent_id, line_off, seq) and returns a row with
// the obs column names, so derive.ts reads exactly what derive.py reads from DuckDB.
import { type Dict, type J, asText, isDict, pyStr, toFloat, toInt, toStr, truthy } from './py.js';

export const DEFAULT_GOAL = 'solvable';
const RUN_RE = /^(?<iface>[a-z]+)-(?<pop>solo|multi(?<n>\d+))-r(?<rep>\d+)(?<goal>-unsolvable)?(?:--(?<mslug>[a-z0-9]{2,12}))?(?:-(?<tag>[A-Za-z0-9_-]+))?$/;
// Browser-lab ids (docs/browser-swarm.md §1, PROTOCOL.md §2): browser-<iface>-(solo|multi<N>)-<UTC>-<hex>.
// obs does not parse them (iface/n come from run.json); the converter needs them for live runs without one.
const BROWSER_RE = /^browser-(?<iface>[a-z]+)-(?<pop>solo|multi(?<n>\d+))-\d{14}-[0-9a-f]+$/;

export interface RunIdInfo { iface: string | null; pop: string | null; n_agents: number | null; rep: number | null;
                             tag: string; model_slug: string; goal: string | null; matched: boolean }

export function parseRunId(runId: string): RunIdInfo {
  const m = RUN_RE.exec(runId);
  if (!m || !m.groups) return { iface: null, pop: null, n_agents: null, rep: null, tag: '', model_slug: '', goal: null, matched: false };
  const g = m.groups;
  const n = g.pop === 'solo' ? 1 : Number(g.n);
  return { iface: g.iface!, pop: n === 1 ? 'solo' : 'multi', n_agents: n, rep: Number(g.rep), tag: g.tag ?? '',
           model_slug: g.mslug ?? '', goal: g.goal ? 'unsolvable' : DEFAULT_GOAL, matched: true };
}
export function parseBrowserRunId(runId: string): { iface: string; n_agents: number } | null {
  const m = BROWSER_RE.exec(runId);
  if (!m || !m.groups) return null;
  return { iface: m.groups.iface!, n_agents: m.groups.pop === 'solo' ? 1 : Number(m.groups.n) };
}
export function runGoal(params: Dict | null): string {
  const g = (params ?? {}).goal;
  return typeof g === 'string' && g ? g : DEFAULT_GOAL;
}
const VM_AGENT_RE = /-a(\d+)$/;
export function vmAgentId(name: string): number | null {
  const m = VM_AGENT_RE.exec(name || '');
  return m ? Number(m[1]) : null;
}

// ------------------------------------------------------------------------------------------------ lines
export interface RawLine { off: number; seq: number; bytes: Uint8Array; tail: boolean }

/** split_lines + the final-ingest tail rule (ingest.py ingest_jsonl_file final=True): complete lines, then an
 *  unterminated last line as one more record with the next seq. */
export function splitLines(buf: Uint8Array): RawLine[] {
  const out: RawLine[] = [];
  let pos = 0, seq = 0;
  const n = buf.length;
  while (pos < n) {
    let j = buf.indexOf(10, pos);
    const tail = j < 0;
    if (tail) j = n;
    out.push({ off: pos, seq: seq++, bytes: buf.subarray(pos, j), tail });
    pos = j + 1;
  }
  return out;
}

const DEC = new TextDecoder('utf-8', { fatal: true });
/** json.loads(bytes) or undefined (invalid UTF-8 / JSON, NaN-free like JSON.parse). */
export function parseJsonBytes(b: Uint8Array): J | undefined {
  try {
    return JSON.parse(DEC.decode(b));
  } catch {
    return undefined;
  }
}
export function isBlank(b: Uint8Array): boolean {
  for (const c of b) if (!(c === 32 || (c >= 9 && c <= 13))) return false;
  return true;
}

// ------------------------------------------------------------------------------------------------ rows
export interface Submission { ts: number | null; value: string; correct: boolean }
export interface StepRow {
  agent_id: number; line_off: number; seq: number; step: number | null; t0_raw: number | null; t1_raw: number | null;
  phase_before: J; phase: J; is_solve: boolean; idle: boolean; command: string | null; exit: number | null;
  output: string | null; output_len: number; submissions: Submission[]; submit_correct: boolean | null;
  // derived
  is_board: boolean; board_via: string | null; cat: string | null;
  persistence: boolean; t: number | null; t_end: number | null;
}
export interface ModelRow {
  agent_id: number; line_off: number; seq: number; call: number | null; ts_raw: number | null; phase: J;
  content: string | null; reasoning: string | null; n_tool_calls: number; prompt_tokens: number | null;
  completion_tokens: number | null; reasoning_tokens: number | null; cached_tokens: number | null;
  // derived
  ts: number | null; t: number | null; latency_s: number | null;
}
export interface RecRow {
  agent_id: number; line_off: number; seq: number; kind: string; ts_raw: number | null; attempt: number | null;
  wait: number | null; detail: string | null; data: Dict;
  ts: number | null; t: number | null;
}
export interface BoardRow {
  line_off: number; seq: number; ts_raw: number | null; agent_id: number | null; kind: string; iface: string | null;
  method: string | null; path: string | null; status: number | null; bytes: number | null; thread: string | null;
  author: string | null; body: string | null; post_id: number | null; thread_seq: number | null; nick: string | null;
  to_nick: string | null; cmd: string | null; line: string | null; chan: string | null; data: Dict;
  // derived
  ts: number | null; t: number | null; step: number | null; attribution: string | null;
  target_kind: string | null; target: string | null; _req?: BoardRow | null;
}
export interface RelayRow {
  agent_id: number; line_off: number; seq: number; ts_raw: number | null; ev: J; conn: number | null; dir: J;
  n: number | null; step: number | null;
}

export function normStep(rec: Dict, agent: number, off: number, seq: number, flag: string | null): StepRow {
  let subs: J = rec.submissions || [];
  if (!Array.isArray(subs)) subs = [subs];
  const parsed: Submission[] = [];
  for (const s0 of subs as J[]) {
    const s = pyStr(s0);
    const i = s.indexOf('\t');
    const ep = i >= 0 ? s.slice(0, i) : null, val = i >= 0 ? s.slice(i + 1) : s;
    parsed.push({ ts: toInt(ep), value: val, correct: Boolean(flag) && val.includes(flag!) });
  }
  const out = rec.output;
  const t0 = toFloat(rec.t0), t1 = toFloat(rec.t1);
  return {
    agent_id: agent, line_off: off, seq, step: toInt(rec.step), t0_raw: t0, t1_raw: t1,
    phase_before: rec.phase_before ?? null, phase: rec.phase ?? null,
    is_solve: rec.phase_before === 'task' && rec.phase === 'free', idle: truthy(rec.idle),
    command: asText(rec.command), exit: toInt(rec.exit), output: toStr(out),
    output_len: typeof out === 'string' ? [...out].length : 0,
    submissions: parsed, submit_correct: parsed.length ? parsed.some((p) => p.correct) : null,
    is_board: false, board_via: null, cat: null, persistence: false, t: null, t_end: null,
  };
}

export function normModel(rec: Dict, agent: number, off: number, seq: number): ModelRow {
  const u = isDict(rec.usage) ? rec.usage : {};
  let calls: J = rec.tool_calls || [];
  if (!Array.isArray(calls)) calls = [];
  const cd = u['completion_tokens_details'], pd = u['prompt_tokens_details'];
  const ctd = isDict(cd) ? cd : {};
  const ptd = isDict(pd) ? pd : {};
  // some providers report reasoning_tokens top-level instead of in completion_tokens_details (0 stays 0)
  let rt = ctd.reasoning_tokens;
  if (rt === null || rt === undefined) rt = u.reasoning_tokens;
  return {
    agent_id: agent, line_off: off, seq, call: toInt(rec.call), ts_raw: toFloat(rec.ts), phase: rec.phase ?? null,
    content: toStr(rec.content), reasoning: toStr(rec.reasoning), n_tool_calls: (calls as J[]).length,
    'prompt_tokens': toInt(u['prompt_tokens']), 'completion_tokens': toInt(u['completion_tokens']), 'reasoning_tokens': toInt(rt),
    'cached_tokens': toInt(ptd['cached_tokens']), ts: null, t: null, latency_s: null,
  };
}

export function normRecord(rec: Dict, agent: number, off: number, seq: number): RecRow {
  const kind = rec.kind;
  let detail: string | null = null;
  if (kind === 'api_retry') {
    detail = toStr(rec.detail);
    try { // compact the provider error to its code when JSON
      const d = JSON.parse(detail || '');
      if (isDict(d) && d.code) detail = pyStr(d.code);
    } catch { /* not JSON */ }
  } else if (kind === 'crash') detail = toStr(rec.error);
  else if (kind === 'end') detail = asText(rec.ended);
  return { agent_id: agent, line_off: off, seq, kind: typeof kind === 'string' && kind ? kind : (kind ? pyStr(kind) : 'unknown'),
           ts_raw: toFloat(rec.ts), attempt: toInt(rec.attempt), wait: toFloat(rec.wait), detail, data: rec, ts: null, t: null };
}

/** norm_board_record. Promoted structural fields; legacy inline post content (author/body/line) is kept. */
export function normBoard(rec: Dict, off: number, seq: number): BoardRow {
  const s = (k: string): string | null => (k in rec ? toStr(rec[k]) : null);
  const i = (k: string): number | null => (k in rec ? toInt(rec[k]) : null);
  const kind = rec.kind;
  const row: BoardRow = {
    line_off: off, seq, ts_raw: toFloat(rec.ts), agent_id: toInt(rec.agent),
    kind: typeof kind === 'string' && kind ? kind : (kind ? pyStr(kind) : 'unknown'),
    iface: s('iface'), method: s('method'), path: s('path'), status: i('status'), bytes: i('bytes'), thread: s('thread'),
    author: s('author'), body: s('body'), post_id: i('post_id'), thread_seq: i('thread_seq'), nick: s('nick'),
    to_nick: 'to' in rec ? toStr(rec.to) : null, cmd: s('cmd'), line: s('line'), chan: s('chan'), data: rec,
    ts: null, t: null, step: null, attribution: null, target_kind: null, target: null,
  };
  // New structural events name the channel `channel`; keep the `iface` column populated.
  if ('channel' in rec && !('iface' in rec)) row.iface = toStr(rec.channel);
  return row;
}

export function normRelay(rec: Dict, agent: number, off: number, seq: number): RelayRow {
  return { agent_id: agent, line_off: off, seq, ts_raw: toFloat(rec.ts), ev: rec.ev ?? null, conn: toInt(rec.conn),
           dir: rec.dir ?? null, n: toInt(rec.n), step: null };
}
