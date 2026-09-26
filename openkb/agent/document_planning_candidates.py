"""Track candidate recovery separately from the immutable response history."""

from __future__ import annotations

from typing import Any

from openkb.agent.document_planning_response import PageAcceptance
from openkb.sources import content_id


def record_candidate_attempt(
    state: dict[str, Any], task_key: str, attempt: int, result: PageAcceptance
) -> bool:
    """Record real attempts and return whether known candidates remain unresolved."""
    for row in state["rejected"]:
        if row["window"] != task_key:
            continue
        unresolved_kind = row["reason"] in {"unknown_kind", "conflicting_kind"}
        if (
            result.no_pages
            or not unresolved_kind
            and row.get("candidate_key") in result.resolved_candidates
            or unresolved_kind
            and list(result.resolved_candidates.values()).count(row.get("candidate_name_key")) == 1
            or row["reason"] == "pages_unparseable"
            and result.resolved_candidates
        ):
            row["resolved"] = True
    state["rejected"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.rejected
    )
    state["filtered"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.filtered
    )
    return any(row["window"] == task_key and not row.get("resolved") for row in state["rejected"])


def rejected_candidate_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Count repeated candidates once while preserving per-request history separately."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[tuple[str, str], set[int]] = {}
    for index, row in enumerate(state["rejected"]):
        if row.get("resolved") or state["tasks"].get(row["window"], {}).get("status") != "skipped":
            continue
        key = (row["window"], row.get("candidate_key") or content_id(row["candidate"]))
        group = groups.setdefault(key, {**row, "reasons": []})
        if row["reason"] not in group["reasons"]:
            group["reasons"].append(row["reason"])
        attempts.setdefault(key, set()).add(row.get("attempt", index + 1))
        group["attempts"] = len(attempts[key])
    return list(groups.values())
