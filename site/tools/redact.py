#!/usr/bin/env python3
"""Redaction rules for public swarmpit.dev bundles (SPEC section 4.8). Python 3 stdlib only.

Usage as a library:
    r = Redactor.from_env(deny=[...], allow=[ctf values], mask=None)
    clean = r.redact(text)          # counts hits per rule id in r.counts
    hits = r.detect(text)           # [(offset, rule_id, matched)] without changing anything (check_public.py)

CLI:
    python3 tools/redact.py --selftest
    echo 'text' | python3 tools/redact.py
"""
from __future__ import annotations

import getpass
import ipaddress
import math
import os
import re
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path

RULES = ("host_path", "username", "email", "api_key", "ip", "host", "ctf_mask")

# Words that are never treated as usernames even if they land on the denylist.
STOPLIST = frozenset("""
user users admin agent agents root guest model models board boards local home test tests default
system service public private shared ubuntu debian python runner worker server client sandbox vault
forum wiki cache irc data docs tools build public macbook laptop desktop mac pro air mini studio
""".split())

# ---------------------------------------------------------------------------------------------- rule 1
# /Users/<x>/..., /home/<x>/... (x != agent), [/private]/var/folders/..., C:\Users\<x>\...
_HOST_PATH = re.compile(
    r"(?P<p>/Users/(?P<u1>[^/\s\"'`<>]+)"
    r"|/home/(?!agent(?![\w.-]))(?P<u2>[^/\s\"'`<>:;]+)"
    r"|(?:/private)?/var/folders(?:/[^/\s\"'`<>]+){0,2}"
    r"|[A-Za-z]:\\\\?Users\\\\?(?P<u3>[^\\\s\"'`<>]+))"
)

# ---------------------------------------------------------------------------------------------- rule 3
_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])")
_EMAIL_KEEP_DOMAIN = re.compile(r"(?:^|\.)(?:internal|local|test)$", re.I)

# ---------------------------------------------------------------------------------------------- rule 4
_KEY_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}"
    r"|sk_[A-Za-z0-9]{32,}"          # ElevenLabs-style sk_<hex>
    r"|gh[pousr]_\w{20,}"
    r"|github_pat_\w+"
    r"|xox[abprs]-[\w-]+"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[\w-]{35}"
    r"|hf_\w{20,}"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+(\S{12,})")
# HTTP Basic credentials: "Authorization: Basic <b64>" or a capitalised "Basic <b64>" token with b64 shape
_BASIC = re.compile(r"(?:(?i:authorization)\s*:\s*)?\b(Basic|BASIC|basic)\s+([A-Za-z0-9+/]{8,}={0,2})(?![\w+/=])")
# URL userinfo: scheme://user:password@host
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)([^/\s:@\"'`<>]+):([^/\s@\"'`<>]+)@")
# generic *key / *_KEY variables holding a 12+ char token that contains a digit
_KV_KEY = re.compile(r"(?i)\b([\w-]*key)(\s*[=:]\s*)([\"']?)([A-Za-z0-9_+/=-]{12,})(?![A-Za-z0-9_+/=-])")
_KV = re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|credential)[\w-]*)(\s*[=:]\s*)([^\s\"'`,;)}\]]+)")
_GENERIC = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}(?![A-Za-z0-9+/_-])")
_HEX = re.compile(r"^[0-9a-fA-F]+$")

# ---------------------------------------------------------------------------------------------- rule 5
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")
_IPV6 = re.compile(
    r"(?<![\w:.])("
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,6}:(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}"
    r"|::1"
    r")(?![\w:])"
)

# ---------------------------------------------------------------------------------------------- rule 6
_HOST = re.compile(r"(?<![\w.@-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:local|lan|home))(?![\w-])", re.I)
_HOST_KEEP = {"board.internal", "localhost"}


def _entropy(s: str) -> float:
    c = Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def _mixed(s: str) -> bool:
    return any(ch.isupper() for ch in s) and any(ch.islower() for ch in s) and any(ch.isdigit() for ch in s)


def _is_secretish(tok: str) -> bool:
    if _HEX.match(tok):
        return False
    if not _mixed(tok) or _entropy(tok) <= 4.0:
        return False
    if "/" in tok or "+" in tok:
        # path-like (/usr/lib/python3.12/...) or form-encoded (a+b+c) runs are not secrets unless one
        # segment itself looks random
        segs = [s for s in re.split(r"[/+]", tok) if s]
        best = max(segs, key=len) if segs else ""
        if len(best) < 20 or not _mixed(best) or _entropy(best) <= 3.6:
            return False
    return True


