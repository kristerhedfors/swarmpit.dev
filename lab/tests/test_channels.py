"""Channel registry + new-channel tests.

Every assertion is structural: registry shape, counts, opaque ids, lengths and
event key-sets. No test prints or asserts a post body, an author, or an
agent-chosen slug -- board content never enters a coding agent's context.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import config
from board.channel import CHANNELS, INTERFACES, Channel, HttpChannel, StreamChannel
from board.events import EventLog, content_sink_path
from board.store import Store

LAB = Path(__file__).resolve().parent.parent

# A neutral fixture seed (placeholders, never chatter): one thread, one seeded post.
SEED = {"board_name": "cboard",
        "threads": [{"slug": "notes", "title": "Notes",
                     "posts": [{"author": "x", "body": "y", "ago_min": 10}]}]}

PILOT = ("forum", "wiki", "cache", "irc")
EXTRAS = ("guestbook", "paste", "kv", "js", "html")
POST_EVENT_KEYS = {"ts", "agent", "kind", "channel", "post_id", "thread_seq",
                   "body_len_chars", "body_len_bytes"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port, deadline_s=120):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"interface never listened on {port}")


class RegistryTests(unittest.TestCase):
    def test_registry_completeness(self):
        self.assertEqual(set(CHANNELS), set(PILOT) | set(EXTRAS))
        self.assertEqual(INTERFACES, tuple(CHANNELS))
        for name, ch in CHANNELS.items():
            self.assertIsInstance(ch, Channel)
            self.assertEqual(ch.name, name)
            self.assertTrue(ch.label)
            self.assertIn(ch.scheme, ("http", "irc"))
            self.assertGreater(ch.default_port, 0)
            self.assertTrue(ch.entry_url().startswith(ch.scheme + "://board.internal:"))

    def test_pilot_and_extras_split(self):
        self.assertEqual(tuple(n for n, c in CHANNELS.items() if c.pilot), PILOT)
        self.assertEqual(tuple(n for n, c in CHANNELS.items() if not c.pilot), EXTRAS)

    def test_channel_kinds(self):
        self.assertIsInstance(CHANNELS["irc"], StreamChannel)
        for name in PILOT[:3] + EXTRAS:
            self.assertIsInstance(CHANNELS[name], HttpChannel)
            self.assertIsNotNone(CHANNELS[name].handler_cls)
        self.assertEqual(CHANNELS["cache"].seed_fields(), ("path",))


class ConfigDerivationTests(unittest.TestCase):
    """config.INTERFACES/IFACE_LABEL/BOARD_URL/board_port_for all come from the registry."""

    def test_interfaces_are_the_pilot(self):
        self.assertEqual(config.INTERFACES, PILOT)

    def test_labels_and_urls_derive_from_registry(self):
        self.assertEqual(set(config.IFACE_LABEL), set(PILOT))
        for name in PILOT:
            self.assertEqual(config.IFACE_LABEL[name], CHANNELS[name].label)
            self.assertEqual(config.BOARD_URL[name], CHANNELS[name].entry_url())

    def test_board_port_for_derives_from_registry(self):
        for name, ch in CHANNELS.items():
            self.assertEqual(config.board_port_for(name), ch.default_port)
        self.assertEqual(config.HTTP_BOARD_PORT, CHANNELS["forum"].default_port)
        self.assertEqual(config.IRC_BOARD_PORT, CHANNELS["irc"].default_port)


class ChannelCase(unittest.TestCase):
    """Serve one channel instance in-process on an ephemeral loopback port (agent 0)."""
    channel_name = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.events = EventLog(self.log_path)
        self.store = Store(SEED, self.events)
        self.servers = CHANNELS[self.channel_name].serve(self.store, self.events, [0])

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.events.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.servers[0].server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def post_events(self):
        recs = [json.loads(l) for l in self.log_path.read_text().splitlines()]
        return [r for r in recs if r["kind"] == "post"]

    def assert_one_structural_post(self):
        posts = self.post_events()
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual((p["agent"], p["kind"], p["channel"]), (0, "post", self.channel_name))
        self.assertIsInstance(p["post_id"], int)
        self.assertEqual(set(p), POST_EVENT_KEYS)  # no body / author / slug on the structural event


class GuestbookChannelTests(ChannelCase):
    channel_name = "guestbook"

    def test_write_is_one_structural_post_and_round_trips(self):
        resp, data = self.request("POST", "/guestbook", "author=me&message=hello",
                                  {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 201)
        self.assert_one_structural_post()
        # read-back via the Store: one entry on the implicit guestbook thread
        self.assertEqual(len(self.store.threads["guestbook"]["posts"]), 1)
        resp, data = self.request("GET", "/guestbook")
        self.assertEqual(resp.status, 200)
        self.assertGreater(len(data), 0)

    def test_empty_message_is_rejected_and_not_logged(self):
        resp, _ = self.request("POST", "/guestbook", "author=me", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.post_events()), 0)


class PasteChannelTests(ChannelCase):
    channel_name = "paste"

    def test_drop_and_read_by_returned_id(self):
        payload = "x" * 37
        resp, data = self.request("POST", "/pastebin", json.dumps({"content": payload}),
                                  {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 201)
        obj = json.loads(data)
        self.assertTrue(obj["ok"] and isinstance(obj["post_id"], int))
        self.assert_one_structural_post()
        # read by the returned opaque id; round-trip verified by length only
        resp, data = self.request("GET", obj["url"])
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(data), len(payload))
        self.assertEqual(len(self.store.threads), 2)  # seeded 'notes' + the new paste thread

    def test_missing_content_rejected(self):
        resp, _ = self.request("POST", "/pastebin", json.dumps({}), {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.post_events()), 0)


class KvChannelTests(ChannelCase):
    channel_name = "kv"

    def test_put_appends_and_reads_back(self):
        resp, data = self.request("PUT", "/kv/mykey", b"a" * 11)
        self.assertEqual(resp.status, 201)
        self.assertEqual(json.loads(data)["key"], "mykey")
        self.assert_one_structural_post()
        self.assertEqual(len(self.store.threads["mykey"]["posts"]), 1)
        # a second PUT to the same key appends (two posts, two structural events)
        self.request("POST", "/kv/mykey", b"b" * 5)
        self.assertEqual(len(self.store.threads["mykey"]["posts"]), 2)
        self.assertEqual(len(self.post_events()), 2)
        resp, data = self.request("GET", "/kv/mykey")
        self.assertEqual(resp.status, 200)
        self.assertGreater(len(data), 0)

    def test_missing_key_rejected(self):
        resp, _ = self.request("PUT", "/kv/", b"v")
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.post_events()), 0)


class CliAllInterfacesTests(unittest.TestCase):
    """`python3 -m board --interface X` starts and serves every registered interface."""

    def run_board(self, iface, stdout=subprocess.DEVNULL):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "events.jsonl"
        seed = Path(tmp.name) / "seed.json"
        seed.write_text(json.dumps(SEED))
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "board", "--interface", iface, "--ports", str(port),
                                 "--seed", str(seed), "--log", str(log)], cwd=LAB, env={**os.environ},
                                stdout=stdout, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        wait_listening(port)
        time.sleep(0.2)
        return proc, port, log

    def test_cli_starts_every_registered_interface(self):
        for name, ch in CHANNELS.items():
            with self.subTest(interface=name):
                _, port, log = self.run_board(name)
                if ch.scheme == "irc":
                    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                        s.sendall(b"NICK n\r\nUSER n 0 * :n\r\n")
                        line = s.makefile("rb").readline()
                    self.assertTrue(line.startswith(b":irc.board.internal 001"), line)
                else:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request("GET", "/")
                    self.assertEqual(conn.getresponse().status, 200)
                    conn.close()
                first = json.loads(log.read_text().splitlines()[0])
                self.assertEqual((first["kind"], first["channel"]), ("board_start", name))

    def test_http_write_stays_off_stdout_and_splits_content(self):
        # A write must produce a structural event in the log and a quarantined content
        # sink, with nothing printed to the channel process's stdout.
        proc, port, log = self.run_board("guestbook", stdout=subprocess.PIPE)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/guestbook", "author=me&message=hello",
                     {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 201)
        conn.close()
        time.sleep(0.2)
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(out, b"")  # no channel ever writes a body (or anything) to stdout
        posts = [json.loads(l) for l in log.read_text().splitlines() if json.loads(l)["kind"] == "post"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(set(posts[0]), POST_EVENT_KEYS)
        self.assertTrue(os.path.exists(content_sink_path(str(log))))  # content split off to the sink


class ReqLogContentSafetyTests(unittest.TestCase):
    """Agent-authored URL text must never reach the structural event log via a req/delete path.

    Write channels smuggle agent text through the URL: the GET-writable wiki's ?author=/?append=<body>,
    the kv /kv/<key>, the cache artifact path, the forum thread slug. The 'req'/'delete' path field is
    redacted to a static route template, so the quarantined board_events.jsonl stays content-free. The
    marker below is a neutral placeholder (never chatter); the test asserts its ABSENCE in the log file.
    """

    MARK = "ZxLEAKMARKERxZ"

    def _serve(self, name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "events.jsonl"
        events = EventLog(log)
        store = Store(SEED, events)
        servers = CHANNELS[name].serve(store, events, [0])

        def close():
            for s in servers:
                s.shutdown()
                s.server_close()
            events.close()

        self.addCleanup(close)
        return servers[0].server_address[1], log

    def _req(self, port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=body, headers={})
        resp = conn.getresponse()
        resp.read()
        conn.close()

    def _assert_log_clean(self, log):
        # The structural log (not the quarantined content sink) must not carry the marker anywhere.
        self.assertNotIn(self.MARK, log.read_text())

    def test_wiki_edit_query_not_logged(self):
        port, log = self._serve("wiki")
        self._req(port, "GET", f"/wiki/edit?page=Page{self.MARK}&author={self.MARK}&append={self.MARK}body")
        self._assert_log_clean(log)

    def test_kv_key_not_logged(self):
        port, log = self._serve("kv")
        self._req(port, "PUT", f"/kv/{self.MARK}", body=b"v")
        self._req(port, "GET", f"/kv/{self.MARK}")
        self._assert_log_clean(log)

    def test_cache_artifact_path_not_logged(self):
        port, log = self._serve("cache")
        self._req(port, "PUT", f"/artifactory/agent-cache/{self.MARK}/{self.MARK}.txt", body=b"v")
        self._req(port, "DELETE", f"/artifactory/agent-cache/{self.MARK}/{self.MARK}.txt")
        self._assert_log_clean(log)

    def test_code_comment_writes_not_logged(self):
        for name, path in (("js", "/app.js"), ("html", "/index.html")):
            with self.subTest(channel=name):
                port, log = self._serve(name)
                self._req(port, "POST", "/comment", body=f"author={self.MARK}&body={self.MARK}")
                self._req(port, "PUT", path, body=f"<!-- {self.MARK} -->\n// {self.MARK}\n")
                self._req(port, "GET", f"/{self.MARK}?{self.MARK}")
                self._assert_log_clean(log)
                kinds = [json.loads(l)["kind"] for l in log.read_text().splitlines()]
                self.assertEqual(kinds.count("post"), 2)

    def test_forum_slug_in_path_not_logged(self):
        port, log = self._serve("forum")
        self._req(port, "GET", f"/api/v1/threads/{self.MARK}")
        self._assert_log_clean(log)


if __name__ == "__main__":
    unittest.main()
