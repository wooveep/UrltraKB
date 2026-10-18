"""Merge cumulative provider receipt fields from decoded stream events."""

from typing import Any


def merge_receipts(events) -> dict:
    metadata: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        receipt = event.get("message", event)
        if not isinstance(receipt, dict):
            continue
        if receipt.get("id"):
            metadata["id"] = receipt["id"]
        if isinstance(receipt.get("usage"), dict):
            for key, value in receipt["usage"].items():
                if value is None:
                    continue
                if key in {
                    "input_tokens_details",
                    "prompt_tokens_details",
                    "output_tokens_details",
                    "completion_tokens_details",
                } and isinstance(value, dict):
                    previous = usage.get(key, {})
                    value = {
                        **(previous if isinstance(previous, dict) else {}),
                        **{name: count for name, count in value.items() if count is not None},
                    }
                usage[key] = value
    return {**metadata, "usage": usage} if usage else {}
