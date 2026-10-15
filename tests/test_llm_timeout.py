"""Configured deadlines reach both compiler call paths with a real reply."""

import asyncio
from dataclasses import replace

from ultrakb.agent.compiler import _llm_call, _llm_call_async
from ultrakb.config import get_timeout, set_timeout


def send(live_llm, monkeypatch, *, asynchronous=False, **options):
    live_llm.install(monkeypatch)
    bundle = replace(live_llm.credentials, timeout=get_timeout())
    messages = [{"role": "user", "content": "Acknowledge this greeting in one word: hello."}]
    options.update(bundle=bundle, max_tokens=64)
    if asynchronous:
        answer = asyncio.run(
            _llm_call_async("deepseek/deepseek-flash", messages, "greeting", **options)
        )
    else:
        answer = _llm_call("deepseek/deepseek-flash", messages, "greeting", **options)
    assert answer.strip()
    return live_llm.calls[-1]


def test_llm_call_forwards_configured_timeout(live_llm, monkeypatch):
    set_timeout(1200.0)
    assert send(live_llm, monkeypatch)["timeout"] == 1200.0


def test_llm_call_omits_timeout_when_unset(live_llm, monkeypatch):
    set_timeout(None)
    assert "timeout" not in send(live_llm, monkeypatch)


def test_llm_call_does_not_override_explicit_timeout(live_llm, monkeypatch):
    set_timeout(1200.0)
    assert send(live_llm, monkeypatch, timeout=30)["timeout"] == 30


def test_llm_call_async_forwards_configured_timeout(live_llm, monkeypatch):
    set_timeout(900.0)
    assert send(live_llm, monkeypatch, asynchronous=True)["timeout"] == 900.0


def test_llm_call_async_omits_timeout_when_unset(live_llm, monkeypatch):
    set_timeout(None)
    assert "timeout" not in send(live_llm, monkeypatch, asynchronous=True)


def test_llm_call_async_does_not_override_explicit_timeout(live_llm, monkeypatch):
    set_timeout(900.0)
    assert send(live_llm, monkeypatch, asynchronous=True, timeout=30)["timeout"] == 30
