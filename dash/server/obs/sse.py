"""SSE broadcaster: monotonically increasing ids, 5000-item ring buffer, Last-Event-ID replay, heartbeat
(CONTRACT section 5.12). publish() is thread-safe (called from the ingest worker thread).

Ids start at the process boot time in milliseconds, so ids of a restarted server are larger than any id the
previous process issued: a client reconnecting with a pre-restart Last-Event-ID gets a `reset` (its id is older
than the ring) instead of silently missing events, and a client id from the future (clock skew) also resets."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque

RING = 5000
HEARTBEAT_S = 15.0
QUEUE_MAX = 10000


def _dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False, default=str)


class Sub:
    """One SSE subscriber. `overflow` is set when its queue filled up: the stream then sends `reset`."""
    __slots__ = ("loop", "q", "overflow")

    def __init__(self, loop, maxsize: int):
        self.loop = loop
        self.q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.overflow = False

    def put(self, item):  # runs on the subscriber's loop
        if self.overflow:
            return
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow = True
            while not self.q.empty():  # wake the stream: it drains, sees overflow and sends reset
                self.q.get_nowait()
            self.q.put_nowait(None)


class Broadcaster:
    def __init__(self, ring: int = RING, start_id: int | None = None, queue_max: int = QUEUE_MAX):
        self.ring: deque = deque(maxlen=ring)
        self.next_id = int(time.time() * 1000) if start_id is None else start_id
        self.first_id = self.next_id
        self.lock = threading.Lock()
        self.subs: set = set()  # Sub
        self.queue_max = queue_max

    def publish(self, event: str, data: dict):
        with self.lock:
            item = {"id": self.next_id, "event": event, "data": data}
            self.next_id += 1
            self.ring.append(item)
            subs = list(self.subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.put, item)
            except RuntimeError:  # loop closed
                with self.lock:
                    self.subs.discard(sub)

    def recent(self, limit: int = 200, runs: set | None = None, kinds: set | None = None) -> list[dict]:
        with self.lock:
            items = list(self.ring)
        items = [i for i in items if _match(i, runs, kinds)]
        return items[-limit:] if limit else items

    def replay_after(self, last_id: int) -> list[dict] | None:
        """Items with id > last_id, or None (= client must reset) if last_id is not one this process can
        continue from: older than the ring, from a previous process, or newer than anything issued."""
        with self.lock:
            items = list(self.ring)
            nxt = self.next_id
        if last_id >= nxt:
            return None
        first = items[0]["id"] if items else nxt
        if last_id < first - 1:
            return None
        return [i for i in items if i["id"] > last_id]

    def subscribe(self):
        sub = Sub(asyncio.get_running_loop(), self.queue_max)
        with self.lock:
            self.subs.add(sub)
        return sub

    def unsubscribe(self, entry):
        with self.lock:
            self.subs.discard(entry)


def _match(item: dict, runs: set | None, kinds: set | None) -> bool:
    d = item["data"] or {}
    if runs and d.get("run_id") not in runs:
        return False
    if kinds and item["event"] == "event" and d.get("kind") not in kinds:
        return False
    return True


def format_sse(item: dict) -> str:
    out = ""
    if item.get("id") is not None:
        out += f"id: {item['id']}\n"
    out += f"event: {item['event']}\ndata: {_dumps(item['data'])}\n\n"
    return out


async def stream(bc: Broadcaster, last_event_id: str | None, runs: set | None, kinds: set | None,
                 is_disconnected=None):
    entry = bc.subscribe()
    q = entry.q
    try:
        yield "retry: 3000\n\n"
        if last_event_id:
            try:
                lid = int(last_event_id)
            except ValueError:
                lid = -1
            items = bc.replay_after(lid) if lid >= 0 else None
            if items is None:
                yield format_sse({"id": None, "event": "reset", "data": {"reason": "last_event_id_too_old"}})
            else:
                for it in items:
                    if _match(it, runs, kinds):
                        yield format_sse(it)
        while True:
            try:
                it = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
            except asyncio.TimeoutError:
                if is_disconnected and await is_disconnected():
                    break
                yield ": hb\n\n"
                continue
            if entry.overflow:  # we fell behind and lost events: tell the client to refetch, then resume live
                while not q.empty():
                    q.get_nowait()
                entry.overflow = False
                yield format_sse({"id": None, "event": "reset", "data": {"reason": "subscriber_overflow"}})
                continue
            if it is None:
                continue
            if _match(it, runs, kinds):
                yield format_sse(it)
    finally:
        bc.unsubscribe(entry)
