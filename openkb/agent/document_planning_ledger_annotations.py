"""Atomically merge source-bound annotations into the planning ledger."""

from __future__ import annotations

import json
from typing import Any

from openkb.agent.document_plan_annotations import ExternalReference, PlanningOmission
from openkb.agent.document_planning_ledger_integrity import (
    canonical_json,
    save_settlement_proof,
)
from openkb.agent.document_window_receipts import (
    skipped_window_receipt,
    target_ranges,
    window_receipt_id,
)
from openkb.sources import content_id


def save_external_references(ledger: Any, values: list[dict[str, Any]]) -> None:
    if values and ledger._meta("planning_metadata", {}).get("protocol") != "document-plan-v2":
        raise ValueError("External references require DocumentPlan v2")
    for value in values:
        reference = ExternalReference.from_dict(value)
        row = ledger.db.execute(
            "SELECT payload FROM external_references WHERE key = ?", (reference.key,)
        ).fetchone()
        if row is not None:
            prior = ExternalReference.from_dict(json.loads(row[0]))
            if (
                prior.location != reference.location
                or prior.raw_quote != reference.raw_quote
                or prior.target_document != reference.target_document
                or prior.target_section != reference.target_section
            ):
                raise ValueError("External reference identity conflict")
            reference.affected_pages = sorted(
                set(prior.affected_pages) | set(reference.affected_pages)
            )
        ledger.db.execute(
            "INSERT INTO external_references(key, payload) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload",
            (reference.key, canonical_json(reference.to_dict())),
        )


def partial_omission(
    ledger: Any, window: dict[str, Any], *, ranges: list[Any],
    affected_pages: list[str], attempts: int, diagnostic_ref: str | None,
    component: str = "document_plan", reason: str = "document_plan_partial_invalid",
    durable_pages: bool = False,
) -> PlanningOmission:
    """Bind independently retained content to one visible failed subset."""
    if not ranges:
        raise ValueError("A partial omission needs exact ranges")
    target_id = window_receipt_id(window)
    omitted_pages = sorted({
        page if durable_pages else
        "omitted-page:" + content_id([ledger.recovery_key, target_id, page])
        for page in affected_pages if page
    })
    key = "omission:" + content_id({
        "identity": ledger._meta("identity"), "target": target_id,
        "stage": "planning", "reason": reason, "ranges": ranges,
        "affected_pages": omitted_pages,
        **({"retry_attempt": window["retry_attempt"]} if "retry_attempt" in window else {}),
    })
    return PlanningOmission(
        key=key, stage="planning", reason=reason,
        target_id=target_id, ranges=ranges,
        affected_pages=omitted_pages, component=component,
        attempts=attempts, diagnostic_ref=diagnostic_ref,
    )


def save_partial_omission(ledger: Any, omission: PlanningOmission) -> None:
    ledger.db.execute(
        "INSERT INTO planning_omissions(key, payload) VALUES (?, ?)",
        (omission.key, canonical_json(omission.to_dict())),
    )


def resolve_ready_blocked_omissions(ledger: Any, page_key: str, sequence: int) -> None:
    """Close a prior blocked-page omission after later evidence unblocks every page."""
    for key, payload in ledger.db.execute(
        "SELECT o.key, o.payload FROM planning_omissions o LEFT JOIN retry_resolutions r "
        "ON r.omission_key = o.key WHERE r.omission_key IS NULL"
    ):
        omission = PlanningOmission.from_dict(json.loads(payload))
        if (
            omission.reason != "document_plan_blocked_page"
            or page_key not in omission.affected_pages
        ):
            continue
        if all(
            (page := ledger._page(affected)) is not None and page.state == "ready"
            for affected in omission.affected_pages
        ):
            ledger._resolve_retry_omission(key, sequence)


def resolve_retry_if_ready(ledger: Any, key: str, sequence: int) -> None:
    """A retry cannot close a blocker while any affected page remains blocked."""
    if ledger.db.execute(
        "SELECT 1 FROM retry_resolutions WHERE omission_key = ?", (key,)
    ).fetchone() is not None:
        return
    row = ledger.db.execute(
        "SELECT payload FROM planning_omissions WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError("Retry omission is missing")
    omission = PlanningOmission.from_dict(json.loads(row[0]))
    if omission.reason == "document_plan_blocked_page" and not all(
        (page := ledger._page(affected)) is not None and page.state == "ready"
        for affected in omission.affected_pages
    ):
        return
    ledger._resolve_retry_omission(key, sequence)


def blocked_retry_page_keys(ledger: Any, window: dict[str, Any]) -> frozenset[str] | None:
    """Return durable pages a blocked-page retry may update, if applicable."""
    key = window.get("retry_omission_key")
    if key is None:
        return None
    row = ledger.db.execute(
        "SELECT payload FROM planning_omissions WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError("Retry omission is missing")
    omission = PlanningOmission.from_dict(json.loads(row[0]))
    return (
        frozenset(omission.affected_pages)
        if omission.reason == "document_plan_blocked_page" else None
    )


def settle_skipped(
    ledger: Any,
    window: dict[str, Any],
    *,
    windows: list[dict[str, Any]],
    completed: int,
    reason: str,
    attempts: int,
    predecessor: str,
    diagnostic_ref: str | None,
) -> PlanningOmission:
    """Atomically settle one failed target with a separate immutable proof."""
    if ledger._meta("planning_metadata", {}).get("protocol") != "document-plan-v2":
        raise ValueError("Skipped settlement requires DocumentPlan v2")
    prior = ledger.progress()
    if prior is not None and (prior[0] != "pending" or prior[2] != completed - 1):
        raise ValueError("Skipped settlement is out of sequence")
    if type(completed) is not int or not 1 <= completed <= len(windows):
        raise ValueError("Skipped settlement sequence is invalid")
    if windows[completed - 1] != window:
        raise ValueError("Skipped settlement target mismatch")
    if not isinstance(reason, str) or not reason or type(attempts) is not int or attempts < 0:
        raise ValueError("Skipped settlement reason or attempt count is invalid")
    if not isinstance(predecessor, str) or len(predecessor) != 64:
        raise ValueError("Skipped settlement predecessor is invalid")
    ranges = target_ranges(window)
    target_id = window_receipt_id(window)
    identity = ledger._meta("identity")
    key = "omission:" + content_id(
        {
            "identity": identity, "target": target_id, "stage": "planning", "ranges": ranges,
            **({"retry_attempt": window["retry_attempt"]} if "retry_attempt" in window else {}),
        }
    )
    omission = PlanningOmission(
        key=key,
        stage="planning",
        reason=reason,
        target_id=target_id,
        ranges=ranges,
        affected_pages=[],
        component="document_plan",
        attempts=attempts,
        diagnostic_ref=diagnostic_ref,
    )
    receipt = skipped_window_receipt(
        window, predecessor=predecessor, omission_key=key, attempts=attempts
    )
    with ledger._transaction():
        if retry_omission := window.get("retry_omission_key"):
            ledger._resolve_retry_omission(retry_omission, completed)
        ledger.db.execute(
            "INSERT INTO planning_omissions(key, payload) VALUES (?, ?)",
            (key, canonical_json(omission.to_dict())),
        )
        ledger.db.execute(
            "INSERT INTO receipts(sequence, payload) VALUES (?, ?)",
            (completed, canonical_json(receipt)),
        )
        ledger._set_meta("windows", windows)
        ledger._set_meta("settled_count", completed)
        ledger._set_meta("status", "pending")
        ledger._refresh_integrity()
        save_settlement_proof(ledger, receipt, completed)
    return omission
