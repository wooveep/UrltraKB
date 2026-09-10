"""Finite HTTP streaming with cooperative cancellation and owned async cleanup."""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import httpx

from openkb.processing import processing_checkpoint


class HttpIncomplete(ValueError):
    pass


def run_http(coroutine):
    """Bridge synchronous document adapters; never abandon a live network thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="openkb-http") as executor:
        return executor.submit(context.run, asyncio.run, coroutine).result()


async def stream_http(method, url, *, seconds, consume, check=None, headers=None, body=None):
    check = check or processing_checkpoint

    async def receive():
        async with httpx.AsyncClient(timeout=seconds, trust_env=False) as client:
            async with client.stream(
                method, url, headers=headers, json=body, follow_redirects=method == "GET"
            ) as response:
                if response.status_code not in {200, 206}:
                    raise HttpIncomplete(f"http_{response.status_code}")
                async for chunk in response.aiter_bytes():
                    check()
                    consume(response, chunk)

    pending = asyncio.create_task(receive())
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while not pending.done():
            check()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise HttpIncomplete("http_time_budget_exhausted")
            await asyncio.wait({pending}, timeout=min(remaining, 0.05))
        check()
        return pending.result()
    except httpx.HTTPError:
        raise HttpIncomplete("http_transport_unavailable") from None
    finally:
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await pending


def post_bytes(url, body, *, headers, seconds, max_bytes):
    content = bytearray()

    def consume(response, chunk):
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HttpIncomplete("http_output_budget_exhausted")

    run_http(stream_http("POST", url, body=body, headers=headers, seconds=seconds, consume=consume))
    return bytes(content)
