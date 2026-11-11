"""Guestbook: the minimal "sign here" surface.

One implicit thread (``guestbook``). GET renders the existing entries and a form;
an urlencoded POST appends one post. Reads go through the Store, the write goes
through ``store.add_post(iface="guestbook")`` and the request body is treated as
opaque text -- never inspected or written to a log.
"""
import html
import urllib.parse

from .http_common import BaseHandler, HttpChannel
from .store import ago

SLUG = "guestbook"
TITLE = "Guestbook"
HTML_CT = "text/html; charset=utf-8"
AUTHOR_CHARS = 64
PATHS = ("/", "/guestbook", "/guestbook/")


class GuestbookHandler(BaseHandler):
    def do_GET(self):
        if urllib.parse.urlparse(self.path).path in PATHS:
            return self.render(200)
        return self.reply(404, "not found")

    def render(self, code):
        t = self.store.threads.get(SLUG)
        entries = "".join(
            f"<li><b>{html.escape(p['author'])}</b> ({ago(p['ts'])}): {html.escape(p['body'])}</li>"
            for p in (t["posts"] if t else []))
        form = ('<form method="POST" action="/guestbook">'
                '<p>Name: <input name="author"></p>'
                '<p>Message: <input name="message" size="60"></p>'
                '<p><input type="submit" value="Sign the guestbook"></p></form>')
        return self.reply(code, f"<html><head><title>{html.escape(self.store.name)} guestbook</title></head>"
                                f"<body><h1>{html.escape(self.store.name)} guestbook</h1>"
                                f"<ul>{entries}</ul>{form}</body></html>", HTML_CT)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path not in PATHS:
            return self.reply(404, "not found")
        d = dict(urllib.parse.parse_qsl(self.read_body().decode(errors="replace")))
        author = str(d.get("author") or "guest")[:AUTHOR_CHARS]
        message = str(d.get("message") or d.get("body") or "")
        if not message:
            return self.reply(400, "message required")
        self.store.add_post(self.agent, SLUG, author, message, iface="guestbook", title=TITLE)
        return self.render(201)


class GuestbookChannel(HttpChannel):
    name = "guestbook"
    label = "Guestbook sign-here"
    default_port = 8080
    entry_path = "/guestbook"
    handler_cls = GuestbookHandler
