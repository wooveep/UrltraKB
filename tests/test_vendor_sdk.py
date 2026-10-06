"""Real vendor/Agents SDK requests against a local, recording model service."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


@pytest.fixture
def model_service(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    monkeypatch.setattr(litellm, "telemetry", False)
    monkeypatch.setattr(litellm, "suppress_debug_info", True)
    service = SimpleNamespace(requests=[], replies=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            service.requests.append(request)
            status, response = service.replies.pop(0)
            if isinstance(response, list):
                data = (
                    "".join(f"data: {json.dumps(item)}\n\n" for item in response)
                    + "data: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            else:
                data = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        service.url = f"http://127.0.0.1:{server.server_port}/v1"
        monkeypatch.setenv("OPENAI_API_BASE", service.url)
        monkeypatch.setenv("ANTHROPIC_API_BASE", service.url)
        try:
            yield service
        finally:
            server.shutdown()
            thread.join(5)


def response(message=None, *, reason="stop", usage=True):
    result = {
        "id": "fixture-response",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": message or {"role": "assistant", "content": "ok"},
                "finish_reason": reason,
            }
        ],
    }
    if usage:
        result["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 4},
        }
    return result


def test_condb_preserves_tool_exchange_and_all_assistant_text(model_service):
    from contextdb import LLMClient

    model_service.replies.append((200, response()))
    client = LLMClient("openai", model="gpt-4o-mini", api_key="fixture-key")
    result = client.chat(
        [
            {"role": "user", "content": "Find the permitted source"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First explanation"},
                    {"type": "text", "text": "Second explanation"},
                    {"type": "tool_use", "id": "lookup-1", "name": "lookup", "input": {"id": "a"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "lookup-1", "content": "Source A"},
                ],
            },
        ],
        tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
    )
    sent = model_service.requests[0]
    assert sent["messages"][1]["content"] == "First explanation\nSecond explanation"
    assert sent["messages"][2] == {
        "role": "tool",
        "tool_call_id": "lookup-1",
        "content": "Source A",
    }
    assert sent["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert result["content"] == [{"type": "text", "text": "ok"}]
    assert result["usage"]["input_tokens"] == 12
    assert result["usage"]["cached_tokens"] == 4


@pytest.mark.asyncio
async def test_current_compiler_uses_local_sdk_and_compatible_endpoint(model_service, kb_dir):
    from openkb.agent.compiler import compile_short_doc
    from openkb.config import LlmCredentialBundle
    from openkb.locks import atomic_write_text

    source = kb_dir / "wiki" / "sources" / "baseline.md"
    atomic_write_text(source, "# Baseline\n\nOnly the permitted source is available.")
    summary = {"description": "Local SDK baseline", "content": "# Baseline\n\nCompiled locally."}
    for content in (summary, {"concepts": {"create": [], "update": [], "related": []}}):
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(content),
                    }
                ),
            )
        )
    await compile_short_doc(
        "baseline",
        source,
        kb_dir,
        "openai/gpt-4o-mini",
        bundle=LlmCredentialBundle(api_key="fixture-key", base_url=model_service.url),
    )
    assert "Compiled locally." in (kb_dir / "wiki" / "summaries" / "baseline.md").read_text()
    assert len(model_service.requests) == 2
    assert all(item["model"] == "gpt-4o-mini" for item in model_service.requests)
    assert "Only the permitted source" in json.dumps(model_service.requests[0]["messages"])


def streamed_response(message, reason="stop"):
    result = response(message, reason=reason)
    chunk = {key: value for key, value in result.items() if key not in {"choices", "usage"}}
    chunk["object"] = "chat.completion.chunk"
    if "tool_calls" in message:
        message = {
            **message,
            "tool_calls": [
                {**tool, "index": index} for index, tool in enumerate(message["tool_calls"])
            ],
        }
    return [
        {**chunk, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        {**chunk, "choices": [], "usage": result["usage"]},
    ]


@pytest.mark.asyncio
async def test_ordinary_query_returns_provider_answer(model_service, kb_dir):
    from openkb.agent.query import build_run_config_from_bundle, run_query
    from openkb.config import LlmCredentialBundle
    from openkb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki" / "index.md", "# Index\n\nBaseline knowledge.")
    model_service.replies.append(
        (200, response({"role": "assistant", "content": "Baseline answer."}))
    )
    bundle = LlmCredentialBundle(api_key="fixture-key", base_url=model_service.url)
    config = build_run_config_from_bundle("openai/gpt-4o-mini", bundle)
    config.tracing_disabled = True
    answer = await run_query(
        "Explain baseline knowledge.",
        kb_dir,
        "openai/gpt-4o-mini",
        bundle=bundle,
        run_config=config,
    )
    assert answer.startswith("Baseline answer.")
    assert len(model_service.requests) == 1
    assert "Explain baseline knowledge." in json.dumps(model_service.requests[0]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_query_agent_tools_stream_and_usage_use_real_sdk(model_service, kb_dir, stream):
    from agents import Runner

    from openkb.agent.query import build_query_agent, build_run_config_from_bundle
    from openkb.config import LlmCredentialBundle
    from openkb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "summaries" / "baseline.md", "# Baseline\n\nEvidence: forty-two.")
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "lookup-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "summaries/baseline.md"}),
                },
            }
        ],
    }
    final_message = {"role": "assistant", "content": "The evidence says forty-two."}
    for message, reason in ((tool_message, "tool_calls"), (final_message, "stop")):
        reply = streamed_response(message, reason) if stream else response(message, reason=reason)
        model_service.replies.append((200, reply))
    bundle = LlmCredentialBundle(api_key="fixture-key", base_url=model_service.url)
    agent = build_query_agent(str(wiki), "openai/gpt-4o-mini", bundle=bundle)
    config = build_run_config_from_bundle("openai/gpt-4o-mini", bundle)
    config.tracing_disabled = True
    if stream:
        result = Runner.run_streamed(agent, "Read the baseline evidence.", run_config=config)
        deltas = []
        async for event in result.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                deltas.append(event.data.delta)
        assert "".join(deltas) == "The evidence says forty-two."
    else:
        result = await Runner.run(agent, "Read the baseline evidence.", run_config=config)
    assert result.final_output == "The evidence says forty-two."
    usage = result.context_wrapper.usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (24, 6, 30)
    assert len(model_service.requests) == 2
    exchange = model_service.requests[1]["messages"]
    tool_result = next(message for message in exchange if message["role"] == "tool")
    assert tool_result["tool_call_id"] == "lookup-1"
    assert "Evidence: forty-two." in tool_result["content"]


def test_condb_anthropic_cache_controls_reach_provider_and_usage_returns(model_service):
    from contextdb import LLMClient

    model_service.replies.append(
        (
            200,
            {
                "id": "cache-response",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Cached answer"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                },
            },
        )
    )
    client = LLMClient("anthropic", model="claude-sonnet-4-6", api_key="fixture-key")
    result = client.chat_with_cache(
        [{"role": "user", "content": "Question"}],
        system="System policy",
        tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
        cache_content=["Stable evidence", "Stable index"],
        non_cached_content="Fresh facts",
    )
    sent = model_service.requests[0]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    blocks = sent["messages"][0]["content"]
    assert [block["text"] for block in blocks] == [
        "Stable evidence",
        "Stable index",
        "Fresh facts",
        "Question",
    ]
    assert [bool(block.get("cache_control")) for block in blocks] == [True, True, False, False]
    assert result["content"] == [{"type": "text", "text": "Cached answer"}]
    assert result["usage"]["cache_creation_input_tokens"] == 20
    assert result["usage"]["cache_read_input_tokens"] == 30
    assert result["usage"]["output_tokens"] == 3


def test_condb_returns_tool_use_and_propagates_sdk_errors(model_service):
    import litellm
    from contextdb import LLMClient

    client = LLMClient("openai", model="gpt-4o-mini", api_key="fixture-key")
    model_service.replies.append(
        (
            200,
            response(
                {
                    "role": "assistant",
                    "content": "Looking up evidence",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"id": "a"}',
                            },
                        }
                    ],
                },
                reason="tool_calls",
            ),
        )
    )
    result = client.chat_with_cache(
        [{"role": "user", "content": "Question"}],
        cache_content="Stable evidence",
        non_cached_content="Fresh facts",
    )
    assert result["stop_reason"] == "tool_use"
    assert result["content"] == [
        {"type": "text", "text": "Looking up evidence"},
        {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"id": "a"}},
    ]
    assert model_service.requests[0]["messages"][0]["content"] == (
        "Stable evidence\n\nFresh facts\n\nQuestion"
    )
    model_service.replies.append(
        (
            401,
            {
                "error": {
                    "message": "Incorrect API key provided: fixture-key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    )
    with pytest.raises(litellm.AuthenticationError, match="Incorrect API key"):
        client.chat([{"role": "user", "content": "Question"}])
