"""DocumentPlan data structures, persistence contracts, and state derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from openkb.agent.document_plan_annotations import (
    ExternalReference,
    PageLimitation,
    PlanningOmission,
)


@dataclass(frozen=True)
class RangeRef:
    """Sub-block character extent within a single block."""

    block_index: int
    start_char: int
    end_char: int

    def to_dict(self) -> dict[str, int]:
        return {
            "block_index": self.block_index,
            "start_char": self.start_char,
            "end_char": self.end_char,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RangeRef:
        if set(data) != {"block_index", "start_char", "end_char"} or any(
            type(data[field]) is not int for field in data
        ):
            raise ValueError("Invalid character range reference")
        return cls(
            block_index=data["block_index"],
            start_char=data["start_char"],
            end_char=data["end_char"],
        )


BlockRange = list[int]  # [start_block, end_block]
RangeValue = BlockRange | dict[str, int] | RangeRef


def table_row_identity(location: Any) -> tuple[Any, ...] | None:
    """Return a native table row identity without splitting a row across requests."""

    while isinstance(location, dict) and isinstance(location.get("attachment"), dict):
        location = location["attachment"].get("position", {})
    if not isinstance(location, dict):
        return None
    row = location.get("row")
    if type(row) is not int and isinstance(location.get("cell_address"), str):
        match = re.search(r"(\d+)$", location["cell_address"])
        row = int(match[1]) if match else None
    if type(row) is not int or ("table" not in location and "cell_address" not in location):
        return None
    identity = tuple(
        (key, location[key])
        for key in ("kind", "table", "sheet", "sheet_index", "slide", "object_id", "page")
        if key in location
    )
    return (*identity, ("row", row))


def range_dict(value: RangeValue) -> BlockRange | dict[str, int]:
    """Return the durable JSON shape for one whole-block or character range."""

    if isinstance(value, RangeRef):
        return value.to_dict()
    if isinstance(value, dict):
        return RangeRef.from_dict(value).to_dict()
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError("Invalid range reference")


def range_dicts(values: list[RangeValue]) -> list[BlockRange | dict[str, int]]:
    return [range_dict(value) for value in values]


def _context_dict(context: Any) -> dict[str, Any]:
    """Normalize one durable necessary-context record from a recovery payload."""

    if not isinstance(context, dict):
        raise ValueError("Invalid necessary context")
    expected = {"relation", "basis", "ranges", "basis_ranges"}
    optional = {"basis_quote", "rationale"}
    if not expected <= set(context) or not set(context) <= expected | optional:
        raise ValueError("Invalid necessary context")
    relation = context.get("relation")
    basis = context.get("basis")
    ranges = context.get("ranges", [])
    basis_ranges = context.get("basis_ranges")
    if (
        not isinstance(relation, str)
        or not isinstance(basis, str)
        or not isinstance(ranges, list)
        or not isinstance(basis_ranges, list)
    ):
        raise ValueError("Invalid necessary context")
    if "basis_quote" in context and context["basis_quote"] != basis:
        raise ValueError("Invalid necessary context")
    if "rationale" in context and not isinstance(context["rationale"], str):
        raise ValueError("Invalid necessary context")
    return {
        "relation": relation,
        "basis": basis,
        "ranges": range_dicts(ranges),
        "basis_ranges": range_dicts(basis_ranges),
        **{key: context[key] for key in optional if key in context},
    }


def source_bound_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose source-checked quotes to writers, never model-only rationales."""

    fields = ("relation", "ranges", "basis_ranges", "basis", "basis_quote")
    return [{key: context[key] for key in fields if key in context} for context in contexts]


def is_character_range(value: Any) -> bool:
    return isinstance(value, (RangeRef, dict))


