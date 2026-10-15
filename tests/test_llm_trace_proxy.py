"""Real LiteLLM requests preserve provider counters and tolerate slow first bytes."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.llm_trace_proxy import AuditProxy, usage_counts
from ultrakb.config import LlmCredentialBundle
from ultrakb.indexer import _build_index_config


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
            status, response = 500, {"error": "test-key local delivery refusal"}
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
    live_llm, tmp_path, monkeypatch
):
    from pageindex import config
    from pageindex.index.utils import llm_acompletion, llm_completion

    # Warm the actual same-run reply before testing delayed local delivery.
    message = "Reply exactly ok."
    live_llm.completion(messages=[{"role": "user", "content": message}], max_tokens=64)
    service = live_llm._proxy
    before = len(service.requests)
    monkeypatch.setattr(service, "delay", 0.08)
    monkeypatch.setattr("litellm.completion", live_llm._sdk_completion)
    monkeypatch.setattr("litellm.acompletion", live_llm._sdk_acompletion)
    monkeypatch.setitem(config._LLM_PARAMS, "timeout", 0.02)
    monkeypatch.setenv("ULTRAKB_LLM_AUDIT", "1")
    directory = tmp_path / "trace"
    key = live_llm.credentials.api_key
    with AuditProxy(directory, upstream=service.url, timeout=90, api_key=key) as audit:
        options = _build_index_config(
            {"timeout": 2},
            bundle=LlmCredentialBundle(api_key=key, base_url=audit.base_url),
        )
        options.llm_params["max_tokens"] = 64
        with config.llm_params_scope(options.llm_params):
            first = llm_completion("deepseek/deepseek-flash", message)
            second = asyncio.run(llm_acompletion("deepseek/deepseek-flash", message))
        assert first.strip() == second.strip() == "ok"
        summary = audit.summary()
        assert summary["completed"] == 2 and summary["pending"] == 0
        assert all(r["step"] == "pageindex.index" for r in summary["attempts"])
    assert len(service.requests) - before == 2
    raw = [json.loads((directory / f"{i:06d}/response.json").read_text()) for i in (1, 2)]
    for delivered in raw:
        usage = delivered["body"]["usage"]
        assert delivered["tokens"]["input_tokens"] == usage["prompt_tokens"]
        assert delivered["tokens"]["input_cache_hit_tokens"] == usage["prompt_cache_hit_tokens"]
        assert delivered["tokens"]["input_cache_miss_tokens"] == usage["prompt_cache_miss_tokens"]
        assert delivered["tokens"]["output_tokens"] == usage["completion_tokens"]
    assert raw[0]["body"] == raw[1]["body"]
    assert summary["usage"]["output_tokens"]["known_sum"] == sum(
        value["body"]["usage"]["completion_tokens"] for value in raw
    )
    assert all(key not in p.read_text() for p in directory.rglob("*.json"))


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
