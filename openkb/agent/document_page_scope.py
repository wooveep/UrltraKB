"""Small program-owned description of the original material actually read for a page."""

from copy import deepcopy

from openkb.agent.document_range_validation import merged_intervals

PROTOCOL = "page-evidence-scope-v1"


def evidence_scope(parsed, navigation, occurrences, unresolved, *, declared, warnings=()):
    by_id = {block.id: i for i, block in enumerate(parsed.blocks)}
    intervals = {}
    for occurrence in occurrences:
        reference = occurrence["reference"]
        index = by_id[reference["block_id"]]
        intervals.setdefault(index, []).append((reference["start"], reference["end"]))

    def covers(index):
        reached = 0
        for left, right in sorted(intervals.get(index, [])):
            if left > reached:
                return False
            reached = max(reached, right)
        return reached >= parsed.blocks[index].chars

    sections = []
    for row in navigation:
        start, end = row["original_range"]
        readable = [
            i
            for i in range(start, end)
            if "attachment" not in parsed.blocks[i].location and parsed.blocks[i].chars
        ]
        if not any(i in intervals for i in readable):
            continue
        sections.append(
            {
                "section_key": row["section_key"],
                "heading_path": row["heading_path"],
                "extent": "full" if all(covers(i) for i in readable) else "partial",
            }
        )
    body = any(any(route["route"] == "page_body" for route in row["routes"]) for row in occurrences)
    return {
        "protocol": PROTOCOL,
        "status": "unavailable"
        if not body
        else "partial"
        if any(row["role"] != "related" for row in unresolved)
        else "located"
        if declared
        else "unassessed",
        "read_sections": sections,
        "read_ranges": [
            {"block_index": index, "start_char": left, "end_char": right}
            for index, spans in sorted(intervals.items())
            for left, right in merged_intervals(spans)
        ],
        "unresolved_hints": deepcopy(unresolved),
        "warnings": list(warnings),
    }


def validate_scope(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) - {"read_ranges"}
        != {"protocol", "status", "read_sections", "unresolved_hints", "warnings"}
        or value["protocol"] != PROTOCOL
        or value["status"] not in {"unassessed", "located", "partial", "unavailable"}
        or any(
            not isinstance(value[key], list)
            for key in ("read_sections", "unresolved_hints", "warnings")
        )
    ):
        raise ValueError("Invalid page evidence scope")
    for row in value["read_sections"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"section_key", "heading_path", "extent"}
            or not isinstance(row["section_key"], str)
            or not isinstance(row["heading_path"], list)
            or any(not isinstance(part, str) for part in row["heading_path"])
            or row["extent"] not in {"full", "partial"}
        ):
            raise ValueError("Invalid page read section")
    for row in value["unresolved_hints"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"role", "value", "reason"}
            or row["role"] not in {"subject", "context", "related"}
            or not isinstance(row["reason"], str)
        ):
            raise ValueError("Invalid unresolved page hint")
    if any(not isinstance(note, str) for note in value["warnings"]):
        raise ValueError("Invalid page scope warnings")
    if "read_ranges" in value:
        if not isinstance(value["read_ranges"], list):
            raise ValueError("Invalid page read ranges")
        for span in value["read_ranges"]:
            if (
                not isinstance(span, dict)
                or set(span) != {"block_index", "start_char", "end_char"}
                or any(type(n) is not int for n in span.values())
                or span["block_index"] < 0
                or not 0 <= span["start_char"] < span["end_char"]
            ):
                raise ValueError("Invalid page read range")
    return deepcopy(value)
