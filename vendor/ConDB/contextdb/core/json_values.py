"""Lossless JSON boundary validation shared by adapters and storage."""

def validate_json(value):
    """Require actual JSON values; encoders otherwise silently coerce keys/types."""
    import math

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            validate_json(child)
    elif isinstance(value, list):
        for child in value:
            validate_json(child)
    elif value is None or type(value) in (str, int, bool):
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise ValueError("Expected finite JSON values without type coercion")
