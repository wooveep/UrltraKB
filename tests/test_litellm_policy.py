"""Local SDK policy verified at import and real provider transport boundaries."""

import asyncio
import os
import subprocess
import sys

import pytest
from test_vendor_sdk import response, running_model_service, streamed_response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
async def test_closing_stream_prevents_another_transport(model_service):
    import litellm

    model_service.replies.extend(
        [(200, streamed_response({"role": "assistant", "content": "ok"}))] * 3
    )
    stream = await litellm.acompletion(
        model="openai/gpt-4o-mini",
        api_key="key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        num_retries=0,
        max_retries=0,
        stream=True,
    )
    await anext(stream)
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert len(model_service.requests) == 1


def test_model_table_has_fixed_identity_and_unknown_capacity():
    import litellm
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

    info = get_model_cost_map_source_info()
    assert info["version"] and len(info["sha256"]) == 64 and not info["prices_are_live"]
    known = litellm.get_model_info("gpt-4o-mini")
    assert known["supports_function_calling"] and known["max_input_tokens"] == 128000
    assert known["input_cost_per_token"] is not None
    with pytest.raises(Exception, match="model"):
        litellm.get_model_info("openai/definitely-unknown-fixture")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_partial_receipt_keeps_missing_counters_unknown(model_service, streaming):
    import litellm

    reply = response()
    reply["usage"] = {"prompt_tokens": 12}
    if streaming:
        reply = streamed_response({"role": "assistant", "content": "ok"})
        reply[-1]["usage"] = {"prompt_tokens": 12}
    model_service.replies.append((200, reply))
    result = await litellm.acompletion(
        model="openai/gpt-4o-mini",
        api_key="key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        num_retries=0,
        max_retries=0,
        stream=streaming,
        **({"stream_options": {"include_usage": True}} if streaming else {}),
    )
    if streaming:
        chunks = [chunk async for chunk in result]
        usage = next(chunk.usage for chunk in chunks if getattr(chunk, "usage", None))
    else:
        usage = result.usage
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens is None and usage.total_tokens is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_supplied_client_obeys_request_binding(model_service, asynchronous):
    import litellm
    import openai

    model_service.replies.append((200, response()))
    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    client = client_type(api_key="old-key", base_url=model_service.url + "/old")
    try:
        kwargs = dict(
            model="openai/gpt-4o-mini",
            client=client,
            api_key="new-key",
            api_base=model_service.url + "/new",
            num_retries=0,
            max_retries=0,
            messages=[{"role": "user", "content": "hello"}],
        )
        if asynchronous:
            await litellm.acompletion(**kwargs)
        else:
            litellm.completion(**kwargs)
        _, headers, path = model_service.records[0]
        assert path == "/v1/new/chat/completions"
        assert {k.lower(): v for k, v in headers.items()}["authorization"] == "Bearer new-key"
        assert client.api_key == "old-key" and str(client.base_url).endswith("/old/")
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.parametrize("status", [401, 429, 500, 503])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_zero_retries_apply_before_first_transport(
    model_service, monkeypatch, status, asynchronous
):
    import litellm

    monkeypatch.setattr(litellm, "num_retries", 3)
    model_service.replies.extend(
        [(status, {"error": {"message": "fixture error", "type": "server_error"}})] * 10
    )
    kwargs = dict(
        model="openai/gpt-4o-mini",
        api_key="isolated-key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        timeout=1,
        num_retries=0,
        max_retries=0,
    )
    with pytest.raises(
        {
            401: litellm.AuthenticationError,
            429: litellm.RateLimitError,
            500: litellm.InternalServerError,
            503: litellm.ServiceUnavailableError,
        }[status]
    ):
        if asynchronous:
            await litellm.acompletion(**kwargs)
        else:
            litellm.completion(**kwargs)
    assert len(model_service.requests) == 1
    assert litellm.num_retries == 3


@pytest.mark.asyncio
async def test_parallel_requests_keep_credentials_endpoint_and_usage_isolated(
    model_service, monkeypatch
):
    import litellm

    async def call(service, key, model):
        return await litellm.acompletion(
            model=model,
            api_key=key,
            api_base=service.url,
            timeout=2,
            num_retries=0,
            max_retries=0,
            messages=[{"role": "user", "content": key}],
        )

    with running_model_service(monkeypatch) as second:
        model_service.replies.append((200, response()))
        second.replies.append((200, response(usage=False)))
        results = await asyncio.gather(
            call(model_service, "kb-a", "openai/gpt-4o-mini"), call(second, "kb-b", "openai/gpt-4o")
        )
        for service, key, model in (
            (model_service, "kb-a", "gpt-4o-mini"),
            (second, "kb-b", "gpt-4o"),
        ):
            body, headers, path = service.records[0]
            assert body["messages"][0]["content"] == key and body["model"] == model
            assert {k.lower(): v for k, v in headers.items()}["authorization"] == "Bearer " + key
            assert path == "/v1/chat/completions"
    assert results[0].usage.prompt_tokens == 12 and results[1].usage is None


