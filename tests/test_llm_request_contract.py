"""Request contracts through immutable inputs and the real SDK's HTTP boundary."""

from copy import deepcopy

import httpx
import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings


@pytest.mark.integration
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_request_reasoning_override_reaches_the_sdk_wire(asynchronous):
    with ErrorTransport(status=400) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                LlmCredentialBundle(api_key="offline-contract-key", base_url=service.url),
                generation={"answer": {"max_tokens": 64, "reasoning_effort": "none"}},
            ),
            ModelCallPolicy(max_calls=2, retries=0),
        )
        request = ModelRequest(
            [{"role": "user", "content": "Request contract; this endpoint only rejects."}],
            operation="query",
            stage="answer",
            generation_options={"reasoning_effort": "low", "max_tokens": 24},
        )
        with pytest.raises(litellm.BadRequestError):
            if asynchronous:
                await executor.acomplete("answer", request)
            else:
                executor.complete("answer", request)
        assert len(service.requests) == executor.calls == 1
        assert service.requests[0]["json"]["reasoning_effort"] == "low"
        assert service.requests[0]["json"]["max_tokens"] == 24


@pytest.mark.integration
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_request_output_alias_overrides_the_captured_role_limit(asynchronous):
    with ErrorTransport(status=400) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                LlmCredentialBundle(api_key="offline-contract-key", base_url=service.url),
                generation={"answer": {"max_tokens": 64}},
            ),
            ModelCallPolicy(max_calls=2, retries=0),
        )
        request = ModelRequest(
            [{"role": "user", "content": "Output parameter contract."}],
            operation="query",
            stage="answer",
            generation_options={"max_completion_tokens": 24},
        )
        with pytest.raises(litellm.BadRequestError):
            if asynchronous:
                await executor.acomplete("answer", request)
            else:
                executor.complete("answer", request)
        assert len(service.requests) == executor.calls == 1
        assert service.requests[0]["json"]["max_tokens"] == 24


@pytest.mark.unit
def test_model_request_freezes_messages_tools_options_and_cache_segments():
    messages = [{"role": "user", "content": "Original question."}]
    tools = [{"type": "function", "function": {"name": "read_source"}}]
    options = {"max_tokens": 24, "response_format": {"type": "json_object"}}
    segments = [0]
    request = ModelRequest(
        messages,
        operation="query",
        stage="answer",
        tools=tools,
        generation_options=options,
        cache_segments=segments,
    )
    messages[0]["content"] = "Later question."
    tools[0]["function"]["name"] = "later_tool"
    options["response_format"]["type"] = "text"
    segments.clear()
    expected = {
        "messages": [{"role": "user", "content": "Original question."}],
        "tools": [{"type": "function", "function": {"name": "read_source"}}],
        "options": {"max_tokens": 24, "response_format": {"type": "json_object"}},
        "cache_segments": [0],
    }
    assert request.payload() == expected
    detached = request.payload()
    detached["messages"].clear()
    assert request.payload() == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "field", ["model", "api_key", "api_base", "extra_headers", "timeout", "retries"]
)
def test_generation_options_cannot_replace_task_owned_transport_settings(field):
    with pytest.raises(ValueError, match="Unpermitted generation options"):
        ModelRequest(
            [{"role": "user", "content": "Request contract."}],
            operation="query",
            stage="answer",
            generation_options={field: "untrusted override"},
        )


@pytest.mark.unit
def test_a_request_cannot_specify_two_competing_output_limits():
    with pytest.raises(ValueError, match="output token"):
        ModelRequest(
            [{"role": "user", "content": "Request contract."}],
            operation="query",
            stage="answer",
            generation_options={"max_tokens": 64, "max_completion_tokens": 24},
        )


