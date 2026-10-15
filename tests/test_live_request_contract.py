"""Make the real-model gateway's intentional request adjustments observable."""

import pytest
from error_transport import ErrorTransport
from live_llm import LiveLLM
from live_request import provider_request, request_changes, sdk_request
from llm_test_budget import TestRunBudget


@pytest.mark.unit
def test_gateway_accounts_for_an_explicit_completion_token_limit():
    payload, allowance = provider_request(
        {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "Bounded request."}],
            "max_completion_tokens": 17,
        },
    )
    assert allowance == 17
    assert payload["max_tokens"] == 17


@pytest.mark.unit
@pytest.mark.parametrize(
    "limits",
    [
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_completion_tokens": -1},
        {"max_completion_tokens": 3.5},
        {"max_tokens": 64, "max_completion_tokens": 32},
    ],
)
def test_gateway_rejects_invalid_or_ambiguous_limits_before_admission(limits):
    with pytest.raises(ValueError, match="output|token"):
        provider_request(
            {"messages": [{"role": "user", "content": "Bounded request."}], **limits},
        )


@pytest.mark.integration
@pytest.mark.parametrize("status", [400, 401, 429, 503])
@pytest.mark.parametrize("streaming", [False, True])
def test_gateway_preserves_the_upstream_error_response(tmp_path, status, streaming):
    import httpx

    env_path = tmp_path / "offline.env"
    env_path.write_text("LLM_API_KEY=offline-gateway-key\n", encoding="utf-8")
    client = LiveLLM(TestRunBudget(tmp_path / "cost.json"), env_path=env_path)
    try:
        with ErrorTransport(status=status, headers={"Retry-After": "7"}) as upstream:
            client._url = upstream.url + "/chat/completions"
            response = httpx.post(
                client._proxy.url + "/chat/completions",
                headers={"Authorization": "Bearer offline-gateway-key"},
                json={
                    "model": "deepseek-flash",
                    "messages": [{"role": "user", "content": "Error propagation only."}],
                    "max_tokens": 16,
                    "stream": streaming,
                },
            )
            assert response.status_code == status
            assert response.headers["Retry-After"] == "7"
            assert response.json() == upstream.body
            assert len(upstream.requests) == 1
            assert client.budget.report()["provider_requests"] == 0
    finally:
        client.close()


@pytest.mark.unit
def test_explicit_thinking_mode_survives_both_request_preparation_hops():
    original = {
        "model": "deepseek/deepseek-flash",
        "messages": [{"role": "user", "content": "Request contract."}],
        "max_tokens": 24,
        "reasoning_effort": "none",
        "thinking": {"type": "enabled"},
    }
    sdk = sdk_request(original, api_key="offline-key", api_base="http://127.0.0.1:1/v1")
    assert sdk["extra_body"]["thinking"] == {"type": "enabled"}
    payload, allowance = provider_request(sdk)
    assert payload["thinking"] == {"type": "enabled"}
    assert allowance == 24
    assert original["thinking"] == {"type": "enabled"} and "extra_body" not in original


@pytest.mark.unit
def test_bounded_profile_reports_its_generation_adjustments():
    from copy import deepcopy

    original = {
        "model": "deepseek/deepseek-flash",
        "api_key": "opaque-source-secret",
        "base_url": "https://unselected.example/v1",
        "num_retries": 3,
        "max_retries": 3,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "A tiny contract request."}],
        "tools": [{"type": "function", "function": {"name": "read_source"}}],
        "tool_choice": "none",
        "temperature": 0.2,
        "top_p": 0.8,
        "seed": 7,
        "stop": ["END"],
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": False},
    }
    snapshot = deepcopy(original)
    sdk = sdk_request(
        original, api_key="opaque-test-secret", api_base="http://127.0.0.1:1/v1", profile="bounded"
    )
    assert set(request_changes(original, sdk)) == {
        "api_key",
        "api_base",
        "base_url",
        "num_retries",
        "max_retries",
        "max_tokens",
        "extra_body",
        "stream_options",
    }
    assert sdk["max_tokens"] == 2048
    assert sdk["stream_options"] == {"include_usage": True}
    assert sdk["num_retries"] == sdk["max_retries"] == 0
    payload, allowance = provider_request(sdk, profile="bounded")
    for field in (
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "seed",
        "stop",
        "response_format",
    ):
        assert payload[field] == snapshot[field]
    assert allowance == 2048 and payload["model"] == "deepseek-flash"
    assert "opaque" not in repr(request_changes(original, sdk))
    sdk["messages"][0]["content"] = "Consumer-side mutation"
    payload["tools"].clear()
    assert original == snapshot


