"""Source-bound annotations stored with an accepted document plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openkb.agent.document_plan import RangeValue


def source_quote(values: list[RangeValue], parsed: Any, context_label: str) -> str:
    """Read exact original text for validated ranges, preserving selected extents."""
    from openkb.agent.document_plan import range_intervals

    spans = [span for value in values for span in range_intervals(value, parsed, context_label)]
    parts: list[str] = []
    for index, start, end in spans:
        body = getattr(parsed.blocks[index], "text", None)
        if not isinstance(body, str) or len(body) < end:
            raise ValueError(f"Original text unavailable in {context_label}")
        parts.append(body[start:end])
    return "\n".join(parts)


def quote_from_evidence(values: list[Any], evidence: Any, block_chars: list[int]) -> str:
    """Extract selected original characters from the text actually sent to planning."""
    from openkb.agent.document_range_validation import range_intervals

    supplied: dict[int, list[tuple[int, int, str]]] = {}
    for row in evidence.get("blocks", []):
        if not isinstance(row, dict):
            continue
        index, body = row.get("order"), row.get("text")
        if type(index) is not int or not isinstance(body, str):
            continue
        reference = row.get("reference")
        start = reference.get("start", 0) if isinstance(reference, dict) else 0
        end = reference.get("end", start + len(body)) if isinstance(reference, dict) else len(body)
        if type(start) is int and type(end) is int and end - start == len(body):
            supplied.setdefault(index, []).append((start, end, body))
    parts: list[str] = []
    for value in values:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            for left, right, body in supplied.get(index, []):
                if left <= start < end <= right:
                    parts.append(body[start - left : end - left])
                    break
            else:
                raise ValueError("Selected original text is outside supplied evidence")
    result = "\n".join(parts)
    if not result.strip():
        raise ValueError("Selected original text is empty")
    return result


@dataclass
class PageLimitation:
    ranges: list[RangeValue]
    reason: str
    source_quote: str

    def to_dict(self) -> dict[str, Any]:
        from openkb.agent.document_plan import range_dicts

        return {
            "ranges": range_dicts(self.ranges),
            "reason": self.reason,
            "source_quote": self.source_quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PageLimitation:
        if (
            not isinstance(data, dict)
            or set(data) != {"ranges", "reason", "source_quote"}
            or not isinstance(data["ranges"], list)
            or not data["ranges"]
            or not isinstance(data["reason"], str)
            or not data["reason"].strip()
            or not isinstance(data["source_quote"], str)
            or not data["source_quote"]
        ):
            raise ValueError("Invalid page limitation")
        from openkb.agent.document_plan import range_dict

        return cls(
            [range_dict(row) for row in data["ranges"]], data["reason"], data["source_quote"]
        )


@dataclass
class ExternalReference:
    key: str
    location: list[RangeValue]
    raw_quote: str
    target_document: str | None = None
    target_section: str | None = None
    affected_pages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from openkb.agent.document_plan import range_dicts

        return {
            "key": self.key,
            "location": range_dicts(self.location),
            "raw_quote": self.raw_quote,
            "target_document": self.target_document,
            "target_section": self.target_section,
            "affected_pages": list(self.affected_pages),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalReference:
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "key",
                "location",
                "raw_quote",
                "target_document",
                "target_section",
                "affected_pages",
            }
            or not isinstance(data["key"], str)
            or not data["key"].startswith("xref:")
            or not isinstance(data["location"], list)
            or not data["location"]
            or not isinstance(data["raw_quote"], str)
            or not data["raw_quote"]
            or any(
                value is not None and not isinstance(value, str)
                for value in (data["target_document"], data["target_section"])
            )
            or not isinstance(data["affected_pages"], list)
            or any(not isinstance(page, str) for page in data["affected_pages"])
        ):
            raise ValueError("Invalid external reference")
        from openkb.agent.document_plan import range_dict

        return cls(
            data["key"],
            [range_dict(row) for row in data["location"]],
            data["raw_quote"],
            data["target_document"],
            data["target_section"],
            list(data["affected_pages"]),
        )


@dataclass
class PlanningOmission:
    key: str
    stage: str
    reason: str
    target_id: str
    ranges: list[RangeValue]
    affected_pages: list[str]
    component: str
    attempts: int
    diagnostic_ref: str | None
    outcome: str = "skipped"

    def to_dict(self) -> dict[str, Any]:
        from openkb.agent.document_plan import range_dicts

        return {
            "key": self.key,
            "stage": self.stage,
            "reason": self.reason,
            "target_id": self.target_id,
            "ranges": range_dicts(self.ranges),
            "affected_pages": list(self.affected_pages),
            "component": self.component,
            "attempts": self.attempts,
            "diagnostic_ref": self.diagnostic_ref,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanningOmission:
        fields = {
            "key",
            "stage",
            "reason",
            "target_id",
            "ranges",
            "affected_pages",
            "component",
            "attempts",
            "diagnostic_ref",
            "outcome",
        }
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or not isinstance(data["key"], str)
            or not data["key"].startswith("omission:")
            or any(
                not isinstance(data[name], str) or not data[name]
                for name in ("stage", "reason", "target_id", "component")
            )
            or not isinstance(data["ranges"], list)
            or not data["ranges"]
            or not isinstance(data["affected_pages"], list)
            or any(not isinstance(page, str) for page in data["affected_pages"])
            or type(data["attempts"]) is not int
            or data["attempts"] < 0
            or data["diagnostic_ref"] is not None
            and not isinstance(data["diagnostic_ref"], str)
            or data["outcome"] != "skipped"
        ):
            raise ValueError("Invalid planning omission")
        from openkb.agent.document_plan import range_dict

        return cls(
            data["key"],
            data["stage"],
            data["reason"],
            data["target_id"],
            [range_dict(row) for row in data["ranges"]],
            list(data["affected_pages"]),
            data["component"],
            data["attempts"],
            data["diagnostic_ref"],
            data["outcome"],
        )
