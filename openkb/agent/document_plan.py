"""DocumentPlan data structures, persistence contracts, and state derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


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
_PAGE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")


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
    if set(context) != expected:
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
    return {
        "relation": relation,
        "basis": basis,
        "ranges": range_dicts(ranges),
        "basis_ranges": range_dicts(basis_ranges),
    }


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
    necessary_context: list[dict[str, Any]] = field(default_factory=list)
    state: str = "ready"  # "ready" | "blocked"
    quality: str = "planned"  # "planned" | "generated" | "verified" | "unverified" | "published"
    local_key: str | None = None
    review_receipt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "type": self.type,
            "name": self.name,
            "title": self.title,
            "purpose": self.purpose,
            "target": self.target,
            "subject_ranges": range_dicts(self.subject_ranges),
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
            "necessary_context",
            "state",
            "quality",
            "local_key",
            "review_receipt",
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
        contexts = data.get("necessary_context", [])
        if not isinstance(subject_ranges, list) or not isinstance(contexts, list):
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
            necessary_context=normalized_contexts,
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


def to_dict(plan: DocumentPlan) -> dict[str, Any]:
    return {
        "metadata": dict(plan.metadata),
        "overview": plan.overview.to_dict(),
        "pages": [p.to_dict() for p in plan.pages],
        "source_only": [s.to_dict() for s in plan.source_only],
        "unresolved": [u.to_dict() for u in plan.unresolved],
        "resolutions": [r.to_dict() for r in plan.resolutions],
    }


def from_dict(data: dict[str, Any]) -> DocumentPlan:
    fields = {"metadata", "overview", "pages", "source_only", "unresolved", "resolutions"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("Invalid DocumentPlan payload")
    containers = ("pages", "source_only", "unresolved", "resolutions")
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


def _check_range(r: RangeValue, parsed: Any, context_label: str) -> None:
    # ``range_intervals`` validates both shape and exact character bounds.  It
    # is deliberately the one authority used by planning, generation and
    # coverage rather than allowing each layer to reinterpret partial blocks.
    for index, _, _ in range_intervals(r, parsed, context_label):
        if "attachment" in getattr(parsed.blocks[index], "location", {}):
            raise ValueError(f"Range in {context_label} references unread attachment content")


def validate_plan(
    plan: DocumentPlan,
    parsed: Any,
    allowed_entity_types: list[str],
    existing_targets: set[str],
) -> bool:
    """Validate all ranges, entity types, and targets against immutable parsed blocks."""
    if plan.metadata.get("protocol") != "document-plan-v1":
        raise ValueError("Invalid DocumentPlan protocol")
    if plan.overview.status not in {"partial", "complete"}:
        raise ValueError("Invalid overview status")
    zero_readable_body = plan.metadata.get("zero_readable_body") is True
    if not zero_readable_body and not plan.overview.text.strip():
        raise ValueError("Readable DocumentPlan needs a non-empty overview")
    if not zero_readable_body and not plan.overview.ranges:
        raise ValueError("Readable DocumentPlan overview needs exact evidence ranges")
    if zero_readable_body and plan.pages:
        raise ValueError("Zero-readable-body plan cannot contain pages")

    # Validate overview ranges
    for r in plan.overview.ranges:
        _check_range(r, parsed, "overview")

    # Validate pages
    page_keys = set()
    page_names = set()
    for page in plan.pages:
        if not isinstance(page.key, str) or not page.key or page.key in page_keys:
            raise ValueError("Page keys must be unique non-empty strings")
        page_keys.add(page.key)
        if not isinstance(page.name, str) or not page.name or page.name in page_names:
            raise ValueError("Page names must be unique non-empty strings")
        page_names.add(page.name)
        if any(
            not isinstance(value, str)
            for value in (page.title, page.purpose, page.target, page.state, page.quality)
        ):
            raise ValueError(f"Invalid page fields for {page.name}")
        if not page.purpose.strip():
            raise ValueError(f"Page {page.name} needs a non-empty purpose")
        if page.type is not None and not isinstance(page.type, str):
            raise ValueError(f"Invalid entity type for {page.name}")
        if page.local_key is not None and not isinstance(page.local_key, str):
            raise ValueError(f"Invalid local key for {page.name}")
        if page.review_receipt is not None and not isinstance(page.review_receipt, dict):
            raise ValueError(f"Invalid review receipt for {page.name}")
        if page.kind not in {"concept", "entity"}:
            raise ValueError(f"Invalid page kind: {page.kind}")
        _check_page_path(page.name, page.kind)
        if page.target and page.target not in existing_targets:
            raise ValueError(f"Unknown existing page target: {page.target}")
        if page.target and page.target != page.name:
            raise ValueError(f"Existing target must equal page name: {page.target}")
        if page.name in existing_targets and not page.target:
            raise ValueError(f"Existing page name requires its actual target: {page.name}")
        if page.state not in {"ready", "blocked"}:
            raise ValueError(f"Invalid page state: {page.state}")
        if page.quality not in {"planned", "generated", "verified", "unverified", "published"}:
            raise ValueError(f"Invalid page quality: {page.quality}")
        from openkb.agent.document_page_receipts import valid_critical_review_receipt

        if page.quality in {"verified", "published"} and not valid_critical_review_receipt(
            page.review_receipt
        ):
            raise ValueError(f"Invalid critical review receipt for {page.name}")
        if page.quality == "unverified" and not isinstance(page.review_receipt, dict):
            raise ValueError(f"Missing review receipt for {page.name}")
        if page.kind == "entity":
            if not page.type or page.type not in allowed_entity_types:
                raise ValueError(
                    f"Invalid entity type '{page.type}' for page '{page.name}', "
                    f"allowed: {allowed_entity_types}"
                )
        if not isinstance(page.subject_ranges, list) or not page.subject_ranges:
            raise ValueError(f"Page {page.name} needs exact subject_ranges")
        for r in page.subject_ranges:
            _check_range(r, parsed, f"page {page.name} subject_ranges")
        for ctx in page.necessary_context:
            if not isinstance(ctx, dict):
                raise ValueError(f"Invalid necessary_context in page {page.name}")
            if set(ctx) != {"relation", "basis", "ranges", "basis_ranges"}:
                raise ValueError(f"Invalid necessary_context in page {page.name}")
            rel = ctx.get("relation")
            if rel not in {"explicit_reference", "applicable_condition"}:
                raise ValueError(f"Invalid relation in necessary_context: {rel}")
            if not isinstance(ctx.get("basis", ""), str) or not ctx.get("basis", "").strip():
                raise ValueError(f"Necessary context in {page.name} needs an original-text basis")
            ranges = ctx.get("ranges", [])
            basis_ranges = ctx.get("basis_ranges", [])
            if not isinstance(ranges, list) or not ranges:
                raise ValueError(f"Necessary context in {page.name} needs exact ranges")
            if not isinstance(basis_ranges, list) or not basis_ranges:
                raise ValueError(f"Necessary context in {page.name} needs exact basis_ranges")
            for r in ranges:
                _check_range(r, parsed, f"page {page.name} necessary_context")
            for r in basis_ranges:
                _check_range(r, parsed, f"page {page.name} necessary_context basis")

    if any(page.quality == "published" for page in plan.pages) and not isinstance(
        plan.metadata.get("publication_receipt"), dict
    ):
        raise ValueError("Published pages need a completed publication receipt")

    # Validate source_only
    for item in plan.source_only:
        if not isinstance(item.reason, str) or not item.reason.strip():
            raise ValueError("source_only item must have a non-empty reason")
        if not isinstance(item.ranges, list):
            raise ValueError("source_only item ranges must be a list")
        for r in item.ranges:
            _check_range(r, parsed, "source_only")

    # Validate unresolved
    unresolved_keys = set()
    for u in plan.unresolved:
        if not isinstance(u.key, str) or not u.key or u.key in unresolved_keys:
            raise ValueError("Unresolved keys must be unique non-empty strings")
        unresolved_keys.add(u.key)
        if u.status not in {"open", "resolved"} or type(u.blocking) is not bool:
            raise ValueError(f"Invalid unresolved status for {u.key}")
        if (
            not all(
                isinstance(value, str) and value.strip()
                for value in (u.problem_type, u.missing_target, u.reason)
            )
            or not isinstance(u.affected_pages, list)
            or any(not isinstance(page, str) for page in u.affected_pages)
        ):
            raise ValueError(f"Unresolved item {u.key} is missing required fields")
        if u.problem_type not in {
            "missing_prerequisite",
            "unresolved_cross_reference",
            "missing_external_material",
            "parsing_limitation",
        }:
            raise ValueError(f"Unresolved item {u.key} has an invalid problem type")
        if not u.affected_pages or any(page not in page_keys for page in u.affected_pages):
            raise ValueError(f"Unresolved item {u.key} has invalid affected pages")
        if not u.location:
            raise ValueError(f"Unresolved item {u.key} needs an exact location")
        for r in u.location:
            _check_range(r, parsed, f"unresolved {u.key}")

    # Validate resolutions
    resolved = set()
    for res in plan.resolutions:
        if (
            not isinstance(res.unresolved_key, str)
            or res.unresolved_key not in unresolved_keys
            or res.unresolved_key in resolved
            or not isinstance(res.basis_ranges, list)
            or not res.basis_ranges
        ):
            raise ValueError("Resolution must refer to one unique unresolved item")
        resolved.add(res.unresolved_key)
        for r in res.basis_ranges:
            _check_range(r, parsed, f"resolution for {res.unresolved_key}")
    if resolved != {item.key for item in plan.unresolved if item.status == "resolved"}:
        raise ValueError("Resolved unresolved items need one exact resolution receipt")

    blocked_keys = _blocking_page_keys(plan.unresolved)
    for page in plan.pages:
        expected_state = _derived_page_state(page, blocked_keys)
        if page.state != expected_state:
            raise ValueError(f"Page {page.name} state must equal its derived state")

    return True


def _check_page_path(name: str, kind: str) -> None:
    path = PurePosixPath(name)
    folder = "concepts" if kind == "concept" else "entities"
    if (
        path.is_absolute()
        or path.parts[:1] != (folder,)
        or len(path.parts) != 2
        or not _PAGE_SEGMENT.fullmatch(path.name)
    ):
        raise ValueError(f"Invalid {kind} page path: {name}")


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
        required_intervals = required.get(index, [(0, chars)])
        actual = sorted(intervals.get(index, []))
        for required_start, required_end in required_intervals:
            cursor = required_start
            for start, end in actual:
                if end <= cursor:
                    continue
                if start > cursor:
                    break
                cursor = max(cursor, end)
                if cursor >= required_end:
                    break
            if cursor < required_end:
                gaps.append(
                    [index, index + 1]
                    if cursor == 0 and required_end == chars
                    else RangeRef(index, cursor, required_end).to_dict()
                )
    return gaps
