// Module C: src/convert (raw run dir -> player bundle, docs/browser-swarm.md §5) over synthetic run dirs built
// here with neutral placeholder text only. Needs `npm run build` (imports public/js/convert/index.js).
// Structural parity against tools/export.py over real run dirs is tools/convert_parity.mjs (needs the dash venv).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const JS = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public', 'js');
const { convertRun, runFilesFromEntries } = await import(join(JS, 'convert', 'index.js'));
const { assertCore, assertDetail } = await import(join(JS, 'data', 'loader.js'));
const T = await import(join(JS, 'types.js'));

const enc = new TextEncoder();
const CTF = { flag: 'FLAG{placeholder0000}', passphrase: 'alpha-beta-gamma', key_hex: '00112233445566778899aabbccddeeff', vault_id: 'v-0001' };
const T0 = 1_700_000_000;
const SEED = {
  board_name: 'test-board',
  threads: [
    { slug: 'intro', title: 'Intro thread', posts: [{ author: 'seedauthor1', body: 'placeholder seed text one', ago_min: 90 },
                                                    { author: 'seedauthor2', body: 'placeholder seed text two', ago_min: 30 }] },
    { slug: 'notes', title: 'Notes thread', posts: [{ author: 'seedauthor1', body: 'placeholder seed text three', ago_min: 10 }] },
  ],
};

