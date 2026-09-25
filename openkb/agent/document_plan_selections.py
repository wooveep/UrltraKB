"""Compile request-bound block selections into exact numeric evidence ranges."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, cast

from openkb.agent.document_plan_issues import ValidationIssue
from openkb.agent.document_range_validation import (
    frozen_evidence_intervals,
    interval_is_covered,
    merged_intervals,
    target_intervals,
)

_RANGE_FIELDS = {"ranges", "subject_ranges", "basis_ranges", "location"}


@dataclass(frozen=True)
class SelectionError(ValueError):
    code: str
    path: str
    actual: Any

    def issue(self) -> ValidationIssue:
        return ValidationIssue(
            code=self.code,
            path=self.path,
            category="evidence",
            expected="one exact selection from supplied block identities",
            actual=self.actual,
            allowed_action="reselect_evidence",
            allowed_operations=("replace_field",),
        )


@dataclass(frozen=True)
class SelectionResult:
    canonical: Any
    partial: Any
    issues: tuple[ValidationIssue, ...]
    normalizations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SectionRanges:
    values: tuple[Any, ...]


class SelectionResolver:
    """The only mapper from this request's frozen block IDs to coordinates."""

    def __init__(
        self,
        blocks: list[Mapping[str, Any]],
        chars: list[int],
        target: dict[int, list[tuple[int, int]]],
        evidence: dict[int, list[tuple[int, int]]],
        navigation_hints: list[Mapping[str, Any]] | None = None,
        plan_protocol: str = "document-plan-v4",
    ) -> None:
        self.chars, self.target, self.evidence = chars, target, evidence
        self.plan_protocol = plan_protocol
        self.sections: dict[str, Mapping[str, Any]] = {}
        duplicates: set[str] = set()
        for hint in navigation_hints or []:
            key = hint.get("section_key")
            if not isinstance(key, str):
                continue
            if key in self.sections:
                duplicates.add(key)
            self.sections[key] = hint
        for key in duplicates:
            del self.sections[key]
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_order: dict[int, dict[str, Any]] = {}
        self.fragments: dict[str, list[tuple[int, int]]] = {}
        for block in blocks:
            identity, order, body = block.get("id"), block.get("order"), block.get("text")
            if not isinstance(identity, str) or type(order) is not int or not isinstance(body, str):
                continue
            if not 0 <= order < len(chars):
                continue
            reference = block.get("reference")
            start = reference.get("start", 0) if isinstance(reference, dict) else 0
            end = reference.get("end", len(body)) if isinstance(reference, dict) else len(body)
            if type(start) is not int or type(end) is not int or end - start != len(body):
                continue
            if start < 0 or end > chars[order] or start >= end:
                continue
            if identity in self.by_id and self.by_id[identity]["order"] != order:
                continue
            if order in self.by_order and self.by_order[order]["id"] != identity:
                continue
            row = {
                "id": identity,
                "order": order,
                "start": start,
                "end": end,
                "complete": False,
            }
            self.by_id.setdefault(identity, row)
            self.by_order.setdefault(order, row)
            self.fragments.setdefault(identity, []).append((start, end))
        for identity, spans in self.fragments.items():
            row = self.by_id[identity]
            merged = merged_intervals(spans)
            self.fragments[identity] = merged
            order = cast(int, row["order"])
            row["complete"] = interval_is_covered(order, 0, chars[order], {order: merged})

    @classmethod
    def from_context(cls, context: Any) -> SelectionResolver:
        chars = (
            list(context.block_chars)
            if context.block_chars is not None
            else [block.chars for block in context.parsed.blocks]
        )
        blocks = list(context.evidence.get("blocks", []))
        target = target_intervals(
            context.target_start,
            context.target_end,
            target_ranges=list(context.target_ranges)
            if context.target_ranges is not None
            else None,
            block_chars=chars,
        )
        evidence_ranges = (
            list(context.evidence_ranges)
            if context.evidence_ranges is not None
            else [
                {
                    "block_index": block["order"],
                    "start_char": (block.get("reference") or {}).get("start", 0),
                    "end_char": (block.get("reference") or {}).get("end", len(block["text"])),
                }
                for block in blocks
            ]
        )
        evidence = frozen_evidence_intervals(evidence_ranges, context.total_blocks, chars)
        return cls(
            blocks, chars, target, evidence, list(context.navigation_hints),
            context.selection_protocol,
        )

    def decode(self, selection: Any, path: str, *, target_only: bool) -> Any:
        if not isinstance(selection, dict):
            raise SelectionError("invalid_selection_shape", path, selection)
        scope = self.target if target_only else self.evidence
        if set(selection) == {"section_key"}:
            key = selection["section_key"]
            row = self.sections.get(key) if isinstance(key, str) else None
            if row is None:
                raise SelectionError("unknown_section_reference", path, selection)
            if row.get("visibility") != "complete":
                raise SelectionError("section_not_complete", path, selection)
            raw_ranges = row.get("ranges")
            if not isinstance(raw_ranges, list) or not raw_ranges:
                raise SelectionError("section_not_complete", path, selection)
            values = tuple(
                self.decode(value, path, target_only=target_only)
                for value in raw_ranges
            )
            if any(isinstance(value, SectionRanges) for value in values):
                raise SelectionError("invalid_selection_shape", path, selection)
            return SectionRanges(values)
        if set(selection) == {"from_block", "start_char", "end_char"}:
            selection = {
                "block": selection["from_block"],
                "start_char": selection["start_char"],
                "end_char": selection["end_char"],
            }
        if set(selection) == {"from_block", "through_block"}:
            first, last = selection["from_block"], selection["through_block"]
            if not isinstance(first, str) or not isinstance(last, str):
                raise SelectionError("invalid_selection_shape", path, selection)
            if first not in self.by_id or last not in self.by_id:
                raise SelectionError("unknown_block_reference", path, selection)
            start, end = self.by_id[first]["order"], self.by_id[last]["order"] + 1
            if start >= end:
                raise SelectionError("invalid_selection_shape", path, selection)
            for index in range(start, end):
                row = self.by_order.get(index)
                if (
                    row is None
                    or not row["complete"]
                    or not interval_is_covered(index, 0, self.chars[index], scope)
                ):
                    raise SelectionError("selection_outside_evidence", path, selection)
            return [start, end]
        if set(selection) == {"block", "start_char", "end_char"}:
            identity, start, end = (
                selection["block"],
                selection["start_char"],
                selection["end_char"],
            )
            if not isinstance(identity, str) or type(start) is not int or type(end) is not int:
                raise SelectionError("invalid_selection_shape", path, selection)
            row = self.by_id.get(identity)
            if row is None:
                raise SelectionError("unknown_block_reference", path, selection)
            index = row["order"]
            if (
                not any(left <= start and end <= right for left, right in self.fragments[identity])
                or start >= end
                or not interval_is_covered(index, start, end, scope)
            ):
                raise SelectionError("selection_outside_evidence", path, selection)
            return {"block_index": index, "start_char": start, "end_char": end}
        raise SelectionError("invalid_selection_shape", path, selection)

    def decode_ranges(self, values: Any, path: str, *, target_only: bool) -> list[Any]:
        if not isinstance(values, list):
            raise SelectionError("invalid_selection_shape", path, values)
        result: list[Any] = []
        for index, value in enumerate(values):
            decoded = self.decode(value, f"{path}[{index}]", target_only=target_only)
            result.extend(decoded.values if isinstance(decoded, SectionRanges) else [decoded])
        return result

    def encode_ranges(self, values: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for value in values:
            if isinstance(value, list):
                first, last = value
                result.append(
                    {
                        "from_block": self.by_order[first]["id"],
                        "through_block": self.by_order[last - 1]["id"],
                    }
                )
            else:
                result.append(
                    {
                        "block": self.by_order[value["block_index"]]["id"],
                        "start_char": value["start_char"],
                        "end_char": value["end_char"],
                    }
                )
        return result

    def resolve_candidate(self, candidate: Any) -> SelectionResult:
        canonical, partial = deepcopy(candidate), deepcopy(candidate)
        issues: list[ValidationIssue] = []
        normalizations: list[dict[str, Any]] = []

        def replace_ranges(
            raw: Any, target: Any, available: Any, path: str, field: str, *, target_only: bool
        ) -> None:
            if (
                not isinstance(raw, dict)
                or not isinstance(target, dict)
                or not isinstance(available, dict)
                or field not in raw
            ):
                return
            values = raw[field]
            singleton = isinstance(values, dict)
            if singleton:
                values = [values]
            if not isinstance(values, list):
                target[field] = None
                available[field] = []
                issues.append(
                    SelectionError("invalid_selection_shape", f"{path}.{field}", values).issue()
                )
                return
            accepted: list[Any] = []
            successful = 0
            for index, value in enumerate(values):
                try:
                    decoded = self.decode(
                        value, f"{path}.{field}[{index}]", target_only=target_only
                    )
                    successful += 1
                    accepted.extend(
                        decoded.values if isinstance(decoded, SectionRanges) else [decoded]
                    )
                except SelectionError as exc:
                    issues.append(exc.issue())
            target[field] = accepted if successful == len(values) else None
            available[field] = accepted
            if singleton and successful == 1:
                normalizations.append({
                    "code": "singleton_selection_list", "path": f"{path}.{field}"
                })

        if not isinstance(candidate, dict):
            return SelectionResult(canonical, partial, ())
        for section in ("overview",):
            replace_ranges(
                candidate.get(section),
                canonical.get(section),
                partial.get(section),
                section,
                "ranges",
                target_only=True,
            )
        for section, field in (
            ("page_changes", "subject_ranges"),
            ("source_only", "ranges"),
            ("unresolved", "location"),
            ("resolutions", "basis_ranges"),
            ("external_references", "location"),
        ):
            raw_rows = candidate.get(section)
            if not isinstance(raw_rows, list):
                continue
            for index, raw in enumerate(raw_rows):
                replace_ranges(
                    raw,
                    canonical[section][index],
                    partial[section][index],
                    f"{section}[{index}]",
                    field,
                    target_only=section in {"page_changes", "source_only", "unresolved"},
                )
                if section == "page_changes" and isinstance(raw, dict):
                    contexts = raw.get("necessary_context")
                    if not isinstance(contexts, list):
                        continue
                    for context_index, item in enumerate(contexts):
                        for context_field in ("ranges", "basis_ranges"):
                            replace_ranges(
                                item,
                                canonical[section][index]["necessary_context"][context_index],
                                partial[section][index]["necessary_context"][context_index],
                                f"{section}[{index}].necessary_context[{context_index}]",
                                context_field,
                                target_only=False,
                            )
                    limitations = raw.get("limitations", [])
                    if isinstance(limitations, list):
                        for limitation_index, item in enumerate(limitations):
                            replace_ranges(
                                item,
                                canonical[section][index]["limitations"][limitation_index],
                                partial[section][index]["limitations"][limitation_index],
                                f"{section}[{index}].limitations[{limitation_index}]",
                                "ranges",
                                target_only=False,
                            )
        return SelectionResult(canonical, partial, tuple(issues), tuple(normalizations))
