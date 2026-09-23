"""Lightweight frozen evidence descriptors, reconstructed from immutable parsing."""

from openkb.agent.source_protocol import PROTOCOL
from openkb.evidence import Evidence, ParseStore, complete_read_bound
from openkb.processing import processing_checkpoint
from openkb.source_context import context_fields
from openkb.sources import content_id


def _descriptor(source, parsed, start, end, protocol):
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(parsed.blocks):
        raise ValueError("Invalid evidence group range")
    value = {
        "protocol": protocol,
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "start": start,
        "end": end,
    }
    return {"id": content_id(value), **value}


def evidence_descriptor(source, parsed, start, end):
    return _descriptor(source, parsed, start, end, PROTOCOL)


def planning_windows(source, parsed, navigation):
    """Re-encode a verified legacy manifest for current planning requests.

    The stored navigation identity and windows remain unchanged.  A v1 manifest
    is accepted only when its complete persisted metadata still hashes to its
    saved identity; ordinary v2 manifests keep their existing validation path.
    """
    windows = navigation.get("windows", [])
    if not isinstance(windows, list):
        raise ValueError("Invalid navigation window manifest")
    if not any(
        isinstance(row, dict)
        and isinstance(row.get("evidence"), dict)
        and row["evidence"].get("protocol") == "source-prefix-v1"
        for row in windows
    ):
        validate_windows(source, parsed, windows)
        return windows
    keys = {
        "schema",
        "source_id",
        "version",
        "parse",
        "profile",
        "status",
        "reason",
        "usage",
        "pageindex",
        "windows",
    }
    if (
        not keys <= navigation.keys()
        or navigation.get("source_id") != source.source_id
        or navigation.get("version") != source.id
        or navigation.get("parse") != parsed.id
        or content_id({key: navigation[key] for key in keys}) != navigation.get("id")
    ):
        raise ValueError("Legacy navigation identity mismatch")
    validate_windows(source, parsed, windows, allow_legacy=True)
    upgraded = [
        {
            **row,
            "evidence": evidence_descriptor(
                source, parsed, row["evidence"]["start"], row["evidence"]["end"]
            ),
        }
        for row in windows
    ]
    validate_windows(source, parsed, upgraded)
    return upgraded


def read_evidence_group(kb_dir, source, parsed, descriptor):
    """The same public reader serves indexing and later task assembly; no reparsing."""
    expected = evidence_descriptor(source, parsed, descriptor["start"], descriptor["end"])
    if descriptor != expected:
        raise ValueError("Frozen evidence identity mismatch")
    from openkb.resource_budget import check_memory

    members = [
        block
        for block in parsed.blocks[descriptor["start"] : descriptor["end"]]
        if "attachment" not in block.location
    ]
    check_memory(sum(complete_read_bound(block) for block in members) * 12, stage="index_structure")
    reader = ParseStore(kb_dir).reader(source, parsed)
    blocks = []
    for block in members:
        processing_checkpoint()
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        blocks.append(
            {
                "id": block.id,
                "order": block.order,
                "kind": block.kind,
                "text": view.text,
                "location": view.location,
                "assets": list(block.assets),
                **context_fields(view),
            }
        )
    return {
        "group_id": descriptor["id"],
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "document": source.name,
        "blocks": blocks,
    }


def validate_windows(source, parsed, windows, *, allow_legacy=False):
    if not isinstance(windows, list):
        raise ValueError("Invalid navigation window manifest")
    following = 0
    block_ids = {block.id for block in parsed.blocks}
    for row in windows:
        if (
            not isinstance(row, dict)
            or not {"evidence", "target_start", "target_end", "status", "reason", "target_tokens"}
            <= row.keys()
        ):
            raise ValueError("Invalid navigation window")
        start, end = row["target_start"], row["target_end"]
        descriptor = row["evidence"]
        if (
            type(start) is not int
            or type(end) is not int
            or start != following
            or not start < end <= len(parsed.blocks)
            or not isinstance(descriptor, dict)
            or descriptor
            != _descriptor(
                source,
                parsed,
                descriptor.get("start"),
                descriptor.get("end"),
                "source-prefix-v1"
                if allow_legacy and descriptor.get("protocol") == "source-prefix-v1"
                else PROTOCOL,
            )
            or descriptor["start"] > start
            or descriptor["end"] != end
            or row["status"] not in {"complete", "basic"}
            or (row["status"] == "basic" and not isinstance(row["reason"], str))
            or type(row["target_tokens"]) is not int
            or row["target_tokens"] <= 0
        ):
            raise ValueError("Invalid navigation window coverage")
        issues = row.get("unlocated", [])
        if not isinstance(issues, list) or (issues and row["status"] != "basic"):
            raise ValueError("Invalid unresolved navigation state")
        for issue in issues:
            if (
                not isinstance(issue, dict)
                or set(issue) != {"entry", "title", "level", "toc_block", "start", "end", "reason"}
                or type(issue["entry"]) is not int
                or issue["entry"] < 0
                or not isinstance(issue["title"], str)
                or not issue["title"]
                or type(issue["level"]) is not int
                or not 1 <= issue["level"] <= 9
                or (
                    issue["toc_block"] is not None
                    and (
                        not isinstance(issue["toc_block"], str)
                        or issue["toc_block"] not in block_ids
                    )
                )
                or type(issue["start"]) is not int
                or type(issue["end"]) is not int
                or not 0 <= issue["start"] < issue["end"] <= len(parsed.blocks)
                or issue["start"] >= end
                or issue["end"] <= start
                or issue["reason"] != "index_unlocated_contents"
            ):
                raise ValueError("Invalid unresolved contents entry")
        following = end
    # An empty manifest is the durable degraded/disabled-navigation form; the
    # document planner then constructs its full fallback W/T itself.  Once a
    # navigator supplies any window, however, it must account for the entire
    # current parse and cannot silently truncate the tail.
    if windows and following != len(parsed.blocks):
        raise ValueError("Incomplete navigation window coverage")