/** A synthetic run dir: {iface, n, goal, sink (content in the chatter sink), live, browser, leak}. */
function makeRun({ iface = 'forum', n = 2, goal = 'solvable', sink = false, live = false, browser = false, leak = true,
                   id } = {}) {
  const runId = id ?? (browser ? `browser-${iface}-${n === 1 ? 'solo' : `multi${n}`}-20260101000000-abcd`
    : `${iface}-${n === 1 ? 'solo' : `multi${n}`}-r1${goal === 'unsolvable' ? '-unsolvable' : ''}`);
  const files = new Map();
  const put = (k, v) => files.set(k, enc.encode(typeof v === 'string' ? v : JSON.stringify(v)));
  const jsonl = (rows) => rows.map((r) => (typeof r === 'string' ? r : JSON.stringify(r))).join('\n') + '\n';
  const harness = { budget: 10, temperature: 0.7, max_tokens: 2048, output_chars: 3000, model: 'test-model' };
  const params = { ...(goal === 'unsolvable' ? { goal } : {}),
                   ...(browser ? { env: 'cheerpx', topology: 'board-split', isolation: 'weak-uid', preview: true, solve_verified: false } : {}) };
  const runJson = { run_id: runId, iface, n_agents: n, rep: 1, harness, ctf: CTF, timed_out: [], params, started: T0,
                    ...(live ? {} : { finished: T0 + 400 }) };
  put('run.json', runJson);
  put('seed.json', SEED);
  const board = [{ ts: T0 + 1, agent: null, kind: 'board_start' }];
  const content = [];
  let pid = 4; // 3 seed posts
  for (let i = 0; i < n; i++) {
    const a0 = T0 + 10 + i * 5;
    put(`cfg-a${i}.json`, { harness, ctf: CTF, agent_id: i, run_id: runId });
    const steps = [{ kind: 'start', ts: a0, agent: i, run: runId }];
    const relay = [];
    let ts = a0 + 1;
    for (let s = 1; s <= 4; s++) {
      steps.push({ kind: 'model', ts, call: s, phase: s > 2 && i === 0 && goal !== 'unsolvable' ? 'free' : 'task',
                   content: 'placeholder reasoning', reasoning: 'placeholder thought',
                   tool_calls: [{ id: `c${s}`, function: { name: 'bash', arguments: '{"command": "echo x"}' } }],
                   usage: { prompt_tokens: 100, completion_tokens: 20, prompt_tokens_details: { cached_tokens: 5 },
                            completion_tokens_details: { reasoning_tokens: 7 } } });
      const boardStep = s === 2 || s === 4;
      const solve = s === 2 && i === 0 && goal !== 'unsolvable';
      const cmd = boardStep ? 'curl -s http://board.internal:8080/api/v1/threads' : (s === 1 ? 'ls /' : 'cat ctf/notes.txt');
      steps.push({ kind: 'step', step: s, t0: ts + 0.5, t1: ts + 2, phase_before: s > 2 && i === 0 && goal !== 'unsolvable' ? 'free' : 'task',
                   phase: solve || (s > 2 && i === 0 && goal !== 'unsolvable') ? 'free' : 'task', command: cmd, exit: 0,
                   output: 'placeholder output', submissions: solve ? [`${Math.floor(ts)}\t${CTF.flag}`] : [] });
      if (boardStep) {
        relay.push({ ts: ts + 0.6, ev: 'open', conn: s }, { ts: ts + 0.7, ev: 'data', conn: s, dir: 'up', n: 80 });
        if (iface === 'irc') {
          board.push({ ts: ts + 0.7, agent: i, kind: 'connect', nick: `nick${i}` });
          board.push({ ts: ts + 0.75, agent: i, kind: 'irc_cmd', cmd: 'JOIN', line: 'JOIN #intro' });
          board.push({ ts: ts + 0.8, agent: i, kind: 'irc_join', chan: '#intro', nick: `nick${i}` });
          if (s === 4) {
            const body = leak && i === 0 ? `placeholder ${CTF.flag}` : `placeholder message from nick${i} to nick${(i + 1) % n}`;
            board.push({ ts: ts + 0.9, agent: i, kind: 'irc_cmd', cmd: 'PRIVMSG', line: 'PRIVMSG #intro :placeholder' });
            if (sink) {
              board.push({ ts: ts + 0.95, agent: i, kind: 'post', channel: 'irc', post_id: pid, thread_seq: 1, body_len_chars: body.length });
              content.push({ post_id: pid, author: `nick${i}`, body, channel: 'irc', thread_seq: 1 });
            } else board.push({ ts: ts + 0.95, agent: i, kind: 'post', iface: 'irc', post_id: pid, thread: 'intro', author: `nick${i}`, body });
            pid++;
            board.push({ ts: ts + 1.0, agent: i, kind: 'dm', nick: `nick${i}`, to: `nick${(i + 1) % n}`, body: 'placeholder dm' });
          }
          board.push({ ts: ts + 1.2, agent: i, kind: 'disconnect', nick: `nick${i}` });
        } else {
          const listPath = iface === 'forum' ? '/api/v1/threads' : iface === 'wiki' ? '/wiki/RecentChanges' : '/artifactory/api/storage/agent-cache/intro';
          const readPath = iface === 'forum' ? '/api/v1/threads/intro' : iface === 'wiki' ? '/wiki/Intro' : '/artifactory/agent-cache/intro/0001-seedauthor1.txt';
          board.push({ ts: ts + 0.7, agent: i, kind: 'req', method: 'GET', path: listPath, status: 200, bytes: 100 });
          board.push({ ts: ts + 0.8, agent: i, kind: 'req', method: 'GET', path: readPath, status: 200, bytes: 100 });
          if (s === 4) {
            const body = leak && i === 0 ? `placeholder ${CTF.flag}` : `placeholder reply from agentname${i} to agentname${(i + 1) % n}`;
            const author = `agentname${i}`;
            if (sink) {
              board.push({ ts: ts + 0.9, agent: i, kind: 'post', channel: iface, post_id: pid, thread_seq: 1, body_len_chars: body.length });
              content.push({ post_id: pid, author, body, channel: iface, thread_seq: 1 });
            } else board.push({ ts: ts + 0.9, agent: i, kind: 'post', iface, post_id: pid, thread: 'intro', author, body });
            const wpath = iface === 'forum' ? '/api/v1/threads/intro/posts' : iface === 'wiki' ? '/wiki/edit?append=x' : '/artifactory/agent-cache/intro/x.txt';
            board.push({ ts: ts + 0.95, agent: i, kind: 'req', method: iface === 'wiki' ? 'GET' : iface === 'cache' ? 'PUT' : 'POST',
                         path: wpath, status: 201, bytes: 10 });
            if (iface === 'forum') {
              board.push({ ts: ts + 1.0, agent: i, kind: 'upvote', post_id: 1, iface });
              board.push({ ts: ts + 1.05, agent: i, kind: 'req', method: 'POST', path: '/api/v1/posts/1/upvote', status: 200, bytes: 5 });
            }
            pid++;
          }
        }
      }
      ts += 3;
    }
    if (!live) {
      steps.push({ kind: 'end', ts: ts + 1, ended: 'budget', solved: i === 0 && goal !== 'unsolvable', steps_used: 4, model_calls: 4 });
      put(`a${i}/summary.json`, { run: runId, agent: i, solved: i === 0 && goal !== 'unsolvable', solve_step: i === 0 && goal !== 'unsolvable' ? 2 : null,
                                  steps_used: 4, model_calls: 4, ended: 'budget', tokens: { prompt: 400, completion: 80, cached: 20 } });
    }
    put(`a${i}/steps.jsonl`, jsonl(steps));
    put(`a${i}/relay.jsonl`, jsonl(relay));
    put(`a${i}/harness.out`, 'placeholder\n');
  }
  board.sort((a, b) => a.ts - b.ts);
  put('board_events.jsonl', jsonl(board));
  if (sink && content.length) put('board_events.content.chatter.json', jsonl(content));
  return { runId, files };
}

