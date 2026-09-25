"""Scoped, atomic edits to a parsed document-plan-v3 candidate."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from openkb.agent._document_plan_compiler_support import (
    _CONTEXT_REQUIRED,
    _PAGE_FIELDS,
    _PAGE_REQUIRED,
    _RESOLUTION_FIELDS,
    _SOURCE_ONLY_FIELDS,
    _UNRESOLVED_FIELDS,
    CONTEXT_FIELDS,
)
from openkb.agent.document_plan_feedback import RepairScopeError
from openkb.agent.document_plan_issues import ValidationIssue
from openkb.agent.document_plan_routes import apply_source_routes
from openkb.agent.document_plan_selections import SelectionError, SelectionResolver
from openkb.agent.document_range_validation import merged_intervals, range_intervals
from openkb.sources import content_id

_SECTIONS = ("page_changes", "source_only", "unresolved", "resolutions")
_RANGE_FIELDS = {"subject_ranges", "ranges", "basis_ranges", "location"}
_OPERATION_FIELDS = {
    "replace_field": frozenset({"issue_id", "op", "item_ref", "field", "value"}),
    "append_item": frozenset({"issue_id", "op", "item_ref", "field", "value"}),
    "remove_field": frozenset({"issue_id", "op", "item_ref", "field"}),
    "remove_item": frozenset({"issue_id", "op", "item_ref"}),
    "resolve_source_only": frozenset({"issue_id", "op", "item_ref", "value"}),
}
_VALUE_TYPES: dict[str, type | tuple[type, ...]] = {
    "overview": dict,
    "page_changes": list,
    "source_only": list,
    "unresolved": list,
    "resolutions": list,
    "limitations": list,
    "subject_ranges": list,
    "necessary_context": list,
    "ranges": list,
    "basis_ranges": list,
    "location": list,
    "affected_pages": list,
    "text": str,
    "local_key": str,
    "kind": str,
    "title": str,
    "purpose": str,
    "reason": str,
    "rationale": str,
    "relation": str,
    "problem_type": str,
    "missing_target": str,
    "unresolved_key": str,
    "target": (str, type(None)),
    "target_key": (str, type(None)),
    "type": (str, type(None)),
}
_ITEM_SCHEMAS = {
    "page_changes": "page_changes[] in document-plan-v3",
    "necessary_context": "page_changes[].necessary_context[] in document-plan-v3",
    "source_only": "source_only[] in document-plan-v3",
    "unresolved": "unresolved[] in document-plan-v3",
    "resolutions": "resolutions[] in document-plan-v3",
}
_VALUE_SHAPES = {
    "page_changes": (_PAGE_REQUIRED, _PAGE_FIELDS),
    "necessary_context": (_CONTEXT_REQUIRED, CONTEXT_FIELDS),
    "source_only": (_SOURCE_ONLY_FIELDS, _SOURCE_ONLY_FIELDS),
    "unresolved": (_UNRESOLVED_FIELDS, _UNRESOLVED_FIELDS),
    "resolutions": (_RESOLUTION_FIELDS, _RESOLUTION_FIELDS),
}


@dataclass(frozen=True)
class PatchResult:
    candidate: dict[str, Any]
    refs: dict[str, Any]
    normalizations: tuple[dict[str, Any], ...] = ()
    derived_changes: tuple[dict[str, Any], ...] = ()


def operation_contract(protocol: str = "document-plan-repair-v1") -> dict[str, Any]:
    """One wire definition also used by the receiver's field-set check."""
    return {
        op: {
            "required_fields": sorted(fields),
            "value": (
                "complete replacement field value"
                if op == "replace_field"
                else "one item object"
                if op == "append_item"
                else "source-only routing decision"
                if op == "resolve_source_only"
                else "forbidden"
            )
            if "value" in fields
            else "forbidden",
        }
        for op, fields in _OPERATION_FIELDS.items()
        if protocol == "document-plan-repair-v2" or op != "resolve_source_only"
    }


def _candidate_hash(candidate: dict[str, Any], protocol: str) -> str:
    if protocol == "document-plan-repair-v2":
        return content_id(
            {
                "plan_protocol": "document-plan-v4",
                "repair_protocol": protocol,
                "candidate": candidate,
            }
        )
    return content_id(candidate)


