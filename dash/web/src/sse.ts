// Live feed client: one shared SSE connection to /api/live, read with fetch streaming so we can
// send Last-Event-ID on manual reconnects (EventSource cannot set headers). Exponential backoff.
// Subscribers filter client-side by run/kind. In mock mode the fixture generator drives it.
import { MOCK, mock } from './api';
import type { LiveMessage } from './lib/types';
import { app } from './store';

type Sub = { runs?: Set<string>; fn: (m: LiveMessage) => void };

class LiveClient {
  private subs = new Set<Sub>();
  private lastId: number | null = null;
  private ctrl: AbortController | null = null;
  private backoff = 1000;
  private running = false;
  private stopMock: (() => void) | null = null;
  status: 'idle' | 'connecting' | 'open' | 'retrying' = 'idle';
  lastMsgAt = 0;
  private wasOpen = false;
  private watchdog = 0;
  /** server heartbeats every 15 s (obs/sse.py HEARTBEAT_S); silence for 2.5x that = a stalled connection */
  static STALL_MS = 37500;

  subscribe(fn: (m: LiveMessage) => void, runs?: string[]): () => void {
    const s: Sub = { fn, runs: runs && runs.length ? new Set(runs) : undefined };
    this.subs.add(s);
    this.ensure();
    return () => { this.subs.delete(s); };
  }

  private ensure() {
    if (this.running) return;
    this.running = true;
    if (MOCK) {
      mock().then((m) => { this.setStatus('open'); this.stopMock = m.liveStream((msg) => this.dispatch(msg)); });
      return;
    }
    // stall watchdog: a dead connection without FIN/RST (sleep/resume, proxy, wifi change) never resolves
    // reader.read(); abort it so loop() reconnects with Last-Event-ID
    this.watchdog = window.setInterval(() => {
      if (this.status === 'open' && Date.now() - this.lastMsgAt > LiveClient.STALL_MS) this.ctrl?.abort();
    }, 5000);
    void this.loop();
  }

  stop() { this.running = false; this.ctrl?.abort(); this.stopMock?.(); clearInterval(this.watchdog); this.watchdog = 0; this.setStatus('idle'); }

  private setStatus(s: LiveClient['status']) {
    this.status = s;
    app.set({ liveConnected: s === 'open' });
  }

  private dispatch(m: LiveMessage) {
    if (m.id != null) this.lastId = m.id;
    this.lastMsgAt = Date.now();
    const rid = typeof m.data?.run_id === 'string' ? (m.data.run_id as string) : null;
    for (const s of this.subs) {
      if (s.runs && rid && !s.runs.has(rid)) continue;
      try { s.fn(m); } catch (e) { console.error(e); }
    }
  }

  private async loop() {
    while (this.running) {
      this.setStatus('connecting');
      this.ctrl = new AbortController();
      try {
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (this.lastId != null) headers['Last-Event-ID'] = String(this.lastId);
        const res = await fetch('/api/live', { headers, signal: this.ctrl.signal, cache: 'no-store' });
        if (!res.ok || !res.body) throw new Error('http ' + res.status);
        this.lastMsgAt = Date.now();
        this.setStatus('open');
        this.backoff = 1000;
        // tell views a reconnect happened (the server replays from Last-Event-ID or sends `reset`)
        if (this.wasOpen) this.dispatch({ id: null, event: 'reconnected', data: {} });
        this.wasOpen = true;
        await this.read(res.body);
      } catch (e) {
        if (!this.running) return;
      }
      if (!this.running) return;
      this.setStatus('retrying');
      await new Promise((r) => setTimeout(r, this.backoff));
      this.backoff = Math.min(30000, this.backoff * 2);
    }
  }

  private async read(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let ev: { id: number | null; event: string; data: string[] } = { id: null, event: 'message', data: [] };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i: number;
      while ((i = buf.indexOf('\n')) >= 0) {
        let line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (line === '') {
          if (ev.data.length) {
            let data: Record<string, unknown> = {};
            try { data = JSON.parse(ev.data.join('\n')); } catch { data = { raw: ev.data.join('\n') }; }
            this.dispatch({ id: ev.id, event: ev.event, data });
          }
          ev = { id: null, event: 'message', data: [] };
        } else if (line.startsWith(':')) {
          this.lastMsgAt = Date.now(); // heartbeat
        } else {
          const c = line.indexOf(':');
          const field = c < 0 ? line : line.slice(0, c);
          const val = c < 0 ? '' : line.slice(c + 1).replace(/^ /, '');
          if (field === 'id') { const n = Number(val); ev.id = Number.isFinite(n) ? n : null; }
          else if (field === 'event') ev.event = val;
          else if (field === 'data') ev.data.push(val);
        }
      }
    }
  }
}

export const live = new LiveClient();
