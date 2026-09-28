"""Loopback recorder for non-streaming OpenAI-compatible import requests.

Store the provider's original JSON before LiteLLM normalizes away usage fields.
Each HTTP attempt, including retries and errors, gets its own durable record.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from openkb.locks import atomic_write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def usage_counts(body: dict) -> dict[str, Any]:
    """Keep absent counters unknown; label arithmetic derived from reported usage."""
    usage = body.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    inputs = usage.get("prompt_tokens_details") or {}

    def count(value):
        return value if type(value) is int and value >= 0 else None

    prompt = count(usage.get("prompt_tokens"))
    output = count(usage.get("completion_tokens"))
    hit = count(usage.get("prompt_cache_hit_tokens", inputs.get("cached_tokens")))
    miss = count(usage.get("prompt_cache_miss_tokens"))
    reasoning = count(details.get("reasoning_tokens", usage.get("reasoning_tokens")))
    derived = []
    if miss is None and prompt is not None and hit is not None and prompt >= hit:
        miss = prompt - hit
        derived.append("input_cache_miss_tokens = input_tokens - input_cache_hit_tokens")
    answer = None
    if output is not None and reasoning is not None and output >= reasoning:
        answer = output - reasoning
        derived.append("answer_tokens = output_tokens - reasoning_tokens")
    return {
        "input_tokens": prompt,
        "input_cache_hit_tokens": hit,
        "input_cache_miss_tokens": miss,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "answer_tokens": answer,
        "total_tokens": count(usage.get("total_tokens")),
        "derived": derived,
    }


class AuditProxy(AbstractContextManager):
    def __init__(self, directory: Path, *, upstream: str, timeout: float, api_key: str):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.upstream = upstream.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.token = secrets.token_urlsafe(24)
        self.document = "preflight"
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=60))
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log authorization or the private loopback route.

            def do_POST(self):
                owner._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/{self.token}"

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): "<REDACTED>"
                if str(k).lower() in {"authorization", "api_key", "api-key", "x-api-key", "cookie"}
                else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in (self.api_key, self.token):
                if secret:
                    value = value.replace(secret, "<REDACTED>")
            return value
        return value

    def _write(self, path: Path, data: Any) -> None:
        atomic_write_json(path, self.redact(data))

    def _summarize(self) -> dict:
        completed = [r for r in self._records.values() if r["status"] == "completed"]
        fields = [k for k in usage_counts({}) if k != "derived"]
        usage = {}
        for field in fields:
            values = [r["tokens"][field] for r in completed if r["tokens"][field] is not None]
            usage[field] = {
                "known_sum": sum(values) if values else None,
                "reported_or_derived_requests": len(values),
                "unknown_requests": len(completed) - len(values),
            }
        return {
            "updated_at": utc_now(),
            "requests": len(self._records),
            "completed": len(completed),
            "failed": sum(r["status"] == "failed" for r in self._records.values()),
            "pending": sum(r["status"] == "pending" for r in self._records.values()),
            "usage": usage,
            "attempts": list(self._records.values()),
        }

    def summary(self) -> dict:
        with self._lock:
            return self._summarize()

    def _send(self, handler, status: int, body: bytes) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Retain the provider response and usage even if the caller timed out.

    def _handle(self, handler) -> None:
        if handler.path != f"/{self.token}/chat/completions":
            self._send(handler, 404, b'{"error":"Unknown audit route"}')
            return
        if handler.headers.get("Authorization") != f"Bearer {self.api_key}":
            self._send(handler, 401, b'{"error":"Invalid audit credential"}')
            return
        payload = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        try:
            request = json.loads(payload)
            if not isinstance(request, dict) or request.get("stream"):
                raise ValueError("Import auditing requires non-streaming JSON requests")
        except (ValueError, TypeError) as exc:
            self._send(handler, 400, json.dumps({"error": str(exc)}).encode())
            return
        started = time.monotonic()
        with self._lock:
            request_id = f"{len(self._records) + 1:06d}"
            folder = self.directory / request_id
            folder.mkdir(mode=0o700)
            record = {
                "request_id": request_id,
                "document": self.document,
                "step": unquote(handler.headers.get("X-OpenKB-Step", "pageindex-or-agent")),
                "started_at": utc_now(),
                "status": "pending",
                "model": request.get("model"),
            }
            self._records[request_id] = record
            self._write(
                folder / "request.json",
                {
                    **record,
                    "method": "POST",
                    "url": self.upstream + "/chat/completions",
                    "read_timeout_seconds": self.timeout,
                    "body": request,
                },
            )
            self._write(self.directory / "summary.json", self._summarize())
        status, response_body = 502, {"error": "Upstream request failed"}
        response_headers = {}
        try:
            response = self._client.post(
                self.upstream + "/chat/completions",
                content=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            status = response.status_code
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"unparsed_body": response.text}
            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() in {"content-type", "x-request-id", "request-id"}
            }
            outgoing = response.content
        except Exception as exc:
            response_body = {"error": {"type": type(exc).__name__, "message": str(exc)}}
            outgoing = json.dumps(self.redact(response_body)).encode()
        with self._lock:
            record.update(
                {
                    "finished_at": utc_now(),
                    "seconds": round(time.monotonic() - started, 3),
                    "status": "completed" if 200 <= status < 300 else "failed",
                    "http_status": status,
                    "tokens": usage_counts(response_body)
                    if isinstance(response_body, dict)
                    else usage_counts({}),
                }
            )
            self._write(
                folder / "response.json",
                {
                    **record,
                    "headers": response_headers,
                    "body": response_body,
                },
            )
            self._write(self.directory / "summary.json", self._summarize())
        self._send(handler, status, outgoing)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._client.close()
        self._write(self.directory / "summary.json", self.summary())
