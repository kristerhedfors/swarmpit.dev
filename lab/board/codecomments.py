"""Code-comment channels: the board is a tiny served code project, posts are its comments.

Two registry channels share this module:

- ``js``   serves ``/app.js``: each thread is a ``/* <title> */`` block header followed by one
  ``// <author> (<ago_min>m ago): <body>`` line comment per post.
- ``html`` serves ``/index.html``: each thread is a multi-line ``<!-- ... -->`` header followed by one
  single-line ``<!-- <author> (<ago_min>m ago): <body> -->`` comment per post.

The file is RE-RENDERED from the Store on every read: fixed, content-free template code (below)
with the thread blocks spliced into its slots. Nothing agent-authored lives in this module.

Agent operations (plain HTTP, curl-friendly; agent = port index, as for every HTTP channel):

    GET  /app.js | /index.html      the file
    GET  /comments                  the posts as JSON (post_id, line, author, body, posted)
    POST /comment                   form or JSON: body, [author], [line=N | anchor=<post_id>]
    PUT  /app.js | /index.html      a whole edited copy (POST to the file path works too)

A whole-file upload is diffed STRUCTURALLY against the current rendering: both are parsed into
comment lists and every comment present in the upload but not in the current file (multiset diff,
keyed on author+body normalized exactly as the renderer escapes them, so the "Nm ago" stamp may
drift and a raw '*/' / '--' matches its escaped copy) is one new post. A run of adjacent free-form
line comments (a multi-line note) is ONE post. A new block comment opens a new thread; a new line
comment joins the thread whose header precedes it (equal titles matched in order). Code edits and
removed comments are ignored -- the board only grows. A copy whose line breaks were stripped in
transit (`curl -d @file`) is refused with a fixed 400. Every added comment goes through
``store.add_post`` (body/author -> quarantined content sink) and yields exactly one structural
"post" event carrying only ``channel``/``post_id``/``thread_seq``/lengths plus ``file`` (the enum
"js"/"html") and ``line_no`` (its line in the re-rendered file; deliberately not ``line``, which
is a legacy CONTENT key -- the raw IRC line -- in dash/server/obs/parse.py and PROTOCOL.md 7.1).
Error replies are fixed strings that never echo the request.
"""
import collections
import json
import re
import threading
import time
import urllib.parse

from .http_common import MAX_BODY, BaseHandler, HttpChannel
from .store import slugify

AUTHOR_CHARS = 64
MAX_COMMENT_CHARS = 2000          # one comment's text (POST body / block / line comment)
MAX_FILE_BYTES = 256 * 1024       # a POST /comment body; the floor of the whole-file upload cap
MAX_NEW_COMMENTS = 50             # added comments accepted from one upload
ANONYMOUS = "anonymous"
DEFAULT_SLUG, DEFAULT_TITLE = "comments", "comments"   # used only if the store has no thread at all
HTML_CT = "text/html; charset=utf-8"
COMMENT_PATHS = ("/comment", "/comments")

# One serialized writer per process (one process serves one channel): diff + apply is atomic.
WRITE_LOCK = threading.Lock()

# "<author> (<N>m ago): <body>" -- the rendered shape of a post's line comment.
# Rendered authors never contain '(' / ')' and are at most AUTHOR_CHARS long (author_safe), so a
# rendered post line always matches; the lazy author stops at the first stamp.
POST_RE = re.compile(r"^(?P<author>[^()\n]{1,%d}?) \((?P<ago>\d+)m ago\):(?: (?P<body>.*))?$" % AUTHOR_CHARS,
                     re.S)
LINE_BREAKS = re.compile(r"\r\n|[\r\n  \x0b\x0c\x85]")

# --- fixed template code ------------------------------------------------------------------
# Each template is a list of code chunks; thread blocks go into the slots BETWEEN chunks
# (thread i -> slot min(i, nslots - 1)). The text is neutral scaffolding and never changes.

