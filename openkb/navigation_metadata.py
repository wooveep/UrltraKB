"""Optional, versioned-by-index provenance for derived navigation work."""

import re


def structure_diagnostics(navigation, extra=(), *, ranges=None):
    return [
        {
            "range": [window["target_start"], window["target_end"]],
            "unlocated_contents": [
                row
                for row in window.get("unlocated", [])
                if ranges is None or any(row["start"] < b and a < row["end"] for a, b in ranges)
            ],
            "unaccepted_candidates": window.get("structure_issues"),
        }
        for window in [*navigation.get("windows", []), *extra]
        if (window.get("unlocated") or window.get("structure_issues"))
        and (
            ranges is None
            or any(window["target_start"] < b and a < window["target_end"] for a, b in ranges)
        )
    ]


def validate_metadata(node):
    origins = {"unknown", "native", "numbering", "model", "toc"}

    if "pdf_page_range" in node:
        pages = node["pdf_page_range"]
        if (
            not isinstance(pages, dict)
            or set(pages) != {"start", "end"}
            or any(type(pages[k]) is not int for k in pages)
            or not 1 <= pages["start"] <= pages["end"]
        ):
            raise ValueError("Invalid PDF physical-page range")

    def level(value):
        return value is None or type(value) is int and 1 <= value <= 9

    if "structure" in node:
        value = node["structure"]
        if (
            not isinstance(value, dict)
            or set(value) != {"level", "level_origin", "anchors"}
            or not level(value["level"])
            or not isinstance(value["level_origin"], str)
            or value["level_origin"] not in origins
            or (value["level"] is None) != (value["level_origin"] == "unknown")
            or not isinstance(value["anchors"], list)
            or not value["anchors"]
        ):
            raise ValueError("Invalid navigation structure provenance")
        for anchor in value["anchors"]:
            if (
                not isinstance(anchor, dict)
                or set(anchor) != {"block_id", "title", "level", "level_origin"}
                or not isinstance(anchor["block_id"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", anchor["block_id"])
                or not isinstance(anchor["title"], str)
                or not anchor["title"]
                or not level(anchor["level"])
                or not isinstance(anchor["level_origin"], str)
                or anchor["level_origin"] not in origins
                or (anchor["level"] is None) != (anchor["level_origin"] == "unknown")
            ):
                raise ValueError("Invalid navigation anchor provenance")
    if "summary_details" in node:
        value = node["summary_details"]
        if (
            not isinstance(value, dict)
            or set(value) - {"basis", "input_signature"} != {"status", "reason", "covered_ranges"}
            or "basis" in value
            and (
                not isinstance(value["basis"], str)
                or value["basis"] not in {"original", "derived", "mixed"}
            )
            or "input_signature" in value
            and (
                not isinstance(value["input_signature"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["input_signature"])
            )
            or not isinstance(value["status"], str)
            or value["status"]
            not in {
                "complete",
                "partial",
                "insufficient_evidence",
                "budget_exceeded",
                "invalid",
                "interrupted",
                "not_scheduled",
                "not_requested",
            }
            or value["reason"] is not None
            and not isinstance(value["reason"], str)
            or not isinstance(value["covered_ranges"], list)
        ):
            raise ValueError("Invalid navigation summary outcome")
        end = node["start"]
        complete = True
        for bounds in value["covered_ranges"]:
            if (
                not isinstance(bounds, list)
                or len(bounds) != 2
                or any(type(n) is not int for n in bounds)
                or not end <= bounds[0] < bounds[1] <= node["end"]
            ):
                raise ValueError("Invalid navigation summary coverage")
            complete &= bounds[0] == end
            end = bounds[1]
        if value["status"] == "complete" and not (
            value["covered_ranges"]
            and complete
            and end == node["end"]
            and node.get("summary")
            and node.get("summary_origin") == "model"
        ):
            raise ValueError("Incomplete navigation summary coverage")
        if value["status"] == "partial" and not (
            value["covered_ranges"]
            and node.get("summary")
            and node.get("summary_origin") == "model"
        ):
            raise ValueError("Missing partial navigation summary coverage")


def validate_pdf_ranges(nodes, source, parsed):
    """Page-level overlap is allowed only with matching saved physical coordinates."""
    pdf_nodes = [node for node in nodes if "pdf_page_range" in node]
    if not pdf_nodes:
        return
    if not source.name.lower().endswith(".pdf"):
        raise ValueError("PDF ranges require a PDF source")
    positions = [
        (block.location["page"], block.order)
        for block in parsed.blocks
        if type(block.location.get("page")) is int and "attachment" not in block.location
    ]
    for node in pdf_nodes:
        pages = node["pdf_page_range"]
        orders = [order for page, order in positions if pages["start"] <= page <= pages["end"]]
        if not orders or (node["start"], node["end"]) != (min(orders), max(orders) + 1):
            raise ValueError("PDF range does not match saved physical pages")


def hint_metadata(node):
    """Carry uncertainty to planning without duplicating source anchor transcriptions."""
    result = {}
    if "structure" in node:
        result["hierarchy"] = {key: node["structure"][key] for key in ("level", "level_origin")}
    if "summary_details" in node:
        result["summary_details"] = node["summary_details"]
    if "pdf_page_range" in node:
        result["pdf_page_range"] = node["pdf_page_range"]
    return result
