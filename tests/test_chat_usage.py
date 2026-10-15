"""Reported usage follows each model request through persistence, including stopped turns."""

import pytest

from ultrakb.agent.chat_session import ChatSession, load_session
from ultrakb.agent.token_usage import TokenUsage, add_usage

pytest_plugins = ("test_vendor_sdk",)


def test_usage_keeps_unknown_details_and_does_not_double_count_reasoning():
    usage = TokenUsage.from_provider(
        {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 60},
            "output_tokens": 25,
            "output_tokens_details": {"reasoning_tokens": 10},
        }
    )
    assert usage == TokenUsage(60, 40, 25, 10)
    unknown = TokenUsage.from_provider({"input_tokens": 100, "output_tokens": 20})
    assert unknown == TokenUsage(None, None, 20, None)
    assert (
        add_usage(usage.to_dict(), unknown.to_dict()) == TokenUsage(None, None, 45, None).to_dict()
    )
    with pytest.raises(ValueError):
        TokenUsage.from_dict({"output": -1})
    with pytest.raises(ValueError):
        TokenUsage.from_dict({"output": True})


@pytest.mark.asyncio
async def test_sdk_response_usage_accumulates_all_requests(model_service, kb_dir):
    from agents import RunConfig, function_tool
    from test_llm_execution import executor_for
    from test_managed_agent import agent_for

    from ultrakb.agent.query import iter_agent_response_events
    from ultrakb.llm_usage import aggregate_usage
    from ultrakb.llm_usage_execution import import_usage_execution

    @function_tool
    def read_original() -> str:
        """Read the retained original budget fact."""
        return "The approved personal budget is 17 credits."

    with import_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service, max_calls=4)
        agent = agent_for(executor, [read_original])
        agent.instructions = "Call read_original exactly once; then report its approved budget."
        agent.model_settings.max_tokens = 128
        events = [
            event
            async for event in iter_agent_response_events(
                agent, "What is the approved budget?", run_config=RunConfig(tracing_disabled=True)
            )
        ]
    starts = [index for index, event in enumerate(events) if event["event"] == "answer_start"]
    usages = [index for index, event in enumerate(events) if event["event"] == "usage"]
    assert len(starts) == len(usages) == executor.calls == len(model_service.requests) == 2
    assert starts[0] < usages[0] < starts[1] < usages[1]
    assert events[-1]["event"] == "final" and "17" in events[-1]["data"]["answer"]
    summary = aggregate_usage(kb_dir)
    total = events[-1]["data"]["usage"]
    unknown = summary["unknown_fields"]
    cached = None if unknown["cached_input"] else summary["cached_input"]
    inputs = None if unknown["input_total"] else summary["input_total"]
    expected = TokenUsage(
        cached,
        inputs - cached if inputs is not None and cached is not None else None,
        None if unknown["output_total"] else summary["output_total"],
        None if unknown["reasoning_output"] else summary["reasoning_output"],
    ).to_dict()
    assert total == expected
    assert total == events[usages[-1]]["data"]


@pytest.mark.parametrize("complete", [True, False])
def test_usage_persists_once_even_if_a_turn_is_stopped(kb_dir, complete):
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    receipt = TokenUsage(60, 40, 25, 10).to_dict()
    session.record_turn("old", "answer", [], usage=receipt)
    baseline = session.token_usage
    session.record_usage(receipt, baseline=baseline)
    session.record_usage(receipt, baseline=baseline)
    if complete:
        session.record_turn("next question", "next answer", [])
    restored = load_session(kb_dir, session.id)
    assert restored.token_usage == TokenUsage(120, 80, 50, 20).to_dict()
    assert restored.turn_count == (2 if complete else 1)


def test_streaming_reasoning_tags_are_hidden_at_every_chunk_boundary():
    from ultrakb.agent.answer_text import visible_answer

    for tag in ("think", "thinking", "analysis", "reasoning"):
        source = f"<{tag}>private hidden details</{tag}>Visible answer"
        for index in range(len(source) + 1):
            assert "private" not in visible_answer(source[:index], streaming=True)
        assert visible_answer(source, streaming=True) == "Visible answer"
    assert "<think>" in visible_answer("```html\n<think>\n```", streaming=True)
