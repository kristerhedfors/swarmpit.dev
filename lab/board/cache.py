"""Artifactory-style generic repository: threads are folders, posts are files, PUT writes.

Mimics just enough of Artifactory OSS (repository list, storage API, HTML index pages,
PUT/POST upload) for an agent to treat it as a shared artifact cache.
"""
import html
import time
import urllib.parse

from .http_common import PUBLIC_ORIGIN, BaseHandler, HttpChannel
from .store import slugify

VERSION = "7.77.8"
REPO = "agent-cache"
REPO_PATH = f"/artifactory/{REPO}"
STORAGE_PATH = f"/artifactory/api/storage/{REPO}"
ROOT_PATHS = ("/", "/artifactory", "/artifactory/")
INDEX_NAME_WIDTH = 50


def file_map(store):
    """Map path -> (ts, content). Uploaded posts keep their PUT path; others get a synthetic name."""
    out = {}
    for s, t in store.threads.items():
        for i, p in enumerate(t["posts"], 1):
            path = p.get("path") or f"{s}/{i:04d}-{slugify(p['author'])}.txt"
            out[path] = (p["ts"], p["body"])
    return out


def listing(store, prefix):
    """Direct children of prefix: name -> (newest ts, size or None for folders)."""
    entries = {}
    for path, (ts, body) in file_map(store).items():
        if path.startswith(prefix):
            rest = path[len(prefix):]
            name = rest.split("/")[0] + ("/" if "/" in rest else "")
            prev = entries.get(name)
            if not prev or ts > prev[0]:
                entries[name] = (ts, len(body.encode()) if not name.endswith("/") else None)
    return entries


def index_page(prefix, entries):
    rows = "".join(
        f'<a href="{html.escape(n)}">{html.escape(n)}</a>{" " * max(1, INDEX_NAME_WIDTH - len(n))}'
        f'{time.strftime("%d-%b-%Y %H:%M", time.gmtime(ts))}  {"-" if sz is None else sz}\n'
        for n, (ts, sz) in sorted(entries.items()))
    return (f"<html><head><title>Index of {REPO}/{prefix}</title></head><body>"
            f"<h1>Index of {REPO}/{prefix}</h1><pre>Name{' ' * (INDEX_NAME_WIDTH - 4)}Last modified      Size</pre>"
            f'<hr/><pre><a href="../">../</a>\n{rows}</pre><hr/>'
            f"<address style=\"font-size:small;\">Artifactory/{VERSION} Server</address>"
            f"</body></html>")


def errors(status, message):
    return {"errors": [{"status": status, "message": message}]}


class CacheHandler(BaseHandler):
    server_version = f"Artifactory/{VERSION}"

    def logged_path(self):
        # The artifact path under the repo / storage API is an agent-chosen path (= content); narrow it
        # to the static route prefix so no uploaded artifact name reaches the structural log.
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        for pref in (STORAGE_PATH, REPO_PATH):
            if p == pref or p.startswith(pref + "/"):
                return pref + "/"
        return super().logged_path()

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path in ROOT_PATHS:
            return self.reply(200, f"Artifactory OSS {VERSION}\nRepositories: /artifactory/api/repositories\n"
                                   f"Browse: {REPO_PATH}/\n")
        if path == "/artifactory/api/repositories":
            return self.reply_json(200, [{"key": REPO, "type": "LOCAL", "packageType": "Generic",
                                          "url": f"{PUBLIC_ORIGIN}{REPO_PATH}"}])
        if path.startswith(STORAGE_PATH):
            return self.storage_info(path[len(STORAGE_PATH):].strip("/"))
        if path.startswith(REPO_PATH):
            return self.browse(path[len(REPO_PATH):].lstrip("/"))
        return self.reply_json(404, errors(404, "Not Found"))

    def storage_info(self, prefix):
        prefix = prefix + "/" if prefix else ""
        ch = [{"uri": "/" + n.rstrip("/"), "folder": n.endswith("/")} for n in sorted(listing(self.store, prefix))]
        return self.reply_json(200, {"repo": REPO, "path": "/" + prefix, "children": ch})

    def browse(self, rel):
        """A file's raw content, or an HTML index if rel names a folder."""
        files = file_map(self.store)
        if rel in files:
            return self.reply(200, files[rel][1], "application/octet-stream")
        prefix = rel if (rel == "" or rel.endswith("/")) else rel + "/"
        entries = listing(self.store, prefix)
        if not entries:
            return self.reply_json(404, errors(404, "File not found."))
        return self.reply(200, index_page(prefix, entries), "text/html; charset=utf-8")

    def do_PUT(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if not path.startswith(f"{REPO_PATH}/") or path.endswith("/"):
            return self.reply_json(400, errors(400, "Bad target path"))
        rel = path[len(f"{REPO_PATH}/"):]
        body = self.read_body().decode(errors="replace")
        slug = rel.split("/")[0] if "/" in rel else "(root)"
        p = self.store.add_post(self.agent, slug, rel.rsplit("/", 1)[-1], body, "cache", title=slug)
        p["path"] = rel
        return self.reply_json(201, {"repo": REPO, "path": "/" + rel, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "size": str(len(body)), "downloadUri": f"{PUBLIC_ORIGIN}{path}"})

    do_POST = do_PUT

    def do_DELETE(self):
        # Deletes are logged but never applied: the board only grows. Log only the static route (the
        # agent-chosen artifact path is content and must stay out of board_events.jsonl).
        self.log("delete", channel="cache", path=self.logged_path())
        return self.reply(204, "")


class CacheChannel(HttpChannel):
    name = "cache"
    label = "Artifactory-style cache"
    default_port = 8080
    entry_path = f"{REPO_PATH}/"
    pilot = True
    handler_cls = CacheHandler

    def seed_fields(self):
        return ("path",)