const K = (core, name) => core.events.k.filter((k) => T.EVENT_KINDS[k] === name).length;
const NK = (core, name) => core.nodes.filter((nd) => T.NODE_KINDS[nd.k] === name).length;
/** Live (non-seed) post nodes. */
const livePosts = (core) => core.nodes.filter((nd) => T.NODE_KINDS[nd.k] === 'post' && !nd.seed && nd.id.startsWith('p'));
/** Append text to a run file. */
const append = (files, k, s) => files.set(k, new Uint8Array([...files.get(k), ...enc.encode(s)]));
/** Rewrite run.json through fn(runJson). */
function editRunJson(files, fn) {
  const rj = JSON.parse(new TextDecoder().decode(files.get('run.json')));
  fn(rj);
  files.set('run.json', enc.encode(JSON.stringify(rj)));
}

function validate(r) {
  const core = JSON.parse(JSON.stringify(r.core));
  assertCore(core);
  assertDetail(JSON.parse(JSON.stringify(r.detail)), core.hash);
  assert.match(core.hash, /^[0-9a-f]{8}$/);
  assert.equal(r.card.hash, core.hash);
  assert.equal(r.card.core, `runs/${core.run.id}.${core.hash}.core.json`);
  assert.equal(r.card.density.length, 64);
  assert.equal(r.card.durationMs, core.run.tEndMs);
  assert.equal(core.agents.length, core.run.nAgents);
  assert.equal(core.series.boardShare.length, core.run.nAgents);
  for (const row of core.series.boardShare) assert.equal(row.length, core.series.density.length);
  assert.equal(core.strings[0], '');
  const sg = core.segments;
  assert.equal(sg[0].t0, 0);
  assert.ok(sg[sg.length - 1].t1 >= core.run.tEndMs);
  for (let i = 1; i < sg.length; i++) assert.equal(sg[i - 1].t1, sg[i].t0);
  for (let i = 1; i < core.events.t.length; i++) assert.ok(core.events.t[i - 1] <= core.events.t[i], 'events sorted');
  const nx = core.events.x.filter((x) => x >= 0);
  assert.deepEqual(nx, nx.map((_, i) => i), 'x dense ascending');
  assert.equal(r.detail.items.length, nx.length);
  for (const c of core.chapters) if (typeof c.ev === 'number') assert.equal(c.t, core.events.t[c.ev]);
  const seen = new Set();
  for (const e of core.edges) {
    const key = `${e.s},${e.d},${e.k}`;
    assert.ok(!seen.has(key), 'edges unique');
    seen.add(key);
  }
  assert.deepEqual(new Set(core.fires.e), new Set(core.edges.map((_, i) => i)), 'every edge fires');
  for (const p of Object.values(r.detail.posts)) for (const [a, b] of p.leakSpans) assert.ok(0 <= a && a < b && b <= p.body.length);
  return core;
}

