"""Merge cumulative provider receipt fields from decoded stream events."""


def merge_receipts(events) -> dict:
    metadata, usage = {}, {}
    for event in events:
        if not isinstance(event, dict):
            continue
        receipt = event.get("message", event)
        if not isinstance(receipt, dict):
            continue
        if receipt.get("id"):
            metadata["id"] = receipt["id"]
        if isinstance(receipt.get("usage"), dict):
            usage.update(
                {key: value for key, value in receipt["usage"].items() if value is not None}
            )
    return {**metadata, "usage": usage} if usage else {}
