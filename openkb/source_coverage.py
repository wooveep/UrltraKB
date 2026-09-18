"""Account for original ranges separately from a successful publication transaction."""

import json

from openkb.sources import valid_id


def validate_coverage(value, source_id=None, version_id=None, parse_id=None, *, parsed=None):
    if not isinstance(value, dict):
        raise ValueError("Invalid source coverage")
    if not value:
        return value  # Historical coverage is unknown, never inferred as complete.
    if (
        set(value)
        != {"source_id", "version_id", "parse_id", "status", "ranges", "assets", "issues"}
        or value["status"] not in {"pending", "partial", "complete"}
        or not isinstance(value["ranges"], list)
        or not isinstance(value["assets"], list)
        or not isinstance(value["issues"], list)
    ):
        raise ValueError("Invalid source coverage manifest")
    for field, expected in (
        ("source_id", source_id),
        ("version_id", version_id),
        ("parse_id", parse_id),
    ):
        valid_id(value[field], source=field == "source_id")
        if expected is not None and value[field] != expected:
            raise ValueError("Source coverage binding mismatch")
    ends = {}
    for row in value["ranges"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"block_id", "start", "end", "kind", "location", "status", "reason"}
            or row["status"] not in {"verified", "referenced", "no_facts", "pending"}
            or not isinstance(row["kind"], str)
            or not isinstance(row["location"], dict)
            or not isinstance(row["reason"], str)
            or not row["reason"]
            or type(row["start"]) is not int
            or type(row["end"]) is not int
            or row["start"] != ends.get(row["block_id"], 0)
            or row["end"] < row["start"]
        ):
            raise ValueError("Invalid source coverage range")
        valid_id(row["block_id"])
        ends[row["block_id"]] = row["end"]
    for row in value["assets"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "blocks", "original", "transcription", "understanding"}
            or row["original"] != "retained"
            or row["transcription"] not in {"available", "pending", "not_required"}
            or row["understanding"] not in {"pending", "not_required"}
            or not isinstance(row["blocks"], list)
            or not row["blocks"]
            or any(block not in ends for block in row["blocks"])
        ):
            raise ValueError("Invalid source asset coverage")
        valid_id(row["id"])
    if any(not isinstance(row, dict) for row in value["issues"]):
        raise ValueError("Invalid source coverage issue")
    pending = (
        any(row["status"] == "pending" for row in value["ranges"])
        or any(
            row["transcription"] == "pending" or row["understanding"] == "pending"
            for row in value["assets"]
        )
        or bool(value["issues"])
    )
    if value["status"] == "complete" and pending:
        raise ValueError("Incomplete original coverage cannot be complete")
    if parsed is not None:
        blocks = {block.id: block for block in parsed.blocks}
        if ends != {block.id: block.chars for block in parsed.blocks} or any(
            row["kind"] != blocks[row["block_id"]].kind
            or row["location"] != blocks[row["block_id"]].location
            for row in value["ranges"]
        ):
            raise ValueError("Source coverage denominator mismatch")
        expected_assets = {}
        for block in parsed.blocks:
            for asset in block.assets:
                expected_assets.setdefault(asset, []).append(block.id)
        actual_assets = {row["id"]: row["blocks"] for row in value["assets"]}
        if len(actual_assets) != len(value["assets"]) or actual_assets != expected_assets:
            raise ValueError("Source coverage asset denominator mismatch")
        if any(gap not in value["issues"] for gap in parsing_gaps(parsed)):
            raise ValueError("Source coverage hides parsing gaps")
    return value


def stored_coverage(document, source, parsed):
    return validate_coverage(
        json.loads((document or {}).get("compilation_coverage", "{}")),
        source.source_id,
        source.id,
        parsed.id,
        parsed=parsed,
    )