def range_intervals(
    value: RangeValue, parsed: Any, context_label: str
) -> list[tuple[int, int, int]]:
    """Expand a durable range to exact character intervals in parsed blocks.

    Whole-block ranges become one interval per block.  A ``RangeRef`` stays
    within exactly one block, so callers never accidentally turn a selected
    excerpt into evidence for the rest of that block.
    """

    blocks = getattr(parsed, "blocks", [])
    if isinstance(value, RangeRef):
        ref = value
        if not 0 <= ref.block_index < len(blocks):
            raise ValueError(f"Character range out of bounds in {context_label}")
        chars = getattr(blocks[ref.block_index], "chars", None)
        if (
            type(chars) is not int
            or ref.start_char < 0
            or ref.end_char <= ref.start_char
            or ref.end_char > chars
        ):
            raise ValueError(f"Character range out of bounds in {context_label}")
        return [(ref.block_index, ref.start_char, ref.end_char)]
    if isinstance(value, dict):
        ref = RangeRef.from_dict(value)
        if not 0 <= ref.block_index < len(blocks):
            raise ValueError(f"Character range out of bounds in {context_label}")
        chars = getattr(blocks[ref.block_index], "chars", None)
        if (
            type(chars) is not int
            or ref.start_char < 0
            or ref.end_char <= ref.start_char
            or ref.end_char > chars
        ):
            raise ValueError(f"Character range out of bounds in {context_label}")
        return [(ref.block_index, ref.start_char, ref.end_char)]

    _check_block_range(value, len(blocks), context_label)
    start, end = value
    intervals = []
    for index in range(start, end):
        chars = getattr(blocks[index], "chars", None)
        if type(chars) is not int or chars < 0:
            raise ValueError(f"Invalid parsed block size in {context_label}")
        if chars:
            intervals.append((index, 0, chars))
    return intervals


@dataclass
class PagePlan:
    key: str
    kind: str  # "concept" | "entity"
    name: str  # safe slug, e.g. "concepts/data-backup"
    title: str  # human neutral title
    purpose: str
    target: str = ""  # target catalog path if reusing existing wiki page
    type: str | None = None  # entity type when kind == "entity"
    subject_ranges: list[RangeValue] = field(default_factory=list)
    context_ranges: list[RangeValue] = field(default_factory=list)
    planning_notes: list[str] = field(default_factory=list)
    scope_resolution: str = "section"
    necessary_context: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[PageLimitation] = field(default_factory=list)
    state: str = "ready"  # "ready" | "blocked"
    quality: str = "planned"  # "planned" | "generated" | "verified" | "unverified" | "published"
    local_key: str | None = None
    review_receipt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "key": self.key,
            "kind": self.kind,
            "type": self.type,
            "name": self.name,
            "title": self.title,
            "purpose": self.purpose,
            "target": self.target,
            "subject_ranges": range_dicts(self.subject_ranges),
            "context_ranges": range_dicts(self.context_ranges),
            "planning_notes": list(self.planning_notes),
            "scope_resolution": self.scope_resolution,
            "necessary_context": [
                {
                    **context,
                    "ranges": range_dicts(context.get("ranges", [])),
                    "basis_ranges": range_dicts(context.get("basis_ranges", [])),
                }
                for context in self.necessary_context
            ],
            "state": self.state,
            "quality": self.quality,
            "local_key": self.local_key,
            "review_receipt": self.review_receipt,
        }
        if self.limitations:
            result["limitations"] = [item.to_dict() for item in self.limitations]
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PagePlan:
        if not isinstance(data, dict):
            raise ValueError("Invalid page plan")
        allowed = {
            "key",
            "kind",
            "type",
            "name",
            "title",
            "purpose",
            "target",
            "subject_ranges",
            "context_ranges",
            "planning_notes",
            "scope_resolution",
            "necessary_context",
            "state",
            "quality",
            "local_key",
            "review_receipt",
            "limitations",
        }
        if not set(data) <= allowed:
            raise ValueError("Invalid page plan")
        required = {"key", "kind", "name", "title", "purpose"}
        if not required <= set(data):
            raise ValueError("Invalid page plan")
        strings = ("key", "kind", "name", "title", "purpose", "target", "state", "quality")
        if any(name in data and not isinstance(data[name], str) for name in strings):
            raise ValueError("Invalid page plan")
        if not data["purpose"].strip():
            raise ValueError("Invalid page plan")
        if data.get("type") is not None and not isinstance(data.get("type"), str):
            raise ValueError("Invalid page plan")
        if data.get("local_key") is not None and not isinstance(data.get("local_key"), str):
            raise ValueError("Invalid page plan")
        subject_ranges = data.get("subject_ranges", [])
        context_ranges = data.get("context_ranges", [])
        planning_notes = data.get("planning_notes", [])
        contexts = data.get("necessary_context", [])
        limitations = data.get("limitations", [])
        if (
            not isinstance(subject_ranges, list)
            or not isinstance(context_ranges, list)
            or not isinstance(planning_notes, list)
            or not all(isinstance(note, str) for note in planning_notes)
            or data.get("scope_resolution", "section")
            not in {"section", "explicit_range", "target_fallback"}
            or not isinstance(contexts, list)
            or not isinstance(limitations, list)
        ):
            raise ValueError("Invalid page plan")
        normalized_contexts = [_context_dict(context) for context in contexts]
        receipt = data.get("review_receipt")
        if receipt is not None and not isinstance(receipt, dict):
            raise ValueError("Invalid page plan")
        return cls(
            key=data["key"],
            kind=data["kind"],
            type=data.get("type"),
            name=data["name"],
            title=data["title"],
            purpose=data.get("purpose", ""),
            target=data.get("target", ""),
            subject_ranges=[range_dict(r) for r in subject_ranges],
            context_ranges=[range_dict(r) for r in context_ranges],
            planning_notes=list(planning_notes),
            scope_resolution=data.get("scope_resolution", "section"),
            necessary_context=normalized_contexts,
            limitations=[PageLimitation.from_dict(row) for row in limitations],
            state=data.get("state", "ready"),
            quality=data.get("quality", "planned"),
            local_key=data.get("local_key"),
            review_receipt=receipt,
        )