@pytest.mark.asyncio
async def test_timeout_and_cancellation_do_not_retry(model_service):
    import litellm

    model_service.delay = 0.25
    model_service.replies.extend([(200, response())] * 5)
    kwargs = dict(
        model="openai/gpt-4o-mini",
        api_key="key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        num_retries=0,
        max_retries=0,
    )
    with pytest.raises(litellm.Timeout):
        await litellm.acompletion(**kwargs, timeout=0.05)
    assert len(model_service.requests) == 1
    task = asyncio.create_task(litellm.acompletion(**kwargs, timeout=2, stream=True))
    for _ in range(100):
        if len(model_service.requests) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert len(model_service.requests) == 2


@pytest.mark.asyncio
async def test_stream_retains_tools_and_server_usage(model_service):
    import litellm

    tool = {"id": "read-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    model_service.replies.append(
        (
            200,
            streamed_response(
                {"role": "assistant", "content": "text", "tool_calls": [tool]}, "tool_calls"
            ),
        )
    )
    stream = await litellm.acompletion(
        model="openai/gpt-4o-mini",
        api_key="key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        num_retries=0,
        max_retries=0,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [chunk async for chunk in stream]
    assert any(c.choices and c.choices[0].delta.tool_calls for c in chunks)
    assert (
        next(
            c.usage for c in chunks if getattr(c, "usage", None)
        ).prompt_tokens_details.cached_tokens
        == 4
    )


@pytest.mark.asyncio
async def test_stream_without_provider_receipt_keeps_usage_unknown(model_service):
    import litellm

    model_service.replies.append(
        (200, streamed_response({"role": "assistant", "content": "ok"})[:-1])
    )
    stream = await litellm.acompletion(
        model="openai/gpt-4o-mini",
        api_key="key",
        api_base=model_service.url,
        messages=[{"role": "user", "content": "hello"}],
        num_retries=0,
        max_retries=0,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [chunk async for chunk in stream]
    assert all(getattr(chunk, "usage", None) is None for chunk in chunks)


def test_import_is_offline_and_does_not_load_implicit_environment(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dotenv\nURLTRAKB_DOTENV_SENTINEL=loaded\n")
    environment = dict(os.environ)
    for name in (
        "OPENAI_API_KEY",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_MODE",
        "URLTRAKB_DOTENV_SENTINEL",
    ):
        environment.pop(name, None)
    script = """
import os, socket
attempts = []
def forbidden(self, address):
    attempts.append(str(address))
    raise AssertionError('Network is forbidden during import')
socket.socket.connect = forbidden
import litellm
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
assert not attempts, attempts
assert 'URLTRAKB_DOTENV_SENTINEL' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
assert litellm.telemetry is False
assert not litellm.callbacks and not litellm.input_callback and not litellm.success_callback
assert 'gpt-4o-mini' in litellm.model_cost
info = get_model_cost_map_source_info()
assert info['source'] == 'local'
assert len(info['sha256']) == 64 and info['version']
assert info['prices_are_live'] is False
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_explicit_client_options_do_not_mutate_shared_client():
    import openai
    from litellm.llms.openai.openai import OpenAIChatCompletion

    with openai.OpenAI(api_key="fixture-key", timeout=30, max_retries=4) as client:
        request_client = OpenAIChatCompletion()._get_openai_client(
            is_async=False, client=client, max_retries=0, timeout=2, organization="kb-a"
        )
        assert client.max_retries == 4 and client.organization is None
        assert request_client.max_retries == 0 and request_client.organization == "kb-a"


def test_subscription_transformations_with_fixed_fixtures(tmp_path, monkeypatch):
    from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.llms.github_copilot.chat.transformation import GithubCopilotConfig
    from litellm.llms.github_copilot.responses.transformation import GithubCopilotResponsesAPIConfig
    from litellm.types.router import GenericLiteLLMParams
    from litellm.types.utils import ModelResponseStream

    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "copilot"))
    chat = ChatGPTConfig()
    chunks = [
        ModelResponseStream(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": name,
                                "type": "function",
                                "function": {"name": "read", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        )
        for name in ("a", "b", "a")
    ]
    normalized = list(chat.post_stream_processing(iter(chunks)))
    assert [c.choices[0].delta.tool_calls[0].index for c in normalized] == [0, 1]
    params = ChatGPTResponsesAPIConfig().transform_responses_api_request(
        "gpt-5", "hi", {"max_output_tokens": 12}, GenericLiteLLMParams(), {}
    )
    assert params["store"] is False and params["stream"] is True
    assert "reasoning.encrypted_content" in params["include"]
    copilot = GithubCopilotConfig()
    assert copilot._determine_initiator([{"role": "user", "content": "hi"}]) == "user"
    assert copilot._determine_initiator([{"role": "tool", "content": "done"}]) == "agent"
    responses = GithubCopilotResponsesAPIConfig()
    assert responses.map_openai_params({"max_output_tokens": 12}, "gpt-5", False) == {
        "max_output_tokens": 12
    }
