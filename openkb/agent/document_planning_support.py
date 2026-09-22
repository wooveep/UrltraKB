"""Small pure helpers and durable contract records for document planning."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from openkb.agent.document_window_schedule import no_readable_body, valid_window_schedule
from openkb.config import compilation_model_options
from openkb.sources import content_id

__all__ = ("no_readable_body", "valid_window_schedule")


def planning_admission_limits(limits: Any) -> Any:
    """Admit a fresh W/S/T with the completion reservation actually dispatched.

    ``max_output_tokens`` is an endpoint capability, not a request reservation.
    In particular it may exceed a user's deliberately smaller shared context.
    A length-finished request re-enters normal admission after its budget has
    expanded; reserving the endpoint maximum before the first request would
    make an otherwise valid small document impossible to plan.
    """

    return limits


def fallback_read_evidence(source: Any, parsed: Any, start: int, end: int) -> dict[str, Any]:
    """Fixture fallback for source readers without PageIndex storage."""

    blocks = []
    for block in parsed.blocks[start:end]:
        if "attachment" in getattr(block, "location", {}):
            continue
        blocks.append(
            {
                "id": block.id,
                "order": block.order,
                "kind": block.kind,
                "text": getattr(block, "text", ""),
                "location": getattr(block, "location", {}),
                "assets": list(getattr(block, "assets", [])),
            }
        )
    return {
        "group_id": f"group_{start}_{end}",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "blocks": blocks,
    }


def read_target_evidence(
    kb_dir: Any,
    source: Any,
    parsed: Any,
    descriptor: dict[str, Any],
    target_ranges: list[dict[str, int]] | None,
) -> dict[str, Any]:
    """Read one frozen planning target, including exact sub-block targets."""

    if not target_ranges:
        from openkb.navigation_evidence import read_evidence_group

        return read_evidence_group(kb_dir, source, parsed, descriptor)

    from openkb.evidence import Evidence, ParseStore

    reader = ParseStore(kb_dir).reader(source, parsed)
    blocks = []
    for item in target_ranges:
        index, start, end = item["block_index"], item["start_char"], item["end_char"]
        block = parsed.blocks[index]
        if "attachment" in getattr(block, "location", {}):
            continue
        reference = Evidence(source.source_id, source.id, parsed.id, block.id, start, end)
        view = reader.read(reference, max_chars=reader.complete_bound(reference))
        if not view.text:
            raise ValueError("Planning target evidence is empty")
        blocks.append(
            {
                "id": block.id,
                "order": block.order,
                "kind": block.kind,
                "text": view.text,
                "location": view.location,
                "assets": list(block.assets),
                "reference": asdict(reference),
                **{
                    key: value
                    for key, value in (
                        ("context", view.context),
                        ("context_data", view.context_data),
                    )
                    if value
                },
            }
        )
    identity = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "ranges": target_ranges,
    }
    return {
        "group_id": content_id(identity),
        **identity,
        "document": source.name,
        "blocks": blocks,
    }


def fallback_target_evidence(
    source: Any, parsed: Any, target_ranges: list[dict[str, int]]
) -> dict[str, Any]:
    """Fixture counterpart to exact target reads for mocked planner tests."""

    blocks = []
    for item in target_ranges:
        block = parsed.blocks[item["block_index"]]
        if "attachment" in getattr(block, "location", {}):
            continue
        text = getattr(block, "text", "")[item["start_char"] : item["end_char"]]
        blocks.append(
            {
                "id": block.id,
                "order": block.order,
                "kind": block.kind,
                "text": text,
                "location": getattr(block, "location", {}),
                "assets": list(getattr(block, "assets", [])),
                "reference": {"start": item["start_char"], "end": item["end_char"]},
            }
        )
    return {
        "group_id": content_id(target_ranges),
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "blocks": blocks,
    }


def canonicalize_context_bases(
    decoded: dict[str, Any], evidence: dict[str, Any], parsed: Any
) -> None:
    """Bind context prose to the exact original slices in frozen evidence.

    Range authorization alone does not prove that a model-supplied ``basis``
    is a quotation of that range.  Preserve the source's literal text only
    after the response has quoted it faithfully. Resolution rows carry their
    exact ranges durably, but never fabricate a page-context relation.
    """

    supplied: dict[int, list[tuple[int, int, str]]] = {}
    for item in evidence.get("blocks", []):
        if not isinstance(item, dict):
            raise ValueError("Frozen evidence block is invalid")
        index, text = item.get("order"), item.get("text")
        if not isinstance(index, int) or not isinstance(text, str):
            raise ValueError("Frozen evidence block is invalid")
        reference = item.get("reference", {})
        start = reference.get("start", 0) if isinstance(reference, dict) else 0
        end = reference.get("end", start + len(text)) if isinstance(reference, dict) else len(text)
        if type(start) is not int or type(end) is not int or end - start != len(text):
            raise ValueError("Frozen evidence block has no exact text extent")
        supplied.setdefault(index, []).append((start, end, text))

    def literal(ranges: list[Any]) -> str:
        requested: list[tuple[int, int, int]] = []
        for value in ranges:
            if isinstance(value, dict):
                requested.append((value["block_index"], value["start_char"], value["end_char"]))
            else:
                start, end = value
                requested.extend(
                    (index, 0, parsed.blocks[index].chars) for index in range(start, end)
                )
        parts: list[str] = []
        for index, start, end in sorted(requested):
            for supplied_start, supplied_end, text in supplied.get(index, []):
                if supplied_start <= start < end <= supplied_end:
                    parts.append(text[start - supplied_start : end - supplied_start])
                    break
            else:
                raise ValueError("Context basis is outside frozen source evidence")
        result = "\n".join(parts)
        if not result.strip():
            raise ValueError("Context basis has no original source text")
        return result

    for change in decoded["page_changes"]:
        for context in change["necessary_context"]:
            basis = literal(context["basis_ranges"])
            if context["basis"].strip() != basis.strip():
                raise ValueError("Necessary context basis must quote frozen source evidence")
            context["basis"] = basis
    for resolution in decoded["resolutions"]:
        literal(resolution["basis_ranges"])


def exclude_attachment_ranges(parsed: Any, ranges: list[Any]) -> tuple[list[Any], bool]:
    """Return an equivalent range set which never authorizes attachment blocks.

    Navigation windows predate the parent-document attachment boundary and may
    still describe a contiguous block interval that crosses an embedded-file
    placeholder.  The frozen reader correctly omits that placeholder, so the
    wire target and the decoder must use the same projection.  Preserve the
    compact original representation unless an attachment actually occurs;
    when it does, use exact character ranges for each remaining readable
    block so a broad interval cannot silently re-authorize the attachment.
    """

    projected: list[Any] = []
    changed = False
    for value in ranges:
        if isinstance(value, dict):
            index = value.get("block_index")
            if not isinstance(index, int) or not 0 <= index < len(parsed.blocks):
                # Let the normal window/protocol validators report malformed
                # stored data with their established diagnostic.
                return ranges, False
            if "attachment" in getattr(parsed.blocks[index], "location", {}):
                changed = True
            else:
                projected.append(value)
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or type(value[0]) is not int
            or type(value[1]) is not int
            or not 0 <= value[0] < value[1] <= len(parsed.blocks)
        ):
            return ranges, False
        attachment_indexes = [
            index
            for index in range(value[0], value[1])
            if "attachment" in getattr(parsed.blocks[index], "location", {})
        ]
        if not attachment_indexes:
            projected.append(value)
            continue
        changed = True
        for index in range(value[0], value[1]):
            block = parsed.blocks[index]
            if "attachment" in getattr(block, "location", {}) or not getattr(block, "chars", 0):
                continue
            projected.append({"block_index": index, "start_char": 0, "end_char": block.chars})
    return (projected if changed else ranges), changed


def _quality_ranges(parsed: Any, row: dict[str, Any]) -> list[list[int]]:
    """Project one parser diagnostic onto exact readable source blocks.

    Parser diagnostics vary by format: some name a native location, some only
    name a physical page, and a document-wide parsing failure has neither.  A
    location that cannot be narrowed safely is therefore bound to every
    readable parent block rather than silently disappearing from the formal
    plan.  Attachment-only diagnostics remain storage concerns, never parent
    evidence or parent-page dependencies.
    """

    location = row.get("location")
    if isinstance(location, dict) and "attachment" in location:
        return []
    readable = [
        (index, block)
        for index, block in enumerate(getattr(parsed, "blocks", []))
        if getattr(block, "chars", 0) > 0 and "attachment" not in getattr(block, "location", {})
    ]
    if not readable:
        return []

    matches: list[tuple[int, Any]] = []
    if isinstance(location, dict) and location:
        matches = [
            (index, block)
            for index, block in readable
            if all(
                getattr(block, "location", {}).get(key) == value for key, value in location.items()
            )
        ]
    if not matches and type(row.get("page")) is int:
        matches = [
            (index, block)
            for index, block in readable
            if getattr(block, "location", {}).get("page") == row["page"]
        ]
    # An unlocated or no-longer-matchable parser failure is conservatively a
    # document-wide unknown, not permission to generate around it.  Store the
    # exact span compactly: a non-readable or attachment block naturally
    # splits it, so this never authorizes a gap between readable parents.
    ranges: list[list[int]] = []
    for index, _ in matches or readable:
        if ranges and index == ranges[-1][1]:
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    return ranges


def parser_omissions(parsed: Any) -> list[dict[str, Any]]:
    """Return range-bound parser omissions for the durable DocumentPlan ledger."""

    from openkb.source_coverage import parsing_gaps

    omissions, seen = [], set()
    for row in parsing_gaps(parsed):
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason:
            continue
        ranges = _quality_ranges(parsed, row)
        if not ranges:
            continue
        identity = {"reason": reason, "ranges": ranges}
        digest = content_id(identity)
        if digest in seen:
            continue
        seen.add(digest)
        omissions.append(
            {
                "key": "parser-" + digest[:16],
                "ranges": ranges,
                "reason": reason,
            }
        )
    return omissions


def parser_limitations(parsed: Any) -> list[str]:
    """Return all non-attachment parser caveats, including zero-body inputs."""

    from openkb.source_coverage import parsing_gaps

    return list(
        dict.fromkeys(
            row["reason"]
            for row in parsing_gaps(parsed)
            if isinstance(row.get("reason"), str) and row["reason"]
        )
    )


def source_conditions(parsed: Any) -> list[dict[str, Any]]:
    """Return exact non-attachment parsing limitations safe to disclose to planning."""

    return [
        {"kind": "parsing_limitation", "reason": item["reason"], "ranges": item["ranges"]}
        for item in parser_omissions(parsed)
    ]


def planning_contract(settings: dict[str, Any], limits: Any) -> dict[str, Any]:
    """Return the source-safe model/capacity contract that binds a plan."""

    return {
        "model": settings.get("model"),
        # Endpoints can contain deployment identifiers; retain an immutable
        # correlation digest rather than copying a potentially sensitive URL.
        "endpoint": content_id(settings.get("_model_endpoint")),
        "capabilities": {
            "context_tokens": limits.max_context_tokens or limits.context_tokens,
            "input_tokens": limits.max_input_tokens if not limits.shared_context else None,
            "max_output_tokens": limits.max_output_tokens or limits.output_tokens,
            "shared_context": limits.shared_context,
        },
        "options": compilation_model_options(settings, stage="planning"),
        "limits": {
            key: getattr(limits, key)
            for key in (
                "context_tokens",
                "input_tokens",
                "output_tokens",
                "max_context_tokens",
                "max_input_tokens",
                "max_output_tokens",
                "shared_context",
            )
        },
    }


def planning_identity(
    checkpoints: Any,
    *,
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    contract: dict[str, Any],
    schema: str,
    language: Any,
    entity_types: list[str],
    rules: str,
    windowing: str,
    implementation: dict[str, str],
) -> str:
    """Build the recovery identity before mutable catalogue projection begins."""

    return checkpoints.identity(
        "document-plan-v1",
        {
            "source_id": source.source_id,
            "version_id": source.id,
            "parse_id": parsed.id,
            "navigation": navigation.get("id") if navigation else None,
            "planning_contract": contract,
            "schema": content_id(schema),
            "language": language,
            "entity_types": entity_types,
            "rules": rules,
            "windowing": windowing,
            "planning_implementation": implementation,
        },
    )


def planning_metadata(
    *,
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    catalog_window: str,
    catalog_targets: set[str],
    catalog_entries: list[tuple[str, str]],
    schema: str,
    language: Any,
    rules: str,
    windowing: str,
    implementation: dict[str, str],
    recovery_key: str,
    contract: dict[str, Any],
    entity_types: list[str],
    catalog_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create replay-safe plan metadata without storing source bodies."""

    metadata = {
        "protocol": "document-plan-v1",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "navigation": navigation.get("id") if navigation else None,
        "schema_snapshot": content_id(schema),
        "language": language,
        "rules": rules,
        "windowing": windowing,
        "planning_implementation": implementation,
        "recovery_key": recovery_key,
        "entity_types": entity_types,
        "planning_contract": contract,
        "zero_readable_body": not getattr(parsed, "blocks", []) or no_readable_body(parsed),
        "parser_omissions": parser_omissions(parsed),
    }
    if catalog_manifest is not None:
        # The complete baseline lives in the private planning ledger.  Keeping
        # every target/digest in a pending recovery record would recreate the
        # O(N) in-memory catalogue that the ledger is meant to avoid.
        metadata.update(
            {
                "catalog_snapshot": catalog_manifest["snapshot"],
                "catalog_count": catalog_manifest["count"],
                "catalog_ledger": catalog_manifest["ledger"],
            }
        )
    else:
        metadata.update(
            {
                "catalog_snapshot": content_id(catalog_window),
                "catalog_targets": sorted(catalog_targets),
                # Keep only stable digests of exactly the per-target brief strings
                # that can enter a later bounded catalog projection.  This permits a
                # source's own newly published page to appear on resume without making
                # a changed pre-existing planning input look cache-equivalent.
                "catalog_entry_snapshot": {
                    target: content_id(brief) for target, brief in catalog_entries
                },
            }
        )
    return metadata