@pytest.mark.live
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_explicit_request_options_reach_flash_and_produce_a_complete_json_reply(
    live_llm, monkeypatch, asynchronous
):
    import json

    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        ModelRequest,
        RoleBindings,
    )

    live_llm.install(monkeypatch)
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            live_llm.credentials,
            generation={"answer": {"max_tokens": 128, "reasoning_effort": "low"}},
        ),
        ModelCallPolicy(max_calls=2, retries=0),
    )
    request = ModelRequest(
        [{"role": "user", "content": 'Return only JSON {"project":"Orion","credits":17}.'}],
        operation="query",
        stage="answer",
        generation_options={
            "max_completion_tokens": 64,
            "reasoning_effort": "none",
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
    )
    before = len(live_llm.calls)
    if asynchronous:
        result = await executor.acomplete("answer", request)
    else:
        result = executor.complete("answer", request)
    assert json.loads(result.text) == {"project": "Orion", "credits": 17}
    assert result.finish_reason == "stop" and result.raw_usage_available
    assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens > 0
    original = live_llm.calls[before]
    assert original["model"] == "deepseek/deepseek-flash"
    assert original["base_url"] == live_llm.credentials.base_url
    assert original["max_tokens"] == 64 and "max_completion_tokens" not in original
    assert original["extra_body"] == {"reasoning_effort": "none"}
    assert original["num_retries"] == original["max_retries"] == 0
    wire = live_llm._proxy.requests[-1]
    assert wire["max_tokens"] == 64 and wire["reasoning_effort"] == "none"
    assert wire["response_format"] == {"type": "json_object"}
    assert wire["thinking"] == {"type": "disabled"}
    assert executor.calls == 1


@pytest.mark.live
@pytest.mark.asyncio
async def test_managed_stream_requests_usage_before_gateway_adjustment_and_delivers_the_answer(
    live_llm, monkeypatch
):
    from agents import Agent, ModelSettings, RunConfig, Runner

    from ultrakb.agent.managed_model import ManagedAgentModel
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    live_llm.install(monkeypatch)
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            live_llm.credentials,
            generation={"answer": {"max_tokens": 64, "reasoning_effort": "none"}},
        ),
        ModelCallPolicy(max_calls=2, retries=0),
    )
    agent = Agent(
        name="request-contract",
        model=ManagedAgentModel(executor),
        instructions="Return exactly the supplied sentence, without additional text.",
        model_settings=ModelSettings(include_usage=True),
    )
    before = len(live_llm.calls)
    result = Runner.run_streamed(
        agent, "Orion has 17 credits.", run_config=RunConfig(tracing_disabled=True)
    )
    deltas = []
    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            deltas.append(event.data.delta)
    assert result.final_output.strip() == "Orion has 17 credits."
    assert "".join(deltas).strip() == result.final_output.strip()
    original = live_llm.calls[before]
    assert original["stream"] is True
    assert original["stream_options"] == {"include_usage": True}
    assert original["max_tokens"] == 64
    assert result.context_wrapper.usage.input_tokens > 0
    assert result.context_wrapper.usage.output_tokens > 0
    assert result.run_loop_task.done() and executor.calls == 1
