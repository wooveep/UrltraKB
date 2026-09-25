"""Provider JSON recovery through the actual document planning boundary."""

import asyncio
from types import SimpleNamespace

import pytest

from openkb.agent.compiler import _llm_call, _llm_call_async
from openkb.agent.document_json_response import classify_json_response
from openkb.execution_receipt import ModelText, derived_text
from openkb.processing import DEFAULT_PROCESSING, processing_scope


@pytest.mark.parametrize(
    "text,finish,expected",
    [
        ("", "stop", "empty_content"),
        (" \n", "stop", "empty_content"),
        ('{"decisions":[]}', "stop", "content"),
        ("", "length", "length"),
        ("{}", "content_filter", "invalid_finish"),
        ("", None, "invalid_finish"),
    ],
)
def test_completed_response_classification(text, finish, expected):
    wire = ModelText(text, 128, raw_content=text, finish_reason=finish, representation="wire")
    assert classify_json_response(wire) == expected
    assert derived_text(text, wire).representation == "derived"
    assert derived_text(text, wire).raw_content is None


def test_default_and_raw_llm_call_modes_are_symmetric(monkeypatch):
    import litellm

    seen = []

    def response(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=' {"key":"@e:a"} '),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )

    monkeypatch.setattr(litellm, "completion", response)

    async def async_response(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", async_response)

    class Messages(list):
        def decode_response(self, value):
            return value.replace("@e:a", "expanded-id")

    messages = Messages([{"role": "user", "content": "JSON"}])
    settings = {
        "model": "openai/offline-test",
        "processing": {
            **DEFAULT_PROCESSING,
            "max_context_tokens": 128_000,
            "max_output_tokens": 4_096,
        },
    }
    with processing_scope(settings):
        sync_default = _llm_call("openai/offline-test", messages, "planning")
        sync_raw = _llm_call("openai/offline-test", messages, "planning", decode_response=False)
        async_default = asyncio.run(_llm_call_async("openai/offline-test", messages, "planning"))
        async_raw = asyncio.run(
            _llm_call_async("openai/offline-test", messages, "planning", decode_response=False)
        )
    assert sync_default == async_default == '{"key":"expanded-id"}'
    assert sync_raw == async_raw == ' {"key":"@e:a"} '
    assert sync_raw.raw_content == sync_raw
    assert async_raw.representation == "wire"
    assert all("decode_response" not in request for request in seen)