def final_document_plan(
    *,
    metadata: dict[str, Any],
    windows: list[dict[str, Any]],
    overview: Any,
    pages: list[Any],
    source_only: list[Any],
    unresolved: list[Any],
    resolutions: list[Any],
    plan_only: bool,
    parsed: Any,
    entity_types: list[str],
    existing_targets: set[str],
) -> Any:
    """Build and validate the completed formal plan from accumulated window state."""

    from openkb.agent.document_plan import (
        DocumentPlan,
        UnresolvedItem,
        derive_page_states,
        range_intervals,
        validate_plan,
    )
    from openkb.agent.document_window_receipts import window_receipt_id

    plan = DocumentPlan(
        metadata={
            **metadata,
            "status": "accepted",
            "completed_windows": len(windows),
            "accepted_window_ids": [window_receipt_id(item) for item in windows],
            "accepted_window_receipts": metadata["accepted_window_receipts"],
            "window_schedule": windows,
            "plan_only": plan_only,
        },
        overview=overview,
        pages=pages,
        source_only=source_only,
        unresolved=unresolved,
        resolutions=resolutions,
    )
    omissions = parser_omissions(parsed)
    plan.metadata["parser_omissions"] = omissions

    def overlaps(left: list[Any], right: list[Any]) -> bool:
        return any(
            left_index == right_index and max(left_start, right_start) < min(left_end, right_end)
            for left_range in left
            for left_index, left_start, left_end in range_intervals(
                left_range, parsed, "parser omission page range"
            )
            for right_range in right
            for right_index, right_start, right_end in range_intervals(
                right_range, parsed, "parser omission range"
            )
        )

    existing_keys = {item.key for item in plan.unresolved}
    for omission in omissions:
        affected = []
        for page in plan.pages:
            page_ranges = [*page.subject_ranges]
            for context in page.necessary_context:
                page_ranges.extend(context["ranges"])
                page_ranges.extend(context["basis_ranges"])
            if overlaps(page_ranges, omission["ranges"]):
                affected.append(page.key)
        if not affected:
            continue
        key = omission["key"]
        suffix = 1
        while key in existing_keys:
            suffix += 1
            key = omission["key"] + f"-{suffix}"
        existing_keys.add(key)
        plan.unresolved.append(
            UnresolvedItem(
                key=key,
                location=omission["ranges"],
                problem_type="parsing_limitation",
                missing_target="parser-limited source material",
                affected_pages=affected,
                blocking=True,
                reason="Parser could not verify required source material: " + omission["reason"],
            )
        )
    derive_page_states(plan.pages, plan.unresolved)
    # Parsing limitations and still-open external material are program facts,
    # not optional model prose. Preserve them when a response omitted wording.
    for reason in parser_limitations(parsed):
        limitation = "Parsing limitation: " + reason
        if limitation not in plan.overview.limitations:
            plan.overview.limitations.append(limitation)
    for item in plan.unresolved:
        if item.status == "open":
            limitation = "Unresolved material: " + item.missing_target
            if limitation not in plan.overview.limitations:
                plan.overview.limitations.append(limitation)
    validate_plan(plan, parsed, entity_types, existing_targets)
    return plan
