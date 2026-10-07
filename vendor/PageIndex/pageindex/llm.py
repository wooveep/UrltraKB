"""Runtime-only indexing seam; integrations own model transport and policy."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import partial
from typing import Protocol

# Successful trees remain compatible with the last content-policy release.
INDEX_CONTENT_VERSION = "0.3.0.dev3+urltrakb.5"
STORAGE_FORMAT = "pageindex.document.v1"


@dataclass(frozen=True)
class IndexResponse:
    text: str
    finish_reason: str = "finished"


class IndexLLM(Protocol):
    def complete(self, messages: list[dict], *, stage: str) -> IndexResponse: ...
    async def acomplete(self, messages: list[dict], *, stage: str) -> IndexResponse: ...


_CLIENT: ContextVar[IndexLLM | None] = ContextVar("pageindex_llm_client", default=None)
# Permit waiters must not consume the default executor used by async SDK calls
# which can already hold those permits. Threads start lazily and join at exit.
_SYNC_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="pageindex-sync")


def current_client():
    return _CLIENT.get()


@contextmanager
def llm_client_scope(client, *, required=False):
    if required and client is None:
        raise ValueError("Indexing requires an injected IndexLLM")
    if client is not None and not all(callable(getattr(client, name, None)) for name in ("complete", "acomplete")):
        raise TypeError("Invalid IndexLLM implementation")
    token = _CLIENT.set(client)
    try:
        yield
    finally:
        _CLIENT.reset(token)


async def gather_required(*coroutines):
    """Propagate errors and settle siblings before leaving the indexing scope."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_sync(function, *args, **kwargs):
    """Keep blocking stages off-loop and settle their work before cancellation."""
    task = asyncio.get_running_loop().run_in_executor(
        _SYNC_EXECUTOR, copy_context().run, partial(function, *args, **kwargs)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling a worker await cannot stop its thread. Keep the client,
        # policy and enclosing storage scope alive until that work has settled.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            task.exception()
        raise
