"""Compact progress and usage events for serial document planning."""

from __future__ import annotations

from typing import Any, Callable

from openkb.agent.document_window_receipts import frozen_evidence_id, target_ranges
from openkb.execution_measurement import record_document_planning
from openkb.sources import content_id


def _completed_ranges(windows: list[dict[str, Any]], completed: int) -> list[Any]:
    return [item for window in windows[:completed] for item in target_ranges(window)]


def frozen_prefix(evidence: dict[str, Any]) -> dict[str, Any]:
    """Summarize one supplied W envelope without retaining its source text."""

    blocks = evidence.get("blocks", [])
    if not isinstance(blocks, list):
        return {"hash": content_id({"group": evidence.get("group_id")}), "blocks": 0, "chars": 0}
    rows = [
        {
            "id": block.get("id"),
            "reference": block.get("reference"),
            "chars": len(block.get("text", "")) if isinstance(block.get("text"), str) else 0,
        }
        for block in blocks
        if isinstance(block, dict)
    ]
    return {
        "hash": content_id({"group": evidence.get("group_id"), "blocks": rows}),
        "blocks": len(rows),
        "chars": sum(row["chars"] for row in rows),
    }


def planning_observation(
    event: str,
    window: dict[str, Any],
    windows: list[dict[str, Any]],
    *,
    completed: int,
    carry_pages: int,
    carry_unresolved: int,
    pages: int,
    unresolved: int,
    checkpoint: str | None = None,
    result: str | None = None,
    attempt: int = 0,
    cached: bool = False,
    reason: str = "",
    parts: int = 0,
    prefix: dict[str, Any] | None = None,
    candidate_count: int = 0,
    elapsed_seconds: float = 0.0,
    frozen_last_use_seconds: float | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe W/S/T state without placing source content in task telemetry."""

    index = next(
        (index for index, candidate in enumerate(windows) if candidate is window),
        min(completed, len(windows) - 1),
    )
    prefix = prefix or {}
    request = request or {}
    input_reservation = request.get("input_reservation")
    output_reservation = request.get("output_reservation")
    actual_input = request.get("input_tokens")
    actual_output = request.get("output_tokens")
    return {
        "event": event,
        "window": index + 1,
        "total_windows": len(windows),
        "frozen_evidence": frozen_evidence_id(window),
        "target_ranges": target_ranges(window),
        "completed_ranges": _completed_ranges(windows, completed),
        "carry_pages": carry_pages,
        "carry_unresolved": carry_unresolved,
        "pages": pages,
        "unresolved": unresolved,
        "checkpoint": checkpoint,
        "result": result,
        "attempt": attempt,
        "cached": cached,
        "reason": reason,
        "parts": parts,
        "frozen_prefix_hash": prefix.get("hash") or frozen_evidence_id(window),
        "frozen_prefix_blocks": prefix.get("blocks", 0),
        "frozen_prefix_chars": prefix.get("chars", 0),
        "candidate_count": candidate_count,
        "frozen_last_use_seconds": (
            elapsed_seconds if frozen_last_use_seconds is None else frozen_last_use_seconds
        ),
        "elapsed_seconds": elapsed_seconds,
        "request_id": request.get("id"),
        "input_reservation": input_reservation,
        "output_reservation": output_reservation,
        "actual_input": actual_input,
        "actual_output": actual_output,
        "input_estimate_error": (
            actual_input - input_reservation
            if type(actual_input) is int and type(input_reservation) is int
            else None
        ),
        "output_estimate_error": (
            actual_output - output_reservation
            if type(actual_output) is int and type(output_reservation) is int
            else None
        ),
    }


def emit_planning_observation(
    on_event: Callable[[dict[str, Any]], None], row: dict[str, Any]
) -> None:
    """Send one validated observation to persisted usage and the active task."""

    record_document_planning(row)
    on_event({"stage": "planning", "event": "planning_observation", "observation": row})
