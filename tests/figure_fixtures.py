"""Small valid image bytes for storage and source-freezing integration tests."""

import io

from PIL import Image


def png_bytes(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(stream, format="PNG")
    return stream.getvalue()
