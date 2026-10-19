"""Small real-model contracts for parameters, parsing, tools and streaming."""

import json

import pytest


@pytest.mark.asyncio
async def test_json_mode_keeps_required_source_facts_and_uses_actual_receipts(live_llm):
    response = await live_llm.acompletion(
        model="deepseek/deepseek-flash",
        messages=[
            {
                "role": "user",
                "content": "Extract JSON fields project, credits, prerequisite from this source: "
                "Orion has 17 credits. Dispatch requires an enabled safety interlock.",
            }
        ],
        response_format={"type": "json_object"},
        max_tokens=128,
        temperature=0,
    )
    obj = json.loads(response.choices[0].message.content)
    assert obj["project"] == "Orion" and obj["credits"] == 17
    assert "interlock" in obj["prerequisite"].lower()
    assert response.usage.prompt_tokens > 0 and response.usage.completion_tokens > 0
    assert response.model == "deepseek-flash"


@pytest.mark.asyncio
async def test_equal_requests_share_one_real_reply_without_mutating_it(live_llm):
    kwargs = {
        "model": "deepseek/deepseek-flash",
        "messages": [{"role": "user", "content": "Return the JSON integer sum of 2 and 3."}],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
    }
    first = await live_llm.acompletion(**kwargs)
    sent = live_llm.budget.report()["requests"]
    original = first.choices[0].message.content
    first.choices[0].message.content = "Consumer-local mutation"
    second = await live_llm.acompletion(**kwargs)
    assert live_llm.budget.report()["requests"] == sent
    assert second.choices[0].message.content == original
    assert 5 in json.loads(original).values()


@pytest.mark.asyncio
async def test_stream_yields_provider_deltas_and_final_usage(live_llm):
    stream = await live_llm.acompletion(
        model="deepseek/deepseek-flash",
        messages=[{"role": "user", "content": "State this fact briefly: Orion has 17 credits."}],
        max_tokens=64,
        stream=True,
    )
    text, receipts = [], []
    try:
        async for chunk in stream:
            text.extend(choice.delta.content for choice in chunk.choices if choice.delta.content)
            if usage := getattr(chunk, "usage", None):
                receipts.append(usage)
    finally:
        await stream.aclose()
    assert "Orion" in "".join(text) and "17" in "".join(text)
    assert receipts[-1].prompt_tokens > 0 and receipts[-1].completion_tokens > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_managed_agent_uses_a_real_tool_result_then_delivers_the_answer(
    live_llm, monkeypatch, streaming
):
    from agents import Agent, ModelSettings, RunConfig, Runner, function_tool

    from ultrakb.agent.managed_model import ManagedAgentModel
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    live_llm.install(monkeypatch)
    reads = []

    @function_tool
    def read_original() -> str:
        """Read the permitted source's original dispatch rule."""
        reads.append("original")
        return "Orion has 17 credits; dispatch requires an enabled safety interlock."

    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            live_llm.credentials,
            generation={"answer": {"max_tokens": 128, "reasoning_effort": "none"}},
        ),
        ModelCallPolicy(max_calls=4, retries=0),
    )
    agent = Agent(
        name="source-rule",
        model=ManagedAgentModel(executor),
        instructions="Read the source with read_original exactly once, then answer its dispatch "
        "prerequisite and credit count in one sentence. Use only the tool's facts.",
        tools=[read_original],
        model_settings=ModelSettings(include_usage=True),
    )
    first = len(live_llm.requests)
    if streaming:
        result = Runner.run_streamed(
            agent, "What permits dispatch?", run_config=RunConfig(tracing_disabled=True)
        )
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent, "What permits dispatch?", run_config=RunConfig(tracing_disabled=True)
        )
    assert reads == ["original"]
    assert "17" in result.final_output and "interlock" in result.final_output.lower()
    sent = live_llm.requests[first:]
    assert len(sent) == 2
    assert any(message["role"] == "tool" for message in sent[-1]["messages"])
    assert result.context_wrapper.usage.input_tokens > 0


@pytest.mark.asyncio
async def test_real_multiturn_conversation_restores_intent_and_applies_a_later_correction(
    kb_dir, live_llm, monkeypatch
):
    import yaml

    from ultrakb.application.conversations import continue_conversation, read_conversation
    from ultrakb.application.execution import ExecutionContext

    live_llm.install(monkeypatch)
    config_path = kb_dir / ".ultrakb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update(
        model="deepseek/deepseek-flash",
        language="en",
        role_generation={"answer": {"max_tokens": 256, "reasoning_effort": "none"}},
    )
    config_path.write_text(yaml.safe_dump(config))
    (kb_dir / "wiki/sources/rooms.md").write_text(
        "Both the indoor room and the garden room permit quiet reading."
    )
    (kb_dir / "wiki/index.md").write_text("# Rooms\n[[sources/rooms]]: quiet reading rooms.")
    events = []
    first = await continue_conversation(
        kb_dir,
        "My preference is the indoor room. Remember my preference; briefly acknowledge.",
        context=ExecutionContext(on_event=events.append),
    )
    assert first.status == "completed", first.error
    session = read_conversation(kb_dir, first.session_id)
    assert session.total_turns == 1 and session.turns[0][1] == first.answer
    second = await continue_conversation(
        kb_dir,
        "Which room do I prefer? Answer in one short sentence.",
        session_id=first.session_id,
        context=ExecutionContext(on_event=events.append),
    )
    assert second.status == "completed", second.error
    assert "indoor" in second.answer.lower()
    third = await continue_conversation(
        kb_dir,
        "Correction: I now prefer the garden room. Which room do I prefer now?",
        session_id=first.session_id,
        context=ExecutionContext(on_event=events.append),
    )
    assert third.status == "completed", third.error
    assert "garden" in third.answer.lower()
    restored = read_conversation(kb_dir, first.session_id)
    assert restored.total_turns == 3
    assert restored.turns[0] == session.turns[0]
    assert restored.turns[-1][1] == third.answer
    assert any(event.get("event") == "delta" for event in events)
