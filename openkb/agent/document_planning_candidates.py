"""Track candidate recovery separately from the immutable response history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openkb.agent.document_range_validation import validate_ranges
from openkb.agent.document_window_receipts import window_receipt_id
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
) -> bool:
    """Record real attempts and return whether known candidates remain unresolved."""
    for row in pending_candidates(state, task_key):
        unresolved_kind = row["reason"] in {"unknown_kind", "conflicting_kind"}
        name_key = row.get("candidate_name_key")
        named_match = (
            row.get("identity_kind") == "named"
            and isinstance(name_key, str)
            and bool(name_key)
            and list(result.resolved_candidates.values()).count(name_key) == 1
        )
        exact_match = (
            row.get("identity_kind") in {"named", "opaque"}
            and row.get("candidate_key") in result.resolved_candidates
        )
        if (
            not unresolved_kind
            and exact_match
            or unresolved_kind
            and named_match
            or row["reason"] == "pages_unparseable"
            and "candidate_key" not in row
            and result.usable
            and not result.truncated
        ):
            row["resolved"] = True
            row["resolved_attempt"] = attempt
            row["resolution_reason"] = next(
                (
                    item["reason"]
                    for item in result.filtered
                    if item["candidate_key"] in result.resolved_candidates
                    and (
                        item["candidate_key"] == row.get("candidate_key")
                        or named_match
                        and item.get("candidate_name_key") == name_key
                    )
                ),
                "response_recovered" if row["reason"] == "pages_unparseable" else "accepted",
            )
    state["rejected"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.rejected
    )
    state["filtered"].extend(
        {"window": task_key, "attempt": attempt, **row} for row in result.filtered
    )
    return bool(pending_candidates(state, task_key))


def pending_candidates(state: dict[str, Any], task_key: str | None = None) -> list[dict[str, Any]]:
    """One projection for retry instructions, settlement, and the final report."""
    return [
        row
        for row in state["rejected"]
        if not row.get("resolved") and (task_key is None or row.get("window") == task_key)
    ]


def retain_split_candidates(
    state: dict[str, Any], window: dict[str, Any], ranges: list[Any]
) -> None:
    """Keep unresolved parent work explicit; child tasks cannot settle another target."""
    task_key = window_receipt_id(window) + ":pages"
    if pending_candidates(state, task_key):
        state["tasks"][task_key].update(
            status="skipped",
            reason="page_candidates_rejected",
            retired_target={
                "window_id": window_receipt_id(window),
                "target_start": window["target_start"],
                "target_end": window["target_end"],
                "target_ranges": ranges,
            },
        )


def valid_split_candidate_targets(state: dict[str, Any], windows: list[dict[str, Any]]) -> bool:
    """Validate retired task bounds before their pending candidates reach the report."""
    for key, task in state["tasks"].items():
        if not isinstance(task, dict) or "retired_target" not in task:
            continue
        target = task["retired_target"]
        if (
            not isinstance(target, dict)
            or set(target) != {"window_id", "target_start", "target_end", "target_ranges"}
            or not isinstance(target["window_id"], str)
            or key != target["window_id"] + ":pages"
            or any(type(target[field]) is not int for field in ("target_start", "target_end"))
            or not target["target_ranges"]
            or not any(
                parent["target_start"]
                <= target["target_start"]
                < target["target_end"]
                <= parent["target_end"]
                for parent in windows
            )
        ):
            return False
        try:
            validate_ranges(target["target_ranges"], target["target_end"], "retired target")
        except ValueError:
            return False
        if any(
            (value["block_index"] if isinstance(value, dict) else value[0]) < target["target_start"]
            for value in target["target_ranges"]
        ):
            return False
    return True


def rejected_candidate_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Count repeated candidates once while preserving per-request history separately."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[tuple[str, str], set[int]] = {}
    for index, row in enumerate(pending_candidates(state)):
        key = (row["window"], row.get("candidate_key") or content_id(row["candidate"]))
        group = groups.setdefault(key, {**row, "reasons": []})
        if row.get("identity_kind") == "opaque":
            group["description"] = "未识别输出项，无法确认是否被后续结果替代"
        if row["reason"] not in group["reasons"]:
            group["reasons"].append(row["reason"])
        attempts.setdefault(key, set()).add(row.get("attempt", index + 1))
        group["attempts"] = len(attempts[key])
    return list(groups.values())
