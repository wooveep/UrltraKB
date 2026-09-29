"""Bounded SSE observation of existing directory watchers."""

import asyncio
import os
import time
from typing import AsyncIterator

from fastapi import Request

from openkb.watch_service import WatchRegistry

# Default cap for /watch/events SSE so abandoned clients do not poll forever.
_WATCH_SSE_TIMEOUT = float(os.environ.get("OPENKB_WATCH_SSE_TIMEOUT", "300"))


async def _stream_watch_events(
    registry: WatchRegistry,
    kb: str,
    max_events: int | None,
    timeout_seconds: float | None,
    request: Request,
) -> AsyncIterator[str]:
    """Tail a KB's watch event ring buffer as an SSE stream.

    Replays existing events then polls for new ones. Terminates when the
    watcher stops, or when ``max_events``/``timeout_seconds`` is reached (so
    bounded clients and tests can drain without hanging). With both unset the
    stream is capped by a default timeout when none is given.
    """
    from openkb.api_helpers import _sse

    state = await asyncio.to_thread(registry.get, kb)
    yield _sse("start", {"endpoint": "watch", "kb": kb, "active": state is not None})
    if state is None:
        yield _sse("error", {"message": f"No active watcher for KB: {kb}"})
        yield _sse("done", {})
        return
    if timeout_seconds is None:
        timeout_seconds = _WATCH_SSE_TIMEOUT
    next_seq = 0
    emitted = 0
    started = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return
            for ev in list(state.events):
                if ev["seq"] < next_seq:
                    continue
                next_seq = ev["seq"] + 1
                yield _sse(ev["event"], ev["data"])
                emitted += 1
                if ev["event"] == "watcher_stopped":
                    yield _sse("done", {})
                    return
                if max_events is not None and emitted >= max_events:
                    yield _sse("done", {})
                    return
            if timeout_seconds is not None and (time.monotonic() - started) >= timeout_seconds:
                yield _sse("done", {})
                return
            await asyncio.sleep(0.5)
    except Exception as exc:
        yield _sse("error", {"message": f"Watch events stream failed: {exc}"})
    yield _sse("done", {})