def _value_contract(op: str, field: str | None) -> str:
    if op == "resolve_source_only":
        return "object: discard_claim or retain_in_source decision in response_contract.route_value"
    if op == "append_item":
        return f"object: {_ITEM_SCHEMAS.get(field or '', 'one item')}"
    if op != "replace_field" or field is None:
        return "value forbidden"
    expected = _VALUE_TYPES.get(field)
    names = expected if isinstance(expected, tuple) else (expected,)
    basic = " or ".join(value.__name__ for value in names if value is not None)
    if field in _ITEM_SCHEMAS:
        return f"{basic}; elements: {_ITEM_SCHEMAS[field]}"
    if field in _RANGE_FIELDS:
        return f"{basic}; exact source ranges in document-plan-v3"
    return basic


def item_refs(candidate: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Give each item an identity independent of its current list position."""
    old = previous or {}
    serial = int(old.get("serial", 0))
    result: dict[str, Any] = {"serial": serial, "sections": {}, "contexts": {}}
    for section in _SECTIONS:
        prior = list(old.get("sections", {}).get(section, []))
        rows = candidate.get(section)
        count = len(rows) if isinstance(rows, list) else 0
        refs = prior[:count]
        while len(refs) < count:
            result["serial"] += 1
            refs.append(f"item:{result['serial']}")
        result["sections"][section] = refs
    pages = candidate.get("page_changes")
    for page_index, page in enumerate(pages if isinstance(pages, list) else []):
        if not isinstance(page, dict):
            continue
        page_ref = result["sections"]["page_changes"][page_index]
        prior = list(old.get("contexts", {}).get(page_ref, []))
        contexts = page.get("necessary_context", [])
        refs = prior[: len(contexts)] if isinstance(contexts, list) else []
        while isinstance(contexts, list) and len(refs) < len(contexts):
            result["serial"] += 1
            refs.append(f"item:{result['serial']}")
        result["contexts"][page_ref] = refs
    return result


def _path_ref(candidate: dict[str, Any], refs: dict[str, Any], path: str) -> tuple[str, str]:
    if path.startswith("overview."):
        return "overview", path.removeprefix("overview.").split("[", 1)[0]
    for section in _SECTIONS:
        if path == section:
            return "plan", section
        prefix = section + "["
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        try:
            index = int(rest.split("]", 1)[0])
            ref = refs["sections"][section][index]
        except (IndexError, KeyError, TypeError, ValueError):
            return "plan", section
        suffix = rest.split("]", 1)[1]
        if section == "page_changes" and suffix.startswith(".necessary_context["):
            try:
                context_index = int(suffix.split("[", 1)[1].split("]", 1)[0])
                context_ref = refs["contexts"][ref][context_index]
                field = suffix.split("]", 1)[1].removeprefix(".").split("[", 1)[0]
                return context_ref, field
            except (IndexError, KeyError, TypeError, ValueError):
                return ref, "necessary_context"
        return ref, suffix.removeprefix(".").split("[", 1)[0]
    return "plan", path


def _located(
    candidate: dict[str, Any], refs: dict[str, Any], ref: str
) -> tuple[list[Any] | None, int | None, dict[str, Any]]:
    if ref == "plan":
        return None, None, candidate
    if ref == "overview":
        return None, None, candidate["overview"]
    for section in _SECTIONS:
        rows = refs["sections"][section]
        if ref in rows:
            index = rows.index(ref)
            return candidate[section], index, candidate[section][index]
    for page_ref, rows in refs["contexts"].items():
        if ref in rows:
            page_index = refs["sections"]["page_changes"].index(page_ref)
            contexts = candidate["page_changes"][page_index]["necessary_context"]
            index = rows.index(ref)
            return contexts, index, contexts[index]
    raise RepairScopeError("Unknown item reference")


def _intervals(values: Any, chars: list[int]) -> dict[int, list[tuple[int, int]]]:
    if not isinstance(values, list):
        raise RepairScopeError("Range field is not a list")
    result: dict[int, list[tuple[int, int]]] = {}
    try:
        for value in values:
            for index, start, end in range_intervals(value, block_chars=chars):
                result.setdefault(index, []).append((start, end))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RepairScopeError("Malformed patch ranges") from exc
    return {index: merged_intervals(rows) for index, rows in result.items()}


def _within(values: Any, allowed: Any, chars: list[int]) -> bool:
    actual, permitted = _intervals(values, chars), _intervals(allowed, chars)
    return all(
        any(left <= start and end <= right for left, right in permitted.get(index, []))
        for index, rows in actual.items()
        for start, end in rows
    )


def _ranges_of(value: Any, field: str) -> Any:
    if field == "page_changes":
        return value.get("subject_ranges") if isinstance(value, dict) else None
    if field == "necessary_context":
        return value.get("ranges") if isinstance(value, dict) else None
    if field == "source_only":
        return value.get("ranges") if isinstance(value, dict) else None
    if field == "unresolved":
        return value.get("location") if isinstance(value, dict) else None
    return None


def repair_policy(
    candidate: dict[str, Any],
    refs: dict[str, Any],
    issues: tuple[ValidationIssue, ...],
    *,
    protocol: str = "document-plan-repair-v1",
    editable_page_refs: list[str] | None = None,
    target_ranges: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """The single source of repair options for both prompt and authorization."""
    allowed: list[dict[str, Any]] = []
    pages = refs["sections"]["page_changes"]
    for issue in issues:
        issue_id = issue.issue_id
        ref, field = _path_ref(candidate, refs, issue.path)
        options: list[tuple[str, str, str | None]] = []
        if issue.code in {"coverage_gap", "coverage_pending"}:
            options.extend(
                ("append_item", "plan", section)
                for section in ("page_changes", "source_only", "unresolved")
            )
            options.extend(("replace_field", page, "subject_ranges") for page in pages)
            options.extend(("append_item", page, "necessary_context") for page in pages)
        elif (
            protocol == "document-plan-repair-v2"
            and ref in refs["sections"]["source_only"]
            and field == "ranges"
            and issue.code
            in {
                "source_only_conflict",
                "invalid_selection_shape",
                "unknown_block_reference",
                "selection_outside_evidence",
                "invalid_range_shape",
                "range_out_of_bounds",
                "range_empty",
                "target_range_violation",
                "evidence_range_violation",
            }
        ):
            options.append(("resolve_source_only", ref, None))
        elif issue.code == "source_only_conflict":
            options.extend((("replace_field", ref, "ranges"), ("remove_item", ref, None)))
            for path in issue.related_paths:
                page_ref, _ = _path_ref(candidate, refs, path)
                options.append(("replace_field", page_ref, "subject_ranges"))
        elif issue.code in {"program_owned_field", "unknown_field"}:
            options.append(("remove_field", ref, field))
        elif issue.code in {
            "json_syntax",
            "json_duplicate_field",
            "projection_required",
            "invalid_planning_context",
            "page_name_collision",
        }:
            continue
        elif ref != "plan" and field:
            options.append(("replace_field", ref, field))
            if issue.code == "invalid_entity_type" and field == "type":
                _, _, affected = _located(candidate, refs, ref)
                if isinstance(affected, dict) and affected.get("kind") == "concept":
                    options.append(("remove_field", ref, field))
            if issue.code == "invalid_page_kind":
                options.append(("replace_field", ref, "type"))
            if (
                issue.code
                in {
                    "invalid_item_shape",
                    "invalid_context_relation",
                    "invalid_problem_type",
                    "invalid_text",
                }
                and ref != "overview"
            ):
                options.append(("remove_item", ref, None))
        elif ref != "plan" and issue.code == "invalid_item_shape":
            options.append(("remove_item", ref, None))
        elif ref == "plan" and field in {*_SECTIONS, "overview"}:
            options.append(("replace_field", ref, field))
        for op, item_ref, target_field in options:
            allowed.append(
                {
                    "issue_id": issue_id,
                    "code": issue.code,
                    "path": issue.path,
                    "op": op,
                    "item_ref": item_ref,
                    "field": target_field,
                    "source_ranges": deepcopy(issue.source_ranges),
                    "expected": issue.expected,
                    **(
                        {
                            "editable_page_refs": list(editable_page_refs or pages),
                            "target_ranges": deepcopy(target_ranges or []),
                        }
                        if op == "resolve_source_only"
                        else {}
                    ),
                }
            )
    grouped: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for row in allowed:
        policy = (
            row["code"]
            if row["code"] in {"coverage_gap", "coverage_pending", "source_only_conflict"}
            else "same_field_repair"
        )
        key = (policy, row["op"], row["item_ref"], row["field"])
        if key not in grouped:
            grouped[key] = {**row, "related_issue_ids": [], "source_ranges": []}
        merged = grouped[key]
        merged["related_issue_ids"].append(row["issue_id"])
        for source_range in row["source_ranges"]:
            if source_range not in merged["source_ranges"]:
                merged["source_ranges"].append(source_range)
    for row in grouped.values():
        row["related_issue_ids"] = sorted(set(row["related_issue_ids"]))
        row["issue_id"] = row["related_issue_ids"][0]
        row["value_type"] = _value_contract(row["op"], row["field"])
    return list(grouped.values())


def patch_request(
    candidate: dict[str, Any],
    refs: dict[str, Any],
    issues: tuple[ValidationIssue, ...],
    *,
    protocol: str = "document-plan-repair-v1",
    editable_page_refs: list[str] | None = None,
    target_ranges: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "repair_protocol": protocol,
        "candidate_hash": _candidate_hash(candidate, protocol),
        "candidate": deepcopy(candidate),
        "item_refs": deepcopy(refs),
        "issues": [
            {
                **asdict(issue),
                "issue_id": issue.issue_id,
                "item_ref": _path_ref(candidate, refs, issue.path)[0],
                "field": _path_ref(candidate, refs, issue.path)[1],
            }
            for issue in issues
        ],
        "allowed_operations": repair_policy(
            candidate,
            refs,
            issues,
            protocol=protocol,
            editable_page_refs=editable_page_refs,
            target_ranges=target_ranges,
        ),
        "response_contract": {
            "top_level_fields": ["repair_protocol", "candidate_hash", "operations"],
            "operations": operation_contract(protocol),
            **(
                {
                    "route_value": {
                        "discard_claim": {"decision": "discard_claim"},
                        "retain_in_source": {
                            "decision": "retain_in_source",
                            "ranges": "v4 block selections",
                            "reason": "concrete source-only reason",
                        },
                    }
                }
                if protocol == "document-plan-repair-v2"
                else {}
            ),
            "value_shapes": {
                field: {
                    "required_fields": sorted(required),
                    "optional_fields": sorted(allowed - required),
                }
                for field, (required, allowed) in _VALUE_SHAPES.items()
            },
        },
        "instruction": (
            f"response_mode=patch. Return {protocol}, not a full plan. "
            "Return only repair_protocol, candidate_hash and operations. Copy issue_id, op, "
            "item_ref and field from allowed_operations; only value is newly supplied. "
            "replace_field and append_item must use the literal key value, even when its "
            "value is an empty array. Replace arrays with the complete new array; append "
            "one object, never an array. Use response_contract.value_shapes for item "
            "values; ranges use the exact authorized block selection format. "
            "remove_field and remove_item forbid value. "
            "Each (op, item_ref, field) repair needs at most one operation, even when "
            "several issues mention different elements of that field. Merge those issues "
            "into one complete replace_field value. Every whole-block range object must "
            "contain both from_block and through_block, even for one block. "
            "Distinct append_item "
            "operations may share an array, but do not repeat an identical object. "
            "For source_only_conflict, remove only the exact listed conflict intervals "
            "and preserve every other old source_only interval. For coverage gaps, add "
            "only within the listed gap intervals and retain existing subject evidence. "
            "Do not change unauthorized content. An overview edit cannot close a coverage gap. "
            "If no grounded edit is possible, return empty operations; this means no_progress."
        )
        + (
            " resolve_source_only has no field. Choose discard_claim to withdraw the "
            "wrong source-only claim, or retain_in_source with exact new block selections "
            "and a reason. The program will remove the selected overlap from only the "
            "listed editable_page_refs; this may not empty a page. Do not separately edit "
            "the affected subject_ranges in the same batch."
            if protocol == "document-plan-repair-v2"
            else ""
        ),
    }


def compact_patch_request(request: dict[str, Any], *, issue_limit: int = 8) -> dict[str, Any]:
    """Project just the authorized entries; the full candidate stays in the journal."""
    candidate = request["candidate"]
    refs = request["item_refs"]
    compact = deepcopy(request)
    visible_issues = {row["issue_id"] for row in compact["issues"][:issue_limit]}
    compact["allowed_operations"] = [
        row
        for row in compact["allowed_operations"]
        if visible_issues.intersection(row["related_issue_ids"])
    ]
    relevant = {
        issue_id for row in compact["allowed_operations"] for issue_id in row["related_issue_ids"]
    }
    compact["issues"] = [row for row in compact["issues"] if row["issue_id"] in relevant]
    involved = {
        row["item_ref"]
        for row in compact["allowed_operations"]
        if row["item_ref"] not in {"plan", "overview"}
    }
    involved.update(
        page for row in compact["allowed_operations"] for page in row.get("editable_page_refs", [])
    )
    items = []
    for ref in sorted(involved):
        _, _, value = _located(candidate, refs, ref)
        if ref in refs["sections"]["page_changes"] and isinstance(value, dict):
            value = {
                key: value.get(key) for key in ("local_key", "title", "purpose", "subject_ranges")
            }
        items.append({"item_ref": ref, "value": deepcopy(value)})
    compact["candidate_projection"] = {
        "overview": candidate.get("overview"),
        "items": items,
        "section_counts": {
            section: len(candidate.get(section)) if isinstance(candidate.get(section), list) else 0
            for section in _SECTIONS
        },
    }
    compact.pop("candidate")
    compact.pop("item_refs")
    return compact


def apply_plan_patch(
    baseline: dict[str, Any],
    refs: dict[str, Any],
    request: dict[str, Any],
    response: Any,
    *,
    block_chars: list[int],
    resolver: SelectionResolver | None = None,
) -> PatchResult:
    """Validate and apply an entire patch batch on a copy, or reject all of it."""
    protocol = request.get("repair_protocol")
    if not isinstance(response, dict):
        raise RepairScopeError("Invalid patch response")
    if (
        protocol not in {"document-plan-repair-v1", "document-plan-repair-v2"}
        or response.get("repair_protocol") != protocol
    ):
        raise RepairScopeError("Invalid patch protocol")
    if protocol == "document-plan-repair-v2" and resolver is None:
        raise RepairScopeError("Missing frozen selection resolver")
    baseline_hash = _candidate_hash(baseline, protocol)
    if (
        response.get("candidate_hash") != baseline_hash
        or request.get("candidate_hash") != baseline_hash
    ):
        raise RepairScopeError("Repair candidate identity mismatch")
    if set(response) != {"repair_protocol", "candidate_hash", "operations"}:
        raise RepairScopeError("Invalid patch response fields", code="invalid_selection_shape")
    operations = response.get("operations")
    if not isinstance(operations, list):
        raise RepairScopeError("Invalid patch operations", code="invalid_selection_shape")
    if not operations:
        raise RepairScopeError("Empty patch operations")
    grants = request.get("allowed_operations", [])
    normalized: list[dict[str, Any]] = []
    normalizations: list[dict[str, Any]] = []
    for operation_index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise RepairScopeError(
                "Invalid patch operation",
                code="invalid_selection_shape",
                operation_index=operation_index,
                actual=operation,
            )
        op = operation.get("op")
        if op not in _OPERATION_FIELDS or (
            protocol == "document-plan-repair-v1" and op == "resolve_source_only"
        ):
            raise RepairScopeError(
                "Unknown patch operation",
                code="invalid_selection_shape",
                operation_index=operation_index,
                actual=op,
            )
        fields = _OPERATION_FIELDS[op]
        if (
            op in {"replace_field", "append_item"}
            and "content" in operation
            and "value" not in operation
            and set(operation) == (fields - {"value"}) | {"content"}
        ):
            operation = {**operation}
            operation["value"] = operation.pop("content")
            normalizations.append(
                {"operation_index": operation_index, "conversion": "content_to_value"}
            )
        if set(operation) != fields:
            raise RepairScopeError(
                "Invalid patch operation fields",
                code="invalid_selection_shape",
                operation_index=operation_index,
                actual=sorted(operation),
                allowed=sorted(fields),
            )
        if protocol == "document-plan-repair-v2":
            matching = [
                grant
                for grant in grants
                if all(grant.get(key) == operation.get(key) for key in ("op", "item_ref", "field"))
                and operation.get("issue_id") in grant.get("related_issue_ids", [])
            ]
            if len(matching) == 1 and operation["issue_id"] != matching[0]["issue_id"]:
                original_id = operation["issue_id"]
                operation = {**operation, "issue_id": matching[0]["issue_id"]}
                normalizations.append(
                    {
                        "operation_index": operation_index,
                        "conversion": "related_issue_to_grant",
                        "original_issue_id": original_id,
                        "grant_issue_id": operation["issue_id"],
                    }
                )
        normalized.append(operation)
    route_operations = []
    for operation_index, operation in enumerate(normalized):
        if operation["op"] != "resolve_source_only":
            continue
        grant = next(
            (
                row
                for row in grants
                if all(
                    row.get(key) == operation.get(key)
                    for key in ("issue_id", "op", "item_ref", "field")
                )
            ),
            None,
        )
        if grant is None:
            raise RepairScopeError(
                "Patch operation is outside repair scope",
                operation_index=operation_index,
                item_ref=operation.get("item_ref"),
                field=operation.get("field"),
                actual={key: operation.get(key) for key in ("issue_id", "op", "item_ref", "field")},
                allowed=[
                    {key: row.get(key) for key in ("issue_id", "op", "item_ref", "field")}
                    for row in grants
                    if row.get("item_ref") == operation.get("item_ref")
                ],
            )
        route_operations.append((operation_index, operation, grant))
    route_result = apply_source_routes(
        baseline, refs, route_operations, resolver=resolver, block_chars=block_chars
    )
    candidate, updated_refs = route_result.candidate, route_result.refs
    route_refs = {op["item_ref"] for _, op, _ in route_operations}
    touched: set[tuple[str, str | None]] = set(route_result.touched)
    appended: dict[tuple[str, str | None], set[str]] = {}
    deleted: set[str] = {
        ref for ref in route_refs if ref not in updated_refs["sections"]["source_only"]
    }
    context_owners = {
        child: parent for parent, children in refs.get("contexts", {}).items() for child in children
    }
    removed = {row["item_ref"] for row in normalized if row["op"] == "remove_item"}
    for operation_index, operation in enumerate(normalized):
        if operation["op"] == "resolve_source_only":
            continue
        op, ref, field = (operation.get(key) for key in ("op", "item_ref", "field"))
        if not isinstance(ref, str) or (op != "remove_item" and not isinstance(field, str)):
            raise RepairScopeError("Invalid patch target")
        grant = next(
            (
                row
                for row in grants
                if all(
                    row.get(key) == operation.get(key)
                    for key in ("issue_id", "op", "item_ref", "field")
                )
            ),
            None,
        )
        if (
            grant is None
            or ref in route_refs
            or ref in deleted
            or (ref, field) in touched
            or (op != "append_item" and (ref, field) in appended)
            or (ref in removed and op != "remove_item")
            or context_owners.get(ref) in removed
            or (
                op == "remove_item"
                and any(context_owners.get(other[0]) == ref for other in touched | set(appended))
            )
            or op == "remove_item"
            and any(item_ref == ref for item_ref, _ in touched)
        ):
            raise RepairScopeError(
                "Patch operation is outside repair scope",
                operation_index=operation_index,
                item_ref=ref,
                field=field,
                grant={key: grant.get(key) for key in ("issue_id", "op", "item_ref", "field")}
                if grant is not None
                else None,
                actual={key: operation.get(key) for key in ("issue_id", "op", "item_ref", "field")},
                allowed=[
                    {key: row.get(key) for key in ("issue_id", "op", "item_ref", "field")}
                    for row in grants
                    if row.get("item_ref") == ref
                ],
            )
        if op == "append_item":
            value_hash = content_id(operation["value"])
            seen = appended.setdefault((ref, field), set())
            if value_hash in seen:
                raise RepairScopeError("Duplicate append operation")
            seen.add(value_hash)
        else:
            touched.add((ref, field))
        rows, index, target = _located(candidate, updated_refs, ref)
        if op == "remove_item":
            if rows is None or index is None:
                raise RepairScopeError("Cannot remove this item")
            if grant["code"] == "source_only_conflict" and not _within(
                target.get("ranges"), grant["source_ranges"], block_chars
            ):
                raise RepairScopeError("Cannot remove nonconflicting source content")
            rows.pop(index)
            deleted.add(ref)
            for section, section_refs in updated_refs["sections"].items():
                if ref in section_refs:
                    section_refs.remove(ref)
                    if section == "page_changes":
                        updated_refs["contexts"].pop(ref, None)
                    break
            else:
                for context_refs in updated_refs["contexts"].values():
                    if ref in context_refs:
                        context_refs.remove(ref)
                        break
            continue
        if not isinstance(field, str) or not isinstance(target, dict):
            raise RepairScopeError("Invalid patch target")
        if op == "remove_field":
            if field not in target:
                raise RepairScopeError("Field is absent")
            target.pop(field)
            continue
        value = deepcopy(operation["value"])
        expected = _VALUE_TYPES.get(field)
        if op == "replace_field" and expected is not None and not isinstance(value, expected):
            raise RepairScopeError("Invalid patch value type")
        if op == "append_item":
            array = target.get(field)
            if not isinstance(array, list) or not isinstance(value, dict):
                raise RepairScopeError("Append target or item is invalid")
            if grant["code"] in {"coverage_gap", "coverage_pending"}:
                ranges = _ranges_of(value, field)
                if resolver is not None and ranges is not None:
                    try:
                        ranges = resolver.decode_ranges(
                            ranges, f"{field}.ranges", target_only=field != "necessary_context"
                        )
                    except SelectionError as exc:
                        raise RepairScopeError(
                            "Invalid patch evidence selection",
                            path=exc.path,
                            code=exc.code,
                            operation_index=operation_index,
                            actual=exc.actual,
                        ) from exc
                if ranges is None or not _within(ranges, grant["source_ranges"], block_chars):
                    raise RepairScopeError("Patch claims evidence outside the diagnosed gap")
            array.append(value)
            updated_refs["serial"] += 1
            new_ref = f"item:{updated_refs['serial']}"
            if ref == "plan":
                updated_refs["sections"][field].append(new_ref)
                if field == "page_changes":
                    updated_refs["contexts"][new_ref] = []
            elif field == "necessary_context":
                updated_refs["contexts"][ref].append(new_ref)
            continue
        if grant["code"] in {"coverage_gap", "coverage_pending"} and field == "subject_ranges":
            old = target.get(field, [])
            if resolver is not None:
                try:
                    old = resolver.decode_ranges(old, f"{ref}.subject_ranges", target_only=True)
                    numeric_value = resolver.decode_ranges(
                        value, f"{ref}.subject_ranges", target_only=True
                    )
                except SelectionError as exc:
                    raise RepairScopeError(
                        "Invalid patch evidence selection",
                        path=exc.path,
                        code=exc.code,
                        operation_index=operation_index,
                        actual=exc.actual,
                    ) from exc
            else:
                numeric_value = value
            if not _within(old, numeric_value, block_chars) or not _within(
                numeric_value, [*old, *grant["source_ranges"]], block_chars
            ):
                raise RepairScopeError("Subject edit exceeds the diagnosed gap")
        if grant["code"] == "source_only_conflict" and field in {"ranges", "subject_ranges"}:
            old = target.get(field, [])
            if not _within(old, [*value, *grant["source_ranges"]], block_chars) or not _within(
                value, old, block_chars
            ):
                raise RepairScopeError("Conflict edit exceeds the diagnosed overlap")
        target[field] = value
    return PatchResult(candidate, updated_refs, tuple(normalizations), route_result.derived_changes)