@dataclass
class SourceOnlyItem:
    ranges: list[RangeValue] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranges": range_dicts(self.ranges),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceOnlyItem:
        if (
            not isinstance(data, dict)
            or set(data) != {"ranges", "reason"}
            or not isinstance(data.get("ranges", []), list)
            or not isinstance(data.get("reason", ""), str)
        ):
            raise ValueError("Invalid source-only item")
        return cls(
            ranges=[range_dict(r) for r in data.get("ranges", [])],
            reason=data.get("reason", ""),
        )


@dataclass
class UnresolvedItem:
    key: str
    location: list[RangeValue] = field(default_factory=list)
    problem_type: str = ""
    missing_target: str = ""
    affected_pages: list[str] = field(default_factory=list)
    blocking: bool = True
    reason: str = ""
    status: str = "open"  # "open" | "resolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "location": range_dicts(self.location),
            "problem_type": self.problem_type,
            "missing_target": self.missing_target,
            "affected_pages": list(self.affected_pages),
            "blocking": self.blocking,
            "reason": self.reason,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnresolvedItem:
        if not isinstance(data, dict) or "key" not in data:
            raise ValueError("Invalid unresolved item")
        if set(data) - {
            "key",
            "location",
            "problem_type",
            "missing_target",
            "affected_pages",
            "blocking",
            "reason",
            "status",
        }:
            raise ValueError("Invalid unresolved item")
        strings = ("key", "problem_type", "missing_target", "reason", "status")
        if any(name in data and not isinstance(data[name], str) for name in strings):
            raise ValueError("Invalid unresolved item")
        location = data.get("location", [])
        affected = data.get("affected_pages", [])
        blocking = data.get("blocking", True)
        if (
            not isinstance(location, list)
            or not isinstance(affected, list)
            or not all(isinstance(page, str) for page in affected)
            or type(blocking) is not bool
        ):
            raise ValueError("Invalid unresolved item")
        return cls(
            key=data["key"],
            location=[range_dict(r) for r in location],
            problem_type=data.get("problem_type", ""),
            missing_target=data.get("missing_target", ""),
            affected_pages=list(affected),
            blocking=blocking,
            reason=data.get("reason", ""),
            status=data.get("status", "open"),
        )


