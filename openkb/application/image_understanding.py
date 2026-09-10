"""User-triggered connection verification uses a built-in image, never library content."""

import io
from pathlib import Path

from PIL import Image

from openkb.locks import atomic_write_json
from openkb.vision.connection import capability_path, resolve_connection
from openkb.vision.transport import request_image


async def test_image_connection(kb_dir: Path | None = None) -> dict:
    connection = resolve_connection(kb_dir)
    error = connection.readiness()
    if error:
        return {"status": error, "connection": connection.identity}
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, format="PNG")
    from openkb import config

    # An explicit recheck supersedes the previous capability receipt, including failure.
    with config._with_global_config_lock():
        capability_path(connection).unlink(missing_ok=True)
    result = await request_image(
        connection,
        stream.getvalue(),
        "What color is the square?",
        seconds=connection.settings.limits.request_seconds,
    )
    if result["status"] == "completed" and not any(
        color in result["text"].lower() for color in ("red", "红", "紅")
    ):
        result["status"] = "image_sample_not_understood"
    if result["status"] == "completed":
        with config._with_global_config_lock():
            atomic_write_json(
                capability_path(connection),
                {
                    "connection": connection.identity,
                    "image_input": True,
                },
            )
        return {"status": "ready", "connection": connection.identity}
    return {"status": result["status"], "connection": connection.identity}


setattr(test_image_connection, "__test__", False)
