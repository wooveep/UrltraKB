"""Conversation outcomes through shared use cases and the external SDK seam."""

import asyncio

import pytest
from agents import RawResponsesStreamEvent, Runner
from openai.types.responses import ResponseTextDeltaEvent

from openkb.agent.chat_session import ChatSession, load_session
from openkb.application.conversations import continue_conversation
from openkb.application.execution import ExecutionContext


@pytest.mark.asyncio
async def test_concurrent_continuations_reload_completed_history(kb_dir, monkeypatch):
    first_running, finish_first, second_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = ChatSession.new(kb_dir, "openai/original", "zh")
    session.record_turn("Original", "Saved", [])
    inputs = []

    class ModelRun:
        is_complete = False
        final_output = "Answer"

        def __init__(self, history):
            self.history = history
            inputs.append(history)

        async def stream_events(self):
            question = self.history[-1]["content"]
            if question == "First":
                first_running.set()
                await finish_first.wait()
            yield RawResponsesStreamEvent(
                data=ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    delta="Answer",
                    content_index=0,
                    item_id="message",
                    output_index=0,
                    sequence_number=0,
                    logprobs=[],
                )
            )
            self.is_complete = True

        def to_input_list(self):
            return self.history + [{"role": "assistant", "content": "Answer"}]

    monkeypatch.setattr(Runner, "run_streamed", lambda agent, history, **kw: ModelRun(history))
    first = asyncio.create_task(continue_conversation(kb_dir, "First", session_id=session.id))
    await asyncio.wait_for(first_running.wait(), 5)
    context = ExecutionContext(
        on_event=lambda event: second_waiting.set() if event.get("stage") == "waiting" else None
    )
    second = asyncio.create_task(
        continue_conversation(
            kb_dir,
            "Second",
            session_id=session.id,
            context=context,
        )
    )
    await asyncio.wait_for(second_waiting.wait(), 5)
    assert len(inputs) == 1
    finish_first.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert [result.turn_count for result in outcomes] == [2, 3]
    assert [turn["content"] for turn in inputs[1] if turn["role"] == "user"] == ["First", "Second"]
    saved = load_session(kb_dir, session.id)
    assert saved.user_turns == ["Original", "First", "Second"]
    assert saved.model == "openai/original"
    assert saved.language == "zh"
