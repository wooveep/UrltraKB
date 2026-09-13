"""Real SDK normalization cannot turn missing wire counters into paid zeroes."""

import asyncio

import litellm
import pytest
import yaml
from dotenv import dotenv_values

from openkb.processing import model_acall, model_call, processing_scope


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "wire_usage,expected",
    [
        (None, None),
        ({}, None),
        ({"prompt_tokens": 100}, None),
        (
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            {"input": 0, "output": 0},
        ),
    ],
)
def test_missing_partial_and_explicit_zero_http_usage(
    kb_dir, model_service, asynchronous, wire_usage, expected
):
    settings = yaml.safe_load((kb_dir / ".openkb/config.yaml").read_text())
    credentials = dotenv_values(kb_dir / ".env")
    model_service.usage = wire_usage
    options = {
        "model": settings["model"],
        "messages": [{"role": "user", "content": '{"stage":"verification"}'}],
        "api_key": credentials["LLM_API_KEY"],
        "api_base": credentials["OPENAI_API_BASE"],
    }
    callbacks = []
    options["logger_fn"] = lambda details: callbacks.append(details.get("log_event_type"))
    with processing_scope(settings) as budget:
        if asynchronous:
            asyncio.run(model_acall(litellm.acompletion, **options))
        else:
            model_call(litellm.completion, **options)
    assert len(model_service) == 1
    assert "post_api_call" in callbacks
    assert budget.observations[0]["usage"] == expected
    assert budget.unknown_usage == (1 if expected is None else 0)
    assert budget.charged_tokens == (
        budget.observations[0]["reserved_tokens"] if expected is None else 0
    )
