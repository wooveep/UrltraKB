"""A real loopback HTTP server that can only emit model endpoint errors.

No successful completion, token usage, or SDK result is fabricated here.
"""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ErrorTransport:
    def __init__(self, status=400, *, body=None, headers=None, gate=None):
        if type(status) is not int or not 400 <= status < 600:
            raise ValueError("The local error endpoint only permits HTTP 400–599")
        self.status = status
        self.body = copy.deepcopy(
            body
            if body is not None
            else {
                "error": {
                    "message": f"Local error endpoint returned HTTP {status}.",
                    "type": "offline_error",
                    "code": f"local_http_{status}",
                }
            }
        )
        self.response_headers = dict(headers or {})
        self.gate = gate
        self.requests = []
        self._received = threading.Condition()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                recorded = {
                    "method": "POST",
                    "path": self.path,
                    "headers": dict(self.headers),
                    "json": json.loads(payload),
                }
                with owner._received:
                    owner.requests.append(recorded)
                    owner._received.notify_all()
                if owner.gate is not None:
                    owner.gate.wait(5)
                content = json.dumps(owner.body).encode()
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    for name, value in owner.response_headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Timed-out/cancelled clients can close before our error is delivered.

            def log_message(self, *_):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def wait_for_requests(self, count, timeout=3):
        with self._received:
            return self._received.wait_for(lambda: len(self.requests) >= count, timeout)

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
