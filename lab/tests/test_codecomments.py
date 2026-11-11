"""Code-comment channels (js / html): rendering, write paths, structural-only events, limits.

Every fixture is a neutral placeholder (never chatter). Assertions are structural -- counts,
event key sets, line numbers, ids -- and the leak checks assert a placeholder marker's ABSENCE
from the structural log without printing anything.
"""
import http.client
import json
import socket
import tempfile
import unittest
from pathlib import Path

from board import codecomments as cc
from board.channel import CHANNELS, HttpChannel
from board.events import EventLog, content_sink_path
from board.http_common import start_http_servers
from board.store import Store

SEED = {"board_name": "cboard",
        "threads": [
            {"slug": "alpha", "title": "Alpha", "posts": [
                {"author": "a1", "body": "p one", "ago_min": 30},
                {"author": "a2", "body": "p two\nwrapped", "ago_min": 5}]},
            {"slug": "beta", "title": "Beta", "posts": [
                {"author": "b1", "body": "p three", "ago_min": 50}]},
        ]}
N_SEED_POSTS = 3
POST_EVENT_KEYS = {"ts", "agent", "kind", "channel", "post_id", "thread_seq",
                   "body_len_chars", "body_len_bytes", "file", "line_no"}
MARK = "ZxLEAKMARKERxZ"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
JSON_CT = {"Content-Type": "application/json"}


class ParserTests(unittest.TestCase):
    def test_js_tokenizer_skips_strings_and_tracks_lines(self):
        src = "const u = 'a//b';\nconst t = `x\n/* y */`;\n// one\n/* two\n * three */\nf(); // four\n"
        got = cc.parse_js(src)
        self.assertEqual([(c.kind, c.line) for c in got], [("line", 4), ("block", 5), ("line", 7)])
        self.assertEqual(got[1].text, "two three")

    def test_js_unterminated_block_is_malformed(self):
        with self.assertRaises(cc.Malformed):
            cc.parse_js("x();\n/* open")

    def test_html_block_vs_line(self):
        got = cc.parse_html("<p>\n<!--\n  head\n-->\n<!-- one -->\n</p>")
        self.assertEqual([(c.kind, c.text, c.line) for c in got], [("block", "head", 2), ("line", "one", 5)])
        with self.assertRaises(cc.Malformed):
            cc.parse_html("<!-- open")

    def test_post_shape_round_trips(self):
        self.assertEqual(cc.split_post("au (12m ago): bo dy"), ("au", "bo dy"))
        self.assertEqual(cc.split_post("free text"), (cc.ANONYMOUS, "free text"))

    def test_render_neutralizes_terminators(self):
        threads = [("s", "t */ t", [{"id": 1, "author": "a", "body": "x\n*/ y", "ts": 0}])]
        r = cc.render("js", threads, now=0)
        self.assertEqual(len([c for c in cc.parse_js(r.text) if c.kind == "line"]), 1)
        threads = [("s", "t --> t", [{"id": 1, "author": "a", "body": "x --> <!-- y --", "ts": 0}])]
        r = cc.render("html", threads, now=0)
        parsed = cc.parse_html(r.text)
        self.assertEqual([c.kind for c in parsed][-2:], ["block", "line"])
        self.assertEqual(r.post_line[1], parsed[-1].line)

    def test_template_scaffold_is_comment_free_except_the_doc_header(self):
        for fid in ("js", "html"):
            r = cc.render(fid, [])
            parse = cc.parse_js if fid == "js" else cc.parse_html
            self.assertEqual([c.kind for c in parse(r.text)], ["block"])


class RegistryTests(unittest.TestCase):
    def test_registered_as_http_extras(self):
        for name, path in (("js", "/app.js"), ("html", "/index.html")):
            ch = CHANNELS[name]
            self.assertIsInstance(ch, HttpChannel)
            self.assertFalse(ch.pilot)
            self.assertRegex(ch.name, r"^[a-z]+$")
            self.assertEqual(ch.entry_url(), f"http://board.internal:8080{path}")


