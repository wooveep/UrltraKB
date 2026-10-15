"""Small valid image bytes for storage and source-freezing integration tests."""

import hashlib
import io

from PIL import Image


def png_bytes(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(stream, format="PNG")
    return stream.getvalue()


def record_markdown(wiki, path, text):
    """Declare the syntax of a fixture, as a retained modern Markdown source does."""
    from ultrakb.locks import atomic_write_json, atomic_write_text
    from ultrakb.text_source import FrozenText, TextOrigin

    atomic_write_text(wiki / path, text)
    raw = FrozenText(
        text=text,
        original_characters=len(text),
        original_digest=hashlib.sha256(text.encode()).hexdigest(),
        origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
        assets={},
        tokens=0,
    ).model_dump(mode="json")
    atomic_write_json((wiki / path).with_suffix(".content.json"), raw)
    return raw
