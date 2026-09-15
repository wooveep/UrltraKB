"""Resolve saved omissions to bounded, version-bound original evidence for inspection."""

from dataclasses import asdict
from pathlib import Path

from openkb.application.source_artifacts import artifact_stage, saved_compilation_records
from openkb.compilation_omissions import validate_omissions
from openkb.evidence import Evidence, ParseStore
from openkb.locks import kb_read_lock
from openkb.source_coverage import validate_coverage
from openkb.sources import SourceStore, content_id


def source_issues(
    kb_dir, source_id, version_id, parse_id, coverage, omissions, *, offset=0, limit=50
):
    """Inspect an outcome snapshot. Reading never starts compilation or changes coverage."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid issue page")
    with kb_read_lock(Path(kb_dir) / ".openkb"):
        store = SourceStore(kb_dir)
        source = store.version(version_id)
        parsed = ParseStore(kb_dir).load(parse_id)
        if source.source_id != source_id or parsed.input_key != source.input_key:
            raise ValueError("Source and parsing identities do not match")
        coverage = validate_coverage(coverage, source_id, version_id, parse_id, parsed=parsed)
        omissions = validate_omissions(omissions)
        blocks = {block.id: block for block in parsed.blocks}
        pending = [row for row in coverage.get("ranges", []) if row["status"] == "pending"]
        records = saved_compilation_records(store, source_id, version_id, parse_id)
        topic_ranges, targets = _topic_ranges(records, source_id, version_id, parse_id, blocks)
        rows, linked = [], set()

        def append(reason, stage, item, span=None, location=None):
            reference = None
            if span is not None:
                reference = asdict(
                    Evidence(
                        source_id,
                        version_id,
                        parse_id,
                        span["block_id"],
                        span["start"],
                        span["end"] or None,
                    )
                )
                location = blocks[span["block_id"]].location
                linked.add((span["block_id"], span["start"], span["end"]))
            rows.append(
                {
                    "reason": reason,
                    "stage": stage,
                    "item": item,
                    "reference": reference,
                    "location": location or {},
                }
            )

        for omission in omissions:
            for item in omission["items"]:
                matching = set()
                topics = (
                    targets.get(item, ())
                    if omission["stage"] == "generation"
                    else (topic for topic in topic_ranges if content_id(topic) == item)
                )
                for topic in topics:
                    matching.update(topic_ranges.get(topic, ()))
                # Only link ranges still pending in this outcome; old saved plans/facts
                # may also contain successfully published or superseded contributions.
                spans = []
                for span in pending:
                    if omission["stage"] == "facts" and span["block_id"] == item:
                        spans.append(span)
                    for block_id, start, end in sorted(matching):
                        left, right = max(start, span["start"]), min(end, span["end"])
                        if span["block_id"] == block_id and left < right:
                            candidate = {**span, "start": left, "end": right}
                            if candidate not in spans:
                                spans.append(candidate)
                for span in spans or [None]:
                    append(omission["reason"], omission["stage"], item, span)
        for span in pending:
            if (span["block_id"], span["start"], span["end"]) not in linked:
                append(span["reason"], "", "", span)
        for asset in coverage.get("assets", []):
            if asset["transcription"] == "pending" or asset["understanding"] == "pending":
                block = blocks[asset["blocks"][0]]
                append(
                    "image_content_requires_ocr"
                    if asset["transcription"] == "pending"
                    else "image_understanding_pending",
                    "parsing",
                    "图像 " + asset["id"][:12],
                    {"block_id": block.id, "start": 0, "end": block.chars},
                )
        for issue in coverage.get("issues", []):
            if "items" not in issue:
                append(
                    issue.get("reason", "quality_incomplete"),
                    "parsing",
                    "解析检查",
                    location=issue.get("location", {"page": issue.get("page")}),
                )
        # Prefer a directly readable original over an unmapped historical omission.
        rows.sort(key=lambda row: row["reference"] is None)
        return {
            "rows": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "excluded": sum(len(row["items"]) for row in omissions),
            "pending": len(pending),
            "coverage_known": bool(coverage),
        }


def _topic_ranges(records, source_id, version_id, parse_id, blocks):
    topics, targets = {}, {}
    for record in records:
        if artifact_stage(record) not in {"facts", "planning"}:
            continue
        value = record["value"]
        payload = (record.get("contract") or {}).get("payload", {})
        inputs = {unit["id"]: unit for unit in payload.get("units", [])}
        for unit in value.get("units", []):
            ref = inputs.get(unit["id"], {}).get("reference")
            if ref is None:
                continue  # Older checkpoints did not retain a provable original binding.
            evidence = Evidence(**ref)
            if (evidence.source_id, evidence.version_id, evidence.parse_id) != (
                source_id,
                version_id,
                parse_id,
            ) or evidence.block_id not in blocks:
                raise ValueError("Fact evidence identity mismatch")
            block = blocks[evidence.block_id]
            if evidence.start > block.chars or (evidence.end or block.chars) > block.chars:
                raise ValueError("Fact evidence bounds mismatch")
            for fact in unit.get("facts", []):
                topics.setdefault(fact["topic"], set()).add(
                    (block.id, evidence.start, evidence.end or block.chars)
                )
        for group in value.get("topics", []):
            path = ("entities" if group["kind"] == "entity" else "concepts") + "/" + group["name"]
            targets.setdefault(path, set()).update(group["members"])
    return topics, targets
