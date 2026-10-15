"""Loopback gateway so SDKs and spawned workers share the same real-model budget."""

import asyncio
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from live_errors import ProviderHTTPError
from llm_test_budget import BudgetExceeded


class DeepSeekProxy:
    def __init__(self, owner):
        self.owner = owner
        self.requests = []
        self.records = []
        self.intervals = []
        self.delay = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                expected = f"Bearer {owner._key}"
                if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                    self.send_error(401, "The test gateway requires its captured credential")
                    return
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_error(400, "Use the DeepSeek chat-completion test transport")
                    return
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 1_000_000:
                    self.send_error(413, "Test request body exceeds its admission limit")
                    return
                payload = json.loads(self.rfile.read(length))
                proxy.requests.append(payload)
                headers = {
                    key: "<redacted>" if key.lower() in {"authorization", "x-api-key"} else value
                    for key, value in self.headers.items()
                }
                proxy.records.append((payload, headers, self.path))
                interval = [time.monotonic(), float("inf")]
                proxy.intervals.append(interval)
                try:
                    if payload.get("stream"):
                        asyncio.run(self.stream(payload))
                    else:
                        result = owner.request(**payload)
                        time.sleep(proxy.delay)
                        data = json.dumps(result, ensure_ascii=False).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Consumer cancellation; the real receipt remains in the ledger.
                except BudgetExceeded:
                    self.send_error(402, "Full test-run LLM budget of 2.00 CNY exhausted")
                except ProviderHTTPError as error:
                    self.send_response(error.status_code)
                    for name, value in error.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(error.body)))
                    self.end_headers()
                    self.wfile.write(error.body)
                except Exception as error:
                    self.send_error(502, f"Real DeepSeek transport failed: {type(error).__name__}")
                finally:
                    interval[1] = time.monotonic()

            async def stream(self, payload):
                stream = await owner.remote_acompletion(**payload)
                await asyncio.sleep(proxy.delay)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    async for chunk in stream:
                        data = chunk.model_dump(exclude_none=True)
                        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                finally:
                    await stream.aclose()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
