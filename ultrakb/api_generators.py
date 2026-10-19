"""HTTP generation events preserve accepted facts and wait for actual cleanup."""

import asyncio
from contextlib import aclosing

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ultrakb.api_views import resolve_api_scope
from ultrakb.application.generation_results import generation_receipt
from ultrakb.application.generators import GenerationOptions, generate_artifact
from ultrakb.application.streams import operation_events
from ultrakb.context.workers import run_blocking


async def iter_generation(request, root, kind, *, bundle=None):
    yield {"event": "start", "endpoint": kind}
    scope = None

    async def run(context):
        return await run_blocking(
            lambda: asyncio.run(
                generate_artifact(
                    root,
                    GenerationOptions(
                        kind,
                        request.name,
                        request.intent,
                        overwrite="archive" if request.replace else "refuse",
                        version=request.version,
                    ),
                    scope=scope,
                    bundle=bundle,
                    context=context,
                )
            )
        )

    try:
        scope = await resolve_api_scope(root, request.view_id)
        async with aclosing(operation_events(run)) as events:
            async for event in events:
                if event.get("event") != "result":
                    yield event if "event" in event else {"event": "progress", "data": event}
                    continue
                result = event["data"]
                receipt = generation_receipt(result)
                if result.status != "completed":
                    yield {
                        "event": "error",
                        "code": 409
                        if result.status in {"conflict", "blocked"}
                        else 400
                        if result.status == "invalid" and result.error_type is None
                        else 500,
                        "message": f"{kind.title()} generation failed: "
                        f"{result.message or result.error_type or result.status}",
                        "generation": receipt,
                    }
                    return
                yield {
                    "event": "final",
                    "name": request.name,
                    "status": "done",
                    "path": str(result.output_dir) if result.output_dir is not None else "",
                    "generation": receipt,
                }
    except HTTPException as error:
        yield {"event": "error", "code": error.status_code, "message": str(error.detail)}
    except Exception as error:
        yield {
            "event": "error",
            "code": 500,
            "message": f"{kind.title()} generation failed ({type(error).__name__})",
        }


async def collect_generation_response(events, response_model):
    result, error = {}, None
    async with aclosing(events):
        async for event in events:
            if event.get("event") == "error":
                error = event
            elif event.get("event") == "final":
                result = event
    if error is not None:
        if "generation" in error:
            return JSONResponse(
                {
                    "error": {"code": error["code"], "message": error["message"]},
                    "generation": error["generation"],
                },
                status_code=error["code"],
            )
        raise HTTPException(error["code"], error["message"])
    return response_model(**{key: value for key, value in result.items() if key != "event"})


async def _settle(task):
    finished = asyncio.gather(task, return_exceptions=True)
    cancelled = False
    while not finished.done():
        try:
            await asyncio.shield(finished)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def stream_generation(events, mutation_lock, request):
    from ultrakb.api_helpers import _sse

    async with mutation_lock:
        pending = None
        try:
            disconnected = False
            while not disconnected:
                pending = asyncio.create_task(anext(events))
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=0.05)
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    if done:
                        break
                if disconnected:
                    break
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    break
                yield _sse(
                    event["event"], {key: value for key, value in event.items() if key != "event"}
                )
        finally:
            cancelled = False
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                cancelled = await _settle(pending)
            cancelled = await _settle(asyncio.ensure_future(events.aclose())) or cancelled
            if cancelled:
                raise asyncio.CancelledError
    yield _sse("done", {})
