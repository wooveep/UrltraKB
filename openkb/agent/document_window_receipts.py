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
    reference_check: dict[str, Any] | None = None,
    normalization: list[dict[str, Any]] | None = None,
    salvage_proof: str | None = None,
) -> dict[str, Any]:
    """Capture a durable, content-free binding to the exact accepted result."""

    receipt: dict[str, Any] = {
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
    if reference_check is not None:
        receipt["reference_check"] = reference_check
    if normalization:
        receipt["normalization"] = normalization
    if salvage_proof is not None:
        receipt["salvage_proof"] = salvage_proof
    return receipt


def skipped_window_receipt(
    window: dict[str, Any], *, predecessor: str, omission_key: str, attempts: int
) -> dict[str, Any]:
    """Bind a settled target to its program omission without claiming model acceptance."""
    return {
        "protocol": "document-plan-window-settlement-v2",
        "status": "skipped",
        "window": window_receipt_id(window),
        "frozen_evidence": frozen_evidence_id(window),
        "target_ranges": target_ranges(window),
        "predecessor": predecessor,
        "omission_keys": [omission_key],
        "attempts": attempts,
    }


def valid_skipped_receipt(receipt: Any, window: dict[str, Any]) -> bool:
    return (
        isinstance(receipt, dict)
        and set(receipt) == {
            "protocol", "status", "window", "frozen_evidence", "target_ranges",
            "predecessor", "omission_keys", "attempts",
        }
        and receipt["protocol"] == "document-plan-window-settlement-v2"
        and receipt["status"] == "skipped"
        and receipt["window"] == window_receipt_id(window)
        and receipt["frozen_evidence"] == frozen_evidence_id(window)
        and receipt["target_ranges"] == target_ranges(window)
        and isinstance(receipt["predecessor"], str)
        and len(receipt["predecessor"]) == 64
        and isinstance(receipt["omission_keys"], list)
        and len(receipt["omission_keys"]) == 1
        and isinstance(receipt["omission_keys"][0], str)
        and receipt["omission_keys"][0].startswith("omission:")
        and type(receipt["attempts"]) is int
        and receipt["attempts"] >= 0
    )


def valid_accepted_receipts(receipts: Any, windows: list[dict[str, Any]], completed: int) -> bool:
    """Require each saved prefix receipt to bind its actual W, T and result."""

    if not isinstance(receipts, list) or len(receipts) != completed:
        return False
    for receipt, window in zip(receipts, windows[:completed], strict=True):
        if valid_skipped_receipt(receipt, window):
            continue
        v2 = isinstance(receipt, dict) and receipt.get("protocol") == (
            "document-plan-window-settlement-v2"
        )
        if v2 and (
            receipt.get("status") not in {"accepted", "partial"}
            or not isinstance(receipt.get("omission_keys"), list)
            or any(
                not isinstance(key, str) or not key.startswith("omission:")
                for key in receipt["omission_keys"]
            )
            or bool(receipt["omission_keys"]) != (receipt["status"] == "partial")
        ):
            return False
        core = (
            {key: value for key, value in receipt.items() if key not in {
                "protocol", "status", "omission_keys"
            }} if v2 else receipt
        )
        if isinstance(core, dict) and "normalization" in core:
            normalization = core.pop("normalization")
            if not isinstance(normalization, list) or any(
                not isinstance(row, dict)
                or not row
                or any(not isinstance(key, str) for key in row)
                for row in normalization
            ):
                return False
        if isinstance(core, dict) and "salvage_proof" in core:
            proof = core.pop("salvage_proof")
            if not isinstance(proof, str) or len(proof) != 64:
                return False
        if not isinstance(core, dict) or set(core) not in (
            {
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
            },
            {
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
                "reference_check",
            },
        ):
            return False
        if "reference_check" in core:
            check = core["reference_check"]
            if (
                not isinstance(check, dict)
                or set(check) - {"attempts"}
                not in (
                    {"protocol", "status", "candidate_count", "receipt_hash"},
                    {"protocol", "status", "candidate_count", "receipt_hash", "receipt_key"},
                )
                or "receipt_key" in check
                and (not isinstance(check["receipt_key"], str) or len(check["receipt_key"]) != 64)
                or check["protocol"]
                not in {
                    "document-reference-check-v1",
                    "document-reference-check-v2",
                    "document-reference-check-v3",
                }
                or check["status"] not in {"no_detected_candidates", "accounted", "partial"}
                or type(check["candidate_count"]) is not int
                or check["candidate_count"] < 0
                or "attempts" in check and (
                    type(check["attempts"]) is not int or check["attempts"] < 0
                )
                or not isinstance(check["receipt_hash"], str)
                or len(check["receipt_hash"]) != 64
            ):
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
        if receipt.get("status") == "skipped":
            continue
        checkpoint = receipt["checkpoint"]
        if checkpoint is None:
            continue  # A test-only/mock planner has no model dispatch receipt.
        if lookup(checkpoint) != receipt["dispatch_output_tokens"]:
            return False
    return True
