"""Submission recovery leaves the event loop responsive while GC owns the store."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_storage_questions import question_knowledge

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["conversation", "generation"])
async def test_submission_lookup_can_stop_during_gc_without_blocking_the_event_loop(
    tmp_path, monkeypatch, operation
):
    from ultrakb.application.conversations import continue_conversation
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.views import storage_scope
    from ultrakb.locks import _Lease
    from ultrakb.storage.coordination import try_reclamation

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    scope.reading_view  # Capture the fixed read selection before GC starts.
    held, entered, stopped, tick = (Event() for _ in range(4))
    loop = asyncio.get_running_loop()

    def collect():
        with try_reclamation(root, identity) as exclusive:
            assert exclusive is not None
            held.set()
            assert entered.wait(5)
            stopped.set()
            loop.call_soon_threadsafe(tick.set)
            return tick.wait(2)

    acquire = _Lease.try_acquire

    def observe(lease):
        if lease.path == root / ".ultrakb/locks/reclaim.lock":
            entered.set()
        return acquire(lease)

    with ThreadPoolExecutor(max_workers=1) as collector:
        reclamation = collector.submit(collect)
        assert await asyncio.to_thread(held.wait, 5)
        monkeypatch.setattr(_Lease, "try_acquire", observe)
        if operation == "generation":
            from ultrakb.application.generators import GenerationOptions, generate_artifact
            from ultrakb.locks import LockCancelled

            with pytest.raises(LockCancelled):
                await generate_artifact(
                    root,
                    GenerationOptions("deck", "value", "Present the recorded value."),
                    context=ExecutionContext(cancelled=stopped.is_set),
                    scope=scope,
                )
        else:
            result = await continue_conversation(
                root,
                "Read the retained value.",
                new_session_id="a" * 32,
                attempt_id="b" * 32,
                context=ExecutionContext(cancelled=stopped.is_set),
                scope=scope,
            )
            assert result.status == "stopped", result
        assert reclamation.result(timeout=5), "The submission lookup blocked the event loop"
