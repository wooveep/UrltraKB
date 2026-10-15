"""Observe production generation at the real SDK boundary, without successful replies."""

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.config import LlmCredentialBundle
from ultrakb.index_llm import PageIndexLLM
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        "compile",
        "document_index",
        "conversation_index",
        "conversation_retrieval",
        "knowledge_retrieval",
        "answer",
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("generation", [{}, {"reasoning_effort": "none", "max_tokens": 1024}])
async def test_production_generation_survives_without_gateway_help(role, asynchronous, generation):
    with ErrorTransport(status=401) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                LlmCredentialBundle(api_key="offline-generation-key", base_url=service.url),
                generation={role: generation},
            ),
            ModelCallPolicy(max_calls=1, retries=0),
        )
        messages = [{"role": "user", "content": "Inspect the parameter contract only."}]
        with pytest.raises(litellm.AuthenticationError):
            if role == "document_index":
                adapter = PageIndexLLM(executor)
                if asynchronous:
                    await adapter.acomplete(messages, stage="summary")
                else:
                    adapter.complete(messages, stage="summary")
            else:
                request = ModelRequest(messages, operation="compile", stage="document-summary:1")
                if asynchronous:
                    await executor.acomplete(role, request)
                else:
                    executor.complete(role, request)
        assert len(service.requests) == executor.calls == 1
        wire = service.requests[0]["json"]
        assert "thinking" not in wire
        if generation:
            # The current Chat Completions contract accepts none as the off switch.
            assert wire["reasoning_effort"] == "none"
            assert wire["max_tokens"] == generation["max_tokens"]
        else:
            assert "reasoning_effort" not in wire and "max_tokens" not in wire


def test_summary_admission_counts_output_limit_and_effort(tmp_path, monkeypatch):
    from ultrakb import config as config_module
    from ultrakb.agent.document_summary import summary_capacity
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.model_tokenizer import TokenMeasure

    monkeypatch.setattr(config_module, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    config = {"model": "deepseek/deepseek-flash", "model_capacity": capacity}
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=True))
    messages = [{"role": "user", "content": "Return one JSON object for the supplied fact."}]
    native = {
        "messages": messages,
        "max_tokens": 96,
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",
    }
    required = measure.payload_tokens(native)
    capacity["max_input_tokens"] = required - 1
    generation = {"max_tokens": 96, "reasoning_effort": "none"}
    assert not summary_capacity(config, custom_endpoint=True, generation_options=generation)(
        messages
    )
    capacity["max_input_tokens"] = required
    assert summary_capacity(config, custom_endpoint=True, generation_options=generation)(messages)


@pytest.mark.unit
@pytest.mark.parametrize(
    "options,reserved",
    [({}, 65536), ({"reasoning_effort": "none"}, 8192), ({"reasoning_effort": "max"}, 131072)],
)
def test_gateway_keeps_production_defaults_and_reserves_the_provider_budget(options, reserved):
    from live_request import provider_request, sdk_request

    original = {
        "model": "deepseek/deepseek-flash",
        "messages": [{"role": "user", "content": "Read the supplied fact."}],
        **options,
    }
    sdk = sdk_request(original, api_key="offline-profile-key", api_base="http://127.0.0.1:1/v1")
    payload, allowance = provider_request(sdk)
    assert "max_tokens" not in sdk and "max_tokens" not in payload
    assert "thinking" not in payload
    assert payload.get("reasoning_effort") == options.get("reasoning_effort")
    assert allowance == reserved


@pytest.mark.unit
def test_production_profile_does_not_shrink_an_explicit_output_budget():
    from live_request import provider_request, sdk_request

    original = {
        "model": "deepseek/deepseek-flash",
        "messages": [{"role": "user", "content": "Read the supplied fact."}],
        "max_tokens": 8192,
        "reasoning_effort": "high",
    }
    sdk = sdk_request(original, api_key="offline-profile-key", api_base="http://127.0.0.1:1/v1")
    payload, allowance = provider_request(sdk)
    assert sdk["max_tokens"] == payload["max_tokens"] == allowance == 8192
    assert "thinking" not in payload and payload["reasoning_effort"] == "high"


@pytest.mark.unit
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("limit", [17, 131072])
def test_production_budget_counts_the_effective_extra_body_output_limit(field, limit):
    from live_request import provider_request, sdk_request

    original = {
        "messages": [{"role": "user", "content": "Inspect the budget only."}],
        "extra_body": {field: limit},
    }
    sdk = sdk_request(original, api_key="offline-key", api_base="http://127.0.0.1:1/v1")
    payload, allowance = provider_request(sdk)
    assert payload["max_tokens"] == allowance == limit
    assert "max_completion_tokens" not in payload


@pytest.mark.unit
def test_extra_body_cannot_hide_an_invalid_output_limit():
    from live_request import provider_request

    with pytest.raises(ValueError, match="output|token"):
        provider_request({"messages": [], "extra_body": {"max_tokens": True}})
