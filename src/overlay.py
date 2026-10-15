"""
overlay.py — local web server for the OBS browser source.

Serves overlay/ over http://127.0.0.1:<port>/ and pushes bee events to the page
with Server-Sent Events (/events). Stdlib only — no extra dependencies.
"""

import json
import mimetypes
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class OverlayServer:
    def __init__(self, folder, port=8765, settings=None):
        self.folder = Path(folder).resolve()
        self.port = int(port)
        self.settings = settings or {}
        self._clients = set()
        self._lock = threading.Lock()
        self._httpd = None

    def send(self, event: dict):
        """Broadcast one event to every connected overlay page."""
        data = json.dumps(event)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # a stalled page just misses events

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/events":
                    return self._sse()
                if path == "/config.json":
                    return self._send(200, json.dumps(server.settings).encode(),
                                      "application/json")
                if path == "/":
                    path = "/index.html"
                target = (server.folder / path.lstrip("/")).resolve()
                if server.folder not in target.parents or not target.is_file():
                    return self.send_error(404)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype)

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")   # page opened as a local file
                self.end_headers()
                self.wfile.write(body)

            def _sse(self):
                q = queue.Queue(maxsize=100)
                with server._lock:
                    server._clients.add(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(b": hello\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            msg = f"data: {q.get(timeout=15)}\n\n"
                        except queue.Empty:
                            msg = ": ping\n\n"  # keeps the connection alive
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server._lock:
                        server._clients.discard(q)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        print(f"[Overlay] browser source URL: http://127.0.0.1:{self.port}/")

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
