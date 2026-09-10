"""Old multimodal history remains readable without sending images to a text model."""

from copy import deepcopy


def text_history(value):
    if isinstance(value, list):
        return [text_history(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url", "image"}:
            return {
                "type": "input_text",
                "text": "[Historical image retained; not sent to the model.]",
            }
        return {key: text_history(item) for key, item in value.items()}
    return deepcopy(value)


def retain_images(value):
    """Keep historical bytes outside model input before normalizing a saved turn."""
    import hashlib

    result = {}

    def visit(item):
        if isinstance(item, str) and item.startswith("data:image/") and ";base64," in item:
            media, data = item.split(";base64,", 1)
            identity = hashlib.sha256(item.encode()).hexdigest()
            result[identity] = {"media_type": media.removeprefix("data:"), "base64": data}
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    return result
