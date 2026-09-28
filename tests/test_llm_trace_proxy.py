"""Real LiteLLM requests preserve provider counters and tolerate slow first bytes."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openkb.config import LlmCredentialBundle
from openkb.indexer import _build_index_config
from scripts.llm_trace_proxy import AuditProxy, usage_counts


@pytest.fixture
def provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((body, dict(self.headers)))
            time.sleep(0.08)
            response = {
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "OK",
                            "reasoning_content": "fixture reasoning",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "prompt_cache_hit_tokens": 20,
                    "prompt_cache_miss_tokens": 30,
                    "completion_tokens": 32,
                    "completion_tokens_details": {"reasoning_tokens": 12},
                    "total_tokens": 82,
                },
            }
            status = 200
            if "fail" in body["messages"][0]["content"]:
                status, response = 500, {"error": "test-key failed"}
            raw = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sync_and_async_index_requests_use_kb_deadline_and_keep_raw_usage(
    provider, tmp_path, monkeypatch
):
    from pageindex import config
    from pageindex.index.utils import llm_acompletion, llm_completion

    monkeypatch.setitem(config._LLM_PARAMS, "timeout", 0.02)
    monkeypatch.setenv("OPENKB_LLM_AUDIT", "1")
    directory = tmp_path / "trace"
    with AuditProxy(directory, upstream=provider[0], timeout=2, api_key="test-key") as audit:
        options = _build_index_config(
            {"timeout": 2},
            bundle=LlmCredentialBundle(
                api_key="test-key",
                base_url=audit.base_url,
            ),
        )
        with config.llm_params_scope(options.llm_params):
            assert llm_completion("deepseek/deepseek-flash", "hello") == "OK"
            assert asyncio.run(llm_acompletion("deepseek/deepseek-flash", "hello")) == "OK"
        summary = audit.summary()
        assert summary["completed"] == 2
        assert summary["pending"] == 0
        assert summary["usage"]["reasoning_tokens"]["known_sum"] == 24
        assert summary["usage"]["answer_tokens"]["known_sum"] == 40
        assert all(r["step"] == "pageindex.index" for r in summary["attempts"])
    assert len(provider[1]) == 2
    assert provider[1][0][1]["Authorization"] == "Bearer test-key"
    assert "X-OpenKB-Step" not in provider[1][0][1]
    raw = json.loads((directory / "000001/response.json").read_text())
    assert raw["body"]["choices"][0]["message"]["reasoning_content"] == "fixture reasoning"
    assert raw["body"]["usage"]["prompt_cache_hit_tokens"] == 20
    assert raw["tokens"]["input_cache_miss_tokens"] == 30
    assert "test-key" not in "".join(p.read_text() for p in directory.rglob("*.json"))


def test_failed_attempt_and_redaction_are_recorded(provider, tmp_path):
    import httpx

    directory = tmp_path / "trace"
    with AuditProxy(directory, upstream=provider[0], timeout=2, api_key="test-key") as audit:
        response = httpx.post(
            audit.base_url + "/chat/completions",
            json={
                "model": "fixture",
                "messages": [{"role": "user", "content": "fail test-key"}],
            },
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 500
        assert audit.summary()["failed"] == 1
    text = "".join(p.read_text() for p in directory.rglob("*.json"))
    assert "test-key" not in text
    assert "<REDACTED>" in text


def test_absent_usage_is_unknown_and_derived_counts_are_labeled():
    assert usage_counts({})["reasoning_tokens"] is None
    assert usage_counts({"usage": {"completion_tokens": 32}})["answer_tokens"] is None
    counts = usage_counts(
        {
            "usage": {
                "prompt_tokens": 50,
                "prompt_cache_hit_tokens": 20,
                "completion_tokens": 32,
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        }
    )
    assert counts["input_cache_miss_tokens"] == 30
    assert counts["answer_tokens"] == 20
    assert len(counts["derived"]) == 2
