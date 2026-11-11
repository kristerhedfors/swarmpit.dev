"""vaultd-like line protocol (placeholder values only): banner naming the server, AUTH <x>, KEY <id>.
    python3 lined.py <port> [name]"""
import socketserver
import sys

NAME = sys.argv[2] if len(sys.argv) > 2 else "x"


class H(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(f"lined {NAME} AUTH REQUIRED\n".encode())
        if self.rfile.readline().strip() != b"AUTH placeholder-pass":
            return self.wfile.write(b"ERR bad passphrase\n")
        self.wfile.write(b"OK\n")
        if self.rfile.readline().strip() == b"KEY vlt-000000":
            self.wfile.write(b"KEY 00112233\n")


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
