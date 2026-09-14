"""Carry a human missing-image decision across parses of unchanged source bytes."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from openkb.locks import kb_ingest_lock
from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object

if TYPE_CHECKING:
    from openkb.evidence import ParseStore, ParseVersion
    from openkb.sources import SourceVersion


def _position(location):
    # Heading text can contain OCR output; physical source positions cannot.
    # A detached image's surrounding document/table is not its original position.
    if location.get("kind") == "docx" and "paragraph" not in location:
        return None
    result = {
        key: value for key, value in location.items() if key not in {"headings", "attachment"}
    }
    if "attachment" in location:
        attachment = location["attachment"]
        position = _position(attachment["position"])
        if position is None:
            return None
        result["attachment"] = {
            "part": attachment["part"],
            "blob": attachment["blob"],
            "position": position,
        }
    return result


def _markers(store: ParseStore, parsed: ParseVersion) -> list[str]:
    missing = [
        row
        for row in parsed.quality
        if row["reason"] == "docx_image_asset_missing"
        or row["reason"].endswith(":docx_image_asset_missing")
    ]
    counts: Counter[str] = Counter()
    if any("location" in row for row in missing):
        # New parses locate omissions independently of source wording. Partial
        # location metadata cannot authorize inheriting a historical decision.
        for row in missing:
            processing_checkpoint()
            if not isinstance(row.get("location"), dict):
                return []
            position = _position(row["location"])
            if position is None:
                return []
            counts[content_id(position)] += row.get("count", 1)
    else:
        # Read old snapshots as recorded, without migrating bytes or guessing
        # whether same-named literal text in a new parse is a missing object.
        for block in parsed.blocks:
            processing_checkpoint()
            text = store.sources.asset(block.blob).read_text(encoding="utf-8")
            count = text.count("[Original image unavailable]")
            if count:
                position = _position(block.location)
                if position is None:
                    return []
                counts[content_id(position)] += count
    return sorted(content_id({"position": key, "count": count}) for key, count in counts.items())


def inherit_missing_images(store: ParseStore, version: SourceVersion, parsed: ParseVersion) -> None:
    """Persist a new exact decision only when prior permission covers identical missing markers.

    Legacy decisions are discoverable without rewriting them. A changed source,
    additional missing location or unrelated quality failure is never waived.
    Reading status alone does not create permission or change the selected parse.
    """
    reasons = store._missing_images(version, parsed)
    if not reasons:
        return
    with kb_ingest_lock(store.kb_dir / ".openkb"):
        store._bind(version, parsed)
        if store.accepted_missing_images(version, parsed):
            return
        markers = None
        for path in store.sources.owned_path(store.root / "confirmations").glob("*.json"):
            processing_checkpoint()
            try:
                record = read_object(store.sources.owned_path(path))
                if (
                    record.get("decision") != "continue_with_missing_original_images"
                    or record.get("source") != version.id
                    or record.get("reasons") != reasons
                ):
                    continue
                parse_id = record.get("parse")
                if not isinstance(parse_id, str):
                    continue
                previous = store.load(parse_id)
                if store.accepted_missing_images(version, previous) != reasons:
                    continue
            except (ValueError, OSError):
                # A malformed/unrelated historical receipt is not permission.
                continue
            if markers is None:
                markers = _markers(store, parsed)
                if not markers:
                    return
            if _markers(store, previous) == markers:
                store.accept_missing_images(version, parsed)
                return
