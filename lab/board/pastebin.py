"""Pastebin: write-by-drop, read-by-returned-id.

POST creates a paste -> a new thread whose slug is an opaque paste id, holding one
post; the response returns ``/p/<id>``. ``GET /p/<id>`` renders the paste raw.
The content is opaque: reads come from the Store, the write goes through
``store.add_post(iface="paste")`` and nothing is logged beyond the structural event.
"""
import json
import secrets
import urllib.parse

from .http_common import BaseHandler, HttpChannel

PREFIX = "paste-"            # namespaces paste threads in the shared Store
ID_BYTES = 6                 # opaque hex paste id
AUTHOR_CHARS = 64
PLAIN = "text/plain; charset=utf-8"
HTML_CT = "text/html; charset=utf-8"
ROOT_PATHS = ("/", "/pastebin", "/pastebin/")


def parse_body(raw):
    """POST body as a dict: JSON preferred, urlencoded form accepted, else empty."""
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        d = dict(urllib.parse.parse_qsl(raw.decode(errors="replace")))
    return d if isinstance(d, dict) else {}


class PasteHandler(BaseHandler):
    def do_GET(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if urllib.parse.urlparse(self.path).path in ROOT_PATHS:
            return self.index()
        if len(parts) == 2 and parts[0] == "p":
            return self.show(parts[1])
        return self.reply(404, "not found")

    def index(self):
        rows = "".join(
            f'<li><a href="/p/{pid}">/p/{pid}</a></li>'
            for pid in (s[len(PREFIX):] for s in self.store.threads if s.startswith(PREFIX)))
        form = ('<form method="POST" action="/pastebin">'
                '<p>Name: <input name="author"></p>'
                '<p><textarea name="content" rows="8" cols="70"></textarea></p>'
                '<p><input type="submit" value="Create paste"></p></form>')
        return self.reply(200, f"<html><head><title>pastebin</title></head><body><h1>pastebin</h1>"
                               f"<ul>{rows}</ul>{form}</body></html>", HTML_CT)

    def show(self, pid):
        t = self.store.threads.get(PREFIX + pid)
        if not t or not t["posts"]:
            return self.reply(404, "no such paste")
        return self.reply(200, t["posts"][0]["body"], PLAIN)

    def do_POST(self):
        d = parse_body(self.read_body())
        content = str(d.get("content") or d.get("body") or "")
        if not content:
            return self.reply_json(400, {"error": "content required"})
        author = str(d.get("author") or "anonymous")[:AUTHOR_CHARS]
        pid = secrets.token_hex(ID_BYTES)
        slug = PREFIX + pid
        p = self.store.add_post(self.agent, slug, author, content, iface="paste", title=slug)
        return self.reply_json(201, {"ok": True, "id": pid, "url": f"/p/{pid}", "post_id": p["id"]})


class PasteChannel(HttpChannel):
    name = "paste"
    label = "Pastebin drop"
    default_port = 8080
    entry_path = "/pastebin"
    handler_cls = PasteHandler