for (const iface of ['forum', 'wiki', 'cache', 'irc']) {
  for (const n of [1, 3]) {
    test(`${iface} n=${n}: legacy inline run converts and validates`, async () => {
      const { files } = makeRun({ iface, n });
      const r = await convertRun(files);
      const core = validate(r);
      assert.equal(core.run.iface, iface);
      assert.equal(core.run.nAgents, n);
      assert.equal(core.run.pop, n === 1 ? 'solo' : 'multi');
      assert.equal(NK(core, 'agent'), n);
      assert.equal(K(core, 'post'), n);
      assert.equal(K(core, 'think'), 4 * n);
      assert.equal(K(core, 'start'), n);
      assert.equal(K(core, 'end'), n);
      assert.equal(core.agents.filter((a) => a.metrics.discovered).length, n);
      assert.equal(core.agents.every((a) => a.metrics.boardSteps === 2 && a.metrics.episodes === 2), true);
      assert.equal(core.agents[0].solveStep, 2);
      assert.ok(core.agents[0].solveT !== null);
      assert.ok(core.agents.every((a) => a.status === 'done'));
      assert.ok(r.card.stats.leak, 'flag post flagged');
      assert.equal(r.card.stats.solved, 1);
      assert.ok(core.segments.some((s) => s.title === 'Free time'));
      assert.ok(core.chapters.some((c) => c.k === 'firstContact'));
      assert.ok(core.chapters.some((c) => c.k === 'leak'));
      assert.equal(NK(core, 'post'), 3 + n + (iface === 'irc' ? n : 0), 'seed + live posts (+ dms)');
      // determinism: same input, same bytes, same hash
      const r2 = await convertRun(files);
      assert.equal(r2.core.hash, core.hash);
      assert.equal(JSON.stringify(r2.core), JSON.stringify(r.core));
    });
  }
}

test('content sink: post bodies and threads are joined by post_id / thread_seq', async () => {
  for (const iface of ['forum', 'irc']) {
    const legacy = await convertRun(makeRun({ iface, n: 2 }).files);
    const r = await convertRun(makeRun({ iface, n: 2, sink: true }).files);
    const core = validate(r);
    const live = livePosts(core);
    assert.equal(live.length, 2);
    for (const p of live) {
      assert.ok(p.bodyLen > 0);
      assert.ok(r.detail.posts[p.id].body.length > 0);
      assert.equal(core.nodes[p.thread].id, 'th:intro', 'thread_seq 1 = first seed thread');
    }
    assert.ok(r.card.stats.leak, 'leak detected from sink content');
    assert.equal(r.card.stats.posts, legacy.card.stats.posts);
    assert.equal(core.agents.map((a) => a.metrics.posts).join(), legacy.core.agents.map((a) => a.metrics.posts).join());
  }
});

test('unsolvable goal: goal key + caveat, nobody solves', async () => {
  const r = await convertRun(makeRun({ iface: 'forum', n: 2, goal: 'unsolvable' }).files);
  const core = validate(r);
  assert.equal(core.run.goal, 'unsolvable');
  assert.equal(r.card.goal, 'unsolvable');
  assert.ok(r.card.caveats.includes('unsolvable'));
  assert.equal(r.card.stats.solved, 0);
  assert.ok(!core.segments.some((s) => s.title === 'Free time'));
  const solvable = await convertRun(makeRun({ iface: 'forum', n: 2 }).files);
  assert.equal('goal' in solvable.core.run, false, 'solvable runs carry no goal key');
});

test('browser-lab swarm params: tagged preview caveat, listed (not hidden)', async () => {
  const { runId, files } = makeRun({ iface: 'wiki', n: 3, browser: true });
  files.set('manifest.json', enc.encode(JSON.stringify({ v: 1, run_id: runId, files: [], agent_stopped: [true, true, true], topology: 'board-split' })));
  for (let i = 0; i < 3; i++) files.set(`a${i}/agent-meta.json`, enc.encode('{"v":1}'));
  const r = await convertRun(files);
  const core = validate(r);
  assert.equal(core.run.id, runId);
  assert.equal(core.run.tag, 'preview');
  assert.ok(r.card.caveats.includes('tagged'));
  assert.match(core.run.caveats.find((c) => c.code === 'tagged').text, /Browser-lab preview/);
  assert.equal(r.card.hidden, false);
  assert.equal((await convertRun(files, { hidden: true })).card.hidden, true);
  assert.equal(core.run.nAgents, 3);
});