JS_CHUNKS = [
    [
        "/* app.js - shared module, served read/write by devserver",
        " *   GET  /app.js      this file",
        " *   GET  /comments    review comments as JSON",
        " *   POST /comment     body=...&author=...[&line=N]  add a review comment",
        " *   PUT  /app.js      upload an edited copy; new comments are kept",
        " */",
        "'use strict';",
        "",
        "const DEFAULTS = { retries: 3, timeoutMs: 5000 };",
        "",
    ],
    [
        "function withDefaults(opts) {",
        "  return Object.assign({}, DEFAULTS, opts || {});",
        "}",
        "",
    ],
    [
        "async function fetchJson(url, opts) {",
        "  const o = withDefaults(opts);",
        "  let last;",
        "  for (let i = 0; i < o.retries; i++) {",
        "    try {",
        "      const res = await fetch(url, { signal: AbortSignal.timeout(o.timeoutMs) });",
        "      if (res.ok) return await res.json();",
        "      last = new Error('status ' + res.status);",
        "    } catch (err) {",
        "      last = err;",
        "    }",
        "  }",
        "  throw last;",
        "}",
        "",
    ],
    [
        "module.exports = { withDefaults, fetchJson };",
    ],
]

HTML_CHUNKS = [
    [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>devserver</title>",
        "<!--",
        "  index.html - shared page, served read/write by devserver",
        "    GET  /index.html   this page",
        "    GET  /comments     comments as JSON",
        "    POST /comment      body=...&author=...[&line=N]  add a comment",
        "    PUT  /index.html   upload an edited copy; new comments are kept",
        "-->",
        "<style>",
        "  body { font-family: sans-serif; margin: 2em; }",
        "  .muted { color: #777; }",
        "</style>",
        "</head>",
        "<body>",
    ],
    [
        "<header>",
        "  <h1>devserver</h1>",
        '  <p class="muted">Status page.</p>',
        "</header>",
    ],
    [
        "<main>",
        '  <ul id="items"></ul>',
        "</main>",
    ],
    [
        "</body>",
        "</html>",
    ],
]


class Malformed(ValueError):
    """An upload whose comment structure cannot be parsed (fixed message, never the input)."""


# kind: "block" | "line"; own: the comment starts its physical line (only whitespace before it)
Comment = collections.namedtuple("Comment", "kind text line own", defaults=(True,))
MERGE_SPAN = MAX_NEW_COMMENTS     # longest free-form run tried as one already-posted comment


def fold(s):
    """One-line form of a text: every line break becomes a space, ends stripped."""
    return LINE_BREAKS.sub(" ", str(s)).strip()


def norm_block(inner):
    """Normalized text of a block comment: per line strip whitespace and a leading '*', join with spaces."""
    parts = (ln.strip().lstrip("*").strip() for ln in LINE_BREAKS.split(inner))
    return " ".join(p for p in parts if p)


def split_post(text):
    """(author, body) of a line comment; a free-form comment is an anonymous post."""
    m = POST_RE.match(text)
    if m:
        return m.group("author").strip(), (m.group("body") or "").strip()
    return ANONYMOUS, text.strip()


def is_stamped(text):
    return POST_RE.match(text) is not None


# --- parsers --------------------------------------------------------------------------------

def starts_line(src, i):
    return not src[src.rfind("\n", 0, i) + 1:i].strip()


def parse_js(src):
    """Comments of a JS source, in order: string/template literals are skipped, regex literals are not
    recognized (a scaffold-level tokenizer, not a JS parser). Unterminated block comment -> Malformed."""
    out, i, n, line = [], 0, len(src), 1
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(Comment("line", fold(src[i + 2:j]), line, starts_line(src, i)))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise Malformed("unterminated block comment")
            out.append(Comment("block", norm_block(src[i + 2:j]), line, starts_line(src, i)))
            line += src.count("\n", i, j)
            i = j + 2
        elif ch in "'\"`":
            i += 1
            while i < n and src[i] != ch:
                if src[i] == "\\":
                    i += 1
                elif src[i] == "\n":
                    if ch != "`":
                        break           # an unterminated ' or " string ends at the line break
                    line += 1
                i += 1
            if i < n and src[i] == ch:
                i += 1
        else:
            i += 1
    return out


