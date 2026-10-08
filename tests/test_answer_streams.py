"""Disconnecting an HTTP observer must not release the SDK's write lease early."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from agents import RawResponsesStreamEvent, Runner
from openai.types.responses import ResponseTextDeltaEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["query", "chat"])
async def test_disconnect_waits_for_sdk_cleanup_even_after_repeated_cancel(
    kb_dir, monkeypatch, endpoint
):
    from openkb.agent.chat_session import list_sessions, load_session
    from openkb.api_helpers import _stream_chat, _stream_query
    from openkb.api_models import ChatRequest, QueryRequest
    from openkb.locks import async_kb_lock

    (kb_dir / "wiki/sources/cancellation-fixture.md").write_text("Readable original.")
    stopping, release, finished, acquired = (asyncio.Event() for _ in range(4))
    started = asyncio.Event()
    pending = asyncio.Event()

    class Run:
        is_complete = False
        final_output = "Must not become a completed answer"

        async def stream_events(self):
            try:
                started.set()
                yield RawResponsesStreamEvent(
                    data=ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        delta="Partial progress",
                        item_id="answer",
                        output_index=0,
                        content_index=0,
                        sequence_number=0,
                        logprobs=[],
                    )
                )
                pending.set()
                await release.wait()
                self.is_complete = True
            finally:
                finished.set()

        def cancel(self, *, mode):
            assert mode == "after_turn"
            stopping.set()

        def to_input_list(self):
            return []

    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kwargs: Run())
    request = AsyncMock()

    # Disconnect after the SDK actually starts, independent of progress-event
    # count and cold model setup; keep the three-second cancellation assertion.
    async def disconnected():
        if not started.is_set():
            return False
        # The fixture must have an outstanding SDK wait before disconnecting;
        # closing a generator paused at yield has no asynchronous work to settle.
        await pending.wait()
        return True

    request.is_disconnected.side_effect = disconnected
    stream = (
        _stream_query(QueryRequest(kb="test", question="Hi", save=True), kb_dir, request)
        if endpoint == "query"
        else _stream_chat(ChatRequest(kb="test", message="Hi"), kb_dir, request)
    )

    async def consume():
        return [frame async for frame in stream]

    async def other_writer():
        async with async_kb_lock(kb_dir / ".openkb", exclusive=True):
            acquired.set()

    consumer = asyncio.create_task(consume())
    writer = None
    try:
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.wait_for(stopping.wait(), 3)
        consumer.cancel()
        writer = asyncio.create_task(other_writer())
        await asyncio.sleep(0.03)
        consumer.cancel()
        await asyncio.sleep(0.03)
        assert not consumer.done()
        assert not acquired.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 3)
        await asyncio.wait_for(writer, 3)
        assert finished.is_set() and acquired.is_set()
        assert not list((kb_dir / "wiki/explorations").glob("*.md"))
        assert all(
            load_session(kb_dir, item["id"]).turn_count == 0 for item in list_sessions(kb_dir)
        )
    finally:
        release.set()
        await asyncio.gather(consumer, *([writer] if writer else []), return_exceptions=True)
