"""Parent summary inputs keep derived coverage separate from direct original text."""

from openkb.agent.document_range_validation import merged_intervals
from openkb.sources import content_id


def child_signature(children):
    return content_id(
        [
            {
                key: node.get(key)
                for key in (
                    "title",
                    "title_origin",
                    "start",
                    "end",
                    "summary",
                    "summary_origin",
                    "summary_details",
                )
            }
            for node in children
        ]
    )


def usable(node):
    details = node.get("summary_details", {})
    return bool(
        node.get("summary")
        and node.get("summary_origin") == "model"
        and details.get("basis") in {"original", "derived", "mixed"}
        and details.get("covered_ranges")
        and details.get("status") in {"complete", "partial"}
    )


def gaps(start, end, covered):
    cursor, result = start, []
    for left, right in merged_intervals(covered):
        if cursor < left:
            result.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < end:
        result.append([cursor, end])
    return result


def parent_input(node, children, pieces, direct_ranges):
    inputs, covered = [], list(direct_ranges)
    for child in sorted([*children, *pieces], key=lambda n: (n["start"], n["end"])):
        accepted = usable(child)
        spans = child.get("summary_details", {}).get("covered_ranges", []) if accepted else []
        inputs.append(
            {
                "title": child["title"],
                "range": [child["start"], child["end"]],
                "provenance": "derived_navigation_not_original_text",
                "summary": child["summary"] if accepted else None,
                "summary_details": child.get("summary_details"),
            }
        )
        covered.extend(spans)
    coverage = [list(span) for span in merged_intervals(covered)]
    return {
        "basis": "mixed" if direct_ranges else "derived",
        "inputs": inputs,
        "direct_ranges": direct_ranges,
        "covered_ranges": coverage,
        "missing_ranges": gaps(node["start"], node["end"], coverage),
        "input_signature": child_signature(children),
    }
