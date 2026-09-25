"""Compile one untrusted planning candidate against trusted source evidence."""

from __future__ import annotations

from typing import Any, Mapping

from openkb.agent._document_plan_compiler_support import (
    CONTEXT_FIELDS as _CONTEXT_FIELDS,
)
from openkb.agent._document_plan_compiler_support import (
    RELATIONS as _RELATIONS,
)
from openkb.agent._document_plan_compiler_support import (
    PlanCompileResult,
    PlanningContext,
    allowed_problem_types,
)
from openkb.agent._document_plan_compiler_support import (
    allocate_name as _allocate_name,
)
from openkb.agent._document_plan_compiler_support import (
    block_chars as _block_chars,
)
from openkb.agent._document_plan_compiler_support import (
    issue as _issue,
)
from openkb.agent._document_plan_compiler_support import (
    next_key as _next_key,
)
from openkb.agent._document_plan_compiler_support import (
    parse_candidate as _parse_candidate,
)
from openkb.agent._document_plan_compiler_support import (
    run_validation as _run,
)
from openkb.agent._document_plan_compiler_support import (
    shape_issues as _shape_issues,
)
from openkb.agent.document_plan_annotation_compiler import (
    attach_annotations,
)
from openkb.agent.document_plan_diagnostics import prepare_candidate
from openkb.agent.document_plan_finalize import finalize_candidate
from openkb.agent.document_plan_issues import (
    PlanValidationError,
)
from openkb.agent.document_plan_normalization import (
    overview_limitation_reasons,
    reclassify_literal_external_material,
)
from openkb.agent.document_range_validation import (
    frozen_evidence_intervals,
    require_nonempty_ranges,
    target_intervals,
    validate_evidence_ranges,
    validate_overview_ranges,
    validate_ranges,
    validate_target_ranges,
)

__all__ = ("PlanningContext", "PlanCompileResult", "compile_plan_candidate")