# Only the microsandbox subnet is public-safe (raw runs use 172.16.0.1/.9/.13); every other private/LAN
# address could describe the exporting host's network. Override with SWARMPIT_KEEP_NETS="172.16.0.0/24,...".
_KEEP_NETS = [ipaddress.ip_network(n.strip()) for n in
              os.environ.get("SWARMPIT_KEEP_NETS", "172.16.0.0/24").split(",") if n.strip()]


def _ip_kept(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return True
    if ip.version == 4:
        if ip in ipaddress.ip_network("127.0.0.0/8") or str(ip) == "0.0.0.0":
            return True
        if any(ip in n for n in _KEEP_NETS if n.version == 4):
            return True
        # netmasks / broadcast / multicast (255.255.255.0, 224.0.0.1) describe nothing about the host
        return ip.is_reserved or ip.is_multicast or str(ip).startswith("255.")
    return ip.is_loopback


def default_denylist(extra: list[str] | None = None) -> list[str]:
    """Names of the exporting host user, from env/git/hostname (+ --deny)."""
    names: set[str] = set()

    def add(v: str | None):
        if not v:
            return
        for tok in re.split(r"[\s._@-]+", v.strip()):
            if len(tok) >= 3:
                names.add(tok)

    add(os.environ.get("USER"))
    try:
        add(getpass.getuser())
    except Exception:
        pass
    add(Path.home().name)
    for args in (["id", "-un"], ["git", "config", "user.name"]):
        try:
            add(subprocess.run(args, capture_output=True, text=True, timeout=5).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=5).stdout.strip()
        if "@" in email:
            add(email.split("@")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        hn = socket.gethostname().split(".")[0]
        if hn:
            names.add(hn)
            add(hn)
    except OSError:
        pass
    for e in extra or []:
        add(e)
        if e:
            names.add(e)
    # the bare word 'user' is never replaced (the sandbox host user is literally 'user')
    return sorted(n for n in names if n.lower() != "user")


class Redactor:
    def __init__(self, deny: list[str] | None = None, allow: list[str] | None = None,
                 mask: dict[str, str] | None = None):
        self.counts: Counter = Counter()
        self.allow = [a for a in (allow or []) if a]
        self.mask = {k: v for k, v in (mask or {}).items() if k}
        deny = [d for d in (deny or []) if d and d.lower() != "user"]
        self.deny = deny
        alt = "|".join(re.escape(d) for d in sorted(deny, key=len, reverse=True))
        # path context: after /Users/, /home/, \Users\, or as name@host
        self._deny_path = re.compile(rf"(?i)(/Users/|/home/|\\Users\\)({alt})(?![\w])|(?<![\w.])({alt})(?=@[A-Za-z0-9])") if alt else None
        loose = [d for d in deny if len(d) > 4 and d.lower() not in STOPLIST]
        lalt = "|".join(re.escape(d) for d in sorted(loose, key=len, reverse=True))
        self._deny_loose = re.compile(rf"(?i)(?<![A-Za-z0-9])({lalt})(?![A-Za-z0-9])") if lalt else None

    @classmethod
    def from_env(cls, deny: list[str] | None = None, **kw) -> "Redactor":
        return cls(deny=default_denylist(deny), **kw)

    # --------------------------------------------------------------------------------------- helpers
    def _allowed(self, s: str) -> bool:
        return any(a in s or s in a for a in self.allow if len(s) >= 6)

    def _sub(self, rule: str, pat: re.Pattern, text: str, fn) -> str:
        def wrap(m):
            out = fn(m)
            if out is None or out == m.group(0):
                return m.group(0)
            self.counts[rule] += 1
            return out
        return pat.sub(wrap, text)

    # --------------------------------------------------------------------------------------- rules
    def _r_host_path(self, t: str) -> str:
        return self._sub("host_path", _HOST_PATH, t, lambda m: "<host-path>")

    def _r_username(self, t: str) -> str:
        if self._deny_path:
            def f(m):
                if m.group(1):
                    return m.group(1) + "<user>"
                return "<user>"
            t = self._sub("username", self._deny_path, t, f)
        if self._deny_loose:
            t = self._sub("username", self._deny_loose, t, lambda m: "<user>")
        return t

    def _r_email(self, t: str) -> str:
        def f(m):
            local, dom = m.group(1), m.group(2)
            if _EMAIL_KEEP_DOMAIN.search(dom) or local.lower() == "noreply":
                return None
            return "<email>"
        return self._sub("email", _EMAIL, t, f)

    def _r_api_key(self, t: str) -> str:
        t = self._sub("api_key", _KEY_PREFIXED, t, lambda m: None if self._allowed(m.group(0)) else "<secret>")
        t = self._sub("api_key", _BEARER, t, lambda m: None if self._allowed(m.group(2)) else f"{m.group(1)} <secret>")

        def kv(m):
            v = m.group(3)
            if (v.startswith("<") and v.endswith(">")) or re.fullmatch(r"[\d.,]+|true|false|null|none|\$\w+|[\[{(][\d.,-]*|<.*", v, re.I):
                return None
            if self._allowed(v):
                return None
            return f"{m.group(1)}{m.group(2)}<secret>"
        t = self._sub("api_key", _KV, t, kv)
        t = self._sub("api_key", _URL_CRED, t,
                      lambda m: None if m.group(3).startswith("<") else f"{m.group(1)}<user>:<secret>@")
        t = self._sub("api_key", _BASIC, t,
                      lambda m: None if self._allowed(m.group(2)) or not (_mixed(m.group(2)) or m.group(2).endswith("="))
                      else m.group(0)[: m.start(2) - m.start(0)] + "<secret>")

        def kvk(m):
            v = m.group(4)
            if not any(ch.isdigit() for ch in v) or self._allowed(v):
                return None
            return f"{m.group(1)}{m.group(2)}{m.group(3)}<secret>"
        t = self._sub("api_key", _KV_KEY, t, kvk)
        def gen(m):
            tok, pre = m.group(0), ""
            # a literal escape like "\nZ5KD..." (\n, \t, \r) is not part of the token: keep the letter
            if m.start() > 0 and m.string[m.start() - 1] == "\\" and tok[0] in "ntr":
                pre, tok = tok[0], tok[1:]
            if self._allowed(tok) or self._allowed(m.group(0)):
                return None
            if len(tok) < 32:  # too short without the escape letter: judge the whole run as before
                return "<secret>" if _is_secretish(m.group(0)) else None
            return pre + "<secret>" if _is_secretish(tok) else None
        t = self._sub("api_key", _GENERIC, t, gen)
        return t

    def _r_ip(self, t: str) -> str:
        def f4(m):
            octs = [int(x) for x in m.groups()]
            if any(o > 255 for o in octs):
                return None
            return None if _ip_kept(m.group(0)) else "<ip>"
        t = self._sub("ip", _IPV4, t, f4)

        def f6(m):
            s = m.group(1)
            if s.count(":") < 2:
                return None
            groups = [g for g in s.split(":") if g]
            if "::" in s and len(groups) < 2 and s != "::1":
                return None
            try:
                ipaddress.IPv6Address(s)
            except ValueError:
                return None
            return None if _ip_kept(s) else "<ip6>"
        return self._sub("ip", _IPV6, t, f6)

    def _r_host(self, t: str) -> str:
        return self._sub("host", _HOST, t, lambda m: None if m.group(1).lower() in _HOST_KEEP else "<host>")

    def _r_ctf_mask(self, t: str) -> str:
        for k in sorted(self.mask, key=len, reverse=True):
            n = t.count(k)
            if n:
                self.counts["ctf_mask"] += n
                t = t.replace(k, self.mask[k])
        return t

    # --------------------------------------------------------------------------------------- API
    def redact(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        t = text
        t = self._r_host_path(t)
        t = self._r_email(t)
        t = self._r_username(t)
        t = self._r_api_key(t)
        t = self._r_ip(t)
        t = self._r_host(t)
        if self.mask:
            t = self._r_ctf_mask(t)
        return t

    def detect(self, text: str) -> list[tuple[int, str, str]]:
        """Return residual leak hits (offset, rule, match) for rules 1-6 without counting."""
        hits: list[tuple[int, str, str]] = []
        saved = self.counts
        for rule, fn in (("host_path", self._r_host_path), ("email", self._r_email), ("username", self._r_username),
                         ("api_key", self._r_api_key), ("ip", self._r_ip), ("host", self._r_host)):
            self.counts = Counter()
            # find spans by diffing per-pattern matches: re-run the rule on the text and compare
            out = fn(text)
            if out != text and self.counts[rule]:
                # locate the first differing offset for reporting
                i = 0
                while i < min(len(out), len(text)) and out[i] == text[i]:
                    i += 1
                hits.append((i, rule, text[i:i + 60]))
        self.counts = saved
        return hits


# ------------------------------------------------------------------------------------------------ selftest
def selftest() -> int:
    r = Redactor(deny=["krister", "hedfors", "MBPexample"], allow=["FLAG{5446e0d5f9c52e159097}", "ember-basalt-kelp"])
    cases = [
        ("/Users/krister/dev/x/y.py", "<host-path>/dev/x/y.py"),
        ("ls /home/agent/ctf /root /tmp", "ls /home/agent/ctf /root /tmp"),
        ("ls -la /home/agent; agent:x:1000:1000::/home/agent:/bin/bash", "ls -la /home/agent; agent:x:1000:1000::/home/agent:/bin/bash"),
        ("Author: dev <dev@vault.local>", "Author: dev <dev@vault.local>"),
        ("cat /home/bob/.ssh/id", "cat <host-path>/.ssh/id"),
        ("user@sandbox:~$ whoami -> user", "user@sandbox:~$ whoami -> user"),
        ("ping krister@laptop", "ping <user>@laptop"),
        ("signed by Krister Hedfors", "signed by <user> <user>"),
        ("mail me at alice@example.com or ops@board.internal", "mail me at <email> or ops@board.internal"),
        ("noreply@github.com", "noreply@github.com"),
        ("key sk-ant-api03-AbCdEf0123456789xyzXYZ", "key <secret>"),
        ("token=abcdef123456 max_tokens: 2048", "token=<secret> max_tokens: 2048"),
        ("Authorization: Bearer abcdefghijklmnopqrstuv", "Authorization: Bearer <secret>"),
        ("commit ccb83c0794516276bfc538649a96942f6352855c", "commit ccb83c0794516276bfc538649a96942f6352855c"),
        ("blob Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O", "blob <secret>"),
        ("/usr/lib/python3.12/site-packages/SomeModule/Thing.py", "/usr/lib/python3.12/site-packages/SomeModule/Thing.py"),
        ("dns 8.8.8.8 board 172.16.0.9 lo 127.0.0.2", "dns <ip> board 172.16.0.9 lo 127.0.0.2"),
        ("version 1.2.3 and 3.12.4.999", "version 1.2.3 and 3.12.4.999"),
        ("v6 2001:db8::8a2e:370:7334 and ::1", "v6 <ip6> and ::1"),
        ("time 13:42:07", "time 13:42:07"),
        ("body=then+KEY+vlt-64e7b1+gave+28ef8d4f079cd84a+and+Reverse", "body=then+KEY+vlt-64e7b1+gave+28ef8d4f079cd84a+and+Reverse"),
        ("host printer.local and board.internal localhost", "host <host> and board.internal localhost"),
        ("flag FLAG{5446e0d5f9c52e159097} passphrase ember-basalt-kelp", "flag FLAG{5446e0d5f9c52e159097} passphrase ember-basalt-kelp"),
        ("on MBPexample now", "on <user> now"),
        ("ssh 192.168.1.23", "ssh <ip>"),
        ("gw 10.0.0.5", "gw <ip>"),
        ("netmask 255.255.255.0", "netmask 255.255.255.0"),
        ("/var/folders/8x/abcd1234/T/x", "<host-path>/T/x"),
        ("postgres://admin:hunter2pass@db:5432", "postgres://<user>:<secret>@db:5432"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "Authorization: Basic <secret>"),
        ("a basic understanding of things", "a basic understanding of things"),
        ("LLM_KEY=abc123def456", "LLM_KEY=<secret>"),
        ("key: 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c", "key: <secret>"),
        ("primary_key: customer_identifier", "primary_key: customer_identifier"),
        ("jane.doe@gmail.com", "<email>"),
        ("id=vlt-1\\nZm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O", "id=vlt-1\\n<secret>"),
    ]
    bad = 0
    for src, want in cases:
        got = r.redact(src)
        if got != want:
            bad += 1
            print(f"FAIL: {src!r}\n   got  {got!r}\n   want {want!r}")
    m = Redactor(mask={"FLAG{5446e0d5f9c52e159097}": "FLAG{5446…}", "ember-basalt-kelp": "⟨passphrase⟩"})
    got = m.redact("FLAG{5446e0d5f9c52e159097} ember-basalt-kelp")
    if got != "FLAG{5446…} ⟨passphrase⟩":
        bad += 1
        print("FAIL mask", got)
    # idempotence
    for src, _ in cases:
        once = r.redact(src)
        if r.redact(once) != once:
            bad += 1
            print("FAIL idempotent", src)
    print("redact selftest:", "OK" if not bad else f"{bad} failure(s)", f"({len(cases)} cases)")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    r = Redactor.from_env()
    sys.stdout.write(r.redact(sys.stdin.read()))
    print(dict(r.counts), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
