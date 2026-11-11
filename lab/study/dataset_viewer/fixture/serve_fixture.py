#!/usr/bin/env python3
"""Serve the dataset viewer against the PLACEHOLDER fixture package (development only).

    python3 lab/study/dataset_viewer/fixture/serve_fixture.py [--port 8765]

Builds the placeholder package (make_fixture.py: synthetic lorem tree -> real builder) into a temp
dir, then serves it. Binds 127.0.0.1 only. Layout matches `lab/study/dataset.py serve`: the viewer
at /, the package files at /data/. Real packages are served by dataset.py, never by this script.
"""
import argparse
import http.server
import sys
import tempfile
from pathlib import Path

VIEWER = Path(__file__).resolve().parents[1]
PACKAGE = None   # set in main(): a temp dir holding the freshly built placeholder package
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
         ".json": "application/json", ".jsonl": "application/x-ndjson", ".md": "text/plain; charset=utf-8"}
VIEWER_FILES = {"/": "index.html", "/index.html": "index.html", "/viewer.js": "viewer.js", "/lib.js": "lib.js",
                "/viewer.css": "viewer.css"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):  # quiet
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/favicon.ico":  # no icon; answer quietly instead of a console-visible 404
            self.send_response(204)
            self.end_headers()
            return
        if path in VIEWER_FILES:
            f = VIEWER / VIEWER_FILES[path]
        elif path.startswith("/data/") and "/" not in path[6:] and not path[6:].startswith("."):
            f = PACKAGE / path[6:]
        else:
            f = None
        if f is None or not f.is_file():
            self.send_error(404)
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    global PACKAGE
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import make_fixture
    td = tempfile.TemporaryDirectory(prefix="dsviewer-fixture-")
    PACKAGE = Path(td.name) / "package"
    make_fixture.build(PACKAGE)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"http://127.0.0.1:{srv.server_address[1]}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
