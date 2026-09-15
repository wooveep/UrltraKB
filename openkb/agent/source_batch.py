"""Bounded parallel reads of one captured source snapshot, with ordered results."""

import asyncio
import json
from collections.abc import Callable

from agents import function_tool
from pydantic import BaseModel, ConfigDict, Field

from openkb.processing import processing_checkpoint


class SourceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    node_id: str
    offset: int = Field(default=0, ge=0)
    start: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=16000)


class SourceSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    query: str = Field(min_length=1, max_length=512)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=20)


async def _read_batch(requests, read: Callable, **options):
    if not 1 <= len(requests) <= 4:
        raise ValueError("Use between one and four independent source reads per batch")

    def run(request):
        processing_checkpoint()
        arguments = request.model_dump()
        try:
            output = json.loads(read(**arguments, **options))
        except (ValueError, OSError) as exc:
            return {"request": arguments, "error": str(exc)}
        processing_checkpoint()
        return {"request": arguments, "output": output}

    # These callbacks read detached evidence; they never reacquire the owner's KB lock.
    # Cancellation and the request budget remain shared through copied context variables.
    tasks = [asyncio.create_task(asyncio.to_thread(run, request)) for request in requests]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return json.dumps({"results": results}, ensure_ascii=False)


def batch_source_tools(read_node: Callable, search_text: Callable):
    @function_tool
    async def read_source_nodes(reads: list[SourceRead]) -> str:
        """Read up to four independent original ranges concurrently in one tool call.
        Use at most 16000 text/context characters across the batch. Each ordered result
        retains its request, original evidence, citations and next cursor. Follow a
        result's next cursor to finish relevant text/context; errors affect only that read.
        """
        if sum(read.max_chars for read in reads) > 16000:
            raise ValueError("A batch may request at most 16000 text/context characters")
        return await _read_batch(reads, read_node)

    @function_tool
    async def search_sources(searches: list[SourceSearch]) -> str:
        """Search up to four independent source/literal pairs concurrently.
        Each result has original evidence and its own next_offset cursor; paginate when
        needed for the requested list. Each search returns at most 4000 text/context
        characters. Search is literal, not semantic; use source-specific terms.
        """
        return await _read_batch(searches, search_text, max_chars=4000)

    return [read_source_nodes, search_sources]