test('live partial run: in-progress agents, tEnd = last event, no end beat', async () => {
  const { files } = makeRun({ iface: 'forum', n: 2, live: true, browser: true });
  // append an unterminated (still being written) line: not malformed in live mode
  const st = files.get('a1/steps.jsonl');
  files.set('a1/steps.jsonl', new Uint8Array([...st, ...enc.encode('{"kind": "model", "ts": 1')]));
  const r = await convertRun(files, { live: true });
  const core = validate(r);
  assert.ok(core.agents.every((a) => a.status === 'running' && a.endT === null));
  assert.ok(r.card.caveats.includes('live'));
  assert.ok(!r.card.caveats.includes('malformed'));
  const lastT = Math.max(...core.events.t), lastEnd = Math.max(...core.events.t.map((t, i) => t + core.events.d[i]));
  assert.ok(core.run.tEndMs >= lastT && core.run.tEndMs <= lastEnd + 1, 'tEnd = last event');
  assert.ok(!core.chapters.some((c) => c.k === 'end'));
  assert.notEqual(core.segments[core.segments.length - 1].title, 'Wind-down');
  // without run.json at all the run id comes from the start records / browser id grammar
  files.delete('run.json');
  const r2 = await convertRun(files, { live: true });
  validate(r2);
  assert.equal(r2.core.run.iface, 'forum');
  assert.equal(r2.core.run.nAgents, 2);
  assert.ok(r2.core.events.t.every((t) => t >= 0), 'no negative times without run.json started');
});

test('incomplete (non-live) trace without run.json: incomplete caveat, unfinished agents crashed', async () => {
  const { files } = makeRun({ iface: 'cache', n: 2, live: true });
  files.delete('run.json');
  const r = await convertRun(files);
  validate(r);
  assert.ok(r.card.caveats.includes('incomplete'));
  assert.ok(r.core.agents.every((a) => a.status === 'crashed'));
  // agent metas (PROTOCOL.md §9.2) supply the clock origin and the timeout flags when the board never exported
  for (let i = 0; i < 2; i++)
    files.set(`a${i}/agent-meta.json`, enc.encode(JSON.stringify({ v: 1, agent: i, started: T0, finished: T0 + 300, timed_out: i === 1 })));
  const m = await convertRun(files);
  validate(m);
  assert.equal(m.core.agents[0].startT, 10000);
  assert.deepEqual(m.core.agents.map((a) => a.status), ['crashed', 'timed_out']);
});

test('malformed lines are skipped and counted, never thrown', async () => {
  const { files } = makeRun({ iface: 'irc', n: 2 });
  const add = (k, s) => append(files, k, s);
  add('board_events.jsonl', 'not json\n[1,2]\n{"kind": 5, "ts": "x", "agent": "y"}\n\n');
  add('a0/steps.jsonl', '{"kind": "step", "step": "x", "t0": null, "output": {"a": 1}, "submissions": "s"}\n{broken\n');
  add('a1/relay.jsonl', '{"ts": "1"');                       // unterminated, unparseable tail
  files.set('a0/summary.json', enc.encode('{oops'));
  files.set('cfg-a1.json', enc.encode('[]'));
  const r = await convertRun(files);
  validate(r);
  assert.ok(r.card.caveats.includes('malformed'));
  const cav = r.core.run.caveats.find((c) => c.code === 'malformed');
  assert.match(cav.text, /^6 log lines could not be parsed/);
  // junk everywhere, empty input
  for (const junk of [new Map(), new Map([['run.json', enc.encode('{')]]), new Map([['board_events.jsonl', enc.encode('\u0000\n')]]),
                      new Map([['a0/steps.jsonl', new Uint8Array([0xff, 0xfe, 10])]])]) {
    const x = await convertRun(junk);
    validate(x);
  }
});

test('maskCtf: no CTF value survives anywhere; leak spans cover the masks', async () => {
  const { files } = makeRun({ iface: 'forum', n: 2 });
  const plain = await convertRun(files);
  assert.ok(JSON.stringify(plain.detail).includes(CTF.flag), 'kept by default');
  assert.equal(plain.core.run.ctfMasked, false);
  const r = await convertRun(files, { maskCtf: true });
  validate(r);
  assert.equal(r.core.run.ctfMasked, true);
  const all = JSON.stringify(r.core) + JSON.stringify(r.detail);
  for (const v of Object.values(CTF)) assert.ok(!all.includes(v), 'ctf value masked');
  const leaky = Object.values(r.detail.posts).filter((p) => p.leakSpans.length);
  assert.ok(leaky.length >= 1);
  assert.ok(leaky.every((p) => p.leakSpans.every(([a, b]) => p.body.slice(a, b).startsWith('FLAG{'))));
  assert.notEqual(r.core.hash, plain.core.hash);
});

