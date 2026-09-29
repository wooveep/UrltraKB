"""Frozen Markdown coordinates and original-file mappings, without a second parser."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from openkb.content_blocks import BLOCK_POLICY, ContentBlock, validate_blocks
from openkb.source_pages import PageRangeError
from openkb.source_records import Digest, EncodingDecision, Record, RelativePath
from openkb.text_measurement import MEASUREMENT_FINGERPRINT, measure_markdown

NORMALIZATION_POLICY = "markdown-unicode-preserved-v1:markdown-it-py-4.2.0-commonmark-images"


class CsvCell(Record):
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    physical_lines: tuple[int, int]
    value: str


class HtmlLocation(Record):
    tag: str
    path: str


class ResourcePolicy(Record):
    download_remote_assets: bool = False
    source: Literal["single", "kb", "global", "default"] = "default"


class SourceResource(Record):
    reference: str
    resolved_reference: str | None = None
    original_span: tuple[int, int]
    kind: Literal["local", "data", "remote"]
    status: Literal["retained", "missing", "not_requested", "failed"]
    path: RelativePath | None = None
    digest: Digest | None = None
    message: str | None = None


class TextOrigin(Record):
    normalized_span: tuple[int, int]
    original_span: tuple[int, int]
    coordinate: Literal["unicode_codepoint"] = "unicode_codepoint"
    kind: Literal[
        "identity", "image_reference", "bom", "generated", "csv_cell", "csv_separator", "html"
    ] = "identity"
    csv: CsvCell | None = Field(default=None, exclude_if=lambda value: value is None)
    html: HtmlLocation | None = Field(default=None, exclude_if=lambda value: value is None)


class FrozenText(Record):
    unit_kind: Literal["text", "block"] = "text"
    text: str
    original_characters: int = Field(ge=0)
    original_digest: Digest
    origins: tuple[TextOrigin, ...]
    assets: dict[RelativePath, Digest]
    tokens: int = Field(ge=0)
    measurement_fingerprint: str = MEASUREMENT_FINGERPRINT
    normalization_policy: str = NORMALIZATION_POLICY
    diagnostics: tuple[str, ...] = ()
    encoding: EncodingDecision | None = Field(default=None, exclude_if=lambda value: value is None)
    resources: tuple[SourceResource, ...] = Field(default=(), exclude_if=lambda value: not value)
    resource_policy: ResourcePolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    blocks: tuple[ContentBlock, ...] = ()
    block_policy: str | None = None

    @model_validator(mode="after")
    def complete_mapping(self):
        for resource in self.resources:
            a, b = resource.original_span
            if not 0 <= a <= b <= self.original_characters:
                raise ValueError("Resource reference is outside the original source")
            if resource.status == "retained":
                if (
                    not resource.path
                    or not resource.digest
                    or self.assets.get(resource.path) != resource.digest
                ):
                    raise ValueError("Retained resource must refer to a frozen verified asset")
            elif resource.path is not None or resource.digest is not None:
                raise ValueError("Unretained resource cannot identify a frozen asset")
        cursor = original_cursor = 0
        for origin in self.origins:
            start, end = origin.normalized_span
            a, b = origin.original_span
            if start != cursor or end < start or end > len(self.text):
                raise ValueError("Text map must cover the frozen text without gaps")
            if not 0 <= a <= b <= self.original_characters or a != original_cursor:
                raise ValueError("Text origin is outside the original file")
            if origin.kind == "identity" and end - start != b - a:
                raise ValueError("Identity text mapping changed length")
            if origin.kind == "generated" and a != b:
                raise ValueError("Generated formatting cannot own original text")
            if (origin.kind == "csv_cell") != (origin.csv is not None):
                raise ValueError("CSV cells must retain their string value and row/column")
            if origin.csv and not 1 <= origin.csv.physical_lines[0] <= origin.csv.physical_lines[1]:
                raise ValueError("Invalid CSV physical line range")
            cursor = end
            original_cursor = b
        if cursor != len(self.text) or original_cursor != self.original_characters:
            raise ValueError("Text map does not cover its complete source")
        if self.unit_kind == "block":
            if self.block_policy != BLOCK_POLICY:
                raise ValueError("Unsupported frozen block policy")
            validate_blocks(self.text, self.blocks)
        elif self.blocks or self.block_policy:
            raise ValueError("Unsegmented text cannot carry a block index")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


def freeze_markdown(
    prepared,
    doc_name: str,
    wiki: Path,
    *,
    original: str | None = None,
    additional_image_edits: list[tuple[int, int, str]] | None = None,
    references=None,
) -> FrozenText:
    from openkb.images import copy_relative_images, extract_base64_images
    from openkb.state import HashRegistry

    if original is None:
        original = prepared.path.read_bytes().decode("utf-8")
    images = wiki / "sources/images" / doc_name
    edits: list[tuple[int, int, str]] = list(additional_image_edits or [])
    extract_base64_images(
        original, f"{doc_name}/embedded", images / "embedded", edits=edits, references=references
    )
    copy_relative_images(
        original,
        prepared.source.parent,
        doc_name,
        images,
        prepared=prepared.images,
        edits=edits,
        references=references,
    )
    if original.startswith("\ufeff"):
        edits.append((0, 1, ""))
    chunks, origins = [], []
    cursor = normalized = 0
    for start, end, replacement in sorted(edits):
        if start < cursor:
            raise ValueError("Overlapping Markdown rewrites")
        unchanged = original[cursor:start]
        if unchanged:
            chunks.append(unchanged)
            origins.append(
                TextOrigin(
                    normalized_span=(normalized, normalized + len(unchanged)),
                    original_span=(cursor, start),
                )
            )
            normalized += len(unchanged)
        chunks.append(replacement)
        origins.append(
            TextOrigin(
                normalized_span=(normalized, normalized + len(replacement)),
                original_span=(start, end),
                kind="image_reference" if replacement else "bom",
            )
        )
        normalized += len(replacement)
        cursor = end
    chunks.append(original[cursor:])
    origins.append(
        TextOrigin(
            normalized_span=(normalized, normalized + len(original) - cursor),
            original_span=(cursor, len(original)),
        )
    )
    text = "".join(chunks)
    assets = {
        path.relative_to(wiki).as_posix(): HashRegistry.hash_file(path)
        for path in images.rglob("*")
        if path.is_file()
    }
    from openkb.markdown_images import image_references

    rewritten = {(start, end) for start, end, _ in edits}
    diagnostics = tuple(
        f"Image not retained: {reference.source[:160]}"
        for reference in (references if references is not None else image_references(original))
        if (reference.start, reference.end) not in rewritten
    )
    return FrozenText(
        text=text,
        original_characters=len(original),
        original_digest=prepared.digest,
        origins=tuple(origins),
        assets=assets,
        tokens=measure_markdown(text),
        diagnostics=diagnostics,
    )


def character_range(text: str, specification: str | None) -> tuple[int, int]:
    if specification is None:
        return 0, len(text)
    if not re.fullmatch(r"[0-9]+:[0-9]+", specification):
        raise PageRangeError("Use a Unicode character range START:END (0-based, end exclusive)")
    start, end = map(int, specification.split(":"))
    if not 0 <= start <= end <= len(text):
        raise PageRangeError("Character range is outside the frozen source")
    return start, end


def text_origins(frozen: FrozenText, start: int, end: int) -> list[dict]:
    """Clip original locations without reparsing a complete text map for each block."""
    return clip_text_origins(
        [
            origin.model_dump(mode="json")
            for origin in frozen.origins
            if origin.normalized_span[0] <= end and origin.normalized_span[1] >= start
        ],
        start,
        end,
    )


def clip_text_origins(locators: list[dict], start: int, end: int) -> list[dict]:
    """The API and desktop narrow the same normalized coordinates and cell metadata."""
    origins = []
    for origin in locators:
        span = origin["normalized_span"]
        left, right = max(start, span[0]), min(end, span[1])
        empty_cell = (
            origin.get("csv") is not None and span[0] == span[1] and start <= span[0] <= end
        )
        if left >= right and not empty_cell:
            continue
        original = origin["original_span"]
        if origin["kind"] == "identity":
            original = (
                original[0] + left - span[0],
                original[0] + right - span[0],
            )
        selected = {
            **origin,
            "normalized_span": [left, right],
            "original_span": list(original),
        }
        if origin.get("csv") and [left, right] != list(span):
            # The original cell location remains usable; do not leak/duplicate
            # a huge complete value in every block or narrow character read.
            selected["csv"] = {key: value for key, value in origin["csv"].items() if key != "value"}
            selected["csv"]["value_complete"] = False
        origins.append(selected)
    return origins


def read_text_selection(raw: dict, chars: str | None = None) -> dict:
    frozen = FrozenText.model_validate_json(json.dumps(raw))
    start, end = character_range(frozen.text, chars)
    return {
        "unit_kind": frozen.unit_kind,
        "block_count": len(frozen.blocks) if frozen.blocks else None,
        "content": frozen.text[start:end],
        "pages": None,
        "characters": len(frozen.text),
        "tokens": frozen.tokens,
        "char_range": [start, end],
        "source_spans": [[start, end]],
        "origin_locators": text_origins(frozen, start, end),
        "normalized_fingerprint": frozen.fingerprint,
        "measurement_fingerprint": frozen.measurement_fingerprint,
        "assets": frozen.assets,
        "diagnostics": list(frozen.diagnostics),
        "encoding": frozen.encoding.model_dump(mode="json") if frozen.encoding else None,
        "resource_policy": frozen.resource_policy.model_dump(
            mode="json", exclude={"schema_version"}
        )
        if frozen.resource_policy
        else None,
        "resources": [resource.model_dump(mode="json") for resource in frozen.resources],
        "coverage": "complete",
    }