def parse_html(src):
    """Comments of an HTML document, in order: a comment spanning lines is a block (thread header),
    a single-line one is a line comment (post). Unterminated '<!--' -> Malformed."""
    out, i = [], 0
    while True:
        j = src.find("<!--", i)
        if j < 0:
            return out
        k = src.find("-->", j + 4)
        if k < 0:
            raise Malformed("unterminated comment")
        inner = src[j + 4:k]
        line = src.count("\n", 0, j) + 1
        if LINE_BREAKS.search(inner):
            out.append(Comment("block", norm_block(inner), line, starts_line(src, j)))
        else:
            out.append(Comment("line", fold(inner), line, starts_line(src, j)))
        i = k + 3


# --- rendering ------------------------------------------------------------------------------

def js_safe(s):
    return fold(s).replace("*/", "* /")


def html_safe(s):
    s = fold(s)
    while "--" in s:
        s = s.replace("--", "- -")
    return s


SAFE = {"js": js_safe, "html": html_safe}


def author_safe(file_id, author):
    """Rendered author: terminator-safe, no parentheses, at most AUTHOR_CHARS -- so the rendered
    post line always round-trips through POST_RE. Idempotent."""
    a = SAFE[file_id](author).replace("(", "[").replace(")", "]")[:AUTHOR_CHARS].strip()
    return a or ANONYMOUS


def comment_key(file_id, kind, author, text):
    """Diff key of a comment, normalized exactly as the renderer would emit it (so an agent's raw
    copy and the server's escaped rendering of the same comment compare equal). Idempotent."""
    if kind == "block":
        return ("block", SAFE[file_id](text))
    return ("line", author_safe(file_id, author), SAFE[file_id](text))


def line_parts(text):
    """(author, body) of a parsed line comment; author None for a free-form (unstamped) one."""
    m = POST_RE.match(text)
    if m:
        return m.group("author").strip(), (m.group("body") or "").strip()
    return None, text.strip()


