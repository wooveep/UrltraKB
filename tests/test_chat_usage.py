"""Reported usage follows each model request through persistence, including stopped turns."""

from types import SimpleNamespace

import pytest

from openkb.agent.chat import iter_chat_turn_events
from openkb.agent.chat_session import ChatSession, load_session
from openkb.agent.token_usage import TokenUsage, add_usage


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
async def test_sdk_response_usage_accumulates_all_requests(monkeypatch):
    from agents import RawResponsesStreamEvent, Runner

    from openkb.agent.query import iter_agent_response_events

    class Run:
        is_complete = False
        final_output = "answer"

        async def stream_events(self):
            for n in (1, 2):
                yield RawResponsesStreamEvent(data=SimpleNamespace(type="response.created"))
                yield RawResponsesStreamEvent(
                    data=SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage={
                                "input_tokens": 100 * n,
                                "input_tokens_details": {"cached_tokens": 60 * n},
                                "output_tokens": 20 * n,
                                "output_tokens_details": {"reasoning_tokens": 5 * n},
                            }
                        ),
                    )
                )
            self.is_complete = True

        def to_input_list(self):
            return []

        def cancel(self, **kwargs):
            self.is_complete = True

    monkeypatch.setattr(Runner, "run_streamed", lambda *a, **kw: Run())
    events = [e async for e in iter_agent_response_events(None, "question")]
    assert [e["event"] for e in events] == [
        "answer_start",
        "usage",
        "answer_start",
        "usage",
        "final",
    ]
    assert events[-1]["data"]["usage"] == TokenUsage(180, 120, 60, 15).to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_usage_persists_once_even_if_a_turn_is_stopped(kb_dir, monkeypatch, complete):
    session = ChatSession.new(kb_dir, "test", "zh")
    usage = TokenUsage(60, 40, 25, 10).to_dict()
    session.record_turn("old", "answer", [], usage=usage)

    async def stream(*a, **kw):
        yield {"event": "usage", "data": usage}
        if complete:
            yield {"event": "final", "data": {"answer": "next", "history": [], "usage": usage}}

    monkeypatch.setattr("openkb.agent.chat.iter_agent_response_events", stream)
    events = [e async for e in iter_chat_turn_events(None, session, "next question")]
    assert events[0]["event"] == "usage"
    restored = load_session(kb_dir, session.id)
    assert restored.token_usage == TokenUsage(120, 80, 50, 20).to_dict()
    assert restored.turn_count == (2 if complete else 1)


def test_streaming_reasoning_tags_are_hidden_at_every_chunk_boundary():
    from openkb.agent.answer_text import visible_answer

    for tag in ("think", "thinking", "analysis", "reasoning"):
        source = f"<{tag}>private hidden details</{tag}>Visible answer"
        for index in range(len(source) + 1):
            assert "private" not in visible_answer(source[:index], streaming=True)
        assert visible_answer(source, streaming=True) == "Visible answer"
    assert "<think>" in visible_answer("```html\n<think>\n```", streaming=True)
