"""Controlled local HTTP model used across real spawn adapter tests."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml


def evidence_response(payload):
    if isinstance(payload, dict) and payload.get("stage") == "facts":
        return {
            "units": [
                {
                    "id": unit["id"],
                    "facts": [
                        {
                            "topic": "Notes",
                            "statement": "Confirmed knowledge.",
                            "quote": unit["text"][:40],
                        }
                    ],
                    "empty_reason": "",
                }
                for unit in payload["units"]
            ]
        }
    elif isinstance(payload, dict) and payload.get("stage") == "planning":
        return {
            "topics": [
                {
                    "name": "notes",
                    "title": "Notes",
                    "kind": "concept",
                    "members": payload["topics"],
                }
            ]
        }
    elif isinstance(payload, dict) and payload.get("stage") == "generation":
        return {
            "content": "# Notes\nConfirmed knowledge.",
            "covered": [fact["id"] for fact in payload["facts"]],
        }
    elif isinstance(payload, dict) and payload.get("stage") == "verification":
        return {
            "verdict": "supported",
            "reason": "The controlled contribution matches its evidence.",
        }
    return None


class ModelService(list):
    def __init__(self):
        super().__init__()
        self.received = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.drip_seconds = 0.0
        self.respond = None
        self.finish_reason = "stop"


@pytest.fixture
def model_service(kb_dir):
    calls = ModelService()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            calls.received.set()
            calls.release.wait(30)
            value = {"description": "Notes", "content": "# Notes\nConfirmed knowledge."}
            if len(calls) % 2 == 0:
                value = {"create": [], "update": [], "related": []}
            try:
                payload = json.loads(body["messages"][-1]["content"])
            except (ValueError, TypeError):
                payload = {}
            value = evidence_response(payload) or value
            if calls.respond is not None:
                value = calls.respond(body)
            content = json.dumps(
                {
                    "id": "offline",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(value)},
                            "finish_reason": calls.finish_reason,
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                if calls.drip_seconds:
                    for start in range(0, len(content), 20):
                        self.wfile.write(content[start : start + 20])
                        self.wfile.flush()
                        time.sleep(calls.drip_seconds)
                else:
                    self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A stopped model caller intentionally closes its socket.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "model": "openai/offline-test",
        "language": "en",
        "processing": {
            "request_timeout": 3,
            "stage_timeout": 15,
            "document_timeout": 30,
            "cleanup_timeout": 5,
            "max_attempts": 2,
            "max_requests": 20,
            "max_tokens": 100000,
            "concurrency": 2,
            "context_tokens": 32768,
            "output_tokens": 1024,
        },
    }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    (kb_dir / ".env").write_text(
        f"LLM_API_KEY=synthetic-test\nOPENAI_API_BASE=http://127.0.0.1:{server.server_port}/v1\n"
    )
    try:
        yield calls
    finally:
        calls.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
