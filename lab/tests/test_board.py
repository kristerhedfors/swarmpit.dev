"""Board package tests: each HTTP interface served in-process on ephemeral loopback ports."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

from board.cache import CacheHandler
from board.events import EventLog
from board.forum import ForumHandler
from board.http_common import start_http_servers
from board.store import Store, ago, camel, slugify
from board.wiki import WikiHandler

LAB = Path(__file__).resolve().parent.parent
SEED = {
    "board_name": "testboard",
    "threads": [
        {"slug": "roll-call", "title": "Roll call", "posts": [
            {"author": "alice", "body": "first <b>", "ago_min": 90},
            {"author": "bob", "body": "second", "ago_min": 5},
        ]},
        {"slug": "tips", "title": "Tips", "posts": [{"author": "carol", "body": "tip", "ago_min": 400}]},
    ],
}


class BoardCase(unittest.TestCase):
    """Starts one interface on two ports (agents 0 and 1) with a fresh store and event log."""
    handler = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.events = EventLog(self.log_path)
        self.store = Store(SEED, self.events)
        self.servers = start_http_servers(self.handler, self.store, self.events, [0, 0], host="127.0.0.1")

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.events.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, agent=0, chunked=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.servers[agent].server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def get_json(self, path, agent=0):
        resp, data = self.request("GET", path, agent=agent)
        return resp.status, json.loads(data)

    def post_json(self, path, obj, agent=0):
        resp, data = self.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"}, agent)
        return resp.status, json.loads(data)

    def logged(self, kind=None):
        recs = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [r for r in recs if kind is None or r["kind"] == kind]


class HelperTests(unittest.TestCase):
    def test_slug_and_camel(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("!!!"), "untitled")
        self.assertEqual(len(slugify("a" * 100)), 60)
        self.assertEqual(camel("roll-call"), "RollCall")

    def test_ago(self):
        now = time.time()
        self.assertEqual(ago(now), "just now")
        self.assertEqual(ago(now - 5 * 60 - 1), "5 min ago")
        self.assertEqual(ago(now - 180 * 60 - 1), "3 h ago")

    def test_seed_upvotes_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = EventLog(Path(tmp) / "e.jsonl")
            store = Store(SEED, events, now=1000.0)
            events.close()
        posts = store.threads["roll-call"]["posts"]
        self.assertEqual([p["id"] for p in posts], [1, 2])
        self.assertEqual([p["upvotes"] for p in posts], [10, 12])
        self.assertEqual(posts[0]["ts"], 1000.0 - 90 * 60)
        self.assertEqual(store.threads["tips"]["posts"][0]["upvotes"], 2)
        self.assertTrue(all(p["seeded"] for p in posts))


class ForumTests(BoardCase):
    handler = ForumHandler

    def test_landing_page_and_headers(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.version, 10)
        self.assertEqual(resp.getheader("Server"), "nginx/1.24.0 ")  # empty sys_version leaves a trailing space
        self.assertEqual(resp.getheader("Content-Type"), "text/markdown; charset=utf-8")
        self.assertEqual(int(resp.getheader("Content-Length")), len(data))
        self.assertTrue(data.startswith(b"# testboard\n"))
        self.assertIn(b"curl -s http://board.internal:8080/api/v1/threads", data)

    def test_read_threads(self):
        status, obj = self.get_json("/api/v1/threads")
        self.assertEqual(status, 200)
        self.assertEqual(len(obj["threads"]), 2)
        self.assertEqual(obj["threads"][0]["last_activity"], "5 min ago")
        first_id = obj["threads"][0]["id"]  # seed-derived slug, used as a route, never asserted/printed
        status, obj = self.get_json("/api/v1/threads/" + first_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(obj["posts"]), 2)
        self.assertTrue(all(isinstance(p["post_id"], int) for p in obj["posts"]))
        resp, data = self.request("GET", "/api/v1/threads/" + first_id + "?format=md")
        self.assertEqual(resp.getheader("Content-Type"), "text/markdown; charset=utf-8")
        self.assertGreater(len(data), 0)
        self.assertEqual(self.get_json("/api/v1/threads/nope")[0], 404)
        self.assertEqual(self.get_json("/nope"), (404, {"error": "not found", "see": "GET /"}))

    def test_write_thread_and_reply(self):
        status, obj = self.post_json("/api/v1/threads", {"author": "me", "title": "Hello There!", "body": "hi"}, agent=1)
        self.assertEqual(status, 201)
        self.assertEqual((obj["ok"], obj["post_id"]), (True, 4))
        self.assertIn(slugify("Hello There!"), self.store.threads)
        status, obj = self.post_json("/api/v1/threads/roll-call/posts", {"body": "reply"})
        self.assertEqual((status, obj), (201, {"ok": True, "post_id": 5}))
        self.assertEqual(self.post_json("/api/v1/threads", {"body": "x"})[0], 400)
        self.assertEqual(self.post_json("/api/v1/threads/roll-call/posts", {"author": "x"})[0], 400)
        self.assertEqual(self.post_json("/api/v1/threads/nope/posts", {"body": "x"})[0], 404)
        # two writes -> two structural post events (counts + ids only; no body/author/slug)
        posts = self.logged("post")
        self.assertEqual([(p["agent"], p["kind"]) for p in posts], [(1, "post"), (0, "post")])
        self.assertTrue(all(isinstance(p["post_id"], int) for p in posts))
        self.assertEqual(set(posts[0]), {"ts", "agent", "kind", "channel", "post_id",
                                         "thread_seq", "body_len_chars", "body_len_bytes"})

    def test_form_encoded_post(self):
        resp, data = self.request("POST", "/api/v1/threads", "author=f&title=Form&body=b",
                                  {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((resp.status, json.loads(data)["ok"]), (201, True))

    def test_upvote_and_karma(self):
        self.assertEqual(self.post_json("/api/v1/posts/2/upvote", {}, agent=1), (200, {"ok": True, "upvotes": 13}))
        self.assertEqual(self.post_json("/api/v1/posts/99/upvote", {})[0], 404)
        self.assertEqual(self.post_json("/api/v1/posts/x/upvote", {})[0], 404)
        _, obj = self.get_json("/api/v1/leaderboard")
        self.assertEqual(obj["leaderboard"][0]["karma"], 13)  # top of the board; author text not asserted
        up = self.logged("upvote")
        self.assertEqual(len(up), 1)
        self.assertEqual((up[0]["agent"], up[0]["kind"], up[0]["post_id"]), (1, "upvote", 2))
        self.assertEqual(set(up[0]), {"ts", "agent", "kind", "channel", "post_id", "thread_seq"})

    def test_head_has_no_body(self):
        resp, data = self.request("HEAD", "/")
        self.assertEqual(data, b"")
        self.assertGreater(int(resp.getheader("Content-Length")), 0)

    def test_request_events_attributed_by_port(self):
        self.request("GET", "/api/v1/threads", agent=0, headers={"User-Agent": "ua0"})
        self.request("GET", "/api/v1/leaderboard", agent=1)
        reqs = self.logged("req")
        self.assertEqual([(r["agent"], r["path"], r["status"]) for r in reqs],
                         [(0, "/api/v1/threads", 200), (1, "/api/v1/leaderboard", 200)])
        self.assertEqual(reqs[0]["ua"], "ua0")
        self.assertEqual(list(reqs[0]), ["ts", "agent", "kind", "method", "path", "status", "bytes", "ua"])


class WikiTests(BoardCase):
    handler = WikiHandler

    def test_front_page_and_page(self):
        resp, data = self.request("GET", "/wiki/FrontPage")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        page = camel(next(iter(self.store.threads)))  # seeded page, addressed via its camel name (derived)
        resp, data = self.request("GET", "/wiki/" + page)
        self.assertEqual(resp.status, 200)
        self.assertGreater(len(data), 0)
        resp, data = self.request("GET", "/wiki/Missing")
        self.assertEqual(resp.status, 404)
        self.assertIn(b"does not exist yet", data)

    def test_get_edit_writes(self):
        resp, data = self.request("GET", "/wiki/edit?page=RollCall&author=w&append=" + urllib.parse.quote("hi there"),
                                  agent=1)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Saved.", data)
        self.request("GET", "/wiki/edit?page=BrandNewPage&append=x")
        # one append to an existing page, one to a new page (counts only)
        self.assertEqual(len(self.store.threads["roll-call"]["posts"]), 3)
        self.assertIn("brand-new-page", self.store.threads)
        self.assertEqual(len(self.store.threads["brand-new-page"]["posts"]), 1)
        posts = self.logged("post")
        self.assertEqual([(p["agent"], p["kind"]) for p in posts], [(1, "post"), (0, "post")])
        self.assertTrue(all(isinstance(p["post_id"], int) for p in posts))
        resp, data = self.request("GET", "/wiki/RecentChanges")
        self.assertIn(b"just now", data)

    def test_edit_form_and_errors(self):
        resp, data = self.request("GET", "/wiki/edit?page=RollCall")
        self.assertIn(b'<form method="GET" action="/wiki/edit">', data)
        resp, _ = self.request("GET", "/wiki/edit")
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (400, "text/html"))

    def test_post_is_logged_as_get_edit(self):
        self.request("POST", "/wiki/edit", "page=Posted&append=via+post",
                     {"Content-Type": "application/x-www-form-urlencoded"})
        # the form POST is rewritten to the equivalent GET edit; the appended text is not asserted
        self.assertTrue(self.logged("req")[-1]["path"].startswith("/wiki/edit"))
        self.assertIn("posted", self.store.threads)
        self.assertEqual(len(self.store.threads["posted"]["posts"]), 1)


class CacheTests(BoardCase):
    handler = CacheHandler

    def test_browse_and_storage(self):
        resp, data = self.request("GET", "/artifactory/agent-cache/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Server"), "Artifactory/7.77.8 ")
        _, obj = self.get_json("/artifactory/api/storage/agent-cache/roll-call")
        self.assertEqual(len(obj["children"]), 2)
        uri = obj["children"][0]["uri"]  # seed-derived filename, used as a route (not echoed)
        resp, data = self.request("GET", "/artifactory/agent-cache/roll-call" + uri)
        self.assertEqual(resp.getheader("Content-Type"), "application/octet-stream")
        self.assertGreater(len(data), 0)
        self.assertEqual(self.get_json("/artifactory/agent-cache/nope/")[0], 404)

    def test_put_plain_and_chunked(self):
        resp, data = self.request("PUT", "/artifactory/agent-cache/notes/a.txt", b"plain")
        self.assertEqual(resp.status, 201)
        self.assertEqual(json.loads(data)["downloadUri"], "http://board.internal:8080/artifactory/agent-cache/notes/a.txt")
        resp, data = self.request("PUT", "/artifactory/agent-cache/notes/b.txt", iter([b"chun", b"ked"]),
                                  agent=1, chunked=True)
        self.assertEqual((resp.status, json.loads(data)["size"]), (201, "7"))
        self.assertEqual(len(self.request("GET", "/artifactory/agent-cache/notes/b.txt")[1]), 7)  # round-trip by length
        self.assertEqual(self.request("PUT", "/artifactory/agent-cache/dir/", b"x")[0].status, 400)
        posts = self.logged("post")
        self.assertEqual([(p["agent"], p["kind"]) for p in posts], [(0, "post"), (1, "post")])
        self.assertTrue(all(p["body_len_bytes"] > 0 for p in posts))

    def test_delete_is_logged_not_applied(self):
        resp, data = self.request("DELETE", "/artifactory/agent-cache/notes/x.txt", agent=1)
        self.assertEqual((resp.status, data), (204, b""))
        self.assertEqual(self.logged("delete")[0]["agent"], 1)
        self.assertEqual(len(self.store.threads["roll-call"]["posts"]), 2)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port, deadline_s=120):
    """HTTPServer resolves getfqdn("0.0.0.0") before listen(); reverse DNS on a dev host can take tens of seconds."""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"board never listened on {port}")


class CliTests(unittest.TestCase):
    """`python3 -m board` as the board VM runs it, for an HTTP interface and IRC."""

    def run_board(self, iface):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "events.jsonl"
        seed = Path(tmp.name) / "seed.json"
        seed.write_text(json.dumps(SEED))
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "board", "--interface", iface, "--ports", str(port),
                                 "--seed", str(seed), "--log", str(log)], cwd=LAB, env={**os.environ},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        wait_listening(port)
        time.sleep(0.2)
        return port, log

    def test_http_cli(self):
        port, log = self.run_board("forum")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/v1/threads")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        kinds = [json.loads(line)["kind"] for line in log.read_text().splitlines()]
        self.assertEqual(kinds[0], "board_start")
        self.assertIn("req", kinds)

    def test_irc_cli(self):
        """`--interface irc` is served in-process by board.irc (the asyncio ircd), not a hand-off."""
        port, log = self.run_board("irc")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"NICK neo\r\nUSER n 0 * :Neo\r\n")
            first_line = s.makefile("rb").readline()
        self.assertTrue(first_line.startswith(b":irc.board.internal 001 neo :Welcome"), first_line)
        first = json.loads(log.read_text().splitlines()[0])
        self.assertEqual((first["kind"], first["channel"], first["ports"]), ("board_start", "irc", [port]))

if __name__ == "__main__":
    unittest.main()
