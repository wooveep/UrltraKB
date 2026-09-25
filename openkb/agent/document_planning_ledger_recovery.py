"""Validate a durable planning prefix before it influences resumed work."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from openkb.agent.document_plan import range_dicts, range_intervals
from openkb.agent.document_plan_annotations import PlanningOmission
from openkb.agent.document_planning_ledger_integrity import (
    accepted_proofs_valid,
    salvage_proofs_valid,
)
from openkb.agent.document_planning_ledger_views import terminal_coverage_valid
from openkb.agent.document_window_receipts import (
    accepted_receipts_match_dispatch,
    valid_accepted_receipts,
    window_receipt_id,
)
from openkb.agent.document_windowing import target_intervals


def recovery_valid(
    ledger: Any,
    windows: list[dict[str, Any]],
    completed: int,
    dispatch_lookup: Any,
    *,
    parsed: Any,
    source: Any,
    base_windows: list[dict[str, Any]] | None = None,
) -> bool:
    from openkb.agent.document_planning_support import valid_window_schedule

    try:
        ledger._validate_json_rows(parsed)
    except sqlite3.DatabaseError:
        return False
    if not valid_window_schedule(windows, parsed, source=source, base_schedule=base_windows):
        return False
    if ledger._state_digest() != ledger._meta("state_digest"):
        return False
    if completed < 0 or completed > len(windows):
        return False
    if ledger._meta("status") == "accepted" and completed != len(windows):
        return False
    if int(ledger.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]) != completed:
        return False
    receipts = []
    for index, window in enumerate(windows[:completed], start=1):
        receipt = ledger.receipt(index)
        if receipt is None:
            return False
        if ledger._v2() != (
            receipt.get("protocol") == "document-plan-window-settlement-v2"
        ):
            return False
        if not valid_accepted_receipts([receipt], [window], 1):
            return False
        if not accepted_receipts_match_dispatch([receipt], dispatch_lookup):
            return False
        if retry_of := window.get("retry_of"):
            prior_skips = [
                (earlier, ledger.receipt(position))
                for position, earlier in enumerate(windows[:index - 1], start=1)
                if window_receipt_id(earlier) == retry_of
            ]
            if not any(
                saved is not None and saved.get("omission_keys") for _, saved in prior_skips
            ):
                return False
            retry_key = window.get("retry_omission_key")
            if retry_key is not None and not any(
                saved is not None and retry_key in saved.get("omission_keys", [])
                for _, saved in prior_skips
            ):
                return False
        if receipt.get("status") == "skipped":
            row = ledger.db.execute(
                "SELECT payload FROM planning_omissions WHERE key = ?",
                (receipt["omission_keys"][0],),
            ).fetchone()
            if row is None:
                return False
            omission = PlanningOmission.from_dict(json.loads(row[0]))
            if (
                omission.target_id != receipt["window"]
                or range_dicts(omission.ranges) != receipt["target_ranges"]
                or omission.attempts != receipt["attempts"]
            ):
                return False
        elif receipt.get("omission_keys"):
            if len(receipt["omission_keys"]) != 1:
                return False
            row = ledger.db.execute(
                "SELECT payload FROM planning_omissions WHERE key = ?",
                (receipt["omission_keys"][0],),
            ).fetchone()
            if row is None:
                return False
            omission = PlanningOmission.from_dict(json.loads(row[0]))
            target = target_intervals(window, parsed)
            if (
                omission.target_id != receipt["window"]
                or omission.attempts != receipt["attempt"] + 1
                or not all(
                    any(i == block and left <= start and end <= right
                        for i, left, right in target)
                    for value in omission.ranges
                    for block, start, end in range_intervals(value, parsed, "partial omission")
                )
            ):
                return False
        receipts.append(receipt)
    if ledger._v2():
        omission_keys = {
            key
            for receipt in receipts
            for key in receipt["omission_keys"]
        }
        stored_keys = {
            key for (key,) in ledger.db.execute("SELECT key FROM planning_omissions")
        }
        if stored_keys != omission_keys:
            return False
        for key, sequence in ledger.db.execute(
            "SELECT omission_key, sequence FROM retry_resolutions"
        ):
            if (
                key not in omission_keys
                or not 1 <= sequence <= completed
                or windows[sequence - 1].get("retry_omission_key") != key
            ):
                return False
    if not accepted_proofs_valid(ledger, receipts) or not salvage_proofs_valid(ledger, receipts):
        return False
    if ledger._meta("status") == "accepted":
        if not terminal_coverage_valid(ledger, windows, parsed):
            return False
    return True
