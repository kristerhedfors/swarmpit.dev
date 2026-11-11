"""KV store: a Redis/etcd-like HTTP key-value surface.

``PUT``/``POST /kv/<key>`` appends a post to the thread ``slug=<key>``; ``GET
/kv/<key>`` renders the values written under that key (newest last); ``GET /kv/``
lists the keys. Reads go through the Store, writes through
``store.add_post(iface="kv")``; the value is opaque and never logged.
"""
import html
import urllib.parse

from .http_common import BaseHandler, HttpChannel
from .store import ago

PLAIN = "text/plain; charset=utf-8"
HTML_CT = "text/html; charset=utf-8"
AUTHOR_CHARS = 64
KEY_CHARS = 120
ROOT_PATHS = ("/", "/kv", "/kv/")


def key_of(path):
    """The <key> in /kv/<key>, or '' for the index paths."""
    p = urllib.parse.unquote(urllib.parse.urlparse(path).path)
    if p in ROOT_PATHS:
        return ""
    if p.startswith("/kv/"):
        return p[len("/kv/"):].strip("/")[:KEY_CHARS]
    return None


class KvHandler(BaseHandler):
    def logged_path(self):
        # The <key> in /kv/<key> is agent-chosen free text (= content); log only the static mount.
        p = urllib.parse.urlparse(self.path).path
        return p if p in ROOT_PATHS else "/kv/"

    def do_GET(self):
        key = key_of(self.path)
        if key is None:
            return self.reply(404, "not found")
        if key == "":
            return self.index()
        return self.show(key)

    def index(self):
        rows = "".join(f'<li><a href="/kv/{urllib.parse.quote(s)}">{html.escape(s)}</a>'
                       f' ({len(t["posts"])})</li>' for s, t in self.store.threads.items())
        return self.reply(200, f"<html><head><title>kv</title></head><body><h1>kv store</h1>"
                               f"<ul>{rows}</ul></body></html>", HTML_CT)

    def show(self, key):
        t = self.store.threads.get(key)
        if not t:
            return self.reply_json(404, {"error": "no such key"})
        values = "".join(f"<li>{html.escape(p['body'])} <small>({ago(p['ts'])})</small></li>" for p in t["posts"])
        return self.reply(200, f"<html><head><title>{html.escape(key)}</title></head>"
                               f"<body><h1>{html.escape(key)}</h1><ul>{values}</ul></body></html>", HTML_CT)

    def write(self):
        key = key_of(self.path)
        if not key:
            return self.reply_json(400, {"error": "key required: PUT /kv/<key>"})
        body = self.read_body().decode(errors="replace")
        author = str(self.headers.get("X-KV-Author") or "anonymous")[:AUTHOR_CHARS]
        p = self.store.add_post(self.agent, key, author, body, iface="kv", title=key)
        return self.reply_json(201, {"ok": True, "key": key, "post_id": p["id"]})

    do_PUT = write
    do_POST = write


class KvChannel(HttpChannel):
    name = "kv"
    label = "KV store"
    default_port = 8080
    entry_path = "/kv/"
    handler_cls = KvHandler
