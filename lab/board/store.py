"""Interface-neutral thread/post store plus the text helpers every interface renders with.

All four interfaces are views of the same data model (threads of posts), so a seed and the
event log mean the same thing whichever interface a run uses.
"""
import re
import threading
import time


class Store:
    """Threads keyed by slug, each {"title", "posts": [post, ...]}; post ids are global.

    Each thread also carries an opaque integer ``thread_seq`` assigned in first-seen order; it is the
    only thread identifier that reaches the structural event log (the free-form slug/title is content,
    kept out of board_events.jsonl per PROTOCOL.md section 7.1).
    """

    def __init__(self, seed, events, now=None):
        self.name = seed["board_name"]
        self.events = events
        self.threads = {}
        self.next_id = 1
        self.thread_seq = {}
        self.next_thread_seq = 1
        self.lock = threading.Lock()
        start = time.time() if now is None else now
        for t in seed["threads"]:
            self.threads[t["slug"]] = {"title": t["title"], "posts": []}
            self._thread_seq(t["slug"])
            for p in t["posts"]:
                self._append(t["slug"], p["author"], p["body"], start - p["ago_min"] * 60, seeded=True,
                             upvotes=max(1, 12 - p["ago_min"] // 40))

    def _thread_seq(self, slug):
        """Opaque, stable integer id for a thread slug, assigned in first-seen order."""
        seq = self.thread_seq.get(slug)
        if seq is None:
            seq = self.thread_seq[slug] = self.next_thread_seq
            self.next_thread_seq += 1
        return seq

    def _append(self, slug, author, body, ts, seeded=False, upvotes=0):
        post = {"id": self.next_id, "author": author, "body": body, "ts": ts, "upvotes": upvotes,
                "seeded": seeded}
        self.next_id += 1
        self.threads[slug]["posts"].append(post)
        return post

    def ensure_thread(self, slug, title):
        with self.lock:
            self.threads.setdefault(slug, {"title": title, "posts": []})

    def add_post(self, agent, slug, author, body, iface, title=None, fields=None):
        """Append an agent's post (creating the thread if needed), log structural metadata, and
        quarantine the author/body in the content sink. The slug/title/author/body never reach the
        structural event log.

        ``fields`` optionally adds STRUCTURAL keys to the post event (ints / fixed enums only, e.g. the
        code-comment channels' ``file``/``line_no``): a dict, or a callable(post) -> dict evaluated after
        the post is appended and the store lock released (so it may read the store)."""
        with self.lock:
            if slug not in self.threads:
                self.threads[slug] = {"title": title or slug, "posts": []}
            seq = self._thread_seq(slug)
            post = self._append(slug, author, body, time.time())
        body = body or ""
        self.events.content_post(post["id"], author, body, channel=iface, thread_seq=seq)
        extra = fields(post) if callable(fields) else (fields or {})
        self.events.log_event(agent, "post", channel=iface, post_id=post["id"], thread_seq=seq,
                              body_len_chars=len(body), body_len_bytes=len(body.encode("utf-8")), **extra)
        return post

    def upvote(self, agent, post, iface):
        with self.lock:
            post["upvotes"] += 1
        self.events.log_event(agent, "upvote", channel=iface, post_id=post["id"],
                              thread_seq=self.thread_seq.get(self.find_post(post["id"])[0]))

    def find_post(self, pid):
        for slug, t in self.threads.items():
            for p in t["posts"]:
                if p["id"] == pid:
                    return slug, p
        return None, None

    def karma(self):
        k = {}
        for t in self.threads.values():
            for p in t["posts"]:
                k[p["author"]] = k.get(p["author"], 0) + p["upvotes"]
        return sorted(({"author": a, "karma": v} for a, v in k.items()), key=lambda r: -r["karma"])


def ago(ts):
    m = int((time.time() - ts) // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 120 else f"{m // 60} h ago"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "untitled"


def camel(slug):
    """Wiki page name for a thread slug: roll-call -> RollCall."""
    return "".join(w.capitalize() for w in slug.split("-"))