class CodeCommentCase:
    """Shared tests; mixed into one TestCase per channel (not collected on its own)."""
    channel_name = None
    file_path = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.events = EventLog(self.log_path)
        self.store = Store(SEED, self.events)
        # Same servers HttpChannel.serve() starts (agents 0 and 1), bound to loopback: binding 0.0.0.0
        # makes http.server resolve its fqdn, which is slow on some hosts. The CLI test covers serve().
        self.servers = start_http_servers(CHANNELS[self.channel_name].handler_cls, self.store, self.events,
                                          [0, 0], host="127.0.0.1")

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.events.close()
        self.tmp.cleanup()

    def parse(self, text):
        return cc.parse_js(text) if self.channel_name == "js" else cc.parse_html(text)

    def request(self, method, path, body=None, headers=None, agent=0):
        conn = http.client.HTTPConnection("127.0.0.1", self.servers[agent].server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def records(self):
        return [json.loads(l) for l in self.log_path.read_text().splitlines()]

    def post_events(self):
        return [r for r in self.records() if r["kind"] == "post"]

    def get_file(self):
        resp, data = self.request("GET", self.file_path)
        self.assertEqual(resp.status, 200)
        return data.decode()

    def line_comment(self, text):
        return f"// {text}" if self.channel_name == "js" else f"<!-- {text} -->"

    def block_comment(self, text):
        return f"/* {text} */" if self.channel_name == "js" else f"<!--\n  {text}\n-->"

    def assert_structural(self, ev, agent=0):
        self.assertEqual(set(ev), POST_EVENT_KEYS)
        self.assertEqual((ev["agent"], ev["kind"], ev["channel"], ev["file"]),
                         (agent, "post", self.channel_name, self.channel_name))
        self.assertIsInstance(ev["post_id"], int)
        self.assertIsInstance(ev["thread_seq"], int)
        self.assertIsInstance(ev["line_no"], int)

    # --- reads ---------------------------------------------------------------------------
    def test_seed_renders_as_comments(self):
        text = self.get_file()
        comments = self.parse(text)
        posts = [c for c in comments if c.kind == "line"]
        blocks = [c for c in comments if c.kind == "block"]
        self.assertEqual(len(posts), N_SEED_POSTS)
        self.assertEqual(len(blocks), 1 + len(SEED["threads"]))   # doc header + one per thread
        # every post line is a single physical line in the shape "<author> (<N>m ago): <body>"
        for c in posts:
            self.assertRegex(c.text, cc.POST_RE)
        # /comments listing agrees with the file's structure
        resp, data = self.request("GET", "/comments")
        self.assertEqual(resp.status, 200)
        listing = json.loads(data)["comments"]
        self.assertEqual(len(listing), N_SEED_POSTS)
        self.assertEqual(sorted(r["line"] for r in listing), sorted(c.line for c in posts))
        self.assertEqual(self.post_events(), [])

    def test_reads_log_req_events_with_static_paths(self):
        self.get_file()
        self.request("GET", "/comments")
        self.request("GET", "/")
        self.request("GET", f"/{MARK}?q={MARK}")
        reqs = [r for r in self.records() if r["kind"] == "req"]
        self.assertEqual([(r["method"], r["path"], r["status"]) for r in reqs],
                         [("GET", self.file_path, 200), ("GET", "/comments", 200), ("GET", "/", 200),
                          ("GET", "/*", 404)])
        self.assertTrue(all(r["agent"] == 0 for r in reqs))
        self.assertNotIn(MARK, self.log_path.read_text())

    # --- POST /comment -------------------------------------------------------------------
    def test_post_comment_form_is_one_structural_write(self):
        resp, data = self.request("POST", "/comment", f"author=me&body={MARK}", FORM)
        self.assertEqual(resp.status, 201)
        out = json.loads(data)
        evs = self.post_events()
        self.assertEqual(len(evs), 1)
        self.assert_structural(evs[0])
        self.assertEqual((evs[0]["post_id"], evs[0]["line_no"]), (out["post_id"], out["line"]))
        # the event's line is where the comment now sits in the served file
        by_line = {c.line: c for c in self.parse(self.get_file())}
        self.assertEqual(by_line[out["line"]].kind, "line")
        self.assertEqual(len([c for c in self.parse(self.get_file()) if c.kind == "line"]), N_SEED_POSTS + 1)
        # body never reaches the structural log; it is in the quarantined sink only
        self.assertNotIn(MARK, self.log_path.read_text())
        self.assertIn(MARK, Path(content_sink_path(str(self.log_path))).read_text())

    def test_post_comment_json_and_attribution(self):
        resp, _ = self.request("POST", "/comment", json.dumps({"author": "me", "body": "x"}), JSON_CT, agent=1)
        self.assertEqual(resp.status, 201)
        evs = self.post_events()
        self.assertEqual(len(evs), 1)
        self.assert_structural(evs[0], agent=1)
        reqs = [r for r in self.records() if r["kind"] == "req"]
        self.assertEqual((reqs[-1]["agent"], reqs[-1]["method"], reqs[-1]["path"]), (1, "POST", "/comment"))

    def test_post_comment_line_and_anchor_select_thread(self):
        seq = self.store.thread_seq
        r = cc.render(self.channel_name, cc.snapshot(self.store))
        beta_line = r.post_line[self.store.threads["beta"]["posts"][0]["id"]]
        self.request("POST", "/comment", f"body=x&line={beta_line}", FORM)
        alpha_post = self.store.threads["alpha"]["posts"][0]["id"]
        self.request("POST", "/comment", json.dumps({"body": "y", "anchor": alpha_post}), JSON_CT)
        self.request("POST", "/comment", "body=z", FORM)   # default: the last thread
        self.assertEqual([e["thread_seq"] for e in self.post_events()], [seq["beta"], seq["alpha"], seq["beta"]])

    # --- whole-file upload ---------------------------------------------------------------
    def test_put_whole_file_counts_exactly_the_added_comments(self):
        lines = self.get_file().splitlines()
        # two line comments into the existing Alpha block, then a new block with one line after it
        alpha = next(i for i, l in enumerate(lines) if "Alpha" in l)
        add_alpha = [self.line_comment(f"me (0m ago): {MARK} one"), self.line_comment(f"{MARK} two")]
        # after Alpha's two posts: js header is one line, the html header's title sits mid-block
        at = alpha + (3 if self.channel_name == "js" else 4)
        lines[at:at] = add_alpha
        new_block = [self.block_comment(f"{MARK} topic"), self.line_comment(f"me (0m ago): {MARK} three")]
        lines[-1:-1] = new_block
        resp, data = self.request("PUT", self.file_path, "\n".join(lines) + "\n")
        self.assertEqual(resp.status, 201)
        out = json.loads(data)
        evs = self.post_events()
        self.assertEqual(out["added"], 4)
        self.assertEqual(len(evs), 4)
        for e in evs:
            self.assert_structural(e)
        self.assertEqual([e["post_id"] for e in evs], out["post_ids"])
        seq = self.store.thread_seq
        self.assertEqual(evs[0]["thread_seq"], seq["alpha"])
        self.assertEqual(evs[1]["thread_seq"], seq["alpha"])
        self.assertEqual(evs[2]["thread_seq"], evs[3]["thread_seq"])         # block opens a new thread
        self.assertNotIn(evs[2]["thread_seq"], (seq["alpha"], seq["beta"]))
        self.assertEqual(len(self.store.threads), 3)
        self.assertNotIn(MARK, self.log_path.read_text())

        # re-uploading the served file (stamps drift, code edited, a comment removed) adds nothing
        text = self.get_file().replace("(0m ago)", "(7m ago)")
        kept = [l for l in text.splitlines() if "p three" not in l]
        resp, data = self.request("PUT", self.file_path, "\n".join(kept) + "\nextra();\n")
        self.assertEqual((resp.status, json.loads(data)["added"]), (200, 0))
        self.assertEqual(len(self.post_events()), 4)

    def test_post_to_file_path_is_an_upload_and_x_author(self):
        text = self.get_file() + self.line_comment("x") + "\n"
        resp, data = self.request("POST", self.file_path, text, {"X-Author": "me"}, agent=1)
        self.assertEqual((resp.status, json.loads(data)["added"]), (201, 1))
        self.assert_structural(self.post_events()[0], agent=1)
        self.assertEqual(self.store.find_post(self.post_events()[0]["post_id"])[1]["author"], "me")

    def test_empty_store_upload_creates_default_thread(self):
        self.store.threads.clear()
        resp, data = self.request("PUT", self.file_path, self.get_file() + self.line_comment("x") + "\n")
        self.assertEqual(json.loads(data)["added"], 1)
        self.assertEqual(list(self.store.threads), [cc.DEFAULT_SLUG])

    # --- malformed input / limits --------------------------------------------------------
    def assert_rejected(self, resp, data, codes=(400,)):
        self.assertIn(resp.status, codes)
        self.assertNotIn(MARK.encode(), data)              # never echoes the input
        self.assertEqual(self.post_events(), [])

    def test_malformed_inputs_are_4xx_without_echo(self):
        cases = [
            ("POST", "/comment", "{" + MARK, JSON_CT),                       # broken JSON
            ("POST", "/comment", json.dumps([MARK]), JSON_CT),               # not an object
            ("POST", "/comment", MARK, FORM),                                # not a form
            ("POST", "/comment", f"author={MARK}", FORM),                    # no body
            ("POST", "/comment", json.dumps({"body": [MARK]}), JSON_CT),     # wrong type
            ("POST", "/comment", f"body={MARK}&line={MARK}", FORM),          # bad line
            ("POST", "/comment", f"body={MARK}&line=0", FORM),
            ("POST", "/comment", f"body={MARK}&line=99999", FORM),           # out of range
            ("POST", "/comment", b"body=\xff\xfe" + MARK.encode(), FORM),    # not utf-8
            ("PUT", self.file_path, b"\xff\xfe" + MARK.encode(), {}),
            ("PUT", self.file_path, f"x();\n{self.block_comment(MARK)[:-3]}", {}),  # unterminated
        ]
        for method, path, body, headers in cases:
            with self.subTest(method=method, i=cases.index((method, path, body, headers))):
                resp, data = self.request(method, path, body, headers)
                self.assert_rejected(resp, data)
        resp, data = self.request("POST", "/comment", f"body={MARK}&anchor=987654", FORM)
        self.assert_rejected(resp, data, (404,))
        resp, data = self.request("PUT", "/comment", f"body={MARK}", FORM)
        self.assert_rejected(resp, data, (405,))
        resp, data = self.request("PUT", f"/{MARK}", MARK, {})
        self.assert_rejected(resp, data, (404,))

    def test_bad_content_length_is_400(self):
        port = self.servers[0].server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"POST /comment HTTP/1.0\r\nContent-Length: nope\r\n\r\n")
            data = s.makefile("rb").read()
        self.assertTrue(data.startswith(b"HTTP/1.0 400"))
        self.assertEqual(self.post_events(), [])

    def test_size_limits(self):
        resp, data = self.request("POST", "/comment", "body=" + "x" * (cc.MAX_COMMENT_CHARS + 1), FORM)
        self.assert_rejected(resp, data, (413,))
        cap = len(self.get_file().encode()) + cc.MAX_NEW_COMMENTS * cc.MAX_COMMENT_CHARS * 4
        resp, data = self.request("PUT", self.file_path, "x" * (cap + 1), {})
        self.assert_rejected(resp, data, (413,))
        # separate notes (blank line between): each is its own post
        many = "".join(self.line_comment(f"n{i}") + "\n\n" for i in range(cc.MAX_NEW_COMMENTS + 1))
        resp, data = self.request("PUT", self.file_path, self.get_file() + many)
        self.assert_rejected(resp, data, (413,))
        long_one = self.line_comment("y" * (cc.MAX_COMMENT_CHARS + 1)) + "\n"
        resp, data = self.request("PUT", self.file_path, self.get_file() + long_one)
        self.assert_rejected(resp, data, (413,))
        # at the limit is fine
        resp, _ = self.request("POST", "/comment", "body=" + "x" * cc.MAX_COMMENT_CHARS, FORM)
        self.assertEqual(resp.status, 201)


    # --- re-upload idempotence (review fixes) ----------------------------------------------
    def put(self, text, headers=None):
        resp, data = self.request("PUT", self.file_path, text, headers or {})
        self.assertIn(resp.status, (200, 201))
        return json.loads(data)["added"]

    def test_reupload_of_terminator_bodies_adds_once(self):
        # the agent's raw copy holds '*/' / '--'; the server renders them escaped
        body = "x--y */ z"
        local = self.get_file() + self.line_comment(f"me (0m ago): {body}") + "\n"
        local2 = self.get_file() + self.block_comment("t--u */ v") + "\n"
        self.assertEqual([self.put(local) for _ in range(3)], [1, 0, 0])
        self.assertEqual([self.put(local2 if self.channel_name == "js" else local2.replace("*/", "")) for _ in range(2)],
                         [1, 0])

    def test_reupload_free_form_with_x_author_adds_once(self):
        local = self.get_file() + self.line_comment("plain note") + "\n"
        self.assertEqual([self.put(local, {"X-Author": "bob"}) for _ in range(3)], [1, 0, 0])
        # the freshly served copy (now stamped as bob's) is also a no-op
        self.assertEqual(self.put(self.get_file(), {"X-Author": "bob"}), 0)

    def test_parenthesised_or_long_author_round_trips_after_stamp_drift(self):
        for author in ("bo (x)", "q" * (cc.AUTHOR_CHARS + 20)):
            resp, _ = self.request("POST", "/comment", json.dumps({"author": author, "body": "hello"}), JSON_CT)
            self.assertEqual(resp.status, 201)
        for t in self.store.threads.values():
            for p in t["posts"]:
                p["ts"] -= 600
        n = len(self.post_events())
        stale = self.get_file().replace("(10m ago)", "(3m ago)")
        self.assertEqual(self.put(stale), 0)
        self.assertEqual(len(self.post_events()), n)
        for c in self.parse(self.get_file()):
            if c.kind == "line":
                self.assertRegex(c.text, cc.POST_RE)

    def test_duplicate_titles_match_positionally(self):
        self.store.threads["beta"]["title"] = "Alpha"
        lines = self.get_file().splitlines()
        heads = [i for i, l in enumerate(lines) if "Alpha" in l]
        self.assertEqual(len(heads), 2)
        at = heads[1] + (1 if self.channel_name == "js" else 2)
        lines[at:at] = [self.line_comment("under second")]
        self.assertEqual(self.put("\n".join(lines) + "\n"), 1)
        self.assertEqual(self.post_events()[-1]["thread_seq"], self.store.thread_seq["beta"])

    def test_newline_stripped_upload_is_refused(self):
        local = self.get_file() + self.line_comment("me: note") + "\n"
        stripped = local.replace("\r", "").replace("\n", "")
        resp, data = self.request("POST", self.file_path, stripped, FORM)
        self.assert_rejected(resp, data, (400,))
        # a deliberate one-line upload of a single comment still works
        resp, data = self.request("PUT", self.file_path, self.line_comment("solo"))
        self.assertEqual((resp.status, json.loads(data)["added"]), (201, 1))

    def test_multi_line_note_is_one_post_and_reupload_is_idempotent(self):
        note = "\n".join(self.line_comment(f"part {i}") for i in range(3))
        local = self.get_file() + note + "\n"
        self.assertEqual(self.put(local), 1)
        evs = self.post_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(self.store.find_post(evs[0]["post_id"])[1]["body"].count("\n"), 2)
        self.assertEqual(self.put(local), 0)                     # stale local copy
        self.assertEqual(self.put(self.get_file()), 0)           # fresh copy
        # a stale copy plus one more adjacent line adds just the new line
        self.assertEqual(self.put(local + self.line_comment("part 3") + "\n"), 1)
        self.assertEqual(self.store.find_post(self.post_events()[-1]["post_id"])[1]["body"], "part 3")


class JsChannelTests(CodeCommentCase, unittest.TestCase):
    channel_name, file_path = "js", "/app.js"

    def test_root_is_an_index_listing(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b'href="app.js"', data)
        resp, _ = self.request("GET", "/index.html")
        self.assertEqual(resp.status, 404)


class HtmlChannelTests(CodeCommentCase, unittest.TestCase):
    channel_name, file_path = "html", "/index.html"

    def test_root_serves_the_page(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data.decode(), self.get_file())


if __name__ == "__main__":
    unittest.main()
