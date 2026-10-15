"""Real vendor/Agents SDK requests against a local, recording model service."""

import json

import pytest
from model_service_fixtures import model_service, running_model_service  # noqa: F401


def test_condb_preserves_tool_exchange_and_all_assistant_text(live_llm, monkeypatch):
    from contextdb import LLMClient

    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    client = LLMClient("deepseek", model="deepseek-flash", api_key=live_llm.credentials.api_key)
    result = client.chat(
        [
            {
                "role": "user",
                "content": "Give the permitted source and its cost from the completed lookup. "
                "Do not call it again.",
            },
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "lookup-1",
                        "content": "Source A costs 17 credits.",
                    }
                ],
            },
        ],
        tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
    )
    sent = live_llm.calls[before]
    assert sent["messages"][1]["content"] == "First explanation\nSecond explanation"
    assert sent["messages"][2] == {
        "role": "tool",
        "tool_call_id": "lookup-1",
        "content": "Source A costs 17 credits.",
    }
    assert sent["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert "17" in "".join(item.get("text", "") for item in result["content"])
    assert result["usage"]["input_tokens"] > 0 and result["usage"]["output_tokens"] > 0
    assert (
        result["usage"]["total_tokens"]
        == result["usage"]["input_tokens"] + result["usage"]["output_tokens"]
    )
    assert 0 <= result["usage"].get("cached_tokens", 0) <= result["usage"]["input_tokens"]


@pytest.mark.asyncio
async def test_current_compiler_uses_local_sdk_and_compatible_endpoint(
    live_llm, monkeypatch, kb_dir
):
    from ultrakb.agent.compiler import compile_short_doc
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    source = kb_dir / "wiki/sources/baseline.md"
    atomic_write_text(
        source, "# Baseline\n\nOrion costs 17 credits; inspecting the safety interlock is required."
    )
    await compile_short_doc(
        "baseline", source, kb_dir, "deepseek/deepseek-flash", bundle=live_llm.credentials
    )
    content = (kb_dir / "wiki/summaries/baseline.md").read_text().lower()
    assert "orion" in content and "17" in content and "interlock" in content
    calls = live_llm.calls[before:]
    assert 2 <= len(calls) <= 12
    assert all(item["model"] == "deepseek/deepseek-flash" for item in calls)
    assert "Orion costs 17 credits" in json.dumps(calls[0]["messages"])
    assert calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_ordinary_query_returns_the_provider_answer_without_an_extra_review(
    live_llm, monkeypatch, kb_dir
):
    from copy import deepcopy

    from ultrakb.agent.query import build_run_config_from_bundle, run_query
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    atomic_write_text(kb_dir / "wiki/index.md", "# Index\n\nBaseline: 17 credits.")
    atomic_write_text(kb_dir / "wiki/sources/baseline.md", "Baseline: 17 credits.")
    delivered = []
    completion_calls = []

    async def observe(**kwargs):
        completion_calls.append(deepcopy(kwargs["messages"]))
        result = await live_llm.acompletion(**kwargs)
        delivered.append(result)
        return result

    monkeypatch.setattr("litellm.acompletion", observe)
    config = build_run_config_from_bundle("deepseek/deepseek-flash", live_llm.credentials)
    config.tracing_disabled = True
    answer = await run_query(
        "The baseline is 17 credits. Give only the amount from this supplied fact.",
        kb_dir,
        "deepseek/deepseek-flash",
        bundle=live_llm.credentials,
        run_config=config,
    )
    assert "17" in answer
    assert delivered and len(delivered) == len(completion_calls)
    assert answer == delivered[-1].choices[0].message.content
    assert not delivered[-1].choices[0].message.tool_calls

    def instructions(messages):
        return [message for message in messages if message["role"] in {"system", "developer"}]

    def questions(messages):
        return [message for message in messages if message["role"] == "user"]

    # An ordinary answer may first read the index or a source. Every additional
    # SDK request must continue that real tool exchange, with the original
    # instructions and question; no finished answer is sent to a review agent.
    original_instructions = instructions(completion_calls[0])
    original_questions = questions(completion_calls[0])
    for previous, following in zip(delivered[:-1], completion_calls[1:], strict=True):
        tool_calls = previous.choices[0].message.tool_calls
        assert tool_calls
        assert instructions(following) == original_instructions
        assert questions(following) == original_questions
        returned = {message["tool_call_id"] for message in following if message["role"] == "tool"}
        assert {tool.id for tool in tool_calls} <= returned


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_query_agent_tools_stream_and_usage_use_real_sdk(
    live_llm, monkeypatch, kb_dir, stream
):
    from agents import Runner

    from ultrakb.agent.query import build_query_agent, build_run_config_from_bundle
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "summaries/baseline.md", "# Baseline\n\nEvidence: forty-two.")
    before = len(live_llm.calls)
    agent = build_query_agent(str(wiki), "deepseek/deepseek-flash", bundle=live_llm.credentials)
    config = build_run_config_from_bundle("deepseek/deepseek-flash", live_llm.credentials)
    config.tracing_disabled = True
    question = "Read summaries/baseline.md with read_file, then report its evidence."
    if stream:
        result = Runner.run_streamed(agent, question, run_config=config, max_turns=4)
        deltas = []
        final_deltas = []
        async for event in result.stream_events():
            if event.type == "raw_response_event" and event.data.type == "response.created":
                final_deltas = []
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                deltas.append(event.data.delta)
                final_deltas.append(event.data.delta)
        assert deltas and "".join(final_deltas) == result.final_output
        assert "".join(deltas).endswith(result.final_output)
    else:
        result = await Runner.run(agent, question, run_config=config, max_turns=4)
    assert "forty-two" in result.final_output or "42" in result.final_output
    usage = result.context_wrapper.usage
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens
    calls = live_llm.calls[before:]
    assert 2 <= len(calls) <= 4
    tools = [
        message for call in calls[1:] for message in call["messages"] if message["role"] == "tool"
    ]
    assert tools and all(message["tool_call_id"] for message in tools)
    assert any("Evidence: forty-two." in message["content"] for message in tools)


