"""Small, source-free repair hints for rejected DocumentPlan responses."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from openkb.agent.document_protocol import _validate_page_name
from openkb.agent.document_range_validation import validate_ranges
from openkb.agent.evidence_wire import WireMessages


def _rows(raw: dict[str, Any], section: str) -> list[Any]:
    value = raw.get(section)
    return value if isinstance(value, list) else []


def _range_fields(raw: dict[str, Any]):
    overview = raw.get("overview")
    if isinstance(overview, dict) and "ranges" in overview:
        yield "overview.ranges", overview["ranges"]
    for page_index, page in enumerate(_rows(raw, "page_changes")):
        if not isinstance(page, dict):
            continue
        if "subject_ranges" in page:
            yield f"page_changes[{page_index}].subject_ranges", page["subject_ranges"]
        for context_index, context in enumerate(_rows(page, "necessary_context")):
            if not isinstance(context, dict):
                continue
            for field in ("ranges", "basis_ranges"):
                if field in context:
                    yield (
                        f"page_changes[{page_index}].necessary_context[{context_index}].{field}",
                        context[field],
                    )
    for section, field in (
        ("source_only", "ranges"),
        ("unresolved", "location"),
        ("resolutions", "basis_ranges"),
    ):
        for index, row in enumerate(_rows(raw, section)):
            if isinstance(row, dict) and field in row:
                yield f"{section}[{index}].{field}", row[field]


def _short(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:96]


def validation_feedback(
    raw: Any,
    error: BaseException,
    *,
    total_blocks: int,
    block_chars: list[int],
    ignored_blocks: set[int],
) -> dict[str, Any]:
    """Report representative invalid fields without copying the candidate or evidence."""

    found: list[dict[str, str]] = []
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if isinstance(raw, dict):
        for section, expected in (
            ("overview", dict),
            ("page_changes", list),
            ("source_only", list),
            ("unresolved", list),
            ("resolutions", list),
        ):
            if section not in raw:
                found.append(
                    {"code": "missing_section", "field": section, "value": expected.__name__}
                )
            elif not isinstance(raw[section], expected):
                found.append(
                    {"code": "invalid_section", "field": section, "value": expected.__name__}
                )
        for field, ranges in _range_fields(raw):
            values = ranges if isinstance(ranges, list) else [ranges]
            for index, value in enumerate(values):
                try:
                    validate_ranges(
                        [value],
                        total_blocks,
                        field,
                        block_chars=block_chars,
                        ignored_blocks=ignored_blocks,
                    )
                except ValueError:
                    found.append(
                        {
                            "code": "invalid_range",
                            "field": f"{field}[{index}]",
                            "value": _short(value),
                        }
                    )
        for index, page in enumerate(_rows(raw, "page_changes")):
            if not isinstance(page, dict) or not isinstance(page.get("name"), str):
                continue
            try:
                _validate_page_name(page["name"], page.get("kind", "concept"))
            except ValueError:
                found.append(
                    {
                        "code": "invalid_page_path",
                        "field": f"page_changes[{index}].name",
                        "value": _short(page["name"]),
                    }
                )
        for index, row in enumerate(_rows(raw, "unresolved")):
            if isinstance(row, dict) and row.get("blocking") is False:
                found.append(
                    {
                        "code": "nonblocking_unresolved",
                        "field": f"unresolved[{index}].blocking",
                        "value": "false",
                    }
                )
    counts = Counter(issue["code"] for issue in found)
    issues = [
        issue
        for code in (
            "invalid_range",
            "invalid_page_path",
            "nonblocking_unresolved",
            "missing_section",
            "invalid_section",
        )
        for issue in [item for item in found if item["code"] == code][
            : 5 if code == "missing_section" else 3
        ]
    ][:12]
    if not issues:
        issues = [{"code": "invalid_response", "field": "$", "value": ""}]
    return {
        "instruction": (
            "Return a complete corrected DocumentPlan JSON. Apply the contract to every field, "
            "not only the examples below. These diagnostics are not source evidence."
        ),
        "error_type": type(error).__name__,
        "total_blocks": total_blocks,
        "issue_counts": dict(counts),
        "issues": issues,
    }


def with_retry_feedback(messages: WireMessages, feedback: dict[str, Any]) -> WireMessages:
    """Append a dynamic repair suffix while retaining W, system, and identity mapping."""

    payload = json.loads(messages[-1]["content"])
    payload["retry_feedback"] = feedback
    rows = [dict(message) for message in messages]
    rows[-1]["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return WireMessages(rows, messages.identities)


def retry_messages(
    messages: WireMessages, raw: Any, error: BaseException, decode_kwargs: dict[str, Any]
) -> tuple[WireMessages, dict[str, Any]]:
    feedback = validation_feedback(
        raw,
        error,
        total_blocks=decode_kwargs["total_blocks"],
        block_chars=decode_kwargs["block_chars"],
        ignored_blocks=decode_kwargs["ignored_blocks"],
    )
    return with_retry_feedback(messages, feedback), feedback
