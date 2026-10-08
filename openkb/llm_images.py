"""Image receipts from the actual provider request, without saving image payloads."""

import base64
import binascii
import hashlib


def image_digest(url: str) -> str | None:
    if url.startswith("data:image/"):
        header, separator, data = url.partition(",")
        if not separator or ";base64" not in header:
            return None
        try:
            pixels = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return None
        return hashlib.sha256(pixels).hexdigest() if pixels else None
    if url.startswith(("https://", "http://")):
        return hashlib.sha256(url.encode()).hexdigest()
    return None


def request_image_digests(value) -> set[str]:
    """Recognize OpenAI, Anthropic and Gemini image blocks, never quoted prose."""
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(request_image_digests(item))
    elif isinstance(value, dict):
        url = None
        if value.get("type") in ("image_url", "input_image"):
            image = value.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
        elif value.get("type") == "image":
            source = value.get("source", {})
            if isinstance(source, dict):
                if source.get("type") == "base64":
                    url = f"data:{source.get('media_type', '')};base64,{source.get('data', '')}"
                elif source.get("type") == "url":
                    url = source.get("url")
        for key in ("inline_data", "inlineData"):
            data = value.get(key)
            if isinstance(data, dict):
                mime = data.get("mime_type", data.get("mimeType", ""))
                url = f"data:{mime};base64,{data.get('data', '')}"
        if isinstance(url, str) and (digest := image_digest(url)):
            found.add(digest)
        for item in value.values():
            if isinstance(item, (list, dict)):
                found.update(request_image_digests(item))
    return found
