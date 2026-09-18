"""Immutable parsing checkpoints and bounded, version-bound source evidence."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openkb.locks import atomic_write_json, kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.source_context import block_record, context_size, parse_record, validate_context_data
from openkb.sources import SourceStore, SourceVersion, content_id, read_object, valid_id

_CONFIRMABLE = {"blank_or_illustration", "ocr_blank_or_illustration", "image_content_requires_ocr"}

# All evidence consumers distinguish original text from reader-supplied context.
EVIDENCE_PROVENANCE = {
    "text": "parsed_source_text",
    "context": "reader_context_with_source_excerpts",
    "source_window": "reader_character_extent_not_author_statements",
    "context_data": {
        "source_excerpts": "parsed_source_text",
        "structure": "document_structure",
        "reader_status": "reader_metadata_not_author_statements",
        "image_relations": "recorded_asset_derivations;_source_alt_is_author_supplied",
        "inline_annotations": "original_note_or_comment_text_with_its_native_role",
    },
    "context_format": "structured_json_uses_context_data_roles;_legacy_display_mixes_roles",
    "location": "document_position",
    "presentation_roles": {
        "placeholder_type": "recorded_native_OOXML_placeholder_type; null means this object "
        "is not a placeholder; absent means unrecorded. Object names, visual position and "
        "semantic heading roles do not establish the native placeholder type.",
        "title_object_id": "the sole native TITLE/CENTER_TITLE/VERTICAL_TITLE object when "
        "title_placeholder_count is 1; otherwise null. Count 0 means none; count greater "
        "than 1 means multiple, not absent. Missing fields mean unrecorded. These types do "
        "not establish a unique semantic or visual title. Generated Slide N is not source wording.",
        "semantic_headings": "A heading or title in the content need not be a native "
        "placeholder. Missing native title placeholders do not disprove a semantic heading "
        "supported by original wording and context. Position or an object name alone is "
        "not sufficient; do not turn an ambiguous heading into a unique page title.",
    },
    "analysis_coverage": "knowledge_analysis_status",
}


def source_provenance(value):
    """Describe applicable reader roles without charging other formats for OOXML guidance."""

    def presentation(item):
        if isinstance(item, dict):
            location = item.get("location")
            while isinstance(location, dict):
                if location.get("kind") == "pptx":
                    return True
                attachment = location.get("attachment")
                location = attachment.get("position") if isinstance(attachment, dict) else None
            return any(presentation(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(presentation(child) for child in item)
        return False

    has_presentation = presentation(value)
    return {
        key: copy.deepcopy(role)
        for key, role in EVIDENCE_PROVENANCE.items()
        if key != "presentation_roles" or has_presentation
    }


def validate_location(location: dict[str, Any], *, _depth: int = 0) -> None:
    if _depth > 8:
        raise ValueError("Source attachment nesting is too deep")
    if not isinstance(location, dict) or location.get("kind") not in {
        "pdf",
        "docx",
        "text",
        "converted",
        "xlsx",
        "pptx",
        "csv",
        "html",
        "xml",
    }:
        raise ValueError("Invalid source location")
    allowed = {
        "kind",
        "page",
        "paragraph",
        "table",
        "row",
        "cell",
        "line",
        "line_end",
        "columns",
        "delimiter",
        "dom_path",
        "dom_id",
        "selection",
        "element_path",
        "namespaces",
        "attributes",
        "text_role",
        "role",
        "toc_level",
        "headings",
        "heading_level",
        "bbox",
        "display_bbox",
        "attachment",
        "attachment_files",
        "sheet",
        "sheet_index",
        "cell_address",
        "cell_range",
        "slide",
        "object_id",
        "coordinate_unit",
        "group_ids",
        "notes",
        "placeholder_type",
        "title_object_id",
        "title_placeholder_count",
    }
    if set(location) - allowed:
        raise ValueError("Unknown source location field")
    from openkb.markup_locations import validate_markup_location

    validate_markup_location(location)
    if "role" in location and location["role"] not in {"toc", "header", "footer", "metadata"}:
        raise ValueError("Invalid native content role")
    if "toc_level" in location and (
        location.get("role") != "toc"
        or type(location["toc_level"]) is not int
        or not 1 <= location["toc_level"] <= 9
    ):
        raise ValueError("Invalid contents level")
    if set(location) & {"columns", "delimiter"}:
        if (
            location["kind"] != "csv"
            or not isinstance(location.get("columns"), list)
            or not all(isinstance(c, str) for c in location["columns"])
            or location.get("delimiter") not in {",", ";", "\t", "|"}
        ):
            raise ValueError("Invalid CSV position")
    if "attachment_files" in location:
        from openkb.attachments import validate_attachment_files

        if location["kind"] != "docx":
            raise ValueError("Document attachment references require a DOCX position")
        validate_attachment_files(location["attachment_files"])
    if location["kind"] == "xlsx":
        from openkb.office_locations import validate_spreadsheet_location

        validate_spreadsheet_location(location)
    elif set(location) & {"sheet", "sheet_index", "cell_address", "cell_range"}:
        raise ValueError("Spreadsheet coordinates require a spreadsheet source")
    if location["kind"] == "pptx":
        from openkb.office_locations import validate_presentation_location

        validate_presentation_location(location)
        if "notes" in location and (location["notes"] is not True or "object_id" in location):
            raise ValueError("Invalid speaker note position")
    elif set(location) & {
        "slide",
        "object_id",
        "coordinate_unit",
        "group_ids",
        "notes",
        "placeholder_type",
        "title_object_id",
        "title_placeholder_count",
    }:
        raise ValueError("Slide coordinates require a presentation source")
    if "attachment" in location:
        attachment = location["attachment"]
        if (
            not isinstance(attachment, dict)
            or set(attachment) != {"part", "name", "blob", "position"}
            or not all(
                isinstance(attachment[key], str) and attachment[key] for key in ("part", "name")
            )
        ):
            raise ValueError("Invalid embedded source location")
        valid_id(attachment["blob"])
        validate_location(attachment["position"], _depth=_depth + 1)
    for key in ("page", "paragraph", "table", "row", "cell", "line"):
        if key in location and (type(location[key]) is not int or location[key] < 1):
            raise ValueError("Invalid source position")
    if "line_end" in location and (
        type(location["line_end"]) is not int
        or "line" not in location
        or location["line_end"] < location["line"]
    ):
        raise ValueError("Invalid source line range")
    if location["kind"] == "pdf" and "page" not in location:
        raise ValueError("PDF evidence needs a physical page")
    if location["kind"] != "pdf" and "page" in location:
        raise ValueError("Only PDF evidence has a physical page")
    if "headings" in location and (
        not isinstance(location["headings"], list)
        or not all(isinstance(item, str) for item in location["headings"])
    ):
        raise ValueError("Invalid heading path")
    if "heading_level" in location and (
        type(location["heading_level"]) is not int or not 1 <= location["heading_level"] <= 9
    ):
        raise ValueError("Invalid native heading level")
    if "display_bbox" in location and (location["kind"] != "pdf" or "bbox" not in location):
        raise ValueError("Display coordinates require a positioned PDF block")
    for coordinate in ("bbox", "display_bbox"):
        if coordinate not in location:
            continue
        box = location[coordinate]
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(type(item) in {float, int} and math.isfinite(item) for item in box)
            or box[0] > box[2]
            or box[1] > box[3]
            or (coordinate == "display_bbox" and min(box) < 0)
        ):
            raise ValueError("Invalid source coordinates")


@dataclass(frozen=True)
class BlockDraft:
    text: str
    kind: str
    location: dict[str, Any]
    assets: tuple[str, ...] = ()
    context: str = ""
    context_data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.context, str):
            raise ValueError("Invalid parsed text")
        validate_context_data(self.context_data)
        if self.kind not in {"heading", "paragraph", "table", "code", "image", "metadata", "list"}:
            raise ValueError("Invalid content block kind")
        validate_location(self.location)
        if not isinstance(self.assets, tuple):
            raise ValueError("Invalid block assets")
        for item in self.assets:
            valid_id(item)
        from openkb.image_provenance import validate_image_bindings

        validate_image_bindings(self.context_data, self.assets)
        if "attachment_files" in self.location:
            from openkb.attachments import validate_attachment_files

            validate_attachment_files(self.location["attachment_files"], self.assets)


@dataclass(frozen=True)
class Block:
    id: str
    order: int
    blob: str
    chars: int
    kind: str
    location: dict[str, Any]
    assets: tuple[str, ...]
    context: str
    context_data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        valid_id(self.id)
        valid_id(self.blob)
        if (
            type(self.order) is not int
            or self.order < 0
            or type(self.chars) is not int
            or self.chars < 0
        ):
            raise ValueError("Invalid content block bounds")
        BlockDraft("", self.kind, self.location, self.assets, self.context, self.context_data)
        payload = block_record(self)
        payload.pop("id")
        if self.id != content_id(payload):
            raise ValueError("Content block digest mismatch")


@dataclass(frozen=True)
class ParseVersion:
    id: str
    input_key: str
    lookup_key: str
    profile: dict[str, Any]
    blocks: tuple[Block, ...]
    quality: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for identifier in (self.id, self.input_key, self.lookup_key):
            valid_id(identifier)
        if not isinstance(self.profile, dict) or self.lookup_key != content_id(
            {"input": self.input_key, "profile": self.profile}
        ):
            raise ValueError("Invalid parse lookup key")
        if not isinstance(self.blocks, tuple) or [block.order for block in self.blocks] != list(
            range(len(self.blocks))
        ):
            raise ValueError("Invalid content block order")
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Repeated content block identity")
        if not isinstance(self.quality, list):
            raise ValueError("Invalid parsing quality")
        for row in self.quality:
            if not isinstance(row, dict) or set(row) - {
                "page",
                "block",
                "status",
                "reason",
                "transcriptions",
                "location",
                "count",
            }:
                raise ValueError("Invalid quality record")
            if row.get("status") not in {"verified", "needs_review"} or not isinstance(
                row.get("reason"), str
            ):
                raise ValueError("Invalid parsing quality status")
            if "page" in row and (type(row["page"]) is not int or row["page"] < 1):
                raise ValueError("Invalid quality page")
            if "block" in row and row["block"] not in {block.id for block in self.blocks}:
                raise ValueError("Invalid quality block")
            if "location" in row:
                validate_location(row["location"])
            if "count" in row and (type(row["count"]) is not int or row["count"] < 1):
                raise ValueError("Invalid quality count")
            if "transcriptions" in row:
                assets = {asset for block in self.blocks for asset in block.assets}
                values = row["transcriptions"]
                if (
                    row["status"] != "verified"
                    or not isinstance(values, list)
                    or not values
                    or any(not isinstance(asset, str) or asset not in assets for asset in values)
                    or len(set(values)) != len(values)
                ):
                    raise ValueError("Invalid image transcription binding")
        pages = self.profile.get("physical_pages")
        if pages is not None:
            if type(pages) is not int or pages <= 0:
                raise ValueError("Invalid physical page count")
            if sorted(row.get("page", 0) for row in self.quality) != list(range(1, pages + 1)):
                raise ValueError("Parsing quality does not cover every physical page")
            if any(
                block.location.get("kind") != "pdf" or block.location["page"] > pages
                for block in self.blocks
            ):
                raise ValueError("Parsed block escapes the original PDF pages")
        payload = parse_record(self)
        payload.pop("id")
        if self.id != content_id(payload):
            raise ValueError("Parse artifact digest mismatch")


@dataclass(frozen=True)
class Evidence:
    source_id: str
    version_id: str
    parse_id: str
    block_id: str
    start: int = 0
    end: int | None = None

    def __post_init__(self) -> None:
        valid_id(self.source_id, source=True)
        for item in (self.version_id, self.parse_id, self.block_id):
            valid_id(item)
        if type(self.start) is not int or self.start < 0:
            raise ValueError("Invalid evidence start")
        if self.end is not None and (type(self.end) is not int or self.end <= self.start):
            raise ValueError("Invalid evidence end")


@dataclass(frozen=True)
class EvidenceSlice:
    reference: Evidence
    text: str
    location: dict[str, Any]
    kind: str
    assets: tuple[str, ...]
    context: str
    next_start: int | None
    context_data: dict[str, Any] | None = None
    block_chars: int | None = None


def complete_read_bound(block):
    """Allow exact text and required metadata through the public bounded reader."""
    return max(
        4096,
        block.chars,
        context_size(block)
        + len(json.dumps(block.location, ensure_ascii=False))
        + 64 * len(block.assets),
    )


class ParseStore:
    def __init__(self, kb_dir: Path):
        self.sources = SourceStore(kb_dir)
        self.kb_dir = self.sources.kb_dir
        self.root = self.sources.owned_path(self.sources.root / "parses")

    def select(self, version: SourceVersion, parsed: ParseVersion) -> None:
        """Select the complete-document parsing view without changing old references."""
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            if self.load(parsed.id) != parsed:
                raise ValueError("Parse artifact changed before selection")
            path = self.sources.owned_path(self.root / "selected" / f"{version.id}.json")
            with mutation_scope(self.kb_dir, [path], operation="select source parsing"):
                atomic_write_json(path, {"parse_id": parsed.id})

    def selected(self, version: SourceVersion) -> ParseVersion | None:
        with kb_read_lock(self.kb_dir / ".openkb"):
            path = self.sources.owned_path(self.root / "selected" / f"{version.id}.json")
            if not path.exists():
                return None
            record = read_object(path)
            if set(record) != {"parse_id"}:
                raise ValueError("Invalid selected parsing view")
            parsed = self.load(record["parse_id"])
            self._bind(version, parsed)
            return parsed

    def save(
        self,
        version: SourceVersion,
        profile: dict[str, Any],
        drafts: list[BlockDraft],
        *,
        quality: list[dict[str, Any]] | None = None,
    ) -> ParseVersion:
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            self.sources.original(version)
            blocks = []
            for order, draft in enumerate(drafts):
                for asset in draft.assets:
                    self.sources.asset(asset)
                block_payload: dict[str, Any] = {
                    "order": order,
                    "blob": self.sources.put_bytes(draft.text.encode("utf-8")),
                    "chars": len(draft.text),
                    "kind": draft.kind,
                    "location": draft.location,
                    "assets": draft.assets,
                    "context": draft.context,
                    **(
                        {"context_data": draft.context_data}
                        if draft.context_data is not None
                        else {}
                    ),
                }
                blocks.append(Block(id=content_id(block_payload), **block_payload))
            lookup_key = content_id({"input": version.input_key, "profile": profile})
            payload: dict[str, Any] = {
                "input_key": version.input_key,
                "lookup_key": lookup_key,
                "profile": profile,
                "blocks": tuple(blocks),
                "quality": quality or [],
            }
            serialized = {**payload, "blocks": [block_record(block) for block in blocks]}
            parsed = ParseVersion(id=content_id(serialized), **payload)
            artifact = self.sources.owned_path(self.root / f"{parsed.id}.json")
            lookup = self.sources.owned_path(self.root / "lookup" / f"{lookup_key}.json")
            if artifact.exists() and self.load(parsed.id) != parsed:
                raise ValueError("Immutable parse artifact changed")
            with mutation_scope(self.kb_dir, [artifact, lookup], operation="parse checkpoint"):
                if not artifact.exists():
                    atomic_write_json(artifact, parse_record(parsed))
                atomic_write_json(lookup, {"parse_id": parsed.id})
            return parsed

    def load(self, parse_id: str) -> ParseVersion:
        with kb_read_lock(self.kb_dir / ".openkb"):
            value = read_object(self.sources.owned_path(self.root / f"{valid_id(parse_id)}.json"))
            if not isinstance(value.get("blocks"), list):
                raise ValueError("Invalid parse blocks")
            blocks = []
            for row in value["blocks"]:
                if not isinstance(row, dict) or not isinstance(row.get("assets"), list):
                    raise ValueError("Invalid block manifest")
                blocks.append(Block(**{**row, "assets": tuple(row["assets"])}))
            parsed = ParseVersion(**{**value, "blocks": tuple(blocks)})
            if parsed.id != parse_id:
                raise ValueError("Parse identity mismatch")
            return parsed

    def find(self, version: SourceVersion, profile: dict[str, Any]) -> ParseVersion | None:
        key = content_id({"input": version.input_key, "profile": profile})
        with kb_read_lock(self.kb_dir / ".openkb"):
            path = self.sources.owned_path(self.root / "lookup" / f"{key}.json")
            if not path.exists():
                return None
            lookup = read_object(path)
            if set(lookup) != {"parse_id"}:
                raise ValueError("Invalid parse lookup record")
            parsed = self.load(lookup["parse_id"])
            if parsed.lookup_key != key:
                raise ValueError("Parse cache input mismatch")
            for block in parsed.blocks:
                self.sources.asset(block.blob)
                for asset in block.assets:
                    self.sources.asset(asset)
            return parsed

    def _bind(self, version: SourceVersion, parsed: ParseVersion) -> None:
        if self.sources.version(version.id) != version or parsed.input_key != version.input_key:
            raise ValueError("Parse input does not match the source version")

    def reader(self, version: SourceVersion, parsed: ParseVersion) -> EvidenceReader:
        """Validate a fixed parsing manifest once for many bounded evidence reads."""
        with kb_read_lock(self.kb_dir / ".openkb"):
            stored = self.load(parsed.id)
            self._bind(version, stored)
            if stored != parsed:
                raise ValueError("Parse manifest changed")
            self.sources.original(version)
            return EvidenceReader(self.sources, version, stored)

    def read(self, reference: Evidence, *, max_chars: int) -> EvidenceSlice:
        with kb_read_lock(self.kb_dir / ".openkb"):
            version = self.sources.version(reference.version_id)
            parsed = self.load(reference.parse_id)
            self._bind(version, parsed)
            return EvidenceReader(self.sources, version, parsed).read(
                reference, max_chars=max_chars
            )

    def _confirmation_path(self, version: SourceVersion, parsed: ParseVersion, page: int) -> Path:
        return self.sources.owned_path(
            self.root / "confirmations" / f"{content_id([version.id, parsed.id, page])}.json"
        )

    def confirm_page(
        self, version: SourceVersion, parsed: ParseVersion, page: int, reason: str
    ) -> None:
        """Record an explicit human decision for exactly this source/parse/page."""
        if reason not in {"legitimate_blank", "legitimate_illustration"}:
            raise ValueError("Invalid page confirmation")
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            if not any(
                row.get("page") == page and row["status"] == "needs_review"
                for row in parsed.quality
            ):
                raise ValueError("Page has no pending quality review")
            if any(
                row.get("page") == page and row["reason"] not in _CONFIRMABLE
                for row in parsed.quality
            ):
                raise ValueError(
                    "This page requires reprocessing; "
                    "a blank/illustration decision cannot repair it"
                )
            path = self._confirmation_path(version, parsed, page)
            with mutation_scope(self.kb_dir, [path], operation="confirm source page"):
                atomic_write_json(
                    path,
                    {"version": version.id, "parse": parsed.id, "page": page, "reason": reason},
                )

    def _validate_artifacts(self, version: SourceVersion, parsed: ParseVersion) -> None:
        self._bind(version, parsed)
        if self.load(parsed.id) != parsed:
            raise ValueError("Parse manifest changed")
        self.sources.original(version)
        for asset in version.assets.values():
            if asset is not None:
                self.sources.asset(asset)
        for block in parsed.blocks:
            content = self.sources.asset(block.blob)
            with content.open(encoding="utf-8", newline="") as stream:
                length = sum(len(chunk) for chunk in iter(lambda: stream.read(8192), ""))
            if length != block.chars:
                raise ValueError("Content block length mismatch")
            for asset in block.assets:
                self.sources.asset(asset)

    def complete(self, version: SourceVersion, parsed: ParseVersion) -> bool:
        with kb_read_lock(self.kb_dir / ".openkb"):
            self._validate_artifacts(version, parsed)
            if any(asset is None for asset in version.assets.values()):
                return False
            accepted_missing = self.accepted_missing_images(version, parsed)
            for row in parsed.quality:
                if row["status"] == "verified":
                    continue
                if row["reason"] in accepted_missing:
                    continue
                page = row.get("page")
                if page is None:
                    return False
                if (
                    row["reason"] not in _CONFIRMABLE
                    or self.page_decision(version, parsed, page) is None
                ):
                    return False
            return True

    def compilable(self, version: SourceVersion, parsed: ParseVersion) -> bool:
        """Permit publication with omissions, including no usable knowledge at all.

        Parser quality diagnostics describe missing content. They cannot waive
        corrupt identities, changed originals or missing stored evidence bytes.
        """
        with kb_read_lock(self.kb_dir / ".openkb"):
            self._validate_artifacts(version, parsed)
            return True

    def _missing_images(self, version: SourceVersion, parsed: ParseVersion) -> list[str]:
        if version.suffix != ".docx":
            return []
        return sorted(
            {
                row["reason"]
                for row in parsed.quality
                if row["status"] == "needs_review"
                and (
                    row["reason"] == "docx_image_asset_missing"
                    or (
                        row["reason"].startswith("docx_attachment:")
                        and row["reason"].endswith(":docx_image_asset_missing")
                    )
                )
            }
        )

    def _missing_image_decision(self, version: SourceVersion, parsed: ParseVersion) -> Path:
        return self.sources.owned_path(
            self.root
            / "confirmations"
            / f"{content_id([version.id, parsed.id, 'missing_images'])}.json"
        )

    def accept_missing_images(self, version: SourceVersion, parsed: ParseVersion) -> None:
        """Record explicit permission to continue with named missing-image markers."""
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            reasons = self._missing_images(version, parsed)
            if self.load(parsed.id) != parsed or not reasons:
                raise ValueError("No matching missing-image review")
            path = self._missing_image_decision(version, parsed)
            with mutation_scope(self.kb_dir, [path], operation="accept missing original images"):
                atomic_write_json(
                    path,
                    {
                        "source": version.id,
                        "parse": parsed.id,
                        "reasons": reasons,
                        "decision": "continue_with_missing_original_images",
                    },
                )

    def accepted_missing_images(self, version: SourceVersion, parsed: ParseVersion) -> list[str]:
        with kb_read_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            reasons = self._missing_images(version, parsed)
            path = self._missing_image_decision(version, parsed)
            if not reasons or not path.exists():
                return []
            record = read_object(path)
            if record != {
                "source": version.id,
                "parse": parsed.id,
                "reasons": reasons,
                "decision": "continue_with_missing_original_images",
            }:
                raise ValueError("Missing-image decision does not match this parse")
            return reasons

    def page_decision(self, version: SourceVersion, parsed: ParseVersion, page: int) -> str | None:
        with kb_read_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            path = self._confirmation_path(version, parsed, page)
            if not path.exists():
                return None
            record = read_object(path)
            if record not in [
                {"version": version.id, "parse": parsed.id, "page": page, "reason": reason}
                for reason in ("legitimate_blank", "legitimate_illustration")
            ]:
                raise ValueError("Page confirmation input mismatch")
            return record["reason"]


class EvidenceReader:
    """Read an immutable validated manifest without rescanning it for every span."""

    def __init__(self, sources: SourceStore, version: SourceVersion, parsed: ParseVersion):
        self.sources, self.version, self.parsed = sources, version, parsed
        self.blocks = {block.id: block for block in parsed.blocks}

    def preceding(self, reference, *, max_blocks, max_chars):
        from openkb.evidence_context import preceding_slices

        return preceding_slices(
            self,
            reference,
            (self.version.source_id, self.version.id, self.parsed.id),
            self.blocks,
            max_blocks=max_blocks,
            max_chars=max_chars,
        )

    def complete_bound(self, reference):
        return complete_read_bound(self.blocks[reference.block_id])

    def read(self, reference: Evidence, *, max_chars: int) -> EvidenceSlice:
        if type(max_chars) is not int or max_chars <= 0:
            raise ValueError("Evidence reads require a positive bound")
        with kb_read_lock(self.sources.kb_dir / ".openkb"):
            block, end = evidence_bounds(
                reference,
                (self.version.source_id, self.version.id, self.parsed.id),
                self.blocks,
                max_chars,
            )
            with self.sources.asset(block.blob).open(encoding="utf-8", newline="") as source:
                remaining = reference.start
                while remaining:
                    skipped = source.read(min(remaining, 8192))
                    if not skipped:
                        raise ValueError("Evidence span is missing")
                    remaining -= len(skipped)
                text = source.read(min(max_chars, end - reference.start))
            following = reference.start + len(text)
            for asset in block.assets:
                self.sources.asset(asset)
            return EvidenceSlice(
                reference,
                text,
                block.location,
                block.kind,
                block.assets,
                block.context,
                following if following < end else None,
                copy.deepcopy(block.context_data),
                block.chars,
            )


def evidence_bounds(reference, identity, blocks, max_chars):
    """Apply the same identity, metadata and span bounds to live and snapshot reads."""
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("Evidence reads require a positive bound")
    if (
        reference.source_id != identity[0]
        or reference.version_id != identity[1]
        or reference.parse_id != identity[2]
    ):
        raise ValueError("Evidence source mismatch")
    block = blocks.get(reference.block_id)
    if block is None:
        raise ValueError("Evidence block is missing")
    # Metadata is also output, not an escape hatch around bounded reads.
    # A caller can explicitly request a larger window to include a long
    # table header; do not silently drop required context.
    metadata_size = (
        context_size(block)
        + len(json.dumps(block.location, ensure_ascii=False))
        + 64 * len(block.assets)
    )
    if metadata_size > max(4096, max_chars):
        raise ValueError("Evidence context exceeds the read bound; request a larger window")
    end = reference.end if reference.end is not None else block.chars
    if reference.start > end or end > block.chars:
        raise ValueError("Evidence span exceeds its block")
    return block, end
