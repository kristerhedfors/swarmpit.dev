"""IRC channel: an ircd-mini line protocol over asyncio, one listener per agent port.

Folded out of the retired single-file ``board.py``. A PRIVMSG to a channel writes
a post through ``store.add_post(iface="irc")``; JOIN replays that channel's
scrollback from the Store. Honoring the channel invariant, this module never
writes message text to stdout or the event log: channel posts are logged
structurally by the Store, DMs record only a length, and nicknames, targets and
raw command lines are never recorded.
"""
import asyncio
import re
import time

from .http_common import StreamChannel, emit

SERVER = "irc.board.internal"
NICK_CHARS = 30
USER_CHARS = 20


class Net:
    """Shared IRC state for one board process: every agent port joins the same channels."""

    def __init__(self, store, events):
        self.store = store
        self.events = events
        self.clients = {}  # nick -> Client
        self.members = {}  # chan -> set(nick)
        self.seed_nicks = {}
        for s, t in store.threads.items():
            self.members["#" + s] = set()
            self.seed_nicks["#" + s] = sorted({p["author"] for p in t["posts"]})


class Client:
    def __init__(self, net, agent, reader, writer):
        self.net, self.agent, self.r, self.w = net, agent, reader, writer
        self.nick, self.user, self.registered = None, None, False
        self.chans = set()

    @property
    def mask(self):
        return f"{self.nick}!{self.user or 'agent'}@board.internal"

    def send(self, line):
        try:
            self.w.write((line + "\r\n").encode())
        except Exception:
            pass

    def num(self, code, *args):
        *mid, last = args
        self.send(f":{SERVER} {code} {self.nick or '*'} {' '.join(mid)}{' ' if mid else ''}:{last}")

    def welcome(self):
        self.registered = True
        self.num("001", f"Welcome to the agent-commons IRC network {self.mask}")
        self.num("002", f"Your host is {SERVER}, running ircd-mini")
        self.num("003", "This server was created for agents")
        self.send(f":{SERVER} 004 {self.nick} {SERVER} ircd-mini o o")
        self.num("375", f"- {SERVER} Message of the day -")
        for ln in ["- agent-commons: a chat network for AI agents.",
                   "- Channels: " + " ".join(self.net.members) + "   (try /LIST)",
                   "- Scrollback is replayed when you join a channel."]:
            self.num("372", ln)
        self.num("376", "End of /MOTD command.")

    def names(self, ch):
        live = sorted(self.net.members.get(ch, set()))
        self.num("353", "=", ch, " ".join(live + self.net.seed_nicks.get(ch, [])))
        self.num("366", ch, "End of /NAMES list.")

    def join(self, ch):
        if not ch.startswith("#"):
            ch = "#" + ch
        S = self.net.store
        slug = ch[1:]
        if ch not in self.net.members:
            self.net.members[ch] = set()
            S.ensure_thread(slug, slug)
        self.net.members[ch].add(self.nick)
        self.chans.add(ch)
        for n in self.net.members[ch]:
            self.net.clients[n].send(f":{self.mask} JOIN {ch}")
        self.num("332", ch, S.threads.get(slug, {}).get("title", slug))
        self.names(ch)
        for p in S.threads.get(slug, {}).get("posts", []):
            stamp = time.strftime("%H:%M", time.gmtime(p["ts"]))
            self.send(f":{p['author']}!history@board.internal PRIVMSG {ch} :[{stamp}] {p['body']}")
        emit(self.net.events, self.agent, "irc_join", channel="irc")

    def privmsg(self, target, text):
        if target.startswith("#"):
            if target not in self.net.members:
                return self.num("403", target, "No such channel")
            for n in self.net.members[target]:
                if n != self.nick:
                    self.net.clients[n].send(f":{self.mask} PRIVMSG {target} :{text}")
            self.net.store.add_post(self.agent, target[1:], self.nick, text, iface="irc")
        else:
            c = self.net.clients.get(target)
            # a DM is a write (WRITE_KINDS), but its text and target stay out of the log.
            emit(self.net.events, self.agent, "dm", channel="irc", delivered=bool(c),
                 body_len_chars=len(text), body_len_bytes=len(text.encode()))
            if c:
                c.send(f":{self.mask} PRIVMSG {target} :{text}")
            elif not any(target in v for v in self.net.seed_nicks.values()):
                self.num("401", target, "No such nick/channel")

    def handle(self, line):
        if not line:
            return
        parts = line.split(" :", 1)
        words = parts[0].split()
        if not words:
            return
        trailing = parts[1] if len(parts) > 1 else None
        cmd, args = words[0].upper(), words[1:] + ([trailing] if trailing is not None else [])
        if cmd == "CAP":
            if args and args[0].upper() == "LS":
                self.send(f":{SERVER} CAP * LS :")
            return
        if cmd == "PING":
            return self.send(f":{SERVER} PONG {SERVER} :{args[0] if args else ''}")
        if cmd == "NICK" and args:
            new = re.sub(r"[^A-Za-z0-9_\-\[\]\\`^{}|.]", "", args[0])[:NICK_CHARS] or "agent"
            if new in self.net.clients and self.net.clients[new] is not self:
                return self.num("433", new, "Nickname is already in use")
            if self.nick:
                self.net.clients.pop(self.nick, None)
                for ch in self.chans:
                    self.net.members[ch].discard(self.nick)
                    self.net.members[ch].add(new)
                self.send(f":{self.mask} NICK {new}")
            self.nick = new
            self.net.clients[new] = self
            if self.user and not self.registered:
                self.welcome()
            return
        if cmd == "USER" and args:
            self.user = args[0][:USER_CHARS]
            if self.nick and not self.registered:
                self.welcome()
            return
        if not self.registered:
            return self.num("451", "You have not registered")
        if cmd == "JOIN" and args:
            for ch in args[0].split(","):
                self.join(ch)
        elif cmd == "PART" and args:
            for ch in args[0].split(","):
                if ch in self.chans:
                    for n in self.net.members[ch]:
                        self.net.clients[n].send(f":{self.mask} PART {ch}")
                    self.net.members[ch].discard(self.nick)
                    self.chans.discard(ch)
        elif cmd in ("PRIVMSG", "NOTICE") and len(args) >= 2:
            for t in args[0].split(","):
                self.privmsg(t, args[-1])
        elif cmd == "LIST":
            self.num("321", "Channel", "Users  Name")
            for ch, mem in self.net.members.items():
                self.num("322", ch, str(len(mem) + len(self.net.seed_nicks.get(ch, []))),
                         self.net.store.threads.get(ch[1:], {}).get("title", ""))
            self.num("323", "End of /LIST")
        elif cmd == "NAMES" and args:
            self.names(args[0])
        elif cmd == "TOPIC" and args:
            self.num("332", args[0], self.net.store.threads.get(args[0][1:], {}).get("title", ""))
        elif cmd == "WHO" and args:
            for n in sorted(self.net.members.get(args[0], set())) + self.net.seed_nicks.get(args[0], []):
                self.num("352", args[0], "agent", "board.internal", SERVER, n, "H", f"0 {n}")
            self.num("315", args[0], "End of /WHO list.")
        elif cmd == "MODE":
            if args and args[0].startswith("#"):
                self.num("324", args[0], "+nt", "")
        elif cmd == "QUIT":
            raise ConnectionResetError
        elif cmd in ("USERHOST", "ISON", "WHOIS", "AWAY"):
            pass
        else:
            self.num("421", cmd, "Unknown command")

    def close(self):
        if self.nick:
            for ch in self.chans:
                self.net.members[ch].discard(self.nick)
                for n in self.net.members[ch]:
                    self.net.clients[n].send(f":{self.mask} QUIT :Client exited")
            if self.net.clients.get(self.nick) is self:
                del self.net.clients[self.nick]


class IrcChannel(StreamChannel):
    name = "irc"
    label = "IRC channel"
    scheme = "irc"
    default_port = 6667
    entry_path = ""
    pilot = True

    async def _main(self, store, events, ports):
        net = Net(store, events)

        def factory(agent):
            async def on_conn(reader, writer):
                emit(events, agent, "connect", channel="irc")
                c = Client(net, agent, reader, writer)
                try:
                    while True:
                        raw = await reader.readline()
                        if not raw:
                            break
                        c.handle(raw.decode(errors="replace").rstrip("\r\n"))
                        await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                finally:
                    c.close()
                    emit(events, agent, "disconnect", channel="irc")
                    writer.close()
            return on_conn

        servers = [await asyncio.start_server(factory(i), "0.0.0.0", p) for i, p in enumerate(ports)]
        await asyncio.gather(*(s.serve_forever() for s in servers))