@dataclass
class ResolutionItem:
    unresolved_key: str
    basis_ranges: list[RangeValue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unresolved_key": self.unresolved_key,
            "basis_ranges": range_dicts(self.basis_ranges),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResolutionItem:
        if (
            not isinstance(data, dict)
            or set(data) != {"unresolved_key", "basis_ranges"}
            or not isinstance(data.get("unresolved_key"), str)
            or not isinstance(data.get("basis_ranges", []), list)
        ):
            raise ValueError("Invalid resolution item")
        return cls(
            unresolved_key=data["unresolved_key"],
            basis_ranges=[range_dict(r) for r in data.get("basis_ranges", [])],
        )


@dataclass
class OverviewPlan:
    text: str = ""
    ranges: list[RangeValue] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    status: str = "partial"  # "partial" | "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "ranges": range_dicts(self.ranges),
            "limitations": list(self.limitations),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverviewPlan:
        if not isinstance(data, dict):
            raise ValueError("Invalid overview plan")
        if set(data) - {"text", "ranges", "limitations", "status"}:
            raise ValueError("Invalid overview plan")
        ranges = data.get("ranges", [])
        limitations = data.get("limitations", [])
        if (
            not isinstance(data.get("text", ""), str)
            or not isinstance(ranges, list)
            or not isinstance(limitations, list)
            or not all(isinstance(item, str) for item in limitations)
            or not isinstance(data.get("status", "partial"), str)
        ):
            raise ValueError("Invalid overview plan")
        return cls(
            text=data.get("text", ""),
            ranges=[range_dict(r) for r in ranges],
            limitations=list(limitations),
            status=data.get("status", "partial"),
        )


@dataclass
class DocumentPlan:
    metadata: dict[str, Any] = field(default_factory=dict)
    overview: OverviewPlan = field(default_factory=OverviewPlan)
    pages: list[PagePlan] = field(default_factory=list)
    source_only: list[SourceOnlyItem] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    resolutions: list[ResolutionItem] = field(default_factory=list)
    external_references: list[ExternalReference] = field(default_factory=list)
    planning_omissions: list[PlanningOmission] = field(default_factory=list)


def to_dict(plan: DocumentPlan) -> dict[str, Any]:
    result = {
        "metadata": dict(plan.metadata),
        "overview": plan.overview.to_dict(),
        "pages": [p.to_dict() for p in plan.pages],
        "source_only": [s.to_dict() for s in plan.source_only],
        "unresolved": [u.to_dict() for u in plan.unresolved],
        "resolutions": [r.to_dict() for r in plan.resolutions],
    }
    if plan.metadata.get("protocol") in {"document-plan-v2", "document-plan-v3"}:
        result["external_references"] = [row.to_dict() for row in plan.external_references]
        result["planning_omissions"] = [row.to_dict() for row in plan.planning_omissions]
    return result


def from_dict(data: dict[str, Any]) -> DocumentPlan:
    fields = {"metadata", "overview", "pages", "source_only", "unresolved", "resolutions"}
    if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
        raise ValueError("Invalid DocumentPlan payload")
    protocol = data["metadata"].get("protocol")
    if protocol in {"document-plan-v2", "document-plan-v3"}:
        fields.update({"external_references", "planning_omissions"})
    if set(data) != fields:
        raise ValueError("Invalid DocumentPlan payload")
    containers: tuple[str, ...] = ("pages", "source_only", "unresolved", "resolutions")
    if protocol in {"document-plan-v2", "document-plan-v3"}:
        containers += ("external_references", "planning_omissions")
    if (
        not isinstance(data["metadata"], dict)
        or not isinstance(data["overview"], dict)
        or any(
            not isinstance(data[name], list)
            or any(not isinstance(item, dict) for item in data[name])
            for name in containers
        )
    ):
        raise ValueError("Invalid DocumentPlan payload")
    try:
        return DocumentPlan(
            metadata=dict(data["metadata"]),
            overview=OverviewPlan.from_dict(data["overview"]),
            pages=[PagePlan.from_dict(p) for p in data["pages"]],
            source_only=[SourceOnlyItem.from_dict(s) for s in data["source_only"]],
            unresolved=[UnresolvedItem.from_dict(u) for u in data["unresolved"]],
            resolutions=[ResolutionItem.from_dict(r) for r in data["resolutions"]],
            external_references=[
                ExternalReference.from_dict(row) for row in data.get("external_references", [])
            ],
            planning_omissions=[
                PlanningOmission.from_dict(row) for row in data.get("planning_omissions", [])
            ],
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid DocumentPlan payload") from exc


def _blocking_page_keys(unresolved: list[UnresolvedItem]) -> set[str]:
    """Return every page identifier named by an open blocking item."""

    blocked_keys: set[str] = set()
    for item in unresolved:
        if item.status == "open" and item.blocking:
            for affected in item.affected_pages:
                blocked_keys.add(affected)
    return blocked_keys


def _derived_page_state(page: PagePlan, blocked_keys: set[str]) -> str:
    """Return the only valid execution state for *page*."""

    is_blocked = (
        page.key in blocked_keys
        or page.name in blocked_keys
        or (page.target and page.target in blocked_keys)
    )
    return "blocked" if is_blocked else "ready"


def derive_page_states(pages: list[PagePlan], unresolved: list[UnresolvedItem]) -> list[PagePlan]:
    """Derive page execution state (ready vs blocked) based on open blocking unresolved items."""

    blocked_keys = _blocking_page_keys(unresolved)

    result = []
    for page in pages:
        page.state = _derived_page_state(page, blocked_keys)
        result.append(page)
    return result


def _check_block_range(r: Any, max_blocks: int, context_label: str) -> None:
    if (
        not isinstance(r, (list, tuple))
        or len(r) != 2
        or type(r[0]) is not int
        or type(r[1]) is not int
    ):
        raise ValueError(f"Invalid range format in {context_label}: {r}")
    start, end = r[0], r[1]
    if start < 0 or end <= start or end > max_blocks:
        raise ValueError(
            f"Range out of bounds in {context_label}: [{start}, {end}] (max {max_blocks})"
        )


def validate_plan(
    plan: DocumentPlan,
    parsed: Any,
    allowed_entity_types: list[str],
    existing_targets: set[str],
) -> bool:
    """Validate an accepted plan against the immutable parsed source."""
    from openkb.agent.document_plan_validation import validate_plan as validate

    return validate(plan, parsed, allowed_entity_types, existing_targets)


def check_coverage_gaps(
    parsed: Any,
    plan: DocumentPlan,
    *,
    required_ranges: list[RangeValue] | None = None,
) -> list[BlockRange | dict[str, int]]:
    """Return exact unaccounted source spans.

    ``overview`` is deliberately excluded: it explains the document but is
    not a destination for source material.  Open unresolved locations count
    as accounted-to-unresolved, while attachments retain their independent
    storage route.
    """
    from openkb.agent.document_range_validation import merged_intervals

    intervals: dict[int, list[tuple[int, int]]] = {}

    def add(values: list[RangeValue], label: str) -> None:
        for value in values:
            for index, start, end in range_intervals(value, parsed, label):
                intervals.setdefault(index, []).append((start, end))

    for page in plan.pages:
        add(page.subject_ranges, f"page {page.name} subject ranges")
        for context in page.necessary_context:
            add(context.get("ranges", []), f"page {page.name} necessary context")
            add(context.get("basis_ranges", []), f"page {page.name} necessary context basis")
    for item in plan.source_only:
        add(item.ranges, "source only")
    for unresolved in plan.unresolved:
        add(unresolved.location, f"unresolved {unresolved.key}")

    required: dict[int, list[tuple[int, int]]] = {}
    if required_ranges is not None:
        for value in required_ranges:
            for index, start, end in range_intervals(value, parsed, "required coverage"):
                required.setdefault(index, []).append((start, end))

    gaps: list[BlockRange | dict[str, int]] = []
    for index, block in enumerate(getattr(parsed, "blocks", [])):
        if "attachment" in getattr(block, "location", {}):
            continue
        chars = getattr(block, "chars", 0)
        if type(chars) is not int or not chars:
            continue
        if required_ranges is not None and index not in required:
            continue
        required_intervals = merged_intervals(required.get(index, [(0, chars)]))
        actual = merged_intervals(intervals.get(index, []))
        for required_start, required_end in required_intervals:
            cursor = required_start
            for start, end in actual:
                if end <= cursor:
                    continue
                if start >= required_end:
                    break
                if start > cursor:
                    gap_end = min(start, required_end)
                    gaps.append(
                        [index, index + 1]
                        if cursor == 0 and gap_end == chars
                        else RangeRef(index, cursor, gap_end).to_dict()
                    )
                cursor = max(cursor, min(end, required_end))
                if cursor >= required_end:
                    break
            if cursor < required_end:
                gaps.append(
                    [index, index + 1]
                    if cursor == 0 and required_end == chars
                    else RangeRef(index, cursor, required_end).to_dict()
                )
    return gaps
