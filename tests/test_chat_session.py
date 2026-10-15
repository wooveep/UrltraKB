"""Tests for chat session persistence."""

from __future__ import annotations

import json

import pytest

from ultrakb.agent.chat import iter_chat_turn_events
from ultrakb.agent.chat_session import ChatSession, load_session

pytest_plugins = ("test_vendor_sdk",)


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
    assert "not interpreted by the current text model" in output_part["text"]


def test_load_session_sanitizes_image_history_in_current_format(tmp_path):
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    raw_history = _image_history()
    session.record_turn("Describe the diagram.", "It is a flow chart.", [])
    document = session.to_dict()
    document["history"] = raw_history
    session.path.write_text(
        json.dumps(document),
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
    session = ChatSession.new(tmp_path, "gpt-4o-mini", "en")
    session.record_turn("q1", "a1", [])
    session.record_turn("q2", "a2", [])
    document = session.to_dict()
    document.pop("assistant_traces")  # Display projections are optional.
    session.path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_session(tmp_path, session.id)
    assert loaded.assistant_traces == [[], []]

    new_trace = [{"kind": "text", "text": "a3"}]
    loaded.record_turn("q3", "a3", [], trace=new_trace)
    assert loaded.assistant_traces == [[], [], new_trace]
    assert len(loaded.assistant_traces) == len(loaded.assistant_texts) == 3


def test_whitespace_only_delta_then_final_answer_lands_in_trace(kb_dir):
    from ultrakb.agent.chat_trace import append_event, complete_trace

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    trace = []
    append_event(trace, {"event": "delta", "data": {"text": " "}})
    append_event(trace, {"event": "delta", "data": {"text": "\n"}})
    complete_trace(trace, "The retained answer.")
    session.record_turn("q", "The retained answer.", [], trace=trace)
    restored = load_session(kb_dir, session.id)
    assert restored.assistant_texts == ["The retained answer."]
    assert any(
        step.get("kind") == "text" and step.get("text") == "The retained answer."
        for step in restored.assistant_traces[-1]
    )


def test_substantive_streamed_text_is_not_duplicated_by_final(kb_dir):
    from ultrakb.agent.chat_trace import append_event, complete_trace

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    trace = []
    append_event(trace, {"event": "delta", "data": {"text": "The retained "}})
    append_event(trace, {"event": "delta", "data": {"text": "answer."}})
    complete_trace(trace, "The retained answer.")
    session.record_turn("q", "The retained answer.", [], trace=trace)
    text_steps = [
        step
        for step in load_session(kb_dir, session.id).assistant_traces[-1]
        if step.get("kind") == "text"
    ]
    assert text_steps == [{"kind": "text", "text": "The retained answer."}]


def test_stale_session_cannot_overwrite_or_resurrect_completed_history(kb_dir):
    import pytest

    from ultrakb.agent.chat_session import ChatSession, delete_session, load_session

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
async def test_closing_chat_waits_for_model_work_before_releasing_write_lease(
    kb_dir, model_service, monkeypatch
):
    from agents import RunConfig, Runner
    from test_llm_execution import executor_for
    from test_managed_agent import agent_for

    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/fixture.md", "Available original.")
    executor = executor_for(model_service, max_calls=2)
    agent = agent_for(executor)
    agent.instructions = "Answer without tools. Count from one to twenty in words."
    agent.model_settings.max_tokens = 128
    runs = []
    original = Runner.run_streamed

    def observe(*args, **kwargs):
        actual = original(*args, **kwargs)
        runs.append(actual)
        return actual

    monkeypatch.setattr(Runner, "run_streamed", observe)
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    stream = iter_chat_turn_events(
        agent, session, "Count the numbers.", run_config=RunConfig(tracing_disabled=True)
    )
    first = await anext(stream)
    while first["event"] != "delta":
        first = await anext(stream)
    assert first["data"]["text"]
    await stream.aclose()
    assert runs[0].run_loop_task.done()
    assert not session.path.exists()


@pytest.mark.asyncio
async def test_cancelling_consumer_keeps_lease_until_real_sdk_work_settles(
    kb_dir, model_service, monkeypatch
):
    import asyncio

    from agents import RunConfig, Runner
    from litellm import CustomStreamWrapper
    from test_llm_execution import executor_for
    from test_managed_agent import agent_for

    from ultrakb.locks import async_kb_lock, atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/fixture.md", "Available original.")
    executor = executor_for(model_service, max_calls=2)
    agent = agent_for(executor)
    agent.instructions = "Answer without tools. List the twelve month names in order."
    agent.model_settings.max_tokens = 128
    first_delta = asyncio.Event()
    sdk_waiting = asyncio.Event()
    release_sdk = asyncio.Event()
    writer_waiting = asyncio.Event()
    delivered_content = False
    original_next = CustomStreamWrapper.__anext__

    async def gated_chunk(stream):
        nonlocal delivered_content
        if delivered_content:
            sdk_waiting.set()
            await release_sdk.wait()
        chunk = await original_next(stream)
        if any(choice.delta.content for choice in chunk.choices):
            delivered_content = True
        return chunk

    monkeypatch.setattr(CustomStreamWrapper, "__anext__", gated_chunk)
    runs = []
    original = Runner.run_streamed

    def observe(*args, **kwargs):
        actual = original(*args, **kwargs)
        runs.append(actual)
        return actual

    monkeypatch.setattr(Runner, "run_streamed", observe)
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")

    async def consume():
        from contextlib import aclosing

        stream = iter_chat_turn_events(
            agent, session, "Name the months.", run_config=RunConfig(tracing_disabled=True)
        )
        async with aclosing(stream):
            async for event in stream:
                if event["event"] == "delta":
                    first_delta.set()
                    # Consumer-side scheduling does not fabricate or alter model events.
                    await asyncio.Event().wait()

    async def next_writer():
        async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True, on_wait=writer_waiting.set):
            assert runs[0].run_loop_task.done()

    consumer = asyncio.create_task(consume())
    contender = None
    try:
        await asyncio.wait_for(first_delta.wait(), 30)
        await asyncio.wait_for(sdk_waiting.wait(), 30)
        assert not runs[0].run_loop_task.done()
        consumer.cancel()
        contender = asyncio.create_task(next_writer())
        await asyncio.wait_for(writer_waiting.wait(), 30)
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done() and not contender.done()
        assert not runs[0].run_loop_task.done()
        release_sdk.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 30)
        await asyncio.wait_for(contender, 30)
    finally:
        release_sdk.set()
        consumer.cancel()
        if contender is not None:
            contender.cancel()
        await asyncio.gather(
            consumer, *([contender] if contender is not None else []), return_exceptions=True
        )
    assert runs[0].run_loop_task.done()
    assert not session.path.exists()