test('runFilesFromEntries strips the shared <run_id>/ (and runs/<run_id>/) prefix', async () => {
  const { runId, files } = makeRun({ iface: 'forum', n: 1 });
  const entries = [...files].map(([name, data]) => ({ name: `${runId}/${name}`, data }));
  entries.push({ name: `${runId}/`, data: new Uint8Array() }, { name: `__MACOSX/${runId}/._run.json`, data: new Uint8Array([1]) });
  const m = runFilesFromEntries(entries);
  assert.deepEqual([...m.keys()].sort(), [...files.keys()].sort());
  const m2 = runFilesFromEntries([...files].map(([name, data]) => ({ name: `runs/${runId}/${name}`, data })));
  assert.deepEqual([...m2.keys()].sort(), [...files.keys()].sort());
  // an agent-dir-only upload keeps its a0/ prefix; the run id falls back to the start record
  const m3 = runFilesFromEntries([{ name: 'a0/steps.jsonl', data: files.get('a0/steps.jsonl') }]);
  assert.deepEqual([...m3.keys()], ['a0/steps.jsonl']);
  assert.equal((await convertRun(m3)).core.run.id, runId);
  // no name anywhere: the stripped folder name is used
  const m4 = runFilesFromEntries([{ name: 'some-folder/board_events.jsonl', data: files.get('board_events.jsonl') }]);
  assert.equal((await convertRun(m4)).core.run.id, 'some-folder');
  const r = await convertRun(m);
  validate(r);
  assert.equal(r.core.run.id, runId);
});

