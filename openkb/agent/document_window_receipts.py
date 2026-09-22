"""Stable, source-free receipts for completed document-planning targets."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from openkb.sources import content_id


def window_receipt_id(window: dict[str, Any]) -> str:
    """Return the immutable identity for one accepted planning target."""

    explicit = window.get("window_id") or (window.get("evidence") or {}).get("id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return json.dumps(
        {
            "target_start": window.get("target_start"),
            "target_end": window.get("target_end"),
            "target_ranges": window.get("target_ranges"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def target_ranges(window: dict[str, Any]) -> list[Any]:
    """Copy exact T ranges, using its whole-block form only when explicit."""

    values = window.get("target_ranges")
    if values is None:
        values = [[window["target_start"], window["target_end"]]]
    return deepcopy(values)


def frozen_evidence_id(window: dict[str, Any]) -> str:
    """Return W's frozen receipt without retaining evidence text."""

    value = window.get("frozen_evidence_id") or (window.get("evidence") or {}).get("id")
    if isinstance(value, str) and value:
        return value
    return content_id({"evidence": window.get("evidence"), "ranges": window.get("frozen_ranges")})


def accepted_window_receipt(
    window: dict[str, Any],
    value: Any,
    *,
    checkpoint: str | None,
    attempt: int,
    cached: bool,
    request: str,
    predecessor: str,
    delta: str,
    dispatch_output_tokens: int | None,
) -> dict[str, Any]:
    """Capture a durable, content-free binding to the exact accepted result."""

    return {
        "window": window_receipt_id(window),
        "frozen_evidence": frozen_evidence_id(window),
        "target_ranges": target_ranges(window),
        "checkpoint": checkpoint,
        "result": content_id(value),
        "attempt": attempt,
        "cached": cached,
        "request": request,
        "predecessor": predecessor,
        "delta": delta,
        "dispatch_output_tokens": dispatch_output_tokens,
    }


def valid_accepted_receipts(receipts: Any, windows: list[dict[str, Any]], completed: int) -> bool:
    """Require each saved prefix receipt to bind its actual W, T and result."""

    if not isinstance(receipts, list) or len(receipts) != completed:
        return False
    for receipt, window in zip(receipts, windows[:completed], strict=True):
        if not isinstance(receipt, dict) or set(receipt) != {
            "window",
            "frozen_evidence",
            "target_ranges",
            "checkpoint",
            "result",
            "attempt",
            "cached",
            "request",
            "predecessor",
            "delta",
            "dispatch_output_tokens",
        }:
            return False
        if (
            receipt["window"] != window_receipt_id(window)
            or receipt["frozen_evidence"] != frozen_evidence_id(window)
            or receipt["target_ranges"] != target_ranges(window)
            or not isinstance(receipt["result"], str)
            or len(receipt["result"]) != 64
            or any(
                not isinstance(receipt[key], str) or len(receipt[key]) != 64
                for key in ("request", "predecessor", "delta")
            )
            or not isinstance(receipt["attempt"], int)
            or receipt["attempt"] < 0
            or type(receipt["cached"]) is not bool
            or receipt["checkpoint"] is not None
            and (not isinstance(receipt["checkpoint"], str) or len(receipt["checkpoint"]) != 64)
            or receipt["dispatch_output_tokens"] is not None
            and (
                type(receipt["dispatch_output_tokens"]) is not int
                or receipt["dispatch_output_tokens"] <= 0
            )
            or (receipt["checkpoint"] is None) != (receipt["dispatch_output_tokens"] is None)
        ):
            return False
    return True


def accepted_receipts_match_dispatch(receipts: list[dict[str, Any]], lookup: Any) -> bool:
    """Revalidate saved actual dispatch caps through the live checkpoint contract."""

    for receipt in receipts:
        checkpoint = receipt["checkpoint"]
        if checkpoint is None:
            continue  # A test-only/mock planner has no model dispatch receipt.
        if lookup(checkpoint) != receipt["dispatch_output_tokens"]:
            return False
    return True