@pytest.mark.unit
def test_role_bindings_freeze_independent_models_and_generation_defaults():
    headers = {"X-Contract": "original"}
    generation = {"knowledge_retrieval": {"max_tokens": 4096}, "answer": {"max_tokens": 7000}}
    bindings = RoleBindings(
        "deepseek/deepseek-flash",
        LlmCredentialBundle(api_key="offline-role-key", extra_headers=headers),
        conversation_model="openai/contract-index",
        retrieval_model="openai/contract-retrieval",
        generation=generation,
    )
    headers.clear()
    generation["answer"]["max_tokens"] = 1
    expected = {
        "compile": "deepseek/deepseek-flash",
        "document_index": "deepseek/deepseek-flash",
        "answer": "deepseek/deepseek-flash",
        "conversation_index": "openai/contract-index",
        "knowledge_retrieval": "openai/contract-retrieval",
        "conversation_retrieval": "openai/contract-retrieval",
    }
    assert {role: bindings.model(role) for role in expected} == expected
    assert bindings.credentials().extra_headers == {"X-Contract": "original"}
    assert bindings.generation_options("answer") == {"max_tokens": 7000}
    assert bindings.generation_options("knowledge_retrieval") == {"max_tokens": 4096}
    bindings.generation_options("answer")["max_tokens"] = 2
    assert bindings.generation_options("answer") == {"max_tokens": 7000}


@pytest.mark.unit
def test_native_openai_output_limit_remains_a_completion_token_field():
    from ultrakb.llm_request_options import generation_kwargs

    options = generation_kwargs(
        "openai/contract-model",
        "http://127.0.0.1:1/v1",
        {"max_tokens": 7000, "reasoning_effort": "low"},
        {"max_completion_tokens": 24, "reasoning_effort": "none"},
    )
    assert options == {
        "max_completion_tokens": 24,
        "reasoning_effort": "none",
        "allowed_openai_params": ["reasoning_effort"],
        "drop_params": False,
    }


@pytest.mark.integration
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_frozen_request_parameters_reach_http_without_the_live_gateway(
    monkeypatch, asynchronous
):
    timeouts = []
    original_send, original_asend = httpx.Client.send, httpx.AsyncClient.send

    def observe(client, request, *args, **kwargs):
        timeouts.append(deepcopy(request.extensions.get("timeout")))
        return original_send(client, request, *args, **kwargs)

    async def observe_async(client, request, *args, **kwargs):
        timeouts.append(deepcopy(request.extensions.get("timeout")))
        return await original_asend(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", observe)
    monkeypatch.setattr(httpx.AsyncClient, "send", observe_async)
    monkeypatch.setenv("ULTRAKB_LLM_AUDIT", "1")
    with ErrorTransport(status=400) as service:
        headers = {"X-Contract": "captured-header"}
        bundle = LlmCredentialBundle(
            api_key="captured-contract-key",
            base_url=service.url,
            extra_headers=headers,
            timeout=0.75,
        )
        executor = CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                bundle,
                generation={"answer": {"max_tokens": 7000, "temperature": 1.5, "top_p": 0.9}},
            ),
            ModelCallPolicy(retries=0),
        )
        headers["X-Contract"] = "later-header"
        monkeypatch.setenv("OPENAI_API_KEY", "later-environment-key")
        monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:1/unselected")
        monkeypatch.setattr(litellm, "api_key", "later-global-key")
        options = {
            "max_tokens": 4096,
            "temperature": 0.2,
            "top_p": 0.4,
            "stop": ["CONTRACT_END"],
            "response_format": {"type": "json_object"},
            "seed": 7,
            "tool_choice": "none",
        }
        tools = [
            {
                "type": "function",
                "function": {"name": "read_source", "parameters": {"type": "object"}},
            }
        ]
        messages = [
            {"role": "system", "content": "Keep the contract."},
            {"role": "user", "content": "A tiny request."},
        ]
        request = ModelRequest(
            messages,
            operation="query",
            stage="parameter contract",
            tools=tools,
            generation_options=options,
        )
        snapshot = request.payload()
        messages[-1]["content"] = "Later mutated content."
        with pytest.raises(litellm.BadRequestError):
            if asynchronous:
                await executor.acomplete("answer", request)
            else:
                executor.complete("answer", request)
        assert len(service.requests) == executor.calls == 1
        record = service.requests[0]
        assert record["path"] == "/v1/chat/completions"
        received_headers = httpx.Headers(record["headers"])
        assert received_headers["authorization"] == "Bearer captured-contract-key"
        assert received_headers["x-contract"] == "captured-header"
        assert received_headers["x-ultrakb-step"] == "parameter%20contract"
        assert timeouts == [{"connect": 0.75, "read": 0.75, "write": 0.75, "pool": 0.75}]
        assert record["json"]["model"] == "deepseek-flash"
        assert record["json"]["messages"] == snapshot["messages"]
        assert record["json"]["tools"] == tools
        for name, value in options.items():
            assert record["json"][name] == value
        assert request.payload() == snapshot
