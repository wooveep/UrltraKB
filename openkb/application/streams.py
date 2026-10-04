"""Observe application operations without releasing their leases before cleanup."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import AsyncGenerator, Awaitable, Callable

from openkb.application.conversations import AnswerResult
from openkb.application.execution import ExecutionContext


async def answer_events(
    operation: Callable[[ExecutionContext], Awaitable[AnswerResult]],
) -> AsyncGenerator[dict, None]:
    """Project a use case's progress and result; the use case owns all business work."""
    events: asyncio.Queue[dict] = asyncio.Queue()
    stopped = False
    loop = asyncio.get_running_loop()

    def report(event: dict) -> None:
        loop.call_soon_threadsafe(events.put_nowait, event)

    context = ExecutionContext(cancelled=lambda: stopped, on_event=report)

    async def run():
        try:
            return await operation(context)
        finally:
            # Queue the sentinel after all on_event callbacks, including those
            # scheduled by a worker thread immediately before completion.
            loop.call_soon(events.put_nowait, {"event": "operation_finished"})

    task = asyncio.create_task(run())
    try:
        while True:
            event = await events.get()
            if event.get("event") == "operation_finished":
                yield {"event": "result", "data": task.result()}
                return
            yield event
    finally:
        stopped = True
        if not task.done():
            task.cancel()
        # A child cancellation is an outcome here, not an observer cancellation.
        # Keeping that distinction lets repeated client cancellation propagate
        # after the operation has safely settled.
        settled = asyncio.gather(task, return_exceptions=True)
        cancelled = False
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                cancelled = True
        with suppress(asyncio.CancelledError):
            task.result()
        if cancelled:
            raise asyncio.CancelledError
