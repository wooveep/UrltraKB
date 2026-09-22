"""Critical semantic review requests for complete document-page candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from openkb.config import compilation_model_options
from openkb.sources import content_id

_PROVENANCE_MARKER = re.compile(
    r"<!-- (?:source-evidence:\s*\{[^\r\n]*\}|/?openkb-source:[^>\r\n]*) -->"
)


def _semantic_candidate(content: str) -> str:
    """Project deterministic provenance comments out of semantic review input.

    The full candidate digest remains part of the request contract.  This only
    removes source-boundary annotations that the program already validates and
    that otherwise duplicate every original reference in the frozen evidence.
    """

    # Do not normalize the surrounding Markdown. In particular, leading
    # whitespace can make a literal code block, so stripping it would review
    # different semantic bytes from the candidate that the receipt binds.
    return _PROVENANCE_MARKER.sub("", content)


def _review(
    page: Any,
    evidence: dict[str, Any],
    occurrences: list[dict[str, Any]],
    content: str,
    *,
    settings: dict[str, Any],
    limits: Any,
    checkpoints: Any,
    bundle: Any,
    pool: Any,
    known_omissions: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], None],
    adjudication: bool = False,
    previous_reason: str | None = None,
    retained_identity: str,
    preserved_contribution: dict[str, str],
) -> dict[str, Any]:
    """Review one exact, complete candidate with the ordinary wire contract."""

    # These request helpers remain in document_pages beside generation.  A
    # lazy import avoids a module-level cycle while publication reuses the
    # exact same critical-review contract.
    from openkb.agent.document_pages import (
        VERIFY_RULES,
        _page_fields,
        _request_json,
        _validate_review_response,
    )

    accepted: dict[str, Any] = {}
    task: dict[str, Any] = {
        "page": _page_fields(page),
        "candidate": {
            # Review only semantic text, but make the immutable request record
            # bind the exact rendered candidate that may later be published.
            "content": _semantic_candidate(content),
            "full_content_digest": content_id(content),
        },
        "known_omissions": known_omissions,
        "review_mode": "critical",
        "preserved_contribution": {
            "identity": retained_identity,
            "content": _semantic_candidate(preserved_contribution.get("content", "")),
        },
        "language": settings.get("language"),
        "schema": settings.get("_document_schema", ""),
    }
    if adjudication:
        task["adjudication"] = {
            "previous_reason": previous_reason
            or "The ordinary review did not reach a usable verdict.",
            "instruction": (
                "Independently adjudicate the same candidate against the supplied original "
                "evidence. The prior review may be mistaken; preserve actual restrictions "
                "and necessary conditions."
            ),
        }
    value = _request_json(
        stage="verification",
        evidence=evidence,
        task=task,
        rules=VERIFY_RULES,
        settings=settings,
        limits=limits,
        checkpoints=checkpoints,
        dependencies={
            "page": _page_fields(page),
            "candidate": content_id(content),
            "mode": "critical_adjudication" if adjudication else "critical",
            "previous_reason": previous_reason if adjudication else None,
            "preserved_contribution": retained_identity,
        },
        bundle=bundle,
        pool=pool,
        validator=_validate_review_response,
        dispatch_stage="verification_adjudication" if adjudication else None,
        on_event=on_event,
        on_accepted=accepted.update,
    )
    return {
        "verdict": value["verdict"],
        "reason": value["reason"],
        "issues": value.get("issues", []),
        "candidate": content_id(content),
        "checkpoint": accepted["checkpoint"],
        "result": accepted["result"],
        "cached": accepted["cached"],
        "dispatch_output_tokens": accepted["dispatch_output_tokens"],
    }


def _uses_stronger_adjudication(settings: dict[str, Any]) -> bool:
    """Whether an explicit stronger review profile differs from ordinary review."""

    return compilation_model_options(settings, stage="verification_adjudication") != (
        compilation_model_options(settings, verification=True)
    )


def review_candidate(
    page: Any,
    evidence: dict[str, Any],
    occurrences: list[dict[str, Any]],
    content: str,
    *,
    settings: dict[str, Any],
    limits: Any,
    checkpoints: Any,
    bundle: Any,
    pool: Any,
    known_omissions: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], None],
    retained_identity: str,
    preserved_contribution: dict[str, str],
) -> dict[str, Any]:
    """Run one page-wide critical review and optional stronger adjudication."""

    review = _review(
        page,
        evidence,
        occurrences,
        content,
        settings=settings,
        limits=limits,
        checkpoints=checkpoints,
        bundle=bundle,
        pool=pool,
        known_omissions=known_omissions,
        on_event=on_event,
        retained_identity=retained_identity,
        preserved_contribution=preserved_contribution,
    )
    if review["verdict"] not in {"supported", "advisory"} and _uses_stronger_adjudication(settings):
        review = _review(
            page,
            evidence,
            occurrences,
            content,
            settings=settings,
            limits=limits,
            checkpoints=checkpoints,
            bundle=bundle,
            pool=pool,
            known_omissions=known_omissions,
            adjudication=True,
            previous_reason=review["reason"],
            on_event=on_event,
            retained_identity=retained_identity,
            preserved_contribution=preserved_contribution,
        )
    return review
