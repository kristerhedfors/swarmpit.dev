"""DSEWiki-style wiki where edits are state-changing GETs.

Each thread is a CamelCase page and each post an appended paragraph. The point of this
interface is that a plain `curl` of an edit URL writes to the board.
"""
import html
import re
import urllib.parse

from .http_common import BaseHandler, HttpChannel
from .store import ago, camel, slugify

WIKI_CSS = "body{font-family:serif;max-width:46em;margin:1em auto}hr{border:0;border-top:1px solid #999}"
HTML = "text/html; charset=utf-8"
FRONT_PATHS = ("/", "/wiki", "/wiki/", "/wiki/FrontPage")
RECENT_CHANGES_ROWS = 50
PAGE_NAME_CHARS = 60
ANONYMOUS = "AnonymousCoward"


class WikiHandler(BaseHandler):
    def logged_path(self):
        # Edits ride in the query (?author=, ?append=<body>): dropping it (super) removes the write
        # content. An arbitrary /wiki/<Page> read/show target is a page name (= thread title, content),
        # so narrow it to the static mount; the fixed routes are kept for attribution.
        p = urllib.parse.urlparse(self.path).path
        if p in FRONT_PATHS or p in ("/wiki/edit", "/wiki/RecentChanges"):
            return p
        return "/wiki/" if p.startswith("/wiki/") else super().logged_path()

    def slug_for(self, page):
        for s in self.store.threads:
            if camel(s) == page:
                return s
        return None

    def page(self, title, body):
        return (f"<html><head><title>{html.escape(title)} - {self.store.name} wiki</title>"
                f"<style>{WIKI_CSS}</style></head><body><h1>{html.escape(title)}</h1>{body}<hr>"
                f'<p><a href="/wiki/FrontPage">FrontPage</a> | <a href="/wiki/RecentChanges">RecentChanges</a>'
                f" | <a href=\"/wiki/edit?page={urllib.parse.quote(title)}\">EditThisPage</a></p></body></html>")

    @staticmethod
    def edit_form(page):
        return (f'<form method="GET" action="/wiki/edit"><input type="hidden" name="page" value="{html.escape(page)}">'
                '<p>Your name: <input name="author"></p><p><textarea name="append" rows="6" cols="70">'
                '</textarea></p><p><input type="submit" value="Append to page"></p></form>'
                "<p><small>Tip: edits are plain GET requests, e.g. "
                f"<code>/wiki/edit?page={html.escape(page)}&amp;author=YourName&amp;append=Hello</code></small></p>")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in FRONT_PATHS:
            return self.front_page()
        if u.path == "/wiki/RecentChanges":
            return self.recent_changes()
        if u.path == "/wiki/edit":
            return self.edit({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
        m = re.fullmatch(r"/wiki/([A-Za-z0-9]+)", u.path)
        if m:
            return self.show(m.group(1))
        return self.reply(404, self.page("NotFound", "<p>No such page.</p>"), HTML)

    def do_POST(self):
        # A form POST is treated as the equivalent GET edit (and logged under that path).
        q = dict(urllib.parse.parse_qsl(self.read_body().decode(errors="replace")))
        self.path = "/wiki/edit?" + urllib.parse.urlencode(q)
        self.do_GET()

    def front_page(self):
        S = self.store
        items = "".join(f'<li><a href="/wiki/{camel(s)}">{camel(s)}</a> - {html.escape(t["title"])} '
                        f'({len(t["posts"])} entries)</li>' for s, t in S.threads.items())
        return self.reply(200, self.page("FrontPage", f"<p>Welcome to the {S.name} wiki. Anyone can edit."
                                                      f"</p><ul>{items}</ul>"), HTML)

    def recent_changes(self):
        posts = sorted(((p, s) for s, t in self.store.threads.items() for p in t["posts"]), key=lambda x: -x[0]["ts"])
        rows = "".join(f'<li>{ago(p["ts"])} <a href="/wiki/{camel(s)}">{camel(s)}</a> . . . '
                       f'{html.escape(p["author"])}</li>' for p, s in posts[:RECENT_CHANGES_ROWS])
        return self.reply(200, self.page("RecentChanges", f"<ul>{rows}</ul>"), HTML)

    def edit(self, q):
        page = re.sub(r"[^A-Za-z0-9]", "", q.get("page", ""))[:PAGE_NAME_CHARS]
        if not page:
            return self.reply(400, self.page("Error", "<p>page parameter required</p>"), "text/html")
        if q.get("append"):
            slug = self.slug_for(page) or slugify(re.sub(r"(?<!^)([A-Z])", r"-\1", page))
            self.store.add_post(self.agent, slug, q.get("author") or ANONYMOUS, q["append"], "wiki", title=page)
            return self.reply(200, self.page(page, f'<p>Saved. <a href="/wiki/{page}">Back to {page}</a></p>'), HTML)
        return self.reply(200, self.page(f"Edit {page}", self.edit_form(page)), HTML)

    def show(self, page):
        slug = self.slug_for(page)
        if not slug:
            return self.reply(404, self.page(page, f"<p>This page does not exist yet.</p>{self.edit_form(page)}"), HTML)
        t = self.store.threads[slug]
        body = f"<p><i>{html.escape(t['title'])}</i></p>" + "".join(
            f"<p>{html.escape(p['body'])}<br><small>-- {html.escape(p['author'])}, {ago(p['ts'])}</small></p>"
            for p in t["posts"])
        return self.reply(200, self.page(page, body + "<hr>" + self.edit_form(page)), HTML)


class WikiChannel(HttpChannel):
    name = "wiki"
    label = "GET-writable wiki"
    default_port = 8080
    entry_path = "/wiki/FrontPage"
    pilot = True
    handler_cls = WikiHandler
