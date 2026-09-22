"""Portable contracts for critical document-page review receipts."""

from __future__ import annotations

import re
from typing import Any

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def valid_critical_review_receipt(receipt: Any, candidate: str | None = None) -> bool:
    """Check the durable shape required for a verified document page.

    This only validates the portable receipt shape. Publication recovery also
    loads the referenced immutable checkpoint and checks its request contract,
    evidence-bound candidate identity, result digest, and output-cap receipt.
    """

    if not isinstance(receipt, dict):
        return False
    receipt_candidate = receipt.get("candidate")
    reviews = receipt.get("reviews")
    if (
        receipt.get("mode") != "critical"
        or receipt.get("verdict") not in {"supported", "advisory"}
        or not _is_digest(receipt_candidate)
        or (candidate is not None and receipt_candidate != candidate)
        or not _is_digest(receipt.get("candidate_recovery"))
        or not _is_digest(receipt.get("publication_identity"))
        or not isinstance(reviews, list)
        or len(reviews) != 1
    ):
        return False
    review = reviews[0]
    return (
        isinstance(review, dict)
        and review.get("verdict") in {"supported", "advisory"}
        and review.get("verdict") == receipt.get("verdict")
        and isinstance(review.get("reason"), str)
        and bool(review["reason"].strip())
        and isinstance(review.get("issues"), list)
        and review.get("candidate") == receipt_candidate
        and _is_digest(review.get("checkpoint"))
        and _is_digest(review.get("result"))
        and type(review.get("dispatch_output_tokens")) is int
        and review["dispatch_output_tokens"] > 0
        and type(review.get("cached")) is bool
    )
