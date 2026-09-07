"""Keep SDK work alive until its safe boundary when an observer is cancelled."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, AsyncGenerator


async def settled_stream(result: Any) -> AsyncGenerator[Any, None]:
    """Observe a run without transferring observer cancellation to the SDK.

    The SDK's event iterator uses immediate cancellation when its queue await
    is cancelled. Shield that await, request after-turn stopping, then finish
    its cleanup before returning control to the caller that owns write leases.
    Repeated cancellation cannot skip this cleanup.
    """
    stream = result.stream_events()
    pending: asyncio.Task | None = None
    try:
        while True:
            pending = asyncio.create_task(anext(stream))
            try:
                event = await asyncio.shield(pending)
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            yield event
    finally:
        if not result.is_complete:
            result.cancel(mode="after_turn")

        async def settle() -> None:
            try:
                if pending is not None:
                    with suppress(StopAsyncIteration, asyncio.CancelledError):
                        await pending
            finally:
                await stream.aclose()

        cleanup = asyncio.create_task(settle())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
