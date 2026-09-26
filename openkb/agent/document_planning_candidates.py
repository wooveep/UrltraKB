"""Track candidate recovery separately from the immutable response history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openkb.sources import content_id

if TYPE_CHECKING:
    from openkb.agent.document_planning_response import PageAcceptance


def candidate_identity(
    row: dict[str, Any], kind: str | None, title: str, name: str, *, reliable: bool
) -> dict[str, str]:
    """Named candidates survive location edits; opaque ones retain their full content."""
    if reliable and title and name:
        return {
            "identity_kind": "named",
            "candidate_key": content_id((kind, title, name)),
            "candidate_name_key": content_id((title, name)),
        }
    return {"identity_kind": "opaque", "candidate_key": content_id(("opaque-v1", row))}


def record_candidate_attempt(
    state: dict[str, Any], task_key: str, attempt: int, result: PageAcceptance
) -> None:
    """Retain diagnostic history without turning discarded rows into retry debt."""
    state["rejected"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.rejected
    )
    state["filtered"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.filtered
    )


def rejected_candidate_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize dropped rows, preserving the immutable per-response history."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in state["rejected"]:
        key = (row["window"], row.get("candidate_key") or content_id(row["candidate"]))
        group = groups.setdefault(key, {**row, "reasons": [], "attempts": 0})
        group["attempts"] += 1
        if row["reason"] not in group["reasons"]:
            group["reasons"].append(row["reason"])
    return list(groups.values())
