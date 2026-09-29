"""Frozen Markdown coordinates and original-file mappings, without a second parser."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from openkb.source_pages import PageRangeError
from openkb.source_records import Digest, Record, RelativePath
from openkb.text_measurement import MEASUREMENT_FINGERPRINT, measure_markdown

NORMALIZATION_POLICY = "markdown-unicode-preserved-v1:markdown-it-py-4.2.0-commonmark-images"


class TextOrigin(Record):
    normalized_span: tuple[int, int]
    original_span: tuple[int, int]
    coordinate: Literal["unicode_codepoint"] = "unicode_codepoint"
    kind: Literal["identity", "image_reference", "bom"] = "identity"


class FrozenText(Record):
    unit_kind: Literal["text"] = "text"
    text: str
    original_characters: int = Field(ge=0)
    original_digest: Digest
    origins: tuple[TextOrigin, ...]
    assets: dict[RelativePath, Digest]
    tokens: int = Field(ge=0)
    measurement_fingerprint: str = MEASUREMENT_FINGERPRINT
    normalization_policy: str = NORMALIZATION_POLICY
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_mapping(self):
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
            cursor = end
            original_cursor = b
        if cursor != len(self.text) or original_cursor != self.original_characters:
            raise ValueError("Text map does not cover its complete source")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


def freeze_markdown(prepared, doc_name: str, wiki: Path) -> FrozenText:
    from openkb.images import copy_relative_images, extract_base64_images
    from openkb.state import HashRegistry

    original = prepared.path.read_bytes().decode("utf-8")
    images = wiki / "sources/images" / doc_name
    edits: list[tuple[int, int, str]] = []
    extract_base64_images(original, f"{doc_name}/embedded", images / "embedded", edits=edits)
    copy_relative_images(
        original, prepared.source.parent, doc_name, images, prepared=prepared.images, edits=edits
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
        for reference in image_references(original)
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


def read_text_selection(raw: dict, chars: str | None = None) -> dict:
    frozen = FrozenText.model_validate_json(json.dumps(raw))
    start, end = character_range(frozen.text, chars)
    origins = []
    for origin in frozen.origins:
        left, right = max(start, origin.normalized_span[0]), min(end, origin.normalized_span[1])
        if left >= right:
            continue
        original = origin.original_span
        if origin.kind == "identity":
            original = (
                original[0] + left - origin.normalized_span[0],
                original[0] + right - origin.normalized_span[0],
            )
        origins.append(
            {
                **origin.model_dump(mode="json"),
                "normalized_span": [left, right],
                "original_span": list(original),
            }
        )
    return {
        "unit_kind": "text",
        "content": frozen.text[start:end],
        "pages": None,
        "characters": len(frozen.text),
        "tokens": frozen.tokens,
        "char_range": [start, end],
        "source_spans": [[start, end]],
        "origin_locators": origins,
        "normalized_fingerprint": frozen.fingerprint,
        "measurement_fingerprint": frozen.measurement_fingerprint,
        "assets": frozen.assets,
        "diagnostics": list(frozen.diagnostics),
        "coverage": "complete",
    }
