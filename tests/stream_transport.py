"""Scripted provider-boundary SSE responses; the real HTTP and SDK paths consume them."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Thread


class StreamTransport:
    def __init__(self, responses, *, gate=None, body_gate=None, usage_events=None, statuses=None):
        self.responses = responses
        self.requests = []
        self.bodies = []
        self.gate = gate
        self.body_gate = body_gate
        self.usage_events = usage_events
        self.received = Condition()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                index = len(owner.requests)
                request = json.loads(payload)
                with owner.received:
                    owner.requests.append(request)
                    owner.received.notify_all()
                if owner.gate is not None:
                    owner.gate.wait(5)
                response = (
                    owner.responses(request, index)
                    if callable(owner.responses)
                    else owner.responses[index]
                )
                status = statuses[index] if statuses is not None else 200
                if status >= 400:
                    body = json.dumps(response).encode()
                    owner.bodies.append(body)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                base = {
                    "id": f"chatcmpl-script-{index}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "gpt-4o-mini",
                }
                chunks = [
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", **response},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls"
                                if "tool_calls" in response
                                else "stop",
                            }
                        ],
                    },
                    {
                        **base,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 7,
                            "prompt_tokens_details": {"cached_tokens": 0},
                            "completion_tokens_details": {"reasoning_tokens": 0},
                        },
                    },
                ]
                if owner.usage_events is not None:
                    chunks = chunks[:2] + [
                        {**base, "choices": [], "usage": usage} for usage in owner.usage_events
                    ]
                streaming = request.get("stream", False)
                body = (
                    (
                        b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
                        + b"data: [DONE]\n\n"
                    )
                    if streaming
                    else json.dumps(
                        {
                            **base,
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", **response},
                                    "finish_reason": "tool_calls"
                                    if "tool_calls" in response
                                    else "stop",
                                }
                            ],
                            **({"usage": chunks[-1]["usage"]} if "usage" in chunks[-1] else {}),
                        }
                    ).encode()
                )
                owner.bodies.append(body)
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/event-stream" if streaming else "application/json"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    first, rest = body.split(b"\n\n", 1) if streaming else (body, b"")
                    self.wfile.write(first + (b"\n\n" if streaming else b""))
                    self.wfile.flush()
                    if owner.body_gate is not None:
                        owner.body_gate.wait(5)
                    self.wfile.write(rest)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        return self

    def wait_for_requests(self, count, timeout=3):
        with self.received:
            return self.received.wait_for(lambda: len(self.requests) >= count, timeout)

    def __exit__(self, *_):
        if self.gate is not None:
            self.gate.set()
        if self.body_gate is not None:
            self.body_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