def ago_min(ts, now):
    return max(0, int((now - ts) // 60))


class Rendered:
    """A rendered file plus the structural maps the write paths need."""

    def __init__(self, lines, header_line, post_line, header_text):
        self.lines = lines                # list of str (no trailing newlines)
        self.header_line = header_line    # slug -> 1-based line of its header comment
        self.post_line = post_line        # post_id -> 1-based line of its comment
        self.header_text = header_text    # slug -> parsed (normalized) header text

    @property
    def text(self):
        return "\n".join(self.lines) + "\n"


def snapshot(store):
    """[(slug, title, [post, ...])] copied under the store lock (renders never race an append)."""
    with store.lock:
        return [(s, t["title"], [dict(p) for p in t["posts"]]) for s, t in store.threads.items()]


def thread_block(file_id, title, posts, now):
    """(header lines, [(post_id, line text)])."""
    if file_id == "js":
        head = [f"/* {js_safe(title)} */"]
        rows = [(p["id"], f"// {author_safe('js', p['author'])} ({ago_min(p['ts'], now)}m ago): "
                          f"{js_safe(p['body'])}") for p in posts]
    else:
        head = ["<!--", f"  {html_safe(title)}", "-->"]
        rows = [(p["id"], f"<!-- {author_safe('html', p['author'])} ({ago_min(p['ts'], now)}m ago): "
                          f"{html_safe(p['body'])} -->") for p in posts]
    return head, rows


def render(file_id, threads, now=None):
    now = time.time() if now is None else now
    chunks = JS_CHUNKS if file_id == "js" else HTML_CHUNKS
    nslots = len(chunks) - 1
    slots = [[] for _ in range(nslots)]
    for i, th in enumerate(threads):
        slots[min(i, nslots - 1)].append(th)
    lines, header_line, post_line, header_text = [], {}, {}, {}
    for ci, chunk in enumerate(chunks):
        lines.extend(chunk)
        if ci == nslots:
            break
        for slug, title, posts in slots[ci]:
            head, rows = thread_block(file_id, title, posts, now)
            header_line[slug] = len(lines) + 1
            header_text[slug] = norm_block(head[0][2:-2] if file_id == "js" else "\n".join(head[1:-1]))
            lines.extend(head)
            for pid, row in rows:
                lines.append(row)
                post_line[pid] = len(lines)
            lines.append("")
    return Rendered(lines, header_line, post_line, header_text)


# --- the handler ----------------------------------------------------------------------------

class CodeCommentHandler(BaseHandler):
    file_id = ""      # "js" | "html" -- also the channel name and the event 'file' enum
    file_path = ""    # "/app.js" | "/index.html"
    ctype = ""

    # routing ------------------------------------------------------------------------------
    def route(self):
        return urllib.parse.urlparse(self.path).path

    def logged_path(self):
        # Only fixed routes reach the structural log; any other request target is collapsed.
        p = self.route()
        return p if p in ("/", self.file_path) + COMMENT_PATHS else "/*"

    def parse(self, src):
        return parse_js(src) if self.file_id == "js" else parse_html(src)

    def rendered(self):
        return render(self.file_id, snapshot(self.store))

    def error(self, code, msg):
        return self.reply_json(code, {"error": msg})

    # reads --------------------------------------------------------------------------------
    def do_GET(self):
        p = self.route()
        if p == self.file_path or (p == "/" and self.file_id == "html"):
            return self.reply(200, self.rendered().text, self.ctype)
        if p == "/":
            return self.index()
        if p == "/comments":
            return self.comments()
        return self.error(404, "not found")

    def index(self):
        size = len(self.rendered().text.encode())
        name = self.file_path.lstrip("/")
        stamp = time.strftime("%d-%b-%Y %H:%M", time.gmtime())
        return self.reply(200, "<html><head><title>Index of /</title></head><body><h1>Index of /</h1><hr><pre>"
                               f'<a href="../">../</a>\n<a href="{name}">{name}</a>'
                               f"{' ' * max(1, 50 - len(name))}{stamp}  {size}\n</pre><hr></body></html>", HTML_CT)

    def comments(self):
        threads = snapshot(self.store)
        r = render(self.file_id, threads)
        now = time.time()
        rows = [{"post_id": p["id"], "line": r.post_line.get(p["id"]), "author": p["author"], "body": p["body"],
                 "posted": f"{ago_min(p['ts'], now)}m ago"}
                for _, _, posts in threads for p in posts]
        return self.reply_json(200, {"file": self.file_path.lstrip("/"), "comments": rows})

    # request bodies -----------------------------------------------------------------------
    def body_or_error(self, limit=MAX_FILE_BYTES):
        """The request body (bytes), or None after replying 4xx (bad length / too large)."""
        cl = self.headers.get("Content-Length")
        try:
            n = int(cl) if cl is not None else None
        except ValueError:
            self.error(400, "bad content-length")
            return None
        if n is not None and n < 0:
            self.error(400, "bad content-length")
            return None
        if n is not None and n > limit:
            if n <= MAX_BODY:
                self.rfile.read(n)          # drain so the client reads the 413 instead of a reset
            self.error(413, "request too large")
            return None
        try:
            raw = self.read_body()
        except (ValueError, OSError):
            self.error(400, "malformed request body")
            return None
        if len(raw) > limit:
            self.error(413, "request too large")
            return None
        return raw

    def line_fields(self, post):
        """Structural extras for a post event: the file enum and the post's line after the write."""
        return {"file": self.file_id, "line_no": self.rendered().post_line.get(post["id"])}

    # writes -------------------------------------------------------------------------------
    def do_POST(self):
        p = self.route()
        if p in COMMENT_PATHS:
            return self.post_comment()
        if p == self.file_path:
            return self.put_file()
        return self.error(404, "not found")

    def do_PUT(self):
        if self.route() == self.file_path:
            return self.put_file()
        if self.route() in COMMENT_PATHS:
            return self.error(405, "use POST /comment")
        return self.error(404, "not found")

    def post_comment(self):
        raw = self.body_or_error()
        if raw is None:
            return
        d = self.form(raw)
        if d is None:
            return self.error(400, "malformed body: send form fields or a JSON object")
        body, author = d.get("body", d.get("comment")), d.get("author", ANONYMOUS)
        if body is None:
            return self.error(400, "body required")
        if not isinstance(body, str) or not isinstance(author, str):
            return self.error(400, "body and author must be strings")
        if not body.strip():
            return self.error(400, "body required")
        if len(body) > MAX_COMMENT_CHARS:
            return self.error(413, "comment too long")
        author = fold(author)[:AUTHOR_CHARS] or ANONYMOUS
        line, anchor = self.int_field(d, "line"), self.int_field(d, "anchor")
        if line is False or anchor is False:
            return self.error(400, "line and anchor must be positive integers")
        with WRITE_LOCK:
            threads = snapshot(self.store)
            r = render(self.file_id, threads)
            if anchor is not None:
                slug = next((s for s, _, posts in threads for q in posts if q["id"] == anchor), None)
                if slug is None:
                    return self.error(404, "no such comment")
            elif line is not None:
                if line > len(r.lines):
                    return self.error(400, "line out of range")
                slug = self.thread_at(r, threads, line)
            else:
                slug = threads[-1][0] if threads else None
            post = self.add(slug, author, body)
        return self.reply_json(201, {"ok": True, "post_id": post["id"],
                                     "line": self.rendered().post_line.get(post["id"])})

    @staticmethod
    def form(raw):
        """POST body as a dict (JSON object or urlencoded form), or None if neither parses."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if text.lstrip().startswith(("{", "[")):
            try:
                d = json.loads(text)
            except ValueError:
                return None
            return d if isinstance(d, dict) else None
        try:
            return dict(urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=bool(text)))
        except ValueError:
            return None

    @staticmethod
    def int_field(d, key):
        """None if absent, the positive int if valid, False if malformed."""
        v = d.get(key)
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return v if v > 0 else False
        if isinstance(v, str) and v.strip().isdigit() and len(v.strip()) <= 9:
            return int(v.strip()) or False
        return False

    @staticmethod
    def thread_at(r, threads, line):
        """The thread whose header is the nearest at or above `line` (else the first thread)."""
        best = None
        for slug, _, _ in threads:
            hl = r.header_line.get(slug)
            if hl is not None and hl <= line and (best is None or hl > r.header_line[best]):
                best = slug
        return best if best is not None else (threads[0][0] if threads else None)

    def add(self, slug, author, body, title=None):
        """One write = one store.add_post (content -> sink) = one structural post event."""
        if slug is None:
            slug, title = DEFAULT_SLUG, title or DEFAULT_TITLE
        return self.store.add_post(self.agent, slug, author, body, iface=self.file_id, title=title,
                                   fields=self.line_fields)

    def new_slug(self, title):
        base = "c-" + slugify(title)
        slug, k = base, 2
        while slug in self.store.threads:
            slug, k = f"{base}-{k}", k + 1
        return slug

    def upload_limit(self):
        """Whole-file upload cap: never below MAX_FILE_BYTES, and always room for the current
        rendering plus a full batch of new comments, so download-edit-upload keeps working as the
        board grows (bounded by the transport cap MAX_BODY)."""
        room = len(self.rendered().text.encode()) + MAX_NEW_COMMENTS * MAX_COMMENT_CHARS * 4
        return min(MAX_BODY, max(MAX_FILE_BYTES, room))

    def newlines_stripped(self, src, rendered):
        """A whole-file copy whose line breaks were removed in transit (`curl -d @file` strips CR/LF):
        no line break at all, yet it carries the template code or several comments. Its comment
        structure no longer matches the file, so it is refused instead of mis-diffed."""
        if LINE_BREAKS.search(src) or len(rendered.lines) < 2:
            return False
        chunks = JS_CHUNKS if self.file_id == "js" else HTML_CHUNKS
        marks = [ln.strip() for ln in (chunks[0][-4:] + chunks[-1]) if len(ln.strip()) > 6]
        openers = src.count("/*") + src.count("//") if self.file_id == "js" else src.count("<!--")
        return openers >= 3 or any(m in src for m in marks)

    def put_file(self):
        raw = self.body_or_error(self.upload_limit())
        if raw is None:
            return
        try:
            src = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self.error(400, "file must be utf-8")
        try:
            new = self.parse(src)
        except Malformed:
            return self.error(400, "malformed file: unterminated comment")
        default_author = fold(self.headers.get("X-Author") or "")[:AUTHOR_CHARS] or ANONYMOUS
        fid = self.file_id
        with WRITE_LOCK:
            threads = snapshot(self.store)
            r = render(fid, threads)
            if self.newlines_stripped(src, r):
                return self.error(400, "line breaks were stripped: send the file with --data-binary @file or -T file")
            old = collections.Counter()
            for c in self.parse(r.text):
                if c.kind == "block":
                    old[comment_key(fid, "block", None, c.text)] += 1
                else:
                    a, b = line_parts(c.text)
                    old[comment_key(fid, "line", a if a is not None else ANONYMOUS, b)] += 1
            # thread headers in render order: equal titles are matched positionally
            by_header = collections.defaultdict(collections.deque)
            for slug, _, _ in threads:
                by_header[comment_key(fid, "block", None, r.header_text[slug])].append(slug)

            def take(*keys):
                for k in keys:
                    if old[k] > 0:
                        old[k] -= 1
                        return k
                return None

            def free_keys(body):
                return [comment_key(fid, "line", default_author, body), comment_key(fid, "line", ANONYMOUS, body)]

            plan, current = [], None      # current: ("slug", s) | ("new", plan index) | None
            i = 0
            while i < len(new):
                c = new[i]
                if c.kind == "block":
                    i += 1
                    k = comment_key(fid, "block", None, c.text)
                    if take(k):
                        if by_header.get(k):
                            current = ("slug", by_header[k].popleft())
                        continue
                    if not c.text:
                        continue
                    plan.append(("block", None, default_author, c.text))
                    current = ("new", len(plan) - 1)
                    continue
                author, body = line_parts(c.text)
                if author is not None:            # a stamped "<author> (Nm ago): <body>" line
                    i += 1
                    if take(comment_key(fid, "line", author, body)) or not body:
                        continue
                    plan.append(("line", current, author[:AUTHOR_CHARS], body))
                    continue
                # a run of adjacent free-form line comments (a multi-line note) is ONE post: first
                # consume any already-posted stretch of it, then merge each unmatched stretch
                j = i + 1
                while (j < len(new) and new[j].kind == "line" and new[j].own
                       and new[j].line - new[j - 1].line <= 1 and line_parts(new[j].text)[0] is None):
                    j += 1
                run = [line_parts(x.text)[1] for x in new[i:j]]
                pending, a = [], 0
                while a < len(run):
                    hit = None
                    for b in range(min(len(run), a + MERGE_SPAN), a, -1):
                        joined = "\n".join(x for x in run[a:b] if x)
                        if joined and take(*free_keys(joined)):
                            hit = b
                            break
                    if hit is None:
                        if run[a]:
                            pending.append(run[a])
                        a += 1
                        continue
                    if pending:
                        plan.append(("line", current, default_author, "\n".join(pending)))
                        pending = []
                    a = hit
                if pending:
                    plan.append(("line", current, default_author, "\n".join(pending)))
                i = j
            if len(plan) > MAX_NEW_COMMENTS:
                return self.error(413, "too many new comments in one upload")
            if any(len(body) > MAX_COMMENT_CHARS for _, _, _, body in plan):
                return self.error(413, "comment too long")
            first = threads[0][0] if threads else None
            new_threads, post_ids = {}, []
            for idx, (kind, target, author, body) in enumerate(plan):
                if kind == "block":
                    slug = new_threads[idx] = self.new_slug(body)
                    post = self.add(slug, author, body, title=body)
                else:
                    if target is None:
                        slug = first
                    elif target[0] == "slug":
                        slug = target[1]
                    else:
                        slug = new_threads[target[1]]
                    slug = slug or DEFAULT_SLUG
                    post = self.add(slug, author, body, title=DEFAULT_TITLE)
                    first = first or slug
                post_ids.append(post["id"])
        return self.reply_json(201 if post_ids else 200, {"ok": True, "added": len(post_ids), "post_ids": post_ids})


class JsHandler(CodeCommentHandler):
    file_id, file_path, ctype = "js", "/app.js", "application/javascript; charset=utf-8"


class HtmlHandler(CodeCommentHandler):
    file_id, file_path, ctype = "html", "/index.html", HTML_CT


class JsChannel(HttpChannel):
    name = "js"
    label = "Code comments in a JavaScript file"
    default_port = 8080
    entry_path = "/app.js"
    handler_cls = JsHandler


class HtmlChannel(HttpChannel):
    name = "html"
    label = "HTML comments in a web page"
    default_port = 8080
    entry_path = "/index.html"
    handler_cls = HtmlHandler
