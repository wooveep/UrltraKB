"""Explicit retry attempts over a verified terminal planning ledger."""

from __future__ import annotations

from typing import Any

from openkb.agent.document_window_receipts import window_receipt_id
from openkb.sources import content_id


def skip_resolved_retry(
    ledger: Any, windows: list[dict[str, Any]], index: int,
    checkpoints: Any, retained_key: str, on_event: Any,
) -> bool:
    """Remove a queued retry when earlier accepted work already closed its omission."""
    key = windows[index].get("retry_omission_key")
    if key is None or ledger.db.execute(
        "SELECT 1 FROM retry_resolutions WHERE omission_key = ?", (key,)
    ).fetchone() is None:
        return False
    windows.pop(index)
    ledger.replace_schedule(windows, index)
    checkpoints.save_recovery(retained_key, "plan", ledger.progress_preview())
    on_event({"stage": "planning", "operation": "skip_resolved_retry", "omission": key})
    return True


def resolve_retry_omission(ledger: Any, key: str, sequence: int) -> None:
    if ledger.db.execute(
        "SELECT 1 FROM planning_omissions WHERE key = ?", (key,)
    ).fetchone() is None or ledger.db.execute(
        "SELECT 1 FROM retry_resolutions WHERE omission_key = ?", (key,)
    ).fetchone() is not None:
        raise ValueError("Retry omission is not active")
    ledger.db.execute(
        "INSERT INTO retry_resolutions(omission_key, sequence) VALUES (?, ?)",
        (key, sequence),
    )


def begin_retry(
    ledger: Any, windows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int] | None:
    """Append explicit attempts only for active, already reported omissions."""
    progress = ledger.progress()
    if not ledger._v2() or progress != ("accepted", windows, len(windows)):
        raise ValueError("Only a validated terminal plan can retry omissions")
    active = {
        key for (key,) in ledger.db.execute(
            "SELECT o.key FROM planning_omissions o LEFT JOIN retry_resolutions r "
            "ON r.omission_key = o.key WHERE r.omission_key IS NULL"
        )
    }
    retry_windows = list(windows)
    for index, window in enumerate(windows):
        receipt = ledger.receipt(index + 1)
        if receipt is None or not receipt.get("omission_keys"):
            continue
        for key in receipt["omission_keys"]:
            if key not in active:
                continue
            attempt = 1 + max(
                (
                    row.get("retry_attempt", 0)
                    for row in retry_windows
                    if row.get("retry_of") == window_receipt_id(window)
                ),
                default=0,
            )
            retry = {**window}
            if receipt["status"] == "partial":
                import json

                from openkb.agent.document_plan import range_dicts
                from openkb.agent.document_plan_annotations import PlanningOmission

                row = ledger.db.execute(
                    "SELECT payload FROM planning_omissions WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    raise ValueError("Missing partial omission")
                omission = PlanningOmission.from_dict(json.loads(row[0]))
                ranges = range_dicts(omission.ranges)
                indices = [
                    value[0] if isinstance(value, list) else value["block_index"]
                    for value in ranges
                ]
                ends = [
                    value[1] if isinstance(value, list) else value["block_index"] + 1
                    for value in ranges
                ]
                frozen = window.get("frozen_ranges") or receipt["target_ranges"]
                frozen_id = (
                    window.get("frozen_evidence_id")
                    or (window.get("evidence") or {}).get("id")
                    or content_id(frozen)
                )
                retry.update(
                    target_start=min(indices), target_end=max(ends),
                    target_ranges=ranges, frozen_ranges=frozen,
                    frozen_evidence_id=frozen_id,
                    window_id=content_id({
                        "frozen_evidence": frozen_id, "target_ranges": ranges
                    }),
                )
            retry.update(
                retry_of=window_receipt_id(window),
                retry_omission_key=key,
                retry_attempt=attempt,
            )
            retry_windows.append(retry)
    if len(retry_windows) == len(windows):
        return None
    with ledger._transaction():
        ledger._set_meta("windows", retry_windows)
        ledger._set_meta("settled_count", len(windows))
        ledger._set_meta("status", "pending")
        ledger._refresh_integrity()
    return retry_windows, len(windows)


def mark_accepted(ledger: Any, windows: list[dict[str, Any]]) -> None:
    """Close a dispatch schedule after all original and retry targets settle."""
    with ledger._transaction():
        overview = ledger.overview()
        has_omissions = ledger.db.execute(
            "SELECT 1 FROM planning_omissions o LEFT JOIN retry_resolutions r "
            "ON r.omission_key = o.key WHERE r.omission_key IS NULL LIMIT 1"
        ).fetchone() is not None
        overview.status = "partial" if has_omissions else "complete"
        ledger._set_meta("overview", overview.to_dict())
        ledger._set_meta("windows", windows)
        ledger._set_meta(ledger._progress_key(), len(windows))
        ledger._set_meta("status", "accepted")
        ledger._refresh_integrity()
