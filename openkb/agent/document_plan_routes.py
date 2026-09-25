"""Atomic source-only decisions and their exact subject-range consequences."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan_feedback import RepairScopeError
from openkb.agent.document_plan_selections import SelectionError, SelectionResolver
from openkb.agent.document_range_validation import range_intervals, subtract_exact_ranges


@dataclass(frozen=True)
class RouteResult:
    candidate: dict[str, Any]
    refs: dict[str, Any]
    derived_changes: tuple[dict[str, Any], ...]
    touched: frozenset[tuple[str, str | None]]


def _ranges(
    value: Any,
    resolver: SelectionResolver | None,
    path: str,
    operation_index: int | None = None,
) -> list[Any]:
    if resolver is None:
        if not isinstance(value, list):
            raise RepairScopeError(
                "Invalid route ranges",
                path=path,
                code="invalid_selection_shape",
                operation_index=operation_index,
            )
        return value
    try:
        return resolver.decode_ranges(value, path, target_only=True)
    except SelectionError as exc:
        raise RepairScopeError(
            "Invalid route evidence selection",
            path=exc.path,
            code=exc.code,
            actual=exc.actual,
            operation_index=operation_index,
        ) from exc


def _intersects(left: list[Any], right: list[Any], chars: list[int]) -> bool:
    for first in left:
        for index, start, end in range_intervals(first, block_chars=chars):
            for second in right:
                if any(
                    other == index and start < limit and begin < end
                    for other, begin, limit in range_intervals(second, block_chars=chars)
                ):
                    return True
    return False


def apply_source_routes(
    baseline: dict[str, Any],
    refs: dict[str, Any],
    operations: list[tuple[int, dict[str, Any], dict[str, Any]]],
    *,
    resolver: SelectionResolver | None,
    block_chars: list[int],
) -> RouteResult:
    """Precompute every decision against one baseline, then change a copy."""
    candidate, updated_refs = deepcopy(baseline), deepcopy(refs)
    source_refs = refs["sections"]["source_only"]
    page_refs = refs["sections"]["page_changes"]
    decisions: list[tuple[int, str, str, list[Any] | None, str | None]] = []
    seen: set[str] = set()
    cuts: dict[str, list[Any]] = {}
    cut_operations: dict[str, list[int]] = {}
    affected: dict[int, set[str]] = {}
    touched: set[tuple[str, str | None]] = set()
    derived: list[dict[str, Any]] = []

    for operation_index, operation, grant in operations:
        ref, value = operation["item_ref"], operation["value"]
        if ref not in source_refs or ref in seen or not isinstance(value, dict):
            raise RepairScopeError(
                "Invalid source route target",
                code="route_scope_violation",
                operation_index=operation_index,
            )
        seen.add(ref)
        path = f"source_only[{source_refs.index(ref)}]"
        if value == {"decision": "discard_claim"}:
            decisions.append((operation_index, ref, "discard_claim", None, None))
            touched.add((ref, None))
            continue
        if (
            set(value) != {"decision", "ranges", "reason"}
            or value.get("decision") != "retain_in_source"
            or not isinstance(value.get("reason"), str)
            or not value["reason"].strip()
        ):
            raise RepairScopeError(
                "Invalid source route decision",
                path=f"{path}.ranges",
                code="invalid_selection_shape",
                operation_index=operation_index,
            )
        selected = _ranges(value["ranges"], resolver, f"{path}.ranges", operation_index)
        if not selected:
            raise RepairScopeError(
                "Empty source route",
                path=f"{path}.ranges",
                code="invalid_selection_shape",
                operation_index=operation_index,
            )
        if any(
            earlier[2] == "retain_in_source"
            and _intersects(selected, earlier[3] or [], block_chars)
            for earlier in decisions
        ):
            raise RepairScopeError(
                "Overlapping source route decisions",
                path=f"{path}.ranges",
                code="route_scope_violation",
                operation_index=operation_index,
            )
        decisions.append((operation_index, ref, "retain_in_source", selected, value["reason"]))
        touched.add((ref, "ranges"))
        touched.add((ref, "reason"))
        for page_index, page in enumerate(baseline["page_changes"]):
            page_ref = page_refs[page_index]
            subject = _ranges(
                page.get("subject_ranges") if isinstance(page, dict) else None,
                resolver,
                f"page_changes[{page_index}].subject_ranges",
                operation_index,
            )
            if not _intersects(subject, selected, block_chars):
                continue
            if page_ref not in grant.get("editable_page_refs", []):
                raise RepairScopeError(
                    "Route would change an unauthorized page",
                    path=f"page_changes[{page_index}].subject_ranges",
                    code="route_scope_violation",
                    operation_index=operation_index,
                    actual=page_ref,
                    allowed=grant.get("editable_page_refs", []),
                )
            cuts.setdefault(page_ref, []).extend(selected)
            cut_operations.setdefault(page_ref, []).append(operation_index)
            affected.setdefault(operation_index, set()).add(page_ref)
            touched.add((page_ref, "subject_ranges"))

    for page_ref, removed in cuts.items():
        page_index = page_refs.index(page_ref)
        original = baseline["page_changes"][page_index]["subject_ranges"]
        numeric = _ranges(original, resolver, f"page_changes[{page_index}].subject_ranges")
        retained = subtract_exact_ranges(numeric, removed, block_chars)
        if not retained:
            raise RepairScopeError(
                "Route would empty page body",
                path=f"page_changes[{page_index}].subject_ranges",
                code="route_would_empty_page",
                actual=removed,
                allowed=original,
                operation_index=cut_operations[page_ref][-1],
            )
        candidate["page_changes"][page_index]["subject_ranges"] = (
            resolver.encode_ranges(retained) if resolver else retained
        )
        derived.append(
            {
                "item_ref": page_ref,
                "field": "subject_ranges",
                "old_ranges": deepcopy(original),
                "new_ranges": deepcopy(candidate["page_changes"][page_index]["subject_ranges"]),
            }
        )

    for operation_index, ref, decision, selected_ranges, reason in decisions:
        source_index = updated_refs["sections"]["source_only"].index(ref)
        original = deepcopy(candidate["source_only"][source_index])
        if decision == "discard_claim":
            candidate["source_only"].pop(source_index)
            updated_refs["sections"]["source_only"].pop(source_index)
            new_ranges = None
        else:
            operation = next(row for index, row, _ in operations if index == operation_index)
            candidate["source_only"][source_index] = {
                "ranges": deepcopy(operation["value"]["ranges"]),
                "reason": reason,
            }
            new_ranges = deepcopy(candidate["source_only"][source_index]["ranges"])
        derived.append(
            {
                "operation_index": operation_index,
                "item_ref": ref,
                "decision": decision,
                "old_ranges": original.get("ranges"),
                "new_ranges": new_ranges,
                "affected_page_refs": sorted(affected.get(operation_index, set())),
            }
        )
    return RouteResult(candidate, updated_refs, tuple(derived), frozenset(touched))
