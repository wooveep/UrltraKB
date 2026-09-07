"""Tests for chat session persistence."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from openkb.agent import chat as chat_mod
from openkb.agent.chat import iter_chat_turn_events
from openkb.agent.chat_session import ChatSession, load_session


def _image_history() -> list[dict[str, object]]:
    return [
        {"role": "user", "content": "Describe the diagram."},
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "get_image",
            "arguments": '{"image_path":"sources/images/doc/figure-1.png"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,AAAA",
                }
            ],
        },
    ]


def test_record_turn_replaces_data_image_with_text_reference(tmp_path):
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")

    session.record_turn(
        "Describe the diagram.",
        "It is a flow chart.",
        _image_history(),
    )

    saved = json.loads(session.path.read_text(encoding="utf-8"))
    output_part = saved["history"][2]["output"][0]

    assert output_part["type"] == "input_text"
    assert "data:image/png;base64,AAAA" not in session.path.read_text(encoding="utf-8")
    assert "sources/images/doc/figure-1.png" in output_part["text"]
    assert "Call get_image again" in output_part["text"]


def test_load_session_sanitizes_legacy_image_history(tmp_path):
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    raw_history = _image_history()
    session.path.parent.mkdir(parents=True, exist_ok=True)
    session.path.write_text(
        json.dumps(
            {
                "id": session.id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "model": session.model,
                "language": session.language,
                "title": "",
                "turn_count": 1,
                "history": raw_history,
                "user_turns": ["Describe the diagram."],
                "assistant_texts": ["It is a flow chart."],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_session(tmp_path, session.id)

    output_part = loaded.history[2]["output"][0]
    assert output_part["type"] == "input_text"
    assert "data:image/png;base64,AAAA" not in output_part["text"]
    assert "sources/images/doc/figure-1.png" in output_part["text"]


def test_record_turn_persists_and_roundtrips_trace(tmp_path):
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    trace = [
        {"kind": "text", "text": "Let me check the index."},
        {"kind": "tool", "name": "read_file", "arguments": '{"path":"index.md"}'},
        {"kind": "text", "text": "Here is the answer."},
    ]

    session.record_turn("q", "Here is the answer.", [], trace=trace)

    saved = json.loads(session.path.read_text(encoding="utf-8"))
    assert saved["assistant_traces"] == [trace]

    loaded = load_session(tmp_path, session.id)
    assert loaded.assistant_traces == [trace]


def test_record_turn_without_trace_stores_empty_list(tmp_path):
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    session.record_turn("q", "a", [])
    assert session.assistant_traces == [[]]


def test_load_session_missing_traces_then_new_turn_stays_aligned(tmp_path):
    # A session saved before traces existed: assistant_texts present, no
    # assistant_traces key at all.
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    session.path.parent.mkdir(parents=True, exist_ok=True)
    session.path.write_text(
        json.dumps(
            {
                "id": session.id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "model": session.model,
                "language": session.language,
                "title": "old",
                "turn_count": 2,
                "history": [],
                "user_turns": ["q1", "q2"],
                "assistant_texts": ["a1", "a2"],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_session(tmp_path, session.id)
    assert loaded.assistant_traces == []  # backward compat: absent -> empty, no crash

    # A new turn back-fills empty traces for the two legacy turns (so index i
    # keeps mapping to the same turn), then appends the new turn's real trace.
    new_trace = [{"kind": "text", "text": "a3"}]
    loaded.record_turn("q3", "a3", [], trace=new_trace)
    assert loaded.assistant_traces == [[], [], new_trace]
    assert len(loaded.assistant_traces) == len(loaded.assistant_texts) == 3


def _fake_event_stream(events: list[dict[str, Any]]):
    """Build a stand-in for ``iter_agent_response_events`` that replays *events*."""

    async def _stream(agent, input_data, **kwargs) -> AsyncIterator[dict[str, Any]]:
        for event in events:
            yield event

    return _stream


@pytest.mark.asyncio
async def test_whitespace_only_delta_then_final_answer_lands_in_trace(kb_dir, monkeypatch):
    """A whitespace-only streamed delta must not swallow the final answer.

    Regression: a `" "` / `"\\n"` delta creates a text step, so a
    presence-only guard treats that empty step as "the answer is already in the
    trace" and never records the real answer -> a restored turn renders empty.
    The guard now requires a text step with non-whitespace content.
    """
    session = ChatSession.new(kb_dir, "gpt-4o-mini", "en")

    events = [
        {"event": "delta", "data": {"text": " "}},
        {"event": "delta", "data": {"text": "\n"}},
        {"event": "final", "data": {"answer": "The real answer.", "history": []}},
    ]
    monkeypatch.setattr(chat_mod, "iter_agent_response_events", _fake_event_stream(events))

    collected = [event async for event in iter_chat_turn_events(object(), session, "q")]

    # The final frame still carries the answer...
    final = collected[-1]
    assert final["event"] == "final"
    assert final["data"]["answer"] == "The real answer."

    # ...and the persisted trace contains the real answer as a text step, so a
    # restored turn is not blank.
    persisted_trace = session.assistant_traces[-1]
    assert any(
        step.get("kind") == "text" and step.get("text") == "The real answer."
        for step in persisted_trace
    )


@pytest.mark.asyncio
async def test_substantive_streamed_text_is_not_duplicated_by_final(kb_dir, monkeypatch):
    """When real text was streamed, the final answer must not be appended again."""
    session = ChatSession.new(kb_dir, "gpt-4o-mini", "en")

    events = [
        {"event": "delta", "data": {"text": "The real "}},
        {"event": "delta", "data": {"text": "answer."}},
        {"event": "final", "data": {"answer": "The real answer.", "history": []}},
    ]
    monkeypatch.setattr(chat_mod, "iter_agent_response_events", _fake_event_stream(events))

    _ = [event async for event in iter_chat_turn_events(object(), session, "q")]

    persisted_trace = session.assistant_traces[-1]
    text_steps = [s for s in persisted_trace if s.get("kind") == "text"]
    assert len(text_steps) == 1
    assert text_steps[0]["text"] == "The real answer."


def test_stale_session_cannot_overwrite_or_resurrect_completed_history(kb_dir):
    import pytest

    from openkb.agent.chat_session import ChatSession, delete_session, load_session

    session = ChatSession.new(kb_dir, "test", "zh")
    session.record_turn("One", "First answer", [])
    stale = load_session(kb_dir, session.id)
    session.record_turn("Two", "Second answer", [])
    with pytest.raises(RuntimeError, match="changed"):
        stale.record_turn("Stale", "Old context", [])
    assert load_session(kb_dir, session.id).user_turns == ["One", "Two"]
    assert delete_session(kb_dir, session.id)
    with pytest.raises(FileNotFoundError):
        session.record_turn("Three", "Must not resurrect", [])


@pytest.mark.asyncio
async def test_closing_chat_waits_for_model_work_before_releasing_write_lease(kb_dir, monkeypatch):
    import asyncio

    from agents import RawResponsesStreamEvent, Runner
    from openai.types.responses import ResponseTextDeltaEvent

    settled = asyncio.Event()
    stop_requested = asyncio.Event()

    class ModelRun:
        is_complete = False

        def cancel(self, mode="immediate"):
            assert mode == "after_turn"
            stop_requested.set()

        async def stream_events(self):
            try:
                yield RawResponsesStreamEvent(
                    data=ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        delta="Partial",
                        content_index=0,
                        item_id="message",
                        output_index=0,
                        sequence_number=0,
                        logprobs=[],
                    )
                )
                await stop_requested.wait()
            finally:
                await stop_requested.wait()
                settled.set()

    monkeypatch.setattr(Runner, "run_streamed", lambda *a, **kw: ModelRun())
    session = ChatSession.new(kb_dir, "test", "en")
    stream = iter_chat_turn_events(object(), session, "Question")
    assert (await anext(stream))["event"] == "delta"
    await stream.aclose()
    assert settled.is_set()
    assert not session.path.exists()


@pytest.mark.asyncio
async def test_cancelling_consumer_keeps_lease_until_real_sdk_work_settles(kb_dir, monkeypatch):
    import asyncio

    from agents import Agent, RunContextWrapper, Runner
    from agents.result import QueueCompleteSentinel, RunResultStreaming

    from openkb.locks import async_kb_lock

    started, finish, settled, competing = (asyncio.Event() for _ in range(4))

    async def model_work():
        started.set()
        await finish.wait()
        settled.set()
        result.is_complete = True
        result._event_queue.put_nowait(QueueCompleteSentinel())

    result = RunResultStreaming(
        input="x",
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(None),
        current_agent=Agent(name="test"),
        current_turn=1,
        max_turns=2,
        _current_agent_output_schema=None,
        trace=None,
        run_loop_task=asyncio.create_task(model_work()),
    )
    monkeypatch.setattr(Runner, "run_streamed", lambda *a, **kw: result)
    session = ChatSession.new(kb_dir, "test", "en")

    async def consume():
        return [event async for event in iter_chat_turn_events(object(), session, "x")]

    async def next_writer():
        async with async_kb_lock(kb_dir / ".openkb", exclusive=True, on_wait=competing.set):
            assert settled.is_set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 5)
    while not result._waiting_on_event_queue:
        await asyncio.sleep(0)
    consumer.cancel()
    contender = asyncio.create_task(next_writer())
    await asyncio.wait_for(competing.wait(), 5)
    assert not consumer.done()
    consumer.cancel()  # A second cancellation must not cut short cleanup.
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, 5)
    await asyncio.wait_for(contender, 5)
    assert result.run_loop_task.done()
    assert not session.path.exists()
