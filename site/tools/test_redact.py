#!/usr/bin/env python3
"""Unit tests for tools/redact.py (python3 tools/test_redact.py, or python3 -m unittest tools/test_redact.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import Redactor, default_denylist, selftest  # noqa: E402


class RedactTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(deny=["krister", "hedfors", "MBPhostname"],
                          allow=["FLAG{5446e0d5f9c52e159097}", "ember-basalt-kelp", "0e25cf554b1f66eb", "vlt-953579"])

    def red(self, s):
        return self.r.redact(s)

    def test_host_paths(self):
        self.assertEqual(self.red("/Users/krister/dev/schizboard/runs/x"), "<host-path>/dev/schizboard/runs/x")
        self.assertEqual(self.red("/Users/user/.microsandbox/sandboxes/sb-x"), "<host-path>/.microsandbox/sandboxes/sb-x")
        self.assertEqual(self.red("C:\\Users\\bob\\x"), "<host-path>\\x")
        self.assertEqual(self.red("/private/var/folders/ab/cd/T/x"), "<host-path>/T/x")
        for keep in ("/home/agent/ctf", "/root/logs/steps.jsonl", "/tmp/x", "/var/tmp/submissions", "~/ctf/project",
                     "agent:x:1000:1000::/home/agent:/bin/bash", "ls -la /home/agent; ls"):
            self.assertEqual(self.red(keep), keep)
        self.assertEqual(self.red("cat /home/eve/.bashrc"), "cat <host-path>/.bashrc")

    def test_username(self):
        self.assertEqual(self.red("whoami -> user"), "whoami -> user")  # bare 'user' is never replaced
        self.assertEqual(self.red("Krister was here"), "<user> was here")
        self.assertEqual(self.red("ssh krister@host"), "ssh <user>@host")
        self.assertEqual(self.red("on MBPhostname"), "on <user>")
        self.assertNotIn("user", [d.lower() for d in default_denylist()])

    def test_email(self):
        self.assertEqual(self.red("a.b@gmail.com"), "<email>")
        for keep in ("ops@board.internal", "dev@vault.local", "x@y.test", "noreply@github.com"):
            self.assertEqual(self.red(keep), keep)

    def test_api_keys(self):
        for s in ("sk-ant-api03-abcdefghijklmnop1234", "ghp_" + "a1" * 12, "AKIAABCDEFGHIJKLMNOP",
                  "AIza" + "B" * 35, "hf_" + "x1Y" * 8, "eyJhbGciOi.eyJzdWIiOi.abcDEF123",
                  "xoxb-1234-abcd"):
            self.assertEqual(self.red(f"k {s} k"), "k <secret> k", s)
        self.assertEqual(self.red("Authorization: Bearer abcdefghijklmnop"), "Authorization: Bearer <secret>")
        self.assertEqual(self.red("API_KEY=hunter2hunter2"), "API_KEY=<secret>")
        self.assertEqual(self.red("password: s3cr3t!"), "password: <secret>")
        self.assertEqual(self.red("max_tokens: 2048, prompt_tokens=408"), "max_tokens: 2048, prompt_tokens=408")
        self.assertEqual(self.red("Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O"), "<secret>")

    def test_hex_and_ctf_survive(self):
        for keep in ("ccb83c0794516276bfc538649a96942f6352855c", "key_hex=0e25cf554b1f66eb",
                     "FLAG{5446e0d5f9c52e159097}", "passphrase ember-basalt-kelp", "KEY vlt-953579",
                     "/usr/lib/python3.12/site-packages/SomeModule/Thing.py",
                     "then+KEY+vlt-64e7b1+gave+28ef8d4f079cd84a+and+Reverse"):
            self.assertEqual(self.red(keep), keep)

    def test_ips(self):
        self.assertEqual(self.red("8.8.8.8 1.1.1.1"), "<ip> <ip>")
        for keep in ("127.0.0.2", "172.16.0.9", "172.16.0.13", "172.16.0.1", "0.0.0.0", "::1", "255.255.255.0",
                     "python 3.12.4", "1.2.3", "999.1.1.1", "13:42:07"):
            self.assertEqual(self.red(keep), keep)
        # host-LAN / other private ranges are not sandbox addresses
        for leak in ("10.1.2.3", "192.168.1.1", "169.254.1.1", "172.20.0.5", "172.16.1.9"):
            self.assertEqual(self.red(leak), "<ip>", leak)

    def test_review_probes(self):
        cases = {
            "/var/folders/8x/abcd1234/T/x": "<host-path>/T/x",
            "postgres://admin:hunter2pass@db:5432": "postgres://<user>:<secret>@db:5432",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==": "Authorization: Basic <secret>",
            "LLM_KEY=abc123def456": "LLM_KEY=<secret>",
            "key: 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c": "key: <secret>",
            "jane.doe@gmail.com": "<email>",
            "a basic understanding": "a basic understanding",
            "https://example.com/path": "https://example.com/path",
        }
        for src, want in cases.items():
            self.assertEqual(self.red(src), want, src)
        self.assertEqual(self.red("2001:db8::1 here"), "<ip6> here")

    def test_hosts(self):
        self.assertEqual(self.red("ping nas.lan; ssh mbp.local; x.home"), "ping <host>; ssh <host>; <host>")
        self.assertEqual(self.red("board.internal localhost"), "board.internal localhost")

    def test_mask(self):
        m = Redactor(mask={"FLAG{5446e0d5f9c52e159097}": "FLAG{5446…}", "ember-basalt-kelp": "⟨passphrase⟩",
                           "0e25cf554b1f66eb": "⟨key⟩", "vlt-953579": "⟨vault⟩"})
        self.assertEqual(m.redact("FLAG{5446e0d5f9c52e159097} ember-basalt-kelp 0e25cf554b1f66eb vlt-953579"),
                         "FLAG{5446…} ⟨passphrase⟩ ⟨key⟩ ⟨vault⟩")
        self.assertEqual(m.counts["ctf_mask"], 4)

    def test_counts_and_idempotence(self):
        r = Redactor(deny=["krister"])
        once = r.redact("/Users/krister/x 8.8.8.8 a@b.com")
        self.assertEqual(r.counts["host_path"], 1)
        self.assertEqual(r.counts["ip"], 1)
        self.assertEqual(r.counts["email"], 1)
        before = dict(r.counts)
        self.assertEqual(r.redact(once), once)
        self.assertEqual(dict(r.counts), before)

    def test_detect(self):
        r = Redactor(deny=["krister"])
        rules = {h[1] for h in r.detect("/Users/krister/x and 8.8.8.8")}
        self.assertIn("host_path", rules)
        self.assertIn("ip", rules)
        self.assertEqual(r.detect("all clear: 172.16.0.9 /home/agent"), [])

    def test_model_ids_pass_through(self):
        # the Berget model ids and labels (lab/config.MODELS) appear on every card and in index.models
        import importlib.util
        spec = importlib.util.spec_from_file_location("_lab_config_t", Path(__file__).resolve().parents[2] / "lab" / "config.py")
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        berget = [m for m in cfg.MODELS.values() if m["provider"] == "berget"]
        self.assertEqual(len(berget), 3)
        r = Redactor(deny=default_denylist())
        for m in cfg.MODELS.values():
            self.assertNotIn("api.berget.ai", m["label"])
            for v in (m["model"], m["label"], f"{m['model']} · {m['label']}"):
                self.assertEqual(r.redact(v), v, v)
                self.assertEqual(r.detect(v), [], v)
        self.assertEqual(sum(r.counts.values()), 0)

    def test_selftest(self):
        self.assertEqual(selftest(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
