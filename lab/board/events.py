"""Append-only JSONL logs: a structural event log (Claude-safe) plus a quarantined content sink.

The split keeps board_events.jsonl free of adversarial board text (PROTOCOL.md section 6, 7.1):

- ``log_event`` -> board_events.jsonl: structural metadata only
  ({"ts", "agent", "kind", **structural fields}, in that key order, one json.dumps() per line,
  flushed per record so a killed VM loses nothing). It must never be handed a post body, an author,
  or a free-form thread slug/title.
- ``content_post`` -> a sibling ``*.content.chatter.json`` sink holding the post bodies and authors,
  keyed by ``post_id``. The sink is read only by eval models or stdlib tools (never by a coding agent);
  it is git-ignored and swept by the leak gate.

``EventLog.log`` is retained as an alias of ``log_event`` for the HTTP req / board_start / delete
events; callers that record a post route content through ``content_post``. The req/delete path field is
a static route template (``BaseHandler.logged_path`` drops the query string and collapses agent-chosen
segments -- the wiki ``?append=`` body, a kv key, a cache artifact path, a forum slug), so no
agent-authored URL text reaches this log either.
"""
import json
import os
import threading
import time


def content_sink_path(log_path):
    """Sibling content sink for a structural log path: '<stem>.content.chatter.json' in the same dir.

    The '.chatter.json' suffix puts the sink under the repo-wide chatter git-ignore and leak-gate globs.
    """
    d = os.path.dirname(log_path)
    base = os.path.basename(log_path)
    stem = base[:-6] if base.endswith(".jsonl") else base
    return os.path.join(d, f"{stem}.content.chatter.json")


class EventLog:
    """Thread-safe writer: HTTP handler threads and the IRC loop share one log and one content sink."""

    def __init__(self, path):
        self._fh = open(path, "a")
        self._lock = threading.Lock()
        self._content_path = content_sink_path(path)
        self._content_fh = None

    def log_event(self, agent, kind, **fields):
        """Record one STRUCTURAL event. agent is the port index (None for board-level events).

        Callers must pass only content-free fields (channel, post_id, thread_seq, body_len_chars,
        body_len_bytes, and routing metadata such as method/path/status for requests). Never pass a
        post body, author, or free-form slug/title here -- those go to ``content_post``.
        """
        rec = {"ts": time.time(), "agent": agent, "kind": kind, **fields}
        line = json.dumps(rec) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()

    # Backwards-compatible name for req / board_start / delete events that carry no post content.
    log = log_event

    def content_post(self, post_id, author, body, **meta):
        """Persist a post's quarantined content keyed by post_id. Returns the post_id for convenience.

        Written to the sibling ``*.content.chatter.json`` sink, opened lazily so runs that never post
        leave no sink file behind.
        """
        rec = {"post_id": post_id, "author": author, "body": body, **meta}
        line = json.dumps(rec) + "\n"
        with self._lock:
            if self._content_fh is None:
                self._content_fh = open(self._content_path, "a")
            self._content_fh.write(line)
            self._content_fh.flush()
        return post_id

    def close(self):
        with self._lock:
            self._fh.close()
            if self._content_fh is not None:
                self._content_fh.close()
                self._content_fh = None