def test_condb_anthropic_cache_controls_reach_provider_and_usage_returns(monkeypatch):
    from contextdb import LLMClient

    class PreparedRequest(Exception):
        pass

    prepared = []

    def capture(**kwargs):
        prepared.append(kwargs)
        raise PreparedRequest("Request preparation only; no model output needed")

    client = LLMClient("anthropic", model="claude-sonnet-4-6")
    monkeypatch.setattr(client, "_call", capture)
    with pytest.raises(PreparedRequest):
        client.chat_with_cache(
            [{"role": "user", "content": "Question"}],
            system="System policy",
            tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
            cache_content=["Stable evidence", "Stable index"],
            non_cached_content="Fresh facts",
        )
    sent = prepared[0]
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][-1]["function"]["cache_control"] == {"type": "ephemeral"}
    blocks = sent["messages"][1]["content"]
    assert [block["text"] for block in blocks] == [
        "Stable evidence",
        "Stable index",
        "Fresh facts",
        "Question",
    ]
    assert [bool(block.get("cache_control")) for block in blocks] == [True, True, False, False]


def test_condb_returns_tool_use_and_propagates_sdk_errors(live_llm, monkeypatch):
    import litellm
    from contextdb import LLMClient

    live_llm.install(monkeypatch)
    client = LLMClient("deepseek", model="deepseek-flash", api_key=live_llm.credentials.api_key)
    before = len(live_llm.calls)
    result = client.chat_with_cache(
        [
            {
                "role": "user",
                "content": "Use lookup to fetch source id a; do not answer without calling it.",
            }
        ],
        tools=[
            {
                "name": "lookup",
                "description": "Fetch a source by id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            }
        ],
        cache_content="Stable evidence",
        non_cached_content="Fresh facts",
    )
    assert result["stop_reason"] == "tool_use"
    tool = next(item for item in result["content"] if item["type"] == "tool_use")
    assert tool["id"] and tool["name"] == "lookup" and tool["input"] == {"id": "a"}
    assert live_llm.calls[before]["messages"][0]["content"].startswith(
        "Stable evidence\n\nFresh facts\n\n"
    )
    # Exercise genuine SDK parameter rejection before any provider request.
    monkeypatch.setattr(litellm, "completion", live_llm._sdk_completion)
    invalid = LLMClient("unsupported-provider-for-test", model="invalid-model")
    with pytest.raises(litellm.BadRequestError):
        invalid.chat([{"role": "user", "content": "Question"}])
