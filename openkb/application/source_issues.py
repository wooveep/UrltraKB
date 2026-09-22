"""Resolve saved omissions to bounded, version-bound original evidence for inspection."""

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from openkb.application.source_artifacts import saved_compilation_issue_index
from openkb.compilation_omissions import validate_omissions
from openkb.evidence import Evidence, ParseStore
from openkb.locks import kb_read_lock
from openkb.source_coverage import validate_coverage
from openkb.sources import SourceStore, content_id


def _omission_ranges(summary_rows, omissions, source_id, version_id, parse_id, parsed):
    """Project only the omitted identities from compact saved range summaries."""

    from openkb.agent.document_plan import range_intervals

    wanted = {
        item
        for omission in omissions
        if omission["stage"] in {"planning", "generation"}
        for item in omission["items"]
    }
    ranges = {item: [] for item in wanted}
    legacy_topics = {item: set() for item in wanted}
    for summary in summary_rows():
        index = summary["issue_index"]
        for page in index.get("pages", []):
            targets = {page["target"], content_id(page["target"]), *page["keys"]} & wanted
            for item in targets:
                for value in page["ranges"]:
                    for block, start, end in range_intervals(
                        value, parsed, f"page {page['target']} subject ranges"
                    ):
                        ranges[item].append((parsed.blocks[block].id, start, end))
        for topic in index.get("topics", []):
            for item in {topic["target"], content_id(topic["target"])} & wanted:
                legacy_topics[item].update(topic["topics"])
    for summary in summary_rows():
        for fact in summary["issue_index"].get("facts", []):
            reference, topic = fact["reference"], fact["topic"]
            if (reference["source_id"], reference["version_id"], reference["parse_id"]) != (
                source_id,
                version_id,
                parse_id,
            ):
                raise ValueError("Fact evidence identity mismatch")
            targets = {content_id(topic)} & wanted
            targets.update(item for item, values in legacy_topics.items() if topic in values)
            for item in targets:
                ranges[item].append((reference["block_id"], reference["start"], reference["end"]))
    return {item: tuple(dict.fromkeys(values)) for item, values in ranges.items()}


def _intersections(span, ranges: Iterable[tuple[str, int, int]]):
    """Yield exact pending intersections without copying the coverage denominator."""

    for block_id, start, end in ranges:
        left, right = max(start, span["start"]), min(end, span["end"])
        if span["block_id"] == block_id and left < right:
            yield {**span, "start": left, "end": right}


def source_issues(
    kb_dir, source_id, version_id, parse_id, coverage, omissions, *, offset=0, limit=50
):
    """Page saved omissions without loading every historic request or result body."""

    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid issue page")
    with kb_read_lock(Path(kb_dir) / ".openkb"):
        store = SourceStore(kb_dir)
        source = store.version(version_id)
        parsed = ParseStore(kb_dir).load(parse_id)
        if source.source_id != source_id or parsed.input_key != source.input_key:
            raise ValueError("Source and parsing identities do not match")
        coverage = validate_coverage(
            coverage, source_id, version_id, parse_id, parsed=parsed, streaming=True
        )
        omissions = validate_omissions(omissions)
        ranges = _omission_ranges(
            lambda: saved_compilation_issue_index(store, source_id, version_id, parse_id),
            omissions,
            source_id,
            version_id,
            parse_id,
            parsed,
        )
        selected, total, block_cache = [], 0, {}

        def block_for(block_id):
            if block_id not in block_cache:
                block_cache[block_id] = next(
                    (block for block in parsed.blocks if block.id == block_id), None
                )
            block = block_cache[block_id]
            if block is None:
                raise ValueError("Source coverage block mismatch")
            return block

        def append(reason, stage, item, span=None, location=None):
            nonlocal total
            position, total = total, total + 1
            if not offset <= position < offset + limit:
                return
            reference = None
            if span is not None:
                block = block_for(span["block_id"])
                end = block.chars if span["end"] is None else span["end"]
                if not 0 <= span["start"] < end <= block.chars:
                    raise ValueError("Source coverage bounds mismatch")
                reference = asdict(
                    Evidence(
                        source_id,
                        version_id,
                        parse_id,
                        span["block_id"],
                        span["start"],
                        end,
                    )
                )
                location = block.location
            selected.append(
                {
                    "reason": reason,
                    "stage": stage,
                    "item": item,
                    "reference": reference,
                    "location": location or {},
                }
            )

        def pending_rows():
            return (row for row in coverage.get("ranges", []) if row["status"] == "pending")

        def matching(stage, item, span):
            if stage == "facts":
                return span["block_id"] == item
            return any(_intersections(span, ranges.get(item, ())))

        def spans(stage, item):
            for pending in pending_rows():
                if stage == "facts" and pending["block_id"] == item:
                    yield pending
                elif stage != "facts":
                    yield from _intersections(pending, ranges.get(item, ()))

        # Directly readable omission ranges keep their historical ordering.
        for omission in omissions:
            for item in omission["items"]:
                for span in spans(omission["stage"], item):
                    append(omission["reason"], omission["stage"], item, span)
        # A generic pending range is listed only when no omission above already
        # identifies it.  This predicate streams the compact omitted bindings.
        for span in pending_rows():
            if not any(
                matching(omission["stage"], item, span)
                for omission in omissions
                for item in omission["items"]
            ):
                append(span["reason"], "", "", span)
        for asset in coverage.get("assets", []):
            if asset["transcription"] == "pending" or asset["understanding"] == "pending":
                append(
                    "image_content_requires_ocr"
                    if asset["transcription"] == "pending"
                    else "image_understanding_pending",
                    "parsing",
                    "图像 " + asset["id"][:12],
                    {
                        "block_id": asset["blocks"][0],
                        "start": 0,
                        "end": None,
                    },
                )
        # Unmapped historic identities remain visible after all readable rows.
        for omission in omissions:
            for item in omission["items"]:
                if not any(spans(omission["stage"], item)):
                    append(omission["reason"], omission["stage"], item)
        for issue in coverage.get("issues", []):
            if "items" not in issue:
                append(
                    issue.get("reason", "quality_incomplete"),
                    "parsing",
                    "解析检查",
                    location=issue.get("location", {"page": issue.get("page")}),
                )
        pending = sum(1 for _ in pending_rows())
        return {
            "rows": selected,
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "excluded": sum(len(row["items"]) for row in omissions),
            "pending": pending,
            "coverage_known": bool(coverage),
        }
