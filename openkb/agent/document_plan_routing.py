"""Request-bound route choices; the program owns every resulting source interval."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan_feedback import RepairScopeError
from openkb.agent.document_plan_issues import ValidationIssue
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_range_validation import (
    interval_is_covered,
    merged_intervals,
    range_intervals,
    subtract_exact_ranges,
)
from openkb.sources import content_id

PROTOCOL = "document-plan-repair-v3"
_SOURCE_PATH = re.compile(r"source_only\[(\d+)\]\.ranges\[\d+\]$")
_DECISION_FIELDS = {
    "route_source_only": {"decision_id", "decision", "retained_pieces", "reason"},
    "attach_to_pages": {"decision_id", "decision", "page_refs"},
    "source_only": {"decision_id", "decision", "reason"},
    "new_page": {"decision_id", "decision", "kind", "title", "purpose", "type"},
    "defer": {"decision_id", "decision", "reason"},
}
_CHOICES_BY_KIND = {
    "source_only_conflict": ["route_source_only"],
    "coverage_gap": ["attach_to_pages", "source_only", "new_page", "defer"],
}


@dataclass(frozen=True)
class RoutingResult:
    candidate: dict[str, Any]
    refs: dict[str, Any]
    normalizations: tuple[dict[str, Any], ...] = ()
    derived_changes: tuple[dict[str, Any], ...] = ()


class RoutingDecisionError(RepairScopeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        decision_ids: list[str] | None = None,
        valid_decisions: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)
        self.decision_ids = decision_ids or []
        self.valid_decisions = valid_decisions or {}


def _numeric_piece(index: int, start: int, end: int) -> dict[str, int]:
    return {"block_index": index, "start_char": start, "end_char": end}


def _intervals(values: list[Any], chars: list[int]) -> dict[int, list[tuple[int, int]]]:
    rows: dict[int, list[tuple[int, int]]] = {}
    for value in values:
        for index, start, end in range_intervals(value, block_chars=chars):
            rows.setdefault(index, []).append((start, end))
    return {index: merged_intervals(parts) for index, parts in rows.items()}


def _union(values: list[Any], chars: list[int]) -> list[Any]:
    output: list[Any] = []
    for index, parts in sorted(_intervals(values, chars).items()):
        for start, end in parts:
            if start == 0 and end == chars[index]:
                if output and isinstance(output[-1], list) and output[-1][1] == index:
                    output[-1][1] = index + 1
                else:
                    output.append([index, index + 1])
            else:
                output.append(_numeric_piece(index, start, end))
    return output


def _source_piece(
    value: dict[str, int], context: Any, resolver: SelectionResolver
) -> dict[str, Any]:
    index, start, end = value["block_index"], value["start_char"], value["end_char"]
    if not interval_is_covered(index, start, end, resolver.target) or not interval_is_covered(
        index, start, end, resolver.evidence
    ):
        raise ValueError("Routing piece is outside the frozen target evidence")
    fragments: list[tuple[int, str]] = []
    for block in context.evidence.get("blocks", []):
        if block.get("order") != index or not isinstance(block.get("text"), str):
            continue
        reference = block.get("reference") or {}
        offset = reference.get("start", 0)
        left, right = max(start, offset), min(end, offset + len(block["text"]))
        if left < right:
            fragments.append((left, block["text"][left - offset : right - offset]))
    cursor = start
    parts: list[str] = []
    for left, fragment in sorted(fragments):
        right = left + len(fragment)
        if right <= cursor:
            continue
        if left > cursor:
            break
        parts.append(fragment[cursor - left :])
        cursor = right
        if cursor >= end:
            break
    if cursor != end:
        raise ValueError("Routing piece lacks complete supplied text")
    return {
        "block_id": resolver.by_order[index]["id"],
        "start_char": start,
        "end_char": end,
        "complete_block": start == 0 and end == resolver.chars[index],
        "text": "".join(parts),
    }


def routing_request(
    candidate: dict[str, Any],
    refs: dict[str, Any],
    issues: tuple[ValidationIssue, ...],
    context: Any,
) -> dict[str, Any]:
    """Convert only locatable route diagnostics into a bounded model request."""
    if not issues or any(
        issue.code not in {"source_only_conflict", "coverage_gap", "coverage_pending"}
        for issue in issues
    ):
        raise ValueError("Other validation errors require field repair first")
    resolver = SelectionResolver.from_context(context)
    parsed_blocks = list(context.parsed.blocks)
    candidate_hash = content_id({"plan_protocol": "document-plan-v4", "candidate": candidate})
    source_groups: dict[int, list[ValidationIssue]] = {}
    gaps: list[ValidationIssue] = []
    for issue in issues:
        if issue.code == "source_only_conflict":
            match = _SOURCE_PATH.fullmatch(issue.path)
            if match is None or not issue.source_ranges:
                raise ValueError("Unlocatable source-only conflict")
            source_groups.setdefault(int(match[1]), []).append(issue)
        else:
            if not issue.source_ranges:
                raise ValueError("Unlocatable coverage gap")
            gaps.append(issue)
    items: list[dict[str, Any]] = []
    page_refs = refs["sections"]["page_changes"]
    for index, group in sorted(source_groups.items()):
        source_ref = refs["sections"]["source_only"][index]
        pieces = _union([value for issue in group for value in issue.source_ranges], resolver.chars)
        source_pieces: list[dict[str, Any]] = []
        for value in pieces:
            for block, start, end in range_intervals(value, block_chars=resolver.chars):
                location = _numeric_piece(block, start, end)
                source_pieces.append(
                    {
                        "piece_ref": "piece:"
                        + content_id((candidate_hash, source_ref, location))[:20],
                        "source": _source_piece(location, context, resolver),
                        "exact_range": location,
                    }
                )
        related = sorted(
            {
                page_refs[int(path.split("[", 1)[1].split("]", 1)[0])]
                for issue in group
                for path in issue.related_paths
                if path.startswith("page_changes[")
            }
        )
        items.append(
            {
                "decision_id": "repair:" + content_id((candidate_hash, source_ref, pieces))[:20],
                "kind": "source_only_conflict",
                "item_ref": source_ref,
                "diagnostics": [{"code": row.code, "path": row.path} for row in group],
                "source_pieces": source_pieces,
                "allowed_destinations": {"page_refs": related, "source_only": True},
            }
        )
    for issue in gaps:
        pieces = _union(issue.source_ranges, resolver.chars)
        source_pieces = []
        for value in pieces:
            for block, start, end in range_intervals(value, block_chars=resolver.chars):
                location = _numeric_piece(block, start, end)
                source_pieces.append(
                    {
                        "piece_ref": "piece:"
                        + content_id((candidate_hash, issue.path, location))[:20],
                        "source": _source_piece(location, context, resolver),
                        "exact_range": location,
                    }
                )
        heading_only = all(
            parsed_blocks[piece["exact_range"]["block_index"]].kind in {"heading", "title"}
            for piece in source_pieces
        )
        items.append(
            {
                "decision_id": "repair:" + content_id((candidate_hash, issue.path, pieces))[:20],
                "kind": "coverage_gap",
                "diagnostics": [{"code": issue.code, "path": issue.path}],
                "source_pieces": source_pieces,
                "allowed_destinations": {
                    "page_refs": page_refs,
                    "source_only": True,
                    "new_page": not heading_only,
                    "defer": True,
                    "entity_types": sorted(context.allowed_entity_types),
                },
            }
        )
    scope = {
        "protocol": PROTOCOL,
        "candidate_hash": candidate_hash,
        "target_receipt": context.target_receipt_identity,
        "parse_identity": context.parse_identity,
        "target_ranges": list(
            context.target_ranges or [[context.target_start, context.target_end]]
        ),
        "items": items,
    }
    return {
        "repair_protocol": PROTOCOL,
        "candidate_hash": candidate_hash,
        "scope_hash": content_id(scope),
        "candidate": candidate,
        "items": items,
        "response_contract": {
            "top_fields": ["repair_protocol", "candidate_hash", "decisions"],
            "decision_fields": {key: sorted(value) for key, value in _DECISION_FIELDS.items()},
            "choices_by_kind": _CHOICES_BY_KIND,
            "rules": (
                "For source_only_conflict, decision MUST be route_source_only: list the supplied "
                "piece_ref values retained in source_only; use [] when all conflict pieces "
                "remain in page bodies. For coverage_gap, choose attach_to_pages, source_only, "
                "new_page (only if authorized), or defer. Copy only decision_id, piece_ref and "
                "page_ref identities. Never return ranges. Give one final choice per item. "
                "Defer is pending, not acceptance. Do not include a type/json_object marker."
            ),
        },
    }


def _validate_one(item: dict[str, Any], decision: dict[str, Any], index: int) -> None:
    identity = decision["decision_id"]
    kind = decision.get("decision")
    expected = _DECISION_FIELDS.get(kind) if isinstance(kind, str) else None
    if expected is None or set(decision) != expected or kind not in _CHOICES_BY_KIND[item["kind"]]:
        raise RoutingDecisionError(
            "Invalid decision fields",
            code="invalid_selection_shape",
            operation_index=index,
            decision_ids=[identity],
        )
    allowed = item["allowed_destinations"]
    if kind == "route_source_only":
        selected = decision["retained_pieces"]
        provided = {piece["piece_ref"] for piece in item["source_pieces"]}
        if (
            not isinstance(selected, list)
            or any(not isinstance(ref, str) or ref not in provided for ref in selected)
            or len(selected) != len(set(selected))
        ):
            raise RoutingDecisionError(
                "Piece outside scope",
                code="repair_piece_out_of_scope",
                operation_index=index,
                decision_ids=[identity],
                allowed=sorted(provided),
            )
    elif kind == "attach_to_pages":
        selected = decision["page_refs"]
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(ref, str) or ref not in allowed["page_refs"] for ref in selected)
            or len(selected) != len(set(selected))
        ):
            raise RoutingDecisionError(
                "Page outside scope",
                code="repair_destination_out_of_scope",
                operation_index=index,
                decision_ids=[identity],
                allowed=allowed["page_refs"],
            )
    elif kind == "new_page":
        if (
            not allowed["new_page"]
            or not isinstance(decision["kind"], str)
            or decision["kind"] not in {"concept", "entity"}
            or (decision["kind"] == "entity" and decision["type"] not in allowed["entity_types"])
            or (decision["kind"] == "concept" and decision["type"] is not None)
        ):
            raise RoutingDecisionError(
                "New page outside scope",
                code="repair_destination_out_of_scope",
                operation_index=index,
                decision_ids=[identity],
            )
        if any(
            not isinstance(decision[field], str) or not decision[field].strip()
            for field in ("title", "purpose")
        ):
            raise RoutingDecisionError(
                "New page text is empty",
                code="invalid_selection_shape",
                operation_index=index,
                decision_ids=[identity],
            )
    if kind in {"route_source_only", "source_only", "defer"} and (
        not isinstance(decision["reason"], str) or not decision["reason"].strip()
    ):
        raise RoutingDecisionError(
            "Reason is empty",
            code="invalid_selection_shape",
            operation_index=index,
            decision_ids=[identity],
        )


def _validate_decisions(
    request: dict[str, Any], response: Any, preserved: dict[str, dict[str, Any]] | None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    required = {"repair_protocol", "candidate_hash", "decisions"}
    normalized: list[dict[str, Any]] = []
    if (
        isinstance(response, dict)
        and set(response) == required | {"type"}
        and response["type"] == "json_object"
    ):
        # Some JSON-mode responses echo the format marker. It carries no route
        # authority, and this exact removal is retained in the repair receipt.
        response = {key: value for key, value in response.items() if key != "type"}
        normalized.append({"field": "type", "action": "removed_json_format_marker"})
    if not isinstance(response, dict) or set(response) != required:
        raise RoutingDecisionError("Invalid routing response", code="invalid_selection_shape")
    if (
        response["repair_protocol"] != PROTOCOL
        or response["candidate_hash"] != request["candidate_hash"]
    ):
        raise RoutingDecisionError("Repair candidate mismatch", code="repair_candidate_mismatch")
    if not isinstance(response["decisions"], list):
        raise RoutingDecisionError("Decisions must be an array", code="invalid_selection_shape")
    items = {item["decision_id"]: item for item in request["items"]}
    valid = dict(preserved or {})
    first_error: RoutingDecisionError | None = None
    invalid_ids: set[str] = set()
    for index, decision in enumerate(response["decisions"]):
        identity = decision.get("decision_id") if isinstance(decision, dict) else None
        try:
            if not isinstance(identity, str) or identity not in items:
                raise RoutingDecisionError(
                    "Unknown decision identity",
                    code="unknown_repair_decision",
                    operation_index=index,
                    actual=identity,
                )
            if identity in invalid_ids:
                continue
            if identity in valid:
                if valid[identity] != decision:
                    raise RoutingDecisionError(
                        "Conflicting decisions",
                        code="repair_decision_conflict",
                        operation_index=index,
                        decision_ids=[identity],
                    )
                normalized.append(
                    {"decision_id": identity, "operation_index": index, "action": "deduplicated"}
                )
                continue
            _validate_one(items[identity], decision, index)
            valid[identity] = decision
        except RoutingDecisionError as exc:
            if isinstance(identity, str) and identity in items:
                invalid_ids.add(identity)
                valid.pop(identity, None)
            if first_error is None:
                first_error = exc
    if first_error is not None:
        first_error.valid_decisions = valid
        first_error.decision_ids = sorted(invalid_ids)
        raise first_error
    missing = sorted(set(items) - set(valid))
    if missing:
        raise RoutingDecisionError(
            "Missing decisions",
            code="repair_decision_missing",
            decision_ids=missing,
            valid_decisions=valid,
        )
    return valid, normalized


def apply_routing_decisions(
    baseline: dict[str, Any],
    refs: dict[str, Any],
    request: dict[str, Any],
    response: Any,
    context: Any,
    *,
    preserved: dict[str, dict[str, Any]] | None = None,
) -> RoutingResult:
    """Validate a complete decision set, then apply all interval edits atomically."""
    expected_hash = content_id({"plan_protocol": "document-plan-v4", "candidate": baseline})
    if request.get("candidate_hash") != expected_hash:
        raise RoutingDecisionError("Repair candidate mismatch", code="repair_candidate_mismatch")
    scope = {
        "protocol": PROTOCOL,
        "candidate_hash": expected_hash,
        "target_receipt": context.target_receipt_identity,
        "parse_identity": context.parse_identity,
        "target_ranges": list(
            context.target_ranges or [[context.target_start, context.target_end]]
        ),
        "items": request.get("items"),
    }
    if request.get("scope_hash") != content_id(scope):
        raise RoutingDecisionError("Repair scope mismatch", code="repair_scope_mismatch")
    decisions, normalized = _validate_decisions(request, response, preserved)
    if any(row["decision"] == "defer" for row in decisions.values()):
        raise RoutingDecisionError(
            "Routing deferred", code="repair_deferred", valid_decisions=decisions
        )
    resolver = SelectionResolver.from_context(context)
    candidate = deepcopy(baseline)
    page_refs = refs["sections"]["page_changes"]
    source_refs = refs["sections"]["source_only"]
    page_cuts: dict[str, list[Any]] = {}
    page_adds: dict[str, list[Any]] = {}
    source_updates: dict[str, list[Any]] = {}
    new_sources: list[dict[str, Any]] = []
    new_pages: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for item in request["items"]:
        decision = decisions[item["decision_id"]]
        pieces = {piece["piece_ref"]: piece["exact_range"] for piece in item["source_pieces"]}
        if item["kind"] == "source_only_conflict":
            retained = [pieces[ref] for ref in decision["retained_pieces"]]
            source_updates[item["item_ref"]] = list(pieces.values())
            for page_ref in item["allowed_destinations"]["page_refs"]:
                page_cuts.setdefault(page_ref, []).extend(retained)
        else:
            ranges = list(pieces.values())
            if decision["decision"] == "attach_to_pages":
                for page_ref in decision["page_refs"]:
                    page_adds.setdefault(page_ref, []).extend(ranges)
            elif decision["decision"] == "source_only":
                new_sources.append(
                    {
                        "ranges": resolver.encode_ranges(_union(ranges, resolver.chars)),
                        "reason": decision["reason"],
                    }
                )
            elif decision["decision"] == "new_page":
                local_key = "r-" + content_id((request["scope_hash"], item["decision_id"]))[:12]
                new_pages.append(
                    {
                        "local_key": local_key,
                        "kind": decision["kind"],
                        "type": decision["type"],
                        "title": decision["title"],
                        "purpose": decision["purpose"],
                        "subject_ranges": resolver.encode_ranges(_union(ranges, resolver.chars)),
                        "necessary_context": [],
                    }
                )
    for index, page_ref in enumerate(page_refs):
        cuts, adds = page_cuts.get(page_ref, []), page_adds.get(page_ref, [])
        if not cuts and not adds:
            continue
        original = resolver.decode_ranges(
            baseline["page_changes"][index]["subject_ranges"],
            f"page_changes[{index}].subject_ranges",
            target_only=True,
        )
        result = _union(
            subtract_exact_ranges(original, cuts, resolver.chars) + adds, resolver.chars
        )
        if not result:
            raise RoutingDecisionError(
                "Route would empty page body",
                code="route_would_empty_page",
                path=f"page_changes[{index}].subject_ranges",
            )
        candidate["page_changes"][index]["subject_ranges"] = resolver.encode_ranges(result)
        changes.append(
            {
                "item_ref": page_ref,
                "field": "subject_ranges",
                "old_ranges": baseline["page_changes"][index]["subject_ranges"],
                "new_ranges": candidate["page_changes"][index]["subject_ranges"],
            }
        )
    for index, source_ref in enumerate(source_refs):
        if source_ref not in source_updates:
            continue
        original = resolver.decode_ranges(
            baseline["source_only"][index]["ranges"],
            f"source_only[{index}].ranges",
            target_only=True,
        )
        all_conflicts = source_updates[source_ref]
        item = next(item for item in request["items"] if item.get("item_ref") == source_ref)
        decision = decisions[item["decision_id"]]
        by_ref = {piece["piece_ref"]: piece["exact_range"] for piece in item["source_pieces"]}
        kept = subtract_exact_ranges(original, all_conflicts, resolver.chars) + [
            by_ref[ref] for ref in decision["retained_pieces"]
        ]
        candidate["source_only"][index]["ranges"] = resolver.encode_ranges(
            _union(kept, resolver.chars)
        )
        changes.append(
            {
                "item_ref": source_ref,
                "field": "ranges",
                "old_ranges": baseline["source_only"][index]["ranges"],
                "new_ranges": candidate["source_only"][index]["ranges"],
            }
        )
    kept_source_refs = [
        source_refs[index] for index, row in enumerate(candidate["source_only"]) if row["ranges"]
    ]
    candidate["source_only"] = [
        row for row in candidate["source_only"] if row["ranges"]
    ] + new_sources
    candidate["page_changes"].extend(new_pages)
    updated_refs = deepcopy(refs)
    updated_refs["sections"]["source_only"] = kept_source_refs
    for _ in new_sources:
        updated_refs["serial"] += 1
        updated_refs["sections"]["source_only"].append(f"item:{updated_refs['serial']}")
    for _ in new_pages:
        updated_refs["serial"] += 1
        ref = f"item:{updated_refs['serial']}"
        updated_refs["sections"]["page_changes"].append(ref)
        updated_refs["contexts"][ref] = []
    return RoutingResult(candidate, updated_refs, tuple(normalized), tuple(changes))
