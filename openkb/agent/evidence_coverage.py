"""Exact model-to-source coverage validation with content-free failure diagnostics."""

from collections import Counter

from openkb.agent.evidence_retry import ResponseIncomplete


def require_unit_coverage(outputs, expected):
    rows = outputs if isinstance(outputs, list) else []
    counts = Counter(
        row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    )
    details = {
        "expected": len(expected),
        "received": len(rows),
        "missing": len(set(expected) - counts.keys()),
        "unexpected": len(counts.keys() - set(expected)),
        "duplicates": sum(count - 1 for count in counts.values()),
        "invalid_rows": len(rows) - sum(counts.values()),
    }
    if (
        not isinstance(outputs, list)
        or len(rows) != len(expected)
        or any(details[key] for key in ("missing", "unexpected", "duplicates", "invalid_rows"))
    ):
        raise ResponseIncomplete("section_coverage_incomplete", "facts", **details)
