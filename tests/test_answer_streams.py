"""An HTTP observer keeps the operation's lease until cancelled work settles.

Actual SDK cancellation is covered by test_chat_session. This adapter contract
uses an application operation and preparation activity, with no model responses.
"""

import asyncio

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["query", "chat"])
async def test_disconnect_waits_for_sdk_cleanup_even_after_repeated_cancel(
    kb_dir, monkeypatch, endpoint
):
    from ultrakb.agent.chat_session import ChatSession, list_sessions, load_session
    from ultrakb.api_helpers import _stream_chat, _stream_query
    from ultrakb.api_models import ChatRequest, QueryRequest
    from ultrakb.locks import async_kb_lock

    stopping, release, finished, acquired = (asyncio.Event() for _ in range(4))
    started, pending = asyncio.Event(), asyncio.Event()

    async def operation(root, question=None, *, context, new_session_id=None, **kwargs):
        if endpoint == "chat":
            session = ChatSession.new(root, "", "", identity=new_session_id)
            session.save()
        async with async_kb_lock(root / ".ultrakb", exclusive=True):
            try:
                with context.activity("preparing"):
                    started.set()
                    pending.set()
                    await release.wait()
            except asyncio.CancelledError:
                stopping.set()
                while not release.is_set():
                    try:
                        await asyncio.shield(release.wait())
                    except asyncio.CancelledError:
                        continue
                raise
            finally:
                finished.set()

    monkeypatch.setattr(
        "ultrakb.application.conversations."
        + ("ask_question" if endpoint == "query" else "continue_conversation"),
        operation,
    )

    class Request:
        async def is_disconnected(self):
            if not started.is_set():
                return False
            await pending.wait()
            return True

    stream = (
        _stream_query(QueryRequest(kb="test", question="Hi", save=True), kb_dir, Request())
        if endpoint == "query"
        else _stream_chat(ChatRequest(kb="test", message="Hi"), kb_dir, Request())
    )

    async def consume():
        return [frame async for frame in stream]

    async def other_writer():
        async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
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
        assert not consumer.done() and not acquired.is_set()
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
