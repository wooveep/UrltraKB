"""Classify one completed planning JSON response before parsing its content."""

from __future__ import annotations

from typing import Any, Literal

JsonResponseKind = Literal[
    "content", "empty_content", "length", "invalid_finish", "invalid_content"
]


def classify_json_response(value: Any) -> JsonResponseKind:
    """Keep transport completion distinct from JSON and business validation."""
    finish = getattr(value, "finish_reason", None)
    if finish == "length":
        return "length"
    if getattr(value, "representation", None) == "wire" and finish is None:
        return "invalid_finish"
    if finish not in {None, "stop"}:
        return "invalid_finish"
    if not isinstance(value, str):
        return "invalid_content"
    return "empty_content" if not value.strip() else "content"
