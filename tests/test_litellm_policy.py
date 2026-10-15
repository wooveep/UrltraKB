"""Local SDK policy verified at import and real provider transport boundaries."""

import asyncio
import os
import subprocess
import sys

import pytest

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
async def test_closing_stream_prevents_another_transport(model_service):
    import litellm

    stream = await litellm.acompletion(
        model="deepseek/deepseek-flash",
        api_key=model_service.credentials.api_key,
        api_base=model_service.url,
        messages=[{"role": "user", "content": "Reply exactly ok."}],
        num_retries=0,
        max_retries=0,
        max_tokens=64,
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
async def test_partial_receipt_keeps_missing_counters_unknown(streaming):
    from ultrakb.llm_usage import normalize_usage

    counters = normalize_usage({"prompt_tokens": 12})
    assert counters["input_total"] == 12
    assert counters["output_total"] is None and counters["cached_input"] is None
    assert counters["usage_status"] == "partial"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_supplied_client_obeys_request_binding(model_service, asynchronous, live_llm):
    import openai

    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    client = client_type(api_key="old-key", base_url=model_service.url + "/old")
    try:
        kwargs = dict(
            model="openai/deepseek-flash",
            client=client,
            api_key=model_service.credentials.api_key,
            api_base=model_service.url + "/new",
            num_retries=0,
            max_retries=0,
            max_tokens=64,
            messages=[{"role": "user", "content": "Reply exactly ok."}],
        )
        result = (
            await live_llm._sdk_acompletion(**kwargs)
            if asynchronous
            else live_llm._sdk_completion(**kwargs)
        )
        assert result.choices[0].message.content.strip().lower() == "ok"
        _, headers, path = model_service.records[0]
        assert path == "/v1/new/chat/completions"
        assert {key.lower(): value for key, value in headers.items()}[
            "authorization"
        ] == "<redacted>"
        assert client.api_key == "old-key" and str(client.base_url).endswith("/old/")
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.parametrize("status", [401, 429, 500, 503])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_zero_retries_apply_before_first_transport(monkeypatch, status, asynchronous):
    import httpx
    import litellm
    import openai
    from litellm.llms.openai.openai import OpenAIChatCompletion

    monkeypatch.setattr(litellm, "num_retries", 3)
    client_type = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    client = client_type(
        api_key="local-parameter-check", base_url="http://127.0.0.1:1/v1", max_retries=3
    )
    try:
        prepared = OpenAIChatCompletion()._get_openai_client(
            is_async=asynchronous, client=client, max_retries=0, timeout=2
        )
        assert prepared.max_retries == 0 and client.max_retries == 3
        # HTTP status is an input to the retry classifier; no model reply is generated.
        response = httpx.Response(
            status, request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
        )
        assert prepared._should_retry(response) == (status == 429 or status >= 500)
        assert litellm.num_retries == 3
        if asynchronous:
            await prepared.close()
        else:
            prepared.close()
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.asyncio
async def test_parallel_requests_keep_credentials_endpoint_and_usage_isolated(
    model_service, live_llm
):
    async def call(task):
        return await live_llm._sdk_acompletion(
            model="openai/deepseek-flash",
            api_key=model_service.credentials.api_key,
            api_base=model_service.url + "/" + task,
            extra_headers={"x-test-task": task},
            timeout=20,
            num_retries=0,
            max_retries=0,
            max_tokens=64,
            messages=[{"role": "user", "content": "Reply exactly ok."}],
        )

    results = await asyncio.gather(call("a"), call("b"))
    assert all(result.choices[0].message.content.strip().lower() == "ok" for result in results)
    for body, headers, path in model_service.records:
        task = {key.lower(): value for key, value in headers.items()}["x-test-task"]
        assert (
            body["messages"][0]["content"] == "Reply exactly ok."
            and body["model"] == "deepseek-flash"
        )
        assert path == f"/v1/{task}/chat/completions"
        assert all(result.usage.prompt_tokens > 0 for result in results)
    assert {path for _, _, path in model_service.records} == {
        "/v1/a/chat/completions",
        "/v1/b/chat/completions",
    }


@pytest.mark.asyncio
async def test_timeout_and_cancellation_do_not_retry(model_service):
    import litellm

    kwargs = dict(
        model="deepseek/deepseek-flash",
        api_key=model_service.credentials.api_key,
        api_base=model_service.url,
        messages=[{"role": "user", "content": "Reply exactly ok."}],
        max_tokens=64,
        num_retries=0,
        max_retries=0,
    )
    await litellm.acompletion(**kwargs)
    warm = await litellm.acompletion(**kwargs, stream=True)
    async for _ in warm:
        pass
    model_service.requests.clear()
    model_service.delay = 0.25
    with pytest.raises(litellm.Timeout):
        await litellm.acompletion(**kwargs, timeout=0.05)
    assert len(model_service.requests) == 1
    task = asyncio.create_task(litellm.acompletion(**kwargs, timeout=2, stream=True))
    for _ in range(200):
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

    stream = await litellm.acompletion(
        model="deepseek/deepseek-flash",
        api_key=model_service.credentials.api_key,
        api_base=model_service.url,
        max_tokens=128,
        messages=[{"role": "user", "content": "Call read to obtain the source."}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read the source.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
        num_retries=0,
        max_retries=0,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [chunk async for chunk in stream]
    assert any(chunk.choices and chunk.choices[0].delta.tool_calls for chunk in chunks)
    assert any(chunk.choices and chunk.choices[0].finish_reason == "tool_calls" for chunk in chunks)
    usage = next(chunk.usage for chunk in chunks if getattr(chunk, "usage", None))
    assert usage.prompt_tokens > 0 and usage.completion_tokens > 0
    cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
    assert 0 <= cached <= usage.prompt_tokens


@pytest.mark.asyncio
async def test_stream_without_provider_receipt_keeps_usage_unknown():
    from ultrakb.llm_usage import normalize_usage

    counters = normalize_usage(None)
    assert counters["input_total"] is None and counters["output_total"] is None
    assert counters["usage_status"] == "unknown"


def test_import_is_offline_and_does_not_load_implicit_environment(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dotenv\nULTRAKB_DOTENV_SENTINEL=loaded\n")
    environment = dict(os.environ)
    for name in (
        "OPENAI_API_KEY",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_MODE",
        "ULTRAKB_DOTENV_SENTINEL",
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
assert 'ULTRAKB_DOTENV_SENTINEL' not in os.environ
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