def compile_plan_candidate(
    raw: Any, context: PlanningContext, *, allow_coverage_gaps: bool = False,
    allow_empty_overview: bool = False,
) -> PlanCompileResult:
    """Compile one untrusted versioned plan into a derived durable delta."""

    candidate, issues = _parse_candidate(raw)
    if issues:
        return PlanCompileResult(candidate, tuple(issues), (), None)
    candidate, normalizations = overview_limitation_reasons(
        candidate, context.selection_protocol
    )
    candidate, reclassified = reclassify_literal_external_material(candidate, context)
    normalizations.extend(reclassified)
    issues = _shape_issues(candidate)
    chars = _block_chars(context)
    if len(chars) != context.total_blocks or any(
        type(value) is not int or value < 0 for value in chars
    ):
        issue = _issue(
            "invalid_planning_context",
            "$context.block_chars",
            f"{context.total_blocks} nonnegative block lengths",
            chars,
            item_ref="$context",
            category="identity",
            allowed_operations=("stop",),
        )
        return PlanCompileResult(candidate, (issue,), (), None)
    ignored = set(context.ignored_blocks)
    target_ranges_value = (
        list(context.target_ranges)
        if context.target_ranges is not None
        else [[context.target_start, context.target_end]]
    )
    try:
        target = target_intervals(
            context.target_start,
            context.target_end,
            target_ranges=target_ranges_value,
            block_chars=chars,
        )
        evidence = (
            frozen_evidence_intervals(list(context.evidence_ranges), context.total_blocks, chars)
            if context.evidence_ranges is not None
            else frozen_evidence_intervals(
                [
                    {
                        "block_index": block["order"],
                        "start_char": (
                            block.get("reference", {}).get("start", 0)
                            if isinstance(block.get("reference"), dict)
                            else 0
                        ),
                        "end_char": (
                            block.get("reference", {}).get("end", len(block.get("text", "")))
                            if isinstance(block.get("reference"), dict)
                            else len(block.get("text", ""))
                        ),
                    }
                    for block in context.evidence.get("blocks", [])
                ],
                context.total_blocks,
                chars,
            )
        )
    except (PlanValidationError, TypeError, ValueError) as exc:
        caught = (
            list(exc.issues)
            if isinstance(exc, PlanValidationError)
            else [
                _issue(
                    "invalid_planning_context",
                    "$context",
                    "valid exact target and evidence ranges",
                    str(exc),
                    item_ref="$context",
                    category="identity",
                    allowed_operations=("stop",),
                )
            ]
        )
        return PlanCompileResult(candidate, tuple(caught), (), None)

    raw_candidate = candidate
    prepared = prepare_candidate(candidate, context, target, evidence, chars, ignored)
    normalizations.extend(prepared.normalizations)
    candidate, coverage_issues, coverage_status = (
        prepared.canonical,
        prepared.issues,
        prepared.coverage_status,
    )
    if issues or any(
        issue.code
        in {"invalid_selection_shape", "unknown_block_reference", "selection_outside_evidence"}
        for issue in coverage_issues
    ):
        return PlanCompileResult(
            raw_candidate,
            tuple([*issues, *coverage_issues]),
            (),
            None,
            coverage_status,
        )
    assert isinstance(candidate, dict)

    overview = candidate["overview"]
    empty_overview = allow_empty_overview and overview == {
        "text": "", "ranges": [], "limitations": []
    }
    if not empty_overview and (
        not isinstance(overview.get("text"), str) or not overview["text"].strip()
    ):
        issues.append(
            _issue(
                "invalid_text",
                "overview.text",
                "nonempty string",
                overview.get("text"),
                item_ref="overview",
            )
        )
    limitations = overview.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(row, str) for row in limitations):
        issues.append(
            _issue(
                "invalid_field_type",
                "overview.limitations",
                "list of strings",
                limitations,
                item_ref="overview",
            )
        )
    overview_ranges = overview.get("ranges")
    if not empty_overview and _run(
        issues,
        "overview",
        lambda: validate_ranges(
            overview_ranges,
            context.total_blocks,
            "overview",
            block_chars=chars,
            ignored_blocks=ignored,
            field_path="overview.ranges",
        ),
    ):
        _run(
            issues,
            "overview",
            lambda: require_nonempty_ranges(overview_ranges, "overview.ranges", "Overview"),
        )
        _run(
            issues,
            "overview",
            lambda: validate_overview_ranges(
                overview_ranges,
                context.target_end,
                "overview",
                target_intervals=target,
                evidence_intervals=evidence,
                prior_ranges=list(context.prior_overview_ranges),
                total_blocks=context.total_blocks,
                block_chars=chars,
            ),
        )

    carry = {
        row.get("key"): row
        for row in context.carry_pages
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }
    occupied_page_keys: set[str] = {key for key in carry if isinstance(key, str)}
    used_names: set[str] = {
        name for row in carry.values() if isinstance(name := row.get("name"), str) and name
    }
    used_names.update(context.existing_targets)
    allocated_local: dict[str, str] = {}
    pages: list[dict[str, Any]] = []
    allowed_types = set(context.allowed_entity_types)

    for page_index, change in enumerate(candidate["page_changes"]):
        path = f"page_changes[{page_index}]"
        local_key = change.get("local_key")
        item_ref = f"page:{local_key}" if isinstance(local_key, str) else f"page:{page_index}"
        if not isinstance(local_key, str) or not local_key:
            issues.append(
                _issue(
                    "invalid_local_key",
                    f"{path}.local_key",
                    "nonempty string",
                    local_key,
                    item_ref=item_ref,
                )
            )
            continue
        if local_key in allocated_local:
            issues.append(
                _issue(
                    "duplicate_local_key",
                    f"{path}.local_key",
                    "unique local_key",
                    local_key,
                    item_ref=item_ref,
                )
            )
            continue
        kind = change.get("kind")
        if kind not in {"concept", "entity"}:
            issues.append(
                _issue(
                    "invalid_page_kind",
                    f"{path}.kind",
                    ["concept", "entity"],
                    kind,
                    item_ref=item_ref,
                )
            )
            continue
        type_ = change.get("type")
        if kind == "entity" and type_ not in allowed_types:
            issues.append(
                _issue(
                    "invalid_entity_type",
                    f"{path}.type",
                    sorted(allowed_types),
                    type_,
                    item_ref=item_ref,
                )
            )
        if kind == "concept" and type_ is not None:
            issues.append(
                _issue(
                    "invalid_entity_type",
                    f"{path}.type",
                    "null or absent",
                    type_,
                    item_ref=item_ref,
                )
            )
        title, purpose = change.get("title"), change.get("purpose")
        for field_name, value in (("title", title), ("purpose", purpose)):
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    _issue(
                        "invalid_text",
                        f"{path}.{field_name}",
                        "nonempty string",
                        value,
                        item_ref=item_ref,
                    )
                )

        target_key = "" if change.get("target_key") is None else change["target_key"]
        requested_target = "" if change.get("target") is None else change["target"]
        if not isinstance(target_key, str) or not isinstance(requested_target, str):
            for field_name in ("target_key", "target"):
                if change.get(field_name) is not None and not isinstance(change[field_name], str):
                    issues.append(
                        _issue(
                            "invalid_page_identity",
                            f"{path}.{field_name}",
                            "string or null",
                            change[field_name],
                            item_ref=item_ref,
                        )
                    )
            continue
        previous = carry.get(target_key) if target_key else None
        if target_key and previous is None:
            issues.append(
                _issue(
                    "projection_required"
                    if target_key in context.known_page_keys
                    else "unknown_page_reference",
                    f"{path}.target_key",
                    "visible registered page key",
                    target_key,
                    item_ref=item_ref,
                )
            )
            continue
        if previous is not None:
            name = previous.get("name")
            inherited_target = previous.get("target", "")
            if not isinstance(name, str) or not name:
                issues.append(
                    _issue(
                        "invalid_registered_page",
                        f"{path}.target_key",
                        "registered page with durable name",
                        target_key,
                        item_ref=item_ref,
                        category="identity",
                    )
                )
                continue
            for field_name, supplied in (("kind", kind), ("type", type_)):
                if supplied != previous.get(field_name):
                    issues.append(
                        _issue(
                            "page_identity_change",
                            f"{path}.{field_name}",
                            previous.get(field_name),
                            supplied,
                            item_ref=item_ref,
                            category="identity",
                        )
                    )
            if requested_target and requested_target != inherited_target:
                issues.append(
                    _issue(
                        "page_identity_change",
                        f"{path}.target",
                        inherited_target or None,
                        requested_target,
                        item_ref=item_ref,
                        category="identity",
                    )
                )
            assigned_key, durable_target = target_key, inherited_target
        elif requested_target:
            if requested_target not in context.existing_targets:
                code = (
                    "projection_required"
                    if requested_target in context.reserved_targets
                    else "unknown_page_target"
                )
                issues.append(
                    _issue(
                        code,
                        f"{path}.target",
                        "target in supplied page directory",
                        requested_target,
                        item_ref=item_ref,
                        category="reference",
                    )
                )
                continue
            name, durable_target = requested_target, requested_target
            assigned_key = _next_key("p", occupied_page_keys, context.known_page_keys)
        else:
            if not isinstance(title, str) or not title.strip():
                continue
            name = _allocate_name(context, local_key, kind, title, used_names)
            if name is None:
                issues.append(
                    _issue(
                        "page_name_collision",
                        path,
                        "unique deterministic page path",
                        local_key,
                        item_ref=item_ref,
                        category="identity",
                        allowed_operations=("stop",),
                    )
                )
                continue
            durable_target = ""
            assigned_key = _next_key("p", occupied_page_keys, context.known_page_keys)
        if name in used_names and previous is None and not requested_target:
            issues.append(
                _issue(
                    "page_name_collision",
                    path,
                    "unused page path",
                    name,
                    item_ref=item_ref,
                    category="identity",
                )
            )
            continue
        used_names.add(name)

        subject_ranges = change.get("subject_ranges")
        if _run(
            issues,
            item_ref,
            lambda: validate_ranges(
                subject_ranges,
                context.total_blocks,
                f"page {name} subject_ranges",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{path}.subject_ranges",
            ),
        ):
            _run(
                issues,
                item_ref,
                lambda: require_nonempty_ranges(
                    subject_ranges, f"{path}.subject_ranges", f"Page {name}"
                ),
            )
            _run(
                issues,
                item_ref,
                lambda: validate_target_ranges(
                    subject_ranges,
                    target,
                    f"page {name} subject_ranges",
                    chars,
                    field_path=f"{path}.subject_ranges",
                    item_ref=item_ref,
                ),
            )

        contexts: list[dict[str, Any]] = []
        for context_index, context_value in enumerate(change.get("necessary_context", [])):
            context_path = f"{path}.necessary_context[{context_index}]"
            context_ref = f"context:{local_key}:{context_index}"
            relation = context_value.get("relation")
            if relation not in _RELATIONS:
                issues.append(
                    _issue(
                        "invalid_context_relation",
                        f"{context_path}.relation",
                        sorted(_RELATIONS),
                        relation,
                        item_ref=context_ref,
                    )
                )
            if "rationale" in context_value and not isinstance(context_value["rationale"], str):
                issues.append(
                    _issue(
                        "invalid_field_type",
                        f"{context_path}.rationale",
                        "string",
                        context_value["rationale"],
                        item_ref=context_ref,
                    )
                )
            ranges = context_value.get("ranges")
            basis_ranges = context_value.get("basis_ranges")
            for field_name, values in (("ranges", ranges), ("basis_ranges", basis_ranges)):
                if _run(
                    issues,
                    context_ref,
                    lambda values=values, field_name=field_name: validate_ranges(
                        values,
                        context.total_blocks,
                        f"context {field_name} in {name}",
                        block_chars=chars,
                        ignored_blocks=ignored,
                        field_path=f"{context_path}.{field_name}",
                    ),
                ):
                    _run(
                        issues,
                        context_ref,
                        lambda values=values, field_name=field_name: require_nonempty_ranges(
                            values, f"{context_path}.{field_name}", f"Necessary context in {name}"
                        ),
                    )
                    _run(
                        issues,
                        context_ref,
                        lambda values=values, field_name=field_name: validate_evidence_ranges(
                            values,
                            evidence,
                            f"context {field_name} in {name}",
                            chars,
                            field_path=f"{context_path}.{field_name}",
                            item_ref=context_ref,
                        ),
                    )
            contexts.append(
                {key: context_value[key] for key in _CONTEXT_FIELDS if key in context_value}
            )

        allocated_local[local_key] = assigned_key
        pages.append(
            {
                "local_key": local_key,
                "target_key": assigned_key,
                "kind": kind,
                "type": type_,
                "name": name,
                "title": title,
                "purpose": purpose,
                "target": durable_target,
                "subject_ranges": subject_ranges,
                "necessary_context": contexts,
                "state": "ready",
                "quality": "planned",
            }
        )

    source_only: list[dict[str, Any]] = []
    for index, item in enumerate(candidate["source_only"]):
        path, item_ref = f"source_only[{index}]", f"source_only:{index}"
        reason, ranges = item.get("reason"), item.get("ranges")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(
                _issue(
                    "invalid_text",
                    f"{path}.reason",
                    "nonempty string",
                    reason,
                    item_ref=item_ref,
                    source_ranges=ranges if isinstance(ranges, list) else (),
                )
            )
        if _run(
            issues,
            item_ref,
            lambda: validate_ranges(
                ranges,
                context.total_blocks,
                "source_only",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{path}.ranges",
            ),
        ):
            _run(
                issues,
                item_ref,
                lambda: require_nonempty_ranges(ranges, f"{path}.ranges", "Source-only item"),
            )
            _run(
                issues,
                item_ref,
                lambda: validate_target_ranges(
                    ranges,
                    target,
                    "source_only",
                    chars,
                    field_path=f"{path}.ranges",
                    item_ref=item_ref,
                ),
            )
        source_only.append(
            {"ranges": ranges, "reason": reason.strip() if isinstance(reason, str) else reason}
        )

    unresolved: list[dict[str, Any]] = []
    occupied_unresolved: set[str] = {
        row["key"]
        for row in context.open_unresolved
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }
    for index, item in enumerate(candidate["unresolved"]):
        path, item_ref = f"unresolved[{index}]", f"unresolved:{index}"
        location = item.get("location")
        if _run(
            issues,
            item_ref,
            lambda: validate_ranges(
                location,
                context.total_blocks,
                "unresolved location",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{path}.location",
            ),
        ):
            _run(
                issues,
                item_ref,
                lambda: require_nonempty_ranges(location, f"{path}.location", "Unresolved item"),
            )
            _run(
                issues,
                item_ref,
                lambda: validate_target_ranges(
                    location,
                    target,
                    "unresolved location",
                    chars,
                    field_path=f"{path}.location",
                    item_ref=item_ref,
                ),
            )
        problem_type = item.get("problem_type")
        allowed_types = allowed_problem_types(context.selection_protocol)
        if problem_type not in allowed_types:
            issues.append(
                _issue(
                    "invalid_problem_type",
                    f"{path}.problem_type",
                    sorted(allowed_types),
                    problem_type,
                    item_ref=item_ref,
                    source_ranges=location if isinstance(location, list) else (),
                )
            )
        for field_name in ("missing_target", "reason"):
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    _issue(
                        "invalid_text",
                        f"{path}.{field_name}",
                        "nonempty string",
                        value,
                        item_ref=item_ref,
                        source_ranges=location if isinstance(location, list) else (),
                    )
                )
        affected_raw = item.get("affected_pages")
        affected: list[str] = []
        if (
            not isinstance(affected_raw, list)
            or not affected_raw
            or not all(isinstance(value, str) for value in affected_raw)
        ):
            issues.append(
                _issue(
                    "invalid_affected_pages",
                    f"{path}.affected_pages",
                    "nonempty list of local or durable page keys",
                    affected_raw,
                    item_ref=item_ref,
                    source_ranges=location if isinstance(location, list) else (),
                )
            )
        else:
            for affected_index, value in enumerate(affected_raw):
                key = allocated_local.get(value, value)
                if key not in occupied_page_keys:
                    issues.append(
                        _issue(
                            "projection_required"
                            if key in context.known_page_keys
                            else "unknown_page_reference",
                            f"{path}.affected_pages[{affected_index}]",
                            "local_key or supplied durable page key",
                            value,
                            item_ref=item_ref,
                            category="reference",
                            source_ranges=location if isinstance(location, list) else (),
                        )
                    )
                else:
                    affected.append(key)
        unresolved.append(
            {
                "key": _next_key("u", occupied_unresolved, context.known_unresolved_keys),
                "location": location,
                "problem_type": problem_type,
                "missing_target": item.get("missing_target"),
                "affected_pages": affected,
                "blocking": True,
                "reason": item.get("reason"),
                "status": "open",
            }
        )

    open_by_key = {
        row.get("key"): row
        for row in context.open_unresolved
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }
    resolutions: list[dict[str, Any]] = []
    resolved_keys: set[str] = set()
    for index, item in enumerate(candidate["resolutions"]):
        key = item.get("unresolved_key")
        path, item_ref = f"resolutions[{index}]", f"resolution:{key or index}"
        if not isinstance(key, str) or key in resolved_keys or key not in open_by_key:
            code = (
                "projection_required"
                if key in context.known_open_unresolved_keys
                else "unknown_unresolved_reference"
            )
            issues.append(
                _issue(
                    code,
                    f"{path}.unresolved_key",
                    "one supplied open unresolved key",
                    key,
                    item_ref=item_ref,
                    category="reference",
                )
            )
        else:
            resolved_keys.add(key)
        basis_ranges = item.get("basis_ranges")
        if _run(
            issues,
            item_ref,
            lambda: validate_ranges(
                basis_ranges,
                context.total_blocks,
                f"resolution for {key}",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{path}.basis_ranges",
            ),
        ):
            _run(
                issues,
                item_ref,
                lambda: require_nonempty_ranges(
                    basis_ranges, f"{path}.basis_ranges", f"Resolution {key}"
                ),
            )
            _run(
                issues,
                item_ref,
                lambda: validate_evidence_ranges(
                    basis_ranges,
                    evidence,
                    f"resolution for {key}",
                    chars,
                    field_path=f"{path}.basis_ranges",
                    item_ref=item_ref,
                ),
            )
        resolutions.append({"unresolved_key": key, "basis_ranges": basis_ranges})

    blocked = {key for item in unresolved for key in item["affected_pages"]}
    for page in pages:
        page["state"] = "blocked" if page["target_key"] in blocked else "ready"

    external_references, external_issues = attach_annotations(
        candidate, pages, context, allocated_local, occupied_page_keys, chars, ignored, evidence
    )
    issues.extend(external_issues)

    delta = {
        "overview": {
            "text": overview.get("text"),
            "ranges": overview_ranges,
            "limitations": limitations,
        },
        "page_changes": pages,
        "source_only": source_only,
        "unresolved": unresolved,
        "resolutions": resolutions,
        "external_references": external_references,
    }
    return finalize_candidate(
        raw_candidate, delta, issues, coverage_issues, coverage_status,
        context, normalizations, allow_coverage_gaps=allow_coverage_gaps,
    )
