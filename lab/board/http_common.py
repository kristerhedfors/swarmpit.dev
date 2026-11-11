"""HTTP plumbing shared by the forum, wiki and cache interfaces.

Each agent gets its own listening port; the server object carries that port's index
(agent_id), so every request event is attributed without trusting anything the agent sends.
Responses are HTTP/1.0 with an nginx Server header (the cache overrides it) so the board looks
like an ordinary internal service.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = "0.0.0.0"
PUBLIC_ORIGIN = "http://board.internal:8080"  # how agents address the board
MAX_BODY = 1_000_000
LOGGED_PATH_CHARS = 500
LOGGED_UA_CHARS = 120


def emit(events, agent, kind, **fields):
    """Write one structural event, tolerating either EventLog API name.

    The isolation partition splits ``events.py`` into a structural ``log_event``
    plus a quarantined content sink; until then the writer is ``log``. Channels
    and the CLI route every direct emission through here so both work.
    """
    (getattr(events, "log_event", None) or events.log)(agent, kind, **fields)


class BoardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_cls, agent_id, store, events):
        super().__init__(address, handler_cls)
        self.agent_id, self.store, self.events = agent_id, store, events


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "nginx/1.24.0"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    @property
    def agent(self):
        return self.server.agent_id

    @property
    def store(self):
        return self.server.store

    def log(self, kind, **fields):
        emit(self.server.events, self.agent, kind, **fields)

    def read_body(self):
        """Request body, accepting chunked uploads (curl -T - sends them), capped at MAX_BODY."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = b""
            while len(data) < MAX_BODY:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if not size:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return data
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, MAX_BODY)) if n else b""

    def logged_path(self):
        """The content-free routing target recorded in the structural "req" event.

        The query string is dropped here, so the write channels that smuggle adversarial board text
        through the URL query (the GET-writable wiki's ``?author=``/``?append=<body>``) never reach
        board_events.jsonl. Channels that instead carry agent-chosen text in a PATH segment -- the kv
        ``/kv/<key>``, the cache artifact path, the forum thread slug, a wiki page name -- override this
        to collapse that segment to its static route template. The structural log is the quarantined
        record of record (PROTOCOL.md 7.1), so nothing agent-authored may ride in via the request line.
        """
        return self.path.split("?", 1)[0].split("#", 1)[0][:LOGGED_PATH_CHARS]

    def reply(self, code, body, ctype="text/plain; charset=utf-8"):
        """Send a complete response and log it as a "req" event."""
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.log("req", method=self.command, path=self.logged_path(), status=code, bytes=len(data),
                 ua=self.headers.get("User-Agent", "")[:LOGGED_UA_CHARS])

    def reply_json(self, code, obj):
        self.reply(code, json.dumps(obj, indent=2), "application/json")

    def do_HEAD(self):
        self.do_GET()


def start_http_servers(handler_cls, store, events, ports, host=LISTEN_HOST):
    """Serve handler_cls on each port (index = agent id) from daemon threads; returns the servers."""
    servers = []
    for agent_id, port in enumerate(ports):
        srv = BoardHTTPServer((host, port), handler_cls, agent_id, store, events)
        srv.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv.thread.start()
        servers.append(srv)
    return servers


def serve_http(handler_cls, store, events, ports):
    """Serve until the process is killed."""
    for srv in start_http_servers(handler_cls, store, events, ports):
        srv.thread.join()


# --- channel base classes -------------------------------------------------------
# They live here (not in channel.py) so a leaf channel module can import its base
# without triggering channel.py's registry build, which imports the leaves back.

# Host agents address the board at; the per-agent listen ports (9000+) are passed
# to serve() separately. default_port is the protocol-typical port in the URL.
BOARD_HOST = "board.internal"


class Channel:
    """Base record. Subclasses set the class attributes and implement serve()."""

    name = ""          # registry key; the --interface value and the iface tag on every event
    label = ""         # human string (absorbs config.IFACE_LABEL)
    scheme = "http"    # "http" | "irc" | ...
    default_port = 0   # protocol-typical port the agent addresses (absorbs board_port_for)
    entry_path = "/"   # path component of the agent-facing URL
    pilot = False      # in the experiment grid (config.INTERFACES) vs an opt-in extra

    def entry_url(self):
        """The agent-facing URL (absorbs config.BOARD_URL[name])."""
        return f"{self.scheme}://{BOARD_HOST}:{self.default_port}{self.entry_path}"

    def seed_fields(self):
        """Optional per-post seed keys this channel understands (e.g. cache 'path')."""
        return ()

    def serve(self, store, events, ports):
        raise NotImplementedError


class HttpChannel(Channel):
    """An HTTP interface: one BaseHandler subclass served on each agent port."""

    handler_cls = None

    def serve(self, store, events, ports):
        # Non-blocking: daemon threads, one per port. Returns the servers so the
        # CLI can join them; tests start/stop them directly.
        return start_http_servers(self.handler_cls, store, events, ports)


class StreamChannel(Channel):
    """An asyncio line protocol (irc). serve() blocks until the process is killed."""

    scheme = "irc"

    def serve(self, store, events, ports):
        import asyncio

        asyncio.run(self._main(store, events, ports))

    async def _main(self, store, events, ports):
        raise NotImplementedError
