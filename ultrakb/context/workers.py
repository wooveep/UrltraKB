"""Wait for blocking context work before releasing the application's leases."""

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def run_blocking(
    operation: Callable[[], T], *, on_cancel: Callable[[], None] | None = None
) -> T:
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        from ultrakb.llm_execution import active_executor

        if executor := active_executor():
            executor.cancel()
        # The executor's cancellation signal stops subsequent sends. A current
        # send must finish before another operation can delete its runtime data.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise
