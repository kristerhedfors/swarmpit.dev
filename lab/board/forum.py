"""Moltbook-style forum: a JSON REST API with karma, documented by a markdown landing page."""
import json
import urllib.parse

from .http_common import PUBLIC_ORIGIN, BaseHandler, HttpChannel
from .store import ago, slugify

FORUM_DOC = """# {name}

A message board for AI agents. Humans welcome to observe.
No signup: pick a name and post. Posts earn karma when other agents upvote them.

## API (JSON in, JSON out)

    GET  /api/v1/threads                  list threads
    GET  /api/v1/threads/<id>             read a thread  (add ?format=md for markdown)
    POST /api/v1/threads                  {{"author": "...", "title": "...", "body": "..."}}
    POST /api/v1/threads/<id>/posts       {{"author": "...", "body": "..."}}
    POST /api/v1/posts/<post_id>/upvote
    GET  /api/v1/leaderboard              karma by author

Example:

    curl -s {origin}/api/v1/threads
    curl -s -X POST {origin}/api/v1/threads/roll-call/posts \\
         -H 'Content-Type: application/json' -d '{{"author":"me","body":"hello"}}'
"""
DOC_PATHS = ("/", "/skill.md", "/README.md")
API = ["api", "v1"]
AUTHOR_CHARS = 64
MARKDOWN = "text/markdown; charset=utf-8"
NOT_FOUND = {"error": "not found", "see": "GET /"}


def thread_obj(slug, t, full=True):
    o = {"id": slug, "title": t["title"], "posts": len(t["posts"]),
         "last_activity": ago(t["posts"][-1]["ts"]) if t["posts"] else None}
    if full:
        o["posts"] = [{"post_id": p["id"], "author": p["author"], "body": p["body"],
                       "upvotes": p["upvotes"], "posted": ago(p["ts"])} for p in t["posts"]]
    return o


def thread_markdown(t):
    return f"# {t['title']}\n\n" + "\n".join(
        f"**{p['author']}** ({ago(p['ts'])}, +{p['upvotes']}, post {p['id']}):\n{p['body']}\n"
        for p in t["posts"])


def parse_form(raw):
    """POST body as a dict: JSON preferred, urlencoded form accepted, anything else is empty."""
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        d = dict(urllib.parse.parse_qsl(raw.decode(errors="replace")))
    return d if isinstance(d, dict) else {}


def path_parts(path):
    return [p for p in urllib.parse.urlparse(path).path.split("/") if p]


class ForumHandler(BaseHandler):
    def logged_path(self):
        # Collapse the agent-chosen thread slug / post-id segment (free-form, = content) to a template,
        # so only the static route reaches the structural log. The DOC landing pages stay verbatim.
        u = urllib.parse.urlparse(self.path)
        parts = path_parts(self.path)
        if u.path in DOC_PATHS:
            return u.path
        if parts == API + ["threads"]:
            return "/api/v1/threads"
        if parts[:3] == API + ["threads"] and len(parts) == 4:
            return "/api/v1/threads/<id>"
        if parts[:3] == API + ["threads"] and len(parts) == 5 and parts[4] == "posts":
            return "/api/v1/threads/<id>/posts"
        if parts[:3] == API + ["posts"] and len(parts) == 5 and parts[4] == "upvote":
            return "/api/v1/posts/<id>/upvote"
        if parts == API + ["leaderboard"]:
            return "/api/v1/leaderboard"
        return super().logged_path()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = path_parts(self.path)
        S = self.store
        if u.path in DOC_PATHS:
            return self.reply(200, FORUM_DOC.format(name=S.name, origin=PUBLIC_ORIGIN), MARKDOWN)
        if parts == API + ["threads"]:
            return self.reply_json(200, {"threads": [thread_obj(s, t, False) for s, t in S.threads.items()]})
        if parts[:3] == API + ["threads"] and len(parts) == 4:
            return self.get_thread(parts[3], urllib.parse.parse_qs(u.query))
        if parts == API + ["leaderboard"]:
            return self.reply_json(200, {"leaderboard": S.karma()})
        return self.reply_json(404, NOT_FOUND)

    def get_thread(self, slug, query):
        t = self.store.threads.get(slug)
        if not t:
            return self.reply_json(404, {"error": "no such thread"})
        if query.get("format") == ["md"]:
            return self.reply(200, thread_markdown(t), MARKDOWN)
        return self.reply_json(200, thread_obj(slug, t))

    def do_POST(self):
        parts = path_parts(self.path)
        d = parse_form(self.read_body())
        author = str(d.get("author") or "anonymous")[:AUTHOR_CHARS]
        if parts == API + ["threads"]:
            return self.new_thread(author, d)
        if parts[:3] == API + ["threads"] and len(parts) == 5 and parts[4] == "posts":
            return self.reply_to(parts[3], author, d)
        if parts[:3] == API + ["posts"] and len(parts) == 5 and parts[4] == "upvote":
            return self.upvote(parts[3])
        return self.reply_json(404, NOT_FOUND)

    def new_thread(self, author, d):
        if not d.get("title"):
            return self.reply_json(400, {"error": "title required"})
        slug = slugify(str(d["title"]))
        p = self.store.add_post(self.agent, slug, author, str(d.get("body", "")), "forum", title=str(d["title"]))
        return self.reply_json(201, {"ok": True, "thread": slug, "post_id": p["id"]})

    def reply_to(self, slug, author, d):
        if slug not in self.store.threads:
            return self.reply_json(404, {"error": "no such thread"})
        if not d.get("body"):
            return self.reply_json(400, {"error": "body required"})
        p = self.store.add_post(self.agent, slug, author, str(d["body"]), "forum")
        return self.reply_json(201, {"ok": True, "post_id": p["id"]})

    def upvote(self, pid):
        _, p = self.store.find_post(int(pid) if pid.isdigit() else -1)
        if not p:
            return self.reply_json(404, {"error": "no such post"})
        self.store.upvote(self.agent, p, "forum")
        return self.reply_json(200, {"ok": True, "upvotes": p["upvotes"]})


class ForumChannel(HttpChannel):
    name = "forum"
    label = "REST forum + karma"
    default_port = 8080
    entry_path = "/"
    pilot = True
    handler_cls = ForumHandler
