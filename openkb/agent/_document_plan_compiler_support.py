"""Private schema and identity mechanics for the document-plan-v3 compiler."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from json import JSONDecodeError
from typing import Any, Collection, Mapping, Sequence

from openkb.agent.document_plan_issues import (
    PlanValidationError,
    ValidationIssue,
    parse_plan_json,
)
from openkb.agent.model_json import json_text

_TOP_FIELDS = {"overview", "page_changes", "source_only", "unresolved", "resolutions"}
_OVERVIEW_FIELDS = {"text", "ranges", "limitations"}
_PAGE_REQUIRED = {
    "local_key",
    "kind",
    "title",
    "purpose",
    "subject_ranges",
    "necessary_context",
}
_PAGE_FIELDS = _PAGE_REQUIRED | {"type", "target_key", "target", "limitations"}
_CONTEXT_REQUIRED = {"relation", "ranges", "basis_ranges"}
CONTEXT_FIELDS = _CONTEXT_REQUIRED | {"rationale"}
_SOURCE_ONLY_FIELDS = {"ranges", "reason"}
_UNRESOLVED_FIELDS = {
    "location",
    "problem_type",
    "missing_target",
    "affected_pages",
    "reason",
}
_RESOLUTION_FIELDS = {"unresolved_key", "basis_ranges"}
_LIMITATION_FIELDS = {"ranges", "reason"}
_EXTERNAL_REFERENCE_FIELDS = {"location", "target_document", "target_section", "affected_pages"}
_PROGRAM_FIELDS = {
    "name",
    "key",
    "blocking",
    "status",
    "state",
    "quality",
    "basis",
    "basis_quote",
    "source_quote",
    "raw_quote",
}
PROBLEM_TYPES = {
    "missing_prerequisite",
    "unresolved_cross_reference",
    "missing_external_material",
    "parsing_limitation",
}


def allowed_problem_types(protocol: str) -> set[str]:
    return (
        PROBLEM_TYPES - {"missing_external_material"}
        if protocol == "document-plan-v5" else PROBLEM_TYPES
    )
RELATIONS = {"explicit_reference", "applicable_condition"}
_ASCII_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class PlanningContext:
    """Trusted, read-only inputs needed to compile one candidate."""

    source_version: str
    parse_identity: str
    target_receipt_identity: str
    evidence: Mapping[str, Any]
    parsed: Any
    target_start: int
    target_end: int
    total_blocks: int
    allowed_entity_types: Collection[str] = ()
    existing_targets: Collection[str] = ()
    carry_pages: Sequence[Mapping[str, Any]] = ()
    open_unresolved: Sequence[Mapping[str, Any]] = ()
    block_chars: Sequence[int] | None = None
    ignored_blocks: Collection[int] = frozenset()
    target_ranges: Sequence[Any] | None = None
    evidence_ranges: Sequence[Any] | None = None
    prior_overview_ranges: Sequence[Any] = ()
    known_page_keys: Collection[str] = frozenset()
    known_page_name_keys: Mapping[str, str] = field(default_factory=dict)
    reserved_targets: Collection[str] = frozenset()
    known_unresolved_keys: Collection[str] = frozenset()
    known_open_unresolved_keys: Collection[str] = frozenset()
    selection_protocol: str = "numeric-v3"
    navigation_hints: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class PlanCompileResult:
    """Pure compiler result; a blocking issue always suppresses ``delta``."""

    candidate: Any
    issues: tuple[ValidationIssue, ...]
    unassigned: tuple[Any, ...]
    delta: dict[str, Any] | None
    coverage_status: str = "unchecked"
    normalizations: tuple[dict[str, Any], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.delta is not None


def issue(
    code: str,
    path: str,
    expected: Any,
    actual: Any,
    *,
    item_ref: str,
    category: str = "shape",
    source_ranges: Sequence[Any] = (),
    allowed_operations: tuple[str, ...] = ("replace_field",),
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        path=path,
        category=category,
        expected=expected,
        actual=actual,
        source_ranges=list(source_ranges),
        allowed_action=(
            "reselect_evidence" if "replace_range" in allowed_operations else "field_repair"
        ),
        item_ref=item_ref,
        allowed_operations=allowed_operations,
    )


def _ranges_for_item(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        return []
    for field_name in ("subject_ranges", "location", "ranges", "basis_ranges"):
        ranges = value.get(field_name)
        if isinstance(ranges, list):
            return ranges
    return []


def _check_fields(
    value: Any,
    *,
    path: str,
    item_ref: str,
    required: set[str],
    allowed: set[str],
) -> list[ValidationIssue]:
    if not isinstance(value, dict):
        return [issue("invalid_item_shape", path, "object", value, item_ref=item_ref)]
    issues: list[ValidationIssue] = []
    source_ranges = _ranges_for_item(value)
    for name in sorted(set(value) - allowed):
        issues.append(
            issue(
                "program_owned_field" if name in _PROGRAM_FIELDS else "unknown_field",
                f"{path}.{name}" if path else name,
                "field omitted from document-plan-v3 model output",
                value[name],
                item_ref=item_ref,
                source_ranges=source_ranges,
                allowed_operations=("remove_field",),
            )
        )
    for name in sorted(required - set(value)):
        issues.append(
            issue(
                "missing_field",
                f"{path}.{name}" if path else name,
                "required document-plan-v3 field",
                None,
                item_ref=item_ref,
                source_ranges=source_ranges,
                allowed_operations=("replace_field",),
            )
        )
    return issues


def shape_issues(candidate: Any) -> list[ValidationIssue]:
    if not isinstance(candidate, dict):
        return [issue("invalid_candidate", "$", "JSON object", candidate, item_ref="$")]
    issues = _check_fields(
        candidate,
        path="",
        item_ref="$",
        required=_TOP_FIELDS,
        allowed=_TOP_FIELDS | {"external_references"},
    )
    issues.extend(
        _check_fields(
            candidate.get("overview"),
            path="overview",
            item_ref="overview",
            required=_OVERVIEW_FIELDS,
            allowed=_OVERVIEW_FIELDS,
        )
    )
    sections = (
        ("page_changes", _PAGE_REQUIRED, _PAGE_FIELDS),
        ("source_only", _SOURCE_ONLY_FIELDS, _SOURCE_ONLY_FIELDS),
        ("unresolved", _UNRESOLVED_FIELDS, _UNRESOLVED_FIELDS),
        ("resolutions", _RESOLUTION_FIELDS, _RESOLUTION_FIELDS),
    )
    for section, required, allowed in sections:
        rows = candidate.get(section)
        if not isinstance(rows, list):
            issues.append(issue("invalid_section", section, "list", rows, item_ref=section))
            continue
        for index, row in enumerate(rows):
            if section == "page_changes" and isinstance(row, dict):
                local_key = row.get("local_key")
                item_ref = f"page:{local_key}" if isinstance(local_key, str) else f"page:{index}"
            elif section == "resolutions" and isinstance(row, dict):
                key = row.get("unresolved_key")
                item_ref = f"resolution:{key}" if isinstance(key, str) else f"resolution:{index}"
            else:
                item_ref = f"{section.rstrip('s')}:{index}"
            issues.extend(
                _check_fields(
                    row,
                    path=f"{section}[{index}]",
                    item_ref=item_ref,
                    required=required,
                    allowed=allowed,
                )
            )
            if section != "page_changes" or not isinstance(row, dict):
                continue
            contexts = row.get("necessary_context", [])
            if not isinstance(contexts, list):
                issues.append(
                    issue(
                        "invalid_field_type",
                        f"page_changes[{index}].necessary_context",
                        "list",
                        contexts,
                        item_ref=item_ref,
                    )
                )
                continue
            for context_index, context in enumerate(contexts):
                context_ref = f"context:{row.get('local_key', index)}:{context_index}"
                issues.extend(
                    _check_fields(
                        context,
                        path=f"page_changes[{index}].necessary_context[{context_index}]",
                        item_ref=context_ref,
                        required=_CONTEXT_REQUIRED,
                        allowed=CONTEXT_FIELDS,
                    )
                )
            limitations = row.get("limitations", [])
            if not isinstance(limitations, list):
                issues.append(
                    issue(
                        "invalid_field_type",
                        f"page_changes[{index}].limitations",
                        "list",
                        limitations,
                        item_ref=item_ref,
                    )
                )
                continue
            for limitation_index, limitation in enumerate(limitations):
                issues.extend(
                    _check_fields(
                        limitation,
                        path=f"page_changes[{index}].limitations[{limitation_index}]",
                        item_ref=f"limitation:{row.get('local_key', index)}:{limitation_index}",
                        required=_LIMITATION_FIELDS,
                        allowed=_LIMITATION_FIELDS,
                    )
                )
    references = candidate.get("external_references", [])
    if not isinstance(references, list):
        issues.append(
            issue(
                "invalid_section",
                "external_references",
                "list",
                references,
                item_ref="external_references",
            )
        )
    else:
        for index, reference in enumerate(references):
            issues.extend(
                _check_fields(
                    reference,
                    path=f"external_references[{index}]",
                    item_ref=f"external_reference:{index}",
                    required=_EXTERNAL_REFERENCE_FIELDS,
                    allowed=_EXTERNAL_REFERENCE_FIELDS,
                )
            )
    return issues


def run_validation(issues: list[ValidationIssue], item_ref: str, operation: Any) -> bool:
    try:
        operation()
    except PlanValidationError as exc:
        issues.extend(
            replace(row, item_ref=item_ref) if row.item_ref == "$" else row for row in exc.issues
        )
        return False
    except (AssertionError, IndexError, KeyError, TypeError, ValueError) as exc:
        issues.append(
            issue(
                "invalid_candidate_value",
                item_ref,
                "valid document-plan-v3 value",
                str(exc),
                item_ref=item_ref,
            )
        )
        return False
    return True


def allocate_name(
    context: PlanningContext,
    local_key: str,
    kind: str,
    title: str,
    used_names: set[str],
) -> str | None:
    folder = "concepts" if kind == "concept" else "entities"
    normalized = unicodedata.normalize("NFKC", title).lower()
    stem = "-".join(_ASCII_WORD.findall(normalized))[:60].strip("-") or "page"
    identity = {
        "source_version": context.source_version,
        "parse_identity": context.parse_identity,
        "target_receipt_identity": context.target_receipt_identity,
        "local_key": local_key,
        "kind": kind,
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    for length in (12, 20, len(digest)):
        short_stem = stem[: 120 - length - 1].strip("-") or "page"
        name = f"{folder}/{short_stem}-{digest[:length]}"
        if (
            name not in used_names
            and name not in context.known_page_name_keys
            and name not in context.reserved_targets
            and name not in context.existing_targets
        ):
            return name
    return None


def next_key(prefix: str, occupied: set[str], known: Collection[str] = ()) -> str:
    index = 1
    while f"{prefix}{index}" in occupied or f"{prefix}{index}" in known:
        index += 1
    value = f"{prefix}{index}"
    occupied.add(value)
    return value


def block_chars(context: PlanningContext) -> list[int]:
    if context.block_chars is not None:
        return list(context.block_chars)
    return [getattr(block, "chars", -1) for block in getattr(context.parsed, "blocks", [])]


def parse_candidate(raw: Any) -> tuple[Any, list[ValidationIssue]]:
    if not isinstance(raw, (str, bytes)):
        return raw, []
    try:
        return parse_plan_json(json_text(raw)), []
    except PlanValidationError as exc:
        return None, list(exc.issues)
    except (JSONDecodeError, TypeError, UnicodeDecodeError, ValueError) as exc:
        return None, [
            ValidationIssue(
                code="json_syntax",
                path="$",
                category="syntax",
                expected="one valid JSON object",
                actual=str(exc),
                allowed_action="syntax_repair",
                item_ref="$",
                allowed_operations=("replace_candidate",),
                line=getattr(exc, "lineno", None),
                column=getattr(exc, "colno", None),
            )
        ]