// ------------------------------------------------------------------------------------------------ verifier additions
test('swarm liveFiles shape: ISO started, ctf in run.json, no seed / sink / cfg: posts keep body_len_chars', async () => {
  const { runId, files } = makeRun({ iface: 'forum', n: 2, sink: true, live: true, browser: true });
  // swarm.js liveRunJson(): started is board-ready's ISO string, no finished; the sink and seed are not mirrored live
  editRunJson(files, (rj) => {
    rj.started = new Date(T0 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    rj.params.live = true;
  });
  for (const k of ['seed.json', 'board_events.content.chatter.json', 'cfg-a0.json', 'cfg-a1.json']) files.delete(k);
  const r = await convertRun(files, { live: true });
  const core = validate(r);
  assert.equal(core.run.id, runId);
  assert.equal(core.agents[0].startT, 10000, 'ISO started is the clock origin');
  const live = livePosts(core);
  assert.equal(live.length, 2);
  assert.ok(live.every((p) => p.bodyLen > 0), 'body_len_chars stands in for the missing sink body');
  assert.ok(live.every((p) => core.nodes[p.thread].id === 'th:t1'), 'no seed: thread_seq 1 -> t1');
  assert.ok(r.card.caveats.includes('live') && r.card.caveats.includes('tagged'));
});

test('hostile sizes: huge n_agents / agent ids / board agent ids are clamped, never hang', async () => {
  const { files } = makeRun({ iface: 'forum', n: 1 });
  editRunJson(files, (rj) => { rj.n_agents = 1e300; });
  files.set('a999999999/steps.jsonl', enc.encode('{"kind":"start","ts":1}\n'));
  files.set('cfg-a123456789.json', enc.encode('{}'));
  append(files, 'board_events.jsonl', `${JSON.stringify({ ts: T0 + 5, agent: 1e300, kind: 'req', method: 'GET', path: '/', status: 200 })}\n`
    + `${JSON.stringify({ ts: T0 + 6, agent: -3, kind: 'req', method: 'GET', path: '/', status: 200 })}\n`);
  const r = await convertRun(files);
  const core = validate(r);
  assert.ok(core.run.nAgents <= 64);
  assert.ok(core.agents.every((a) => a.id >= 0 && a.id < 64));
});

test('maskCtf with degenerate (uploaded) ctf values keeps the bundle shape', async () => {
  const { files } = makeRun({ iface: 'irc', n: 2 });
  editRunJson(files, (rj) => { rj.ctf = { flag: 't', passphrase: 'k', key_hex: 'ab', vault_id: 'post' }; });
  for (let i = 0; i < 2; i++) files.delete(`cfg-a${i}.json`);
  validate(await convertRun(files, { maskCtf: true }));
});

test('robustness: seeded fuzz of type-confused records never throws and always validates', async () => {
  let s = 7;
  const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const V = [null, 0, -1, 1.5, 1e300, 'x', '', '12', true, [], {}, [1, 'a'], { a: 1 }, T0, '2026-01-01T00:00:00Z', 3];
  const KINDS = ['step', 'model', 'start', 'end', 'crash', 'api_retry', 'post', 'req', 'dm', 'irc_join', 'irc_cmd', 'upvote',
                 'delete', 'board_start', 'connect', 'disconnect', 5, null];
  const KEYS = ['ts', 't0', 't1', 'step', 'call', 'agent', 'phase', 'phase_before', 'command', 'output', 'exit', 'submissions',
                'usage', 'tool_calls', 'post_id', 'thread', 'thread_seq', 'author', 'body', 'iface', 'channel', 'path', 'method',
                'status', 'nick', 'to', 'chan', 'line', 'cmd', 'ended', 'ev', 'conn', 'dir', 'n', 'body_len_chars'];
  const rec = () => { const o = { kind: pick(KINDS) }; for (let i = 0; i < 6; i++) o[pick(KEYS)] = pick(V); if (rnd() < 0.7) o.ts = T0 + rnd() * 500; return o; };
  const jsonl = (n) => Array.from({ length: n }, () => (rnd() < 0.05 ? '{bad' : JSON.stringify(rec()))).join('\n');
  for (let it = 0; it < 150; it++) {
    const f = new Map();
    const n = 1 + Math.floor(rnd() * 4);
    if (rnd() < 0.8) f.set('run.json', enc.encode(JSON.stringify({ run_id: 'forum-multi2-r1', iface: pick(['forum', 'wiki', 'cache', 'irc', 7]),
      n_agents: pick([n, ...V]), started: pick([T0, ...V]), finished: pick([T0 + 500, null]), ctf: pick([CTF, ...V]),
      timed_out: pick([[0], 'x', ['vm-a1']]), params: pick([{}, { env: 'cheerpx' }, { goal: 'unsolvable' }, 'x']), harness: pick([{ budget: 5 }, 1]) })));
    if (rnd() < 0.5) f.set('seed.json', enc.encode(JSON.stringify(rnd() < 0.7 ? SEED : pick(V))));
    f.set('board_events.jsonl', enc.encode(jsonl(Math.floor(rnd() * 30))));
    if (rnd() < 0.5) f.set('board_events.content.chatter.json', enc.encode(jsonl(4)));
    for (let i = 0; i < n; i++) {
      f.set(`a${i}/steps.jsonl`, enc.encode(jsonl(Math.floor(rnd() * 30))));
      if (rnd() < 0.5) f.set(`a${i}/relay.jsonl`, enc.encode(jsonl(4)));
      if (rnd() < 0.5) f.set(`a${i}/summary.json`, enc.encode(JSON.stringify({ solved: pick(V), steps_used: pick(V), ended: pick(V) })));
    }
    for (const opts of [{}, { live: true }, { maskCtf: true }]) {
      const r = await convertRun(f, opts);
      const core = JSON.parse(JSON.stringify(r.core));
      assertCore(core);
      assertDetail(JSON.parse(JSON.stringify(r.detail)), core.hash);
    }
  }
});

test('scales linearly: an IRC flood of 20k posts in one channel converts quickly', async () => {
  const b = [{ ts: T0, kind: 'board_start' }];
  for (let i = 0; i < 20000; i++) {
    const a = i % 2;
    b.push({ ts: T0 + i * 0.01, agent: a, kind: 'irc_cmd', cmd: 'PRIVMSG', line: 'placeholder' },
           { ts: T0 + i * 0.01 + 0.001, agent: a, kind: 'post', iface: 'irc', post_id: i + 1, thread: 'intro', author: `nick${a}`, body: 'placeholder' });
  }
  const f = new Map([['run.json', enc.encode(JSON.stringify({ run_id: 'irc-multi2-r1', iface: 'irc', n_agents: 2, started: T0, finished: T0 + 300 }))],
                     ['board_events.jsonl', enc.encode(b.map((x) => JSON.stringify(x)).join('\n') + '\n')]]);
  const t = performance.now();
  const r = await convertRun(f);
  assert.ok(performance.now() - t < 10000, 'no quadratic blow-up');
  const core = validate(r);
  assert.ok(core.edges.some((e) => T.EDGE_KINDS[e.k] === 'reply'), 'replies still found');
});
