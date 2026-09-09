"""Immutable parsing checkpoints and bounded, version-bound source evidence."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openkb.locks import atomic_write_json, kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.sources import SourceStore, SourceVersion, content_id, read_object, valid_id

_CONFIRMABLE = {"blank_or_illustration", "ocr_blank_or_illustration", "image_content_requires_ocr"}


def validate_location(location: dict[str, Any]) -> None:
    if not isinstance(location, dict) or location.get("kind") not in {
        "pdf",
        "docx",
        "text",
        "converted",
    }:
        raise ValueError("Invalid source location")
    allowed = {"kind", "page", "paragraph", "table", "row", "cell", "line", "headings", "bbox"}
    if set(location) - allowed:
        raise ValueError("Unknown source location field")
    for key in ("page", "paragraph", "table", "row", "cell", "line"):
        if key in location and (type(location[key]) is not int or location[key] < 1):
            raise ValueError("Invalid source position")
    if location["kind"] == "pdf" and "page" not in location:
        raise ValueError("PDF evidence needs a physical page")
    if location["kind"] != "pdf" and "page" in location:
        raise ValueError("Only PDF evidence has a physical page")
    if "headings" in location and (
        not isinstance(location["headings"], list)
        or not all(isinstance(item, str) for item in location["headings"])
    ):
        raise ValueError("Invalid heading path")
    if "bbox" in location and (
        not isinstance(location["bbox"], list)
        or len(location["bbox"]) != 4
        or not all(type(item) in {float, int} and math.isfinite(item) for item in location["bbox"])
        or location["bbox"][0] > location["bbox"][2]
        or location["bbox"][1] > location["bbox"][3]
    ):
        raise ValueError("Invalid source coordinates")


@dataclass(frozen=True)
class BlockDraft:
    text: str
    kind: str
    location: dict[str, Any]
    assets: tuple[str, ...] = ()
    context: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.context, str):
            raise ValueError("Invalid parsed text")
        if self.kind not in {"heading", "paragraph", "table", "code", "image"}:
            raise ValueError("Invalid content block kind")
        validate_location(self.location)
        if not isinstance(self.assets, tuple):
            raise ValueError("Invalid block assets")
        for item in self.assets:
            valid_id(item)


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
        BlockDraft("", self.kind, self.location, self.assets, self.context)
        payload = asdict(self)
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
            if not isinstance(row, dict) or set(row) - {"page", "block", "status", "reason"}:
                raise ValueError("Invalid quality record")
            if row.get("status") not in {"verified", "needs_review"} or not isinstance(
                row.get("reason"), str
            ):
                raise ValueError("Invalid parsing quality status")
            if "page" in row and (type(row["page"]) is not int or row["page"] < 1):
                raise ValueError("Invalid quality page")
            if "block" in row and row["block"] not in {block.id for block in self.blocks}:
                raise ValueError("Invalid quality block")
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
        payload = asdict(self)
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
            serialized = {**payload, "blocks": [asdict(block) for block in blocks]}
            parsed = ParseVersion(id=content_id(serialized), **payload)
            artifact = self.sources.owned_path(self.root / f"{parsed.id}.json")
            lookup = self.sources.owned_path(self.root / "lookup" / f"{lookup_key}.json")
            if artifact.exists() and self.load(parsed.id) != parsed:
                raise ValueError("Immutable parse artifact changed")
            with mutation_scope(self.kb_dir, [artifact, lookup], operation="parse checkpoint"):
                if not artifact.exists():
                    atomic_write_json(artifact, asdict(parsed))
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

    def read(self, reference: Evidence, *, max_chars: int) -> EvidenceSlice:
        if type(max_chars) is not int or max_chars <= 0:
            raise ValueError("Evidence reads require a positive bound")
        with kb_read_lock(self.kb_dir / ".openkb"):
            version = self.sources.version(reference.version_id)
            if version.source_id != reference.source_id:
                raise ValueError("Evidence source mismatch")
            parsed = self.load(reference.parse_id)
            self._bind(version, parsed)
            block = next((row for row in parsed.blocks if row.id == reference.block_id), None)
            if block is None:
                raise ValueError("Evidence block is missing")
            # Metadata is also output, not an escape hatch around bounded reads.
            # A caller can explicitly request a larger window to include a long
            # table header; do not silently drop required context.
            context_size = (
                len(block.context)
                + len(json.dumps(block.location, ensure_ascii=False))
                + 64 * len(block.assets)
            )
            if context_size > max(4096, max_chars):
                raise ValueError("Evidence context exceeds the read bound; request a larger window")
            end = reference.end if reference.end is not None else block.chars
            if reference.start > end or end > block.chars:
                raise ValueError("Evidence span exceeds its block")
            with self.sources.asset(block.blob).open(encoding="utf-8") as source:
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

    def complete(self, version: SourceVersion, parsed: ParseVersion) -> bool:
        with kb_read_lock(self.kb_dir / ".openkb"):
            self._bind(version, parsed)
            self.sources.original(version)
            for asset in version.assets.values():
                if asset is None:
                    return False
                self.sources.asset(asset)
            for block in parsed.blocks:
                content = self.sources.asset(block.blob)
                with content.open(encoding="utf-8") as stream:
                    length = sum(len(chunk) for chunk in iter(lambda: stream.read(8192), ""))
                if length != block.chars:
                    raise ValueError("Content block length mismatch")
                for asset in block.assets:
                    self.sources.asset(asset)
            for row in parsed.quality:
                if row["status"] == "verified":
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
