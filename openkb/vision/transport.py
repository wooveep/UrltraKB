"""Bounded image requests with explicit protocol and credentials; no SDK fallback."""

import asyncio
import base64
import json
from contextlib import suppress

import httpx

from openkb.processing import processing_checkpoint


async def request_image(connection, content, question, *, seconds):
    settings = connection.settings
    image = base64.b64encode(content).decode("ascii")
    text = "Describe only what this image supports. Distinguish uncertainty.\n" + question
    headers = dict(connection.extra_headers)
    if settings.provider == "anthropic":
        path = "/messages"
        headers["anthropic-version"] = "2023-06-01"
        if connection.api_key and settings.authentication == "api_key":
            headers["x-api-key"] = connection.api_key
        parts = [
            {"type": "text", "text": text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image,
                },
            },
        ]
    else:
        path = "/chat/completions"
        if connection.api_key and settings.authentication == "api_key":
            headers["Authorization"] = "Bearer " + connection.api_key
        parts = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + image,
                },
            },
        ]
    body = {
        "model": settings.model,
        "messages": [{"role": "user", "content": parts}],
        "max_tokens": settings.limits.output_tokens,
        "stream": False,
    }

    async def perform():
        async with httpx.AsyncClient(timeout=seconds, trust_env=False) as client:
            request = client.build_request(
                "POST", connection.identity["endpoint"] + path, headers=headers, json=body
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    return {"status": f"image_http_{response.status_code}", "text": ""}
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    processing_checkpoint()
                    chunks.extend(chunk)
                    if len(chunks) > settings.limits.response_bytes:
                        return {"status": "image_output_too_large", "text": ""}
                value = json.loads(chunks)
            finally:
                await response.aclose()
        if settings.provider == "anthropic":
            answer = "\n".join(row["text"] for row in value["content"] if row["type"] == "text")
            finished = value.get("stop_reason") == "end_turn"
            usage = value.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("image_usage_invalid")
            counts = [usage.get("input_tokens"), usage.get("output_tokens")]
        else:
            choice = value["choices"][0]
            answer = choice["message"]["content"]
            finished = choice.get("finish_reason") == "stop"
            usage = value.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("image_usage_invalid")
            counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        tokens = sum(counts) if all(type(n) is int and n >= 0 for n in counts) else None
        if not isinstance(answer, str):
            return {"status": "image_invalid_result", "text": ""}
        if connection.api_key:
            answer = answer.replace(connection.api_key, "[redacted]")
        for name, value in connection.extra_headers.items():
            if value and any(
                word in name.lower()
                for word in ("authorization", "key", "token", "secret", "cookie", "password")
            ):
                answer = answer.replace(value, "[redacted]")
        return {
            "status": "completed"
            if finished and answer.strip()
            else "image_empty_result"
            if finished
            else "image_output_incomplete",
            "text": answer,
            **({"tokens": tokens} if tokens is not None else {}),
        }

    task = asyncio.create_task(perform())
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while not task.done():
            processing_checkpoint()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {"status": "image_timeout", "text": ""}
            await asyncio.wait({task}, timeout=min(0.1, remaining))
        processing_checkpoint()
        return task.result()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return {"status": "image_request_failed", "text": ""}
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