def source_coverage(source, parsed, report, *, published=False):
    """The denominator is the immutable parse plus its explicit original-content gaps."""
    if parsed is None:
        return {}
    units = {}
    from openkb.agent.evidence_review import unverified_facts
    from openkb.agent.evidence_selection import referenced_facts

    review_pending = unverified_facts(report)
    referenced = referenced_facts(report) - review_pending
    for unit in report.source_units.values():
        units.setdefault(unit["reference"]["block_id"], []).append(unit)
    ranges = []
    assets = {}
    transcriptions = {digest for row in parsed.quality for digest in row.get("transcriptions", [])}
    for block in parsed.blocks:
        cursor = 0

        def append(start, end, status, reason):
            ranges.append(
                {
                    "block_id": block.id,
                    "start": start,
                    "end": end,
                    "kind": block.kind,
                    "location": block.location,
                    "status": status,
                    "reason": reason,
                }
            )

        for unit in sorted(units.get(block.id, []), key=lambda row: row["reference"]["start"]):
            ref = unit["reference"]
            if ref["start"] < cursor:
                continue  # A recovered split may overlap its earlier parent.
            if ref["start"] > cursor:
                append(cursor, ref["start"], "pending", "analysis_pending")
            facts = set(unit["facts"])
            complete = facts <= report.published_facts | referenced
            status = (
                "referenced"
                if facts and complete and facts & referenced
                else "verified"
                if facts and complete
                else "no_facts"
                if not facts
                else "pending"
            )
            reason = (
                "verified_contribution"
                if status == "verified"
                else "secondary_details_in_source"
                if status == "referenced"
                else unit["empty_reason"]
                if status == "no_facts"
                else "knowledge_review_pending"
                if facts & review_pending
                else "knowledge_content_omitted"
            )
            append(ref["start"], ref["end"], status, reason)
            cursor = ref["end"]
        if cursor < block.chars or not block.chars:
            append(cursor, block.chars, "pending", "analysis_pending")
        for digest in block.assets:
            entry = assets.setdefault(
                digest,
                {
                    "id": digest,
                    "blocks": [],
                    "original": "retained",
                    "transcription": "pending",
                    "understanding": "pending",
                },
            )
            entry["blocks"].append(block.id)
            # Transcription is separate from understanding the figure's relationships.
            if digest in transcriptions or (
                block.context.startswith("OCR layout block")
                and "transcription=pending" not in block.context
            ):
                entry["transcription"] = "available"
    issues = parsing_gaps(parsed)
    issues.extend(dict(row) for row in report.omissions)
    issues.extend(
        {
            "stage": "verification",
            "reason": "knowledge_review_" + note["kind"],
            "topic": path,
            "items": note["facts"],
        }
        for path, notes in report.review_notes.items()
        for note in notes
        if note["kind"] in {"coverage", "uncertainty"}
    )
    pending = bool(issues) or any(row["status"] == "pending" for row in ranges) or bool(assets)
    value = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": ("partial" if pending else "complete") if published else "pending",
        "ranges": ranges,
        "assets": list(assets.values()),
        "issues": issues,
    }
    return validate_coverage(value, source.source_id, source.id, parsed.id, parsed=parsed)


def coverage_window(coverage, reference):
    """A constant-size description of this read window, independent of total block size."""
    start, end = reference.get("start", 0), reference.get("end")
    if type(start) is not int or type(end) is not int or end <= start:
        return {"status": "unknown"}
    counts = {"verified": 0, "referenced": 0, "no_facts": 0, "pending": 0}
    for row in coverage.get("ranges", []):
        if row["block_id"] == reference["block_id"]:
            counts[row["status"]] += max(0, min(end, row["end"]) - max(start, row["start"]))
    unknown = end - start - sum(counts.values())
    return {
        "status": "unknown" if unknown else "pending" if counts["pending"] else "complete",
        "characters": counts,
        "unknown_characters": unknown,
    }


def parsing_gaps(parsed):
    """A usable text layer does not resolve a failed image transcription."""
    return [
        dict(row)
        for row in parsed.quality
        if row["status"] == "needs_review"
        or "image_ocr_notice:" in row["reason"]
        or row["reason"].endswith("docx_image_position_unavailable")
    ]


def coverage_text(coverage):
    return {
        "complete": "原文分析覆盖完整",
        "partial": "知识部分可用，仍有内容缺失、待复核或图片待理解",
        "pending": "分析尚未完成，可继续处理",
    }.get((coverage or {}).get("status"), "分析覆盖未知")
