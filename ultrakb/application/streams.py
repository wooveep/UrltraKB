"""Observe application operations without releasing their leases before cleanup."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import AsyncGenerator, Awaitable, Callable, Protocol, TypeVar

import anyio

from ultrakb.application.execution import ExecutionContext


class UsageResult(Protocol):
    @property
    def model_usage(self) -> dict | None: ...


Result = TypeVar("Result", bound=UsageResult)


async def operation_events(
    operation: Callable[[ExecutionContext], Awaitable[Result]],
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
                result = task.result()
                if result.model_usage is not None:
                    # Use the settled operation's authoritative counter receipt.
                    # Request details remain separately paged in the main store.
                    yield {"event": "model_usage", "data": result.model_usage}
                yield {"event": "result", "data": result}
                return
            if event.get("event") == "model_usage_progress":
                # Keep the public answer-stream event name while ordinary
                # updates retain their bounded, explicitly incomplete ID set.
                event = {**event, "event": "model_usage"}
            yield event
    finally:
        stopped = True
        # Starlette uses level cancellation: every await in its cancelled scope
        # is cancelled again. Protect settlement from that scope so the worker
        # can close its SDK and retain receipts without a busy cancellation loop.
        with anyio.CancelScope(shield=True):
            if not task.done():
                task.cancel()
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


answer_events = operation_events
