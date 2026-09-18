"""Controlled local HTTP model used across real spawn adapter tests."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml


def evidence_response(payload):
    if isinstance(payload, dict) and payload.get("stage") == "verification_batch":
        return {
            "reviews": [
                {"id": row["id"], "review": {"verdict": "supported", "reason": "Fixture evidence"}}
                for row in payload["candidates"]
            ]
        }
    if isinstance(payload, dict) and payload.get("stage") == "facts":
        return {
            "units": [
                {
                    "id": unit["id"],
                    "facts": [
                        {
                            "topic": "Notes",
                            "statement": "Confirmed knowledge.",
                            "quote": (
                                unit["text"][:40]
                                if unit["text"].find(unit["text"][:40], 1) < 0
                                else unit["text"]
                            ),
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
        if payload.get("source_scopes"):
            return {
                "title": payload.get("title", payload.get("revision", {}).get("title", "Topic")),
                "covered": [fact["id"] for fact in payload["facts"]],
                "fragments": [
                    {
                        "scope": scope["id"],
                        "occurrences": scope["occurrences"],
                        "heading": "Notes",
                        "content": "Confirmed knowledge.",
                    }
                    for scope in payload["source_scopes"]
                ],
            }
        return {
            "content": "# Notes\nConfirmed knowledge.",
            "covered": [fact["id"] for fact in payload["facts"]],
        }
    elif isinstance(payload, dict) and payload.get("stage") == "verification":
        return {
            "verdict": "supported",
            "reason": "The controlled contribution matches its evidence.",
        }
    elif isinstance(payload, dict) and payload.get("stage") == "index_summary":
        return {
            "summaries": [
                {"id": row["id"], "summary": "Source navigation."} for row in payload["nodes"]
            ]
        }
    elif isinstance(payload, dict) and payload.get("stage") == "index_summary_verification":
        return {
            "summaries": [
                {"id": row["id"], "verdict": "supported", "reason": "Source description preserved."}
                for row in payload["candidates"]
            ]
        }
    elif isinstance(payload, dict) and payload.get("stage") == "index_structure":
        return {
            "sections": [
                {
                    "start": payload["blocks"][0]["id"],
                    "end": payload["blocks"][-1]["id"],
                    "level": 1,
                    "title": "Source range",
                }
            ]
        }
    elif isinstance(payload, dict) and payload.get("stage") == "dependencies":
        return {
            "topics": [
                {
                    "path": candidate["path"],
                    "status": "independent",
                    "reason": "The fixture topics express independent requirements.",
                }
                for candidate in payload["candidates"]
            ]
        }
    return None


def answer_review_response(payload):
    output = payload["observations"][0]["output"] if payload["observations"] else ""
    quote = (output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))[:64]
    return {
        "verdict": "supported",
        "issues": [],
        "units": [
            {
                "id": unit["id"],
                "verdict": "supported" if quote else "non_factual",
                "support": [{"observation": "o1", "quote": quote}] if quote else [],
            }
            for unit in payload["units"]
        ],
    }


class ModelService(list):
    def __init__(self):
        super().__init__()
        self.received = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.drip_seconds = 0.0
        self.stream_disconnect = False
        self.respond = None
        self.chat_response = None
        self.answer_review_response = None
        self.chat_without_tools = False
        self.finish_reason = "stop"
        self.usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}


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
            review = payload.get("stage") == "answer_verification"
            chat = review or (
                calls.chat_response is not None and (body.get("tools") or calls.chat_without_tools)
            )
            if calls.respond is not None and not chat:
                value = calls.respond(body)
            if chat:
                if review:
                    message = (
                        calls.answer_review_response(body)
                        if calls.answer_review_response
                        else {
                            "role": "assistant",
                            "content": json.dumps(answer_review_response(payload)),
                        }
                    )
                else:
                    message = calls.chat_response(body)
                finish = (
                    "tool_calls"
                    if message.get("tool_calls") and calls.finish_reason == "stop"
                    else calls.finish_reason
                )
                if body.get("stream"):
                    delta = {**message}
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [
                            {"index": i, **tool} for i, tool in enumerate(delta["tool_calls"])
                        ]
                    chunks = [
                        {
                            "id": "offline",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": body["model"],
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        },
                        {
                            "id": "offline",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": body["model"],
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                            "usage": calls.usage,
                        },
                    ]
                    content = (
                        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
            content = json.dumps(
                {
                    "id": "offline",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": message
                            if chat
                            else {"role": "assistant", "content": json.dumps(value)},
                            "finish_reason": finish if chat else calls.finish_reason,
                        }
                    ],
                    "usage": calls.usage,
                }
            ).encode()
            if body.get("stream"):
                response = json.loads(content)
                choice = response["choices"][0]
                chunks = [
                    {
                        "id": "offline",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "delta": choice["message"], "finish_reason": None}
                        ],
                    },
                    {
                        "id": "offline",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}
                        ],
                        "usage": calls.usage,
                    },
                ]
                if calls.stream_disconnect:
                    chunks = chunks[:1]
                content = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                    + ("" if calls.stream_disconnect else "data: [DONE]\n\n")
                ).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if body.get("stream") else "application/json"
            )
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
        "navigation": {"enabled": False},
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


def original_source_answer(inspect):
    """A deterministic external model that browses a published source before answering."""

    def tool(name, arguments, index):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"read_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    def respond(body):
        results = [row for row in body["messages"] if row["role"] == "tool"]
        if not results:
            return tool("list_sources", {"offset": 0, "limit": 20}, 0)
        listing = json.loads(results[0]["content"])
        source = listing["sources"][0]["source_id"]
        if len(results) == 1:
            return tool("read_source_tree", {"source_id": source, "offset": 0, "limit": 20}, 1)
        tree = json.loads(results[1]["content"])
        if len(results) == 2:
            return tool(
                "read_source_node",
                {
                    "source_id": source,
                    "node_id": tree["nodes"][0]["id"],
                    "offset": 0,
                    "start": 0,
                    "max_chars": 16000,
                },
                2,
            )
        evidence = json.loads(results[-1]["content"])
        return {"role": "assistant", "content": inspect(tree, evidence)}

    return respond


def wiki_source_answer(answer):
    def chat(body):
        if any(row["role"] == "tool" for row in body["messages"]):
            return {"role": "assistant", "content": answer}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/rows.md"}'},
                }
            ],
        }

    return chat
