"""Loopback provider boundary for connection-control acceptance; never records headers."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProbeServer:
    def __init__(self):
        owner = self
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.mode = "pass"
        self._gates = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                data = json.dumps(
                    {"data": [{"id": "example-model"}, {"id": "another-model"}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                mode, release = owner.mode, owner.release
                owner.started.set()
                if mode == "wait":
                    release.wait(35)
                errors = {
                    "authentication": (401, "invalid_api_key"),
                    "permission": (403, "permission_denied"),
                    "model": (404, "model_not_found"),
                    "quota": (429, "insufficient_quota"),
                    "rate_limit": (429, "rate_limit_exceeded"),
                }
                if mode in errors:
                    status, code = errors[mode]
                    response = {
                        "error": {
                            "message": code + " fixture-private-key",
                            "type": code,
                            "code": code,
                        }
                    }
                else:
                    status = 200
                    message = (
                        {"role": "assistant", "content": "OK"}
                        if any(m.get("role") == "tool" for m in body["messages"])
                        else {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "probe",
                                    "type": "function",
                                    "function": {
                                        "name": "connection_check",
                                        "arguments": json.dumps(
                                            {"value": "ultrakb-connection-check"}
                                        ),
                                    },
                                }
                            ],
                        }
                    )
                    response = {
                        "id": "probe",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "stop" if message.get("content") else "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    }
                data = json.dumps(response).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def select(self, mode):
        self.release.set()
        self.release = threading.Event()
        self._gates.append(self.release)
        self.started.clear()
        self.mode = mode

    def close(self):
        for gate in [self.release, *self._gates]:
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
