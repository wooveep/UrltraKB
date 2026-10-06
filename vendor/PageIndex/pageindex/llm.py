"""Runtime-only indexing seam; integrations own model transport and policy."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
