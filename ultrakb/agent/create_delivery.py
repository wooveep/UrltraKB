"""Finite repair inputs for creation; full diagnostics remain in failure receipts."""

from ultrakb.agent.page_response import page_repair_message

_DETAILS = {
    "page_excerpt_invalid": "Select supplied original excerpt IDs in visible top-level paragraphs.",
    "page_invalid_source_ranges": "Select only supplied figure source range ID strings.",
    "page_incomplete_coverage": "Acknowledge every required covered_ranges ID exactly once.",
    "page_truncated_response": "Return complete JSON and complete statements for this batch.",
    "page_invalid_response": "Return a nonempty page with string description, content and type.",
}


def create_repair_message(code, coverage):
    code = code if code in _DETAILS else "page_invalid_response"
    message = page_repair_message(code, _DETAILS[code])
    if coverage:
        message["content"] += " Retain covered_ranges exactly as requested for this batch."
    return message


def create_repairs(coverage):
    return [create_repair_message(code, coverage) for code in _DETAILS]
