"""Small program-owned description of the original material actually read for a page."""

from copy import deepcopy

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
        if unresolved
        else "located"
        if declared
        else "unassessed",
        "read_sections": sections,
        "unresolved_hints": deepcopy(unresolved),
        "warnings": list(warnings),
    }


def validate_scope(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"protocol", "status", "read_sections", "unresolved_hints", "warnings"}
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
    return deepcopy(value)
