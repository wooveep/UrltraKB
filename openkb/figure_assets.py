"""Freeze figure bytes by content, retaining each source occurrence independently."""

import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import quote

from PIL import Image
from pydantic import Field, model_validator

from openkb.agent.page_response import PageResponseError
from openkb.compile_checkpoints import digest
from openkb.locks import atomic_write_bytes, atomic_write_json
from openkb.markdown_images import ImageReference
from openkb.source_contract import page_images
from openkb.source_qualification import source_images
from openkb.source_records import Digest, Record, RecordId, RelativePath


class FigureOccurrence(Record):
    occurrence_id: Digest
    source_path: RelativePath
    source_digest: Digest
    source_revision_id: RecordId | None = None
    processing_revision_id: RecordId | None = None
    location: dict[str, int]
    ordinal: int = Field(ge=0)
    original_path: RelativePath
    asset_path: RelativePath
    asset_digest: Digest
    caption: str = ""
    reference_span: list[int] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def native_location(self):
        if self.reference_span is not None and not (
            "page" in self.location
            and len(self.reference_span) == 2
            and 0 <= self.reference_span[0] < self.reference_span[1]
        ):
            raise ValueError("A physical image reference requires a nonempty native span")
        if set(self.location) == {"page"} and self.location["page"] >= 1:
            return self
        if (
            set(self.location) == {"start", "end"}
            and 0 <= self.location["start"] < self.location["end"]
        ):
            return self
        raise ValueError("Figure location requires a positive page or nonempty character span")


def validate_figure_source(raw):
    """Use the native readers' shape checks before deriving figure positions."""
    if isinstance(raw, list):
        from openkb.source_pages import read_page_selection

        read_page_selection(raw)
    elif isinstance(raw, dict):
        from openkb.text_source import FrozenText

        FrozenText.model_validate_json(json.dumps(raw))
    elif not isinstance(raw, str):
        raise ValueError("Expected native pages or a frozen text source")


def asset_path(data: bytes) -> str:
    """Determine extension from the bytes, never from an author-supplied filename."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            kind = (image.format or "").lower()
            image.verify()
        extension = {"jpeg": "jpg"}.get(kind, kind)
        if extension not in {"png", "jpg", "gif", "webp", "bmp", "tiff", "ico"}:
            raise ValueError("Unsupported image format")
    except Exception as error:
        from defusedxml.ElementTree import fromstring

        try:
            if fromstring(data).tag.split("}")[-1] != "svg":
                raise ValueError("Not an SVG image")
            extension = "svg"
        except Exception:
            raise PageResponseError("figure_invalid_asset", "Retained image is corrupt") from error
    return f"sources/figure-assets/{hashlib.sha256(data).hexdigest()}.{extension}"


def relative_figure(wiki: Path, page: str, asset: str) -> str:
    root = wiki.resolve()
    target = (root / asset).resolve()
    origin = (root / page).resolve()
    if not target.is_relative_to(root) or not origin.is_relative_to(root):
        raise PageResponseError("figure_invalid_path", "Figure reference escapes the Wiki")
    return quote(os.path.relpath(target, origin.parent), safe="/")


def verify_figure(wiki: Path, figure: FigureOccurrence) -> None:
    target = (wiki / figure.asset_path).resolve()
    if not target.is_relative_to(wiki.resolve()) or not target.is_file():
        raise PageResponseError("figure_missing_asset", f"Missing original: {figure.asset_path}")
    if hashlib.sha256(target.read_bytes()).hexdigest() != figure.asset_digest:
        raise PageResponseError("figure_changed_asset", f"Changed original: {figure.asset_path}")


def source_identity(kb_dir: Path | None, doc_name: str, *, inputs=None) -> dict:
    """Reuse existing processing/source identities; legacy fixtures have no such IDs."""
    if kb_dir is None:
        return {}
    from openkb.ingest_records import ImportUnit, UnitRevision
    from openkb.source_catalog import read_record

    matches = []
    for path in (kb_dir / ".openkb/catalog/units").glob("*.json"):
        unit = read_record(kb_dir, "units", path.stem, ImportUnit)
        if unit.doc_name == doc_name:
            identity = inputs.get(unit.unit_id) if inputs is not None else unit.target_revision_id
            if identity:
                revision = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
                matches.append(
                    {
                        "source_revision_id": revision.source_revision_id,
                        "processing_revision_id": revision.unit_revision_id,
                    }
                )
    if len(matches) > 1:
        raise PageResponseError("figure_ambiguous_source", "Multiple processing units match")
    return matches[0] if matches else {}


def freeze_figures(
    wiki: Path, source_path: str, raw, identity: dict, *, destination: Path | None = None
) -> list[FigureOccurrence]:
    """Repair only references to existing assets; never re-extract or edit frozen text."""
    source_digest = hashlib.sha256((wiki / source_path).read_bytes()).hexdigest()
    validate_figure_source(raw)
    if isinstance(raw, str):
        # A bare legacy normalization has no recorded syntax or occurrence map.
        # Preserve its text, existing assets and historical records without
        # certifying literal references or demanding image reprocessing.
        return []
    destination = destination or wiki
    units = raw if isinstance(raw, list) else [{"content": raw.get("text", "")}]
    assets = raw.get("assets", {}) if isinstance(raw, dict) else {}
    figures = []
    for unit in units:
        refs = (
            page_images(unit)
            if isinstance(raw, list)
            else [ImageReference(**ref) for ref in source_images(raw)]
        )
        entries = []
        if isinstance(raw, list):
            for ordinal, entry in enumerate(unit.get("images", [])):
                matching = refs[ordinal] if ordinal < len(refs) else None
                entries.append(
                    (
                        entry["path"],
                        {"page": unit["page"]},
                        ordinal,
                        entry.get("caption") or "",
                        entry.get("digest"),
                        [matching.start, matching.end] if matching else None,
                    )
                )
        else:
            for ordinal, ref in enumerate(refs):
                if ref.source.startswith("data:image/"):
                    from openkb.processing_policy import ReprocessingRequired

                    raise ReprocessingRequired(
                        f"{source_path}: inline image needs explicit reprocess; "
                        "frozen text retained"
                    )
                if ref.local_path is None:
                    continue
                path = (
                    ref.local_path
                    if ref.local_path.startswith("sources/")
                    else f"sources/{ref.local_path}"
                )
                entries.append(
                    (
                        path,
                        {"start": ref.start, "end": ref.end},
                        ordinal,
                        ref.alt,
                        assets.get(path),
                        None,
                    )
                )
        for path, location, ordinal, caption, expected, reference_span in entries:
            original = (wiki / path).resolve()
            if not original.is_relative_to(wiki.resolve()) or not original.is_file():
                from openkb.processing_policy import ReprocessingRequired

                raise ReprocessingRequired(
                    f"{source_path}: missing or unsafe image {path}; preview reprocess explicitly"
                )
            data = original.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if expected and expected != sha:
                raise PageResponseError("figure_changed_asset", f"Frozen image changed: {path}")
            target = asset_path(data)
            if (destination / target).exists() and (destination / target).read_bytes() != data:
                raise PageResponseError(
                    "figure_changed_asset", f"Content address changed: {target}"
                )
            if not (destination / target).exists():
                atomic_write_bytes(destination / target, data)
            basis = {
                "source_path": source_path,
                "source_digest": source_digest,
                **identity,
                "location": location,
                "ordinal": ordinal,
            }
            figures.append(
                FigureOccurrence(
                    **basis,
                    occurrence_id=digest(basis),
                    original_path=path,
                    asset_path=target,
                    asset_digest=sha,
                    caption=caption,
                    reference_span=reference_span,
                )
            )
    if figures:
        key = digest({"source_path": source_path, "source_digest": source_digest, **identity})
        record_target = destination / f"sources/figure-records/{key}.json"
        if not record_target.exists():
            # Current reading requalifies the projection; an existing historical
            # record is never migrated or overwritten as a side effect of reading.
            atomic_write_json(record_target, [f.model_dump(mode="json") for f in figures])
    return figures


def retained_figures(wiki: Path) -> dict[str, FigureOccurrence]:
    result = {}
    for path in sorted((wiki / "sources/figure-records").glob("*.json")):
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, list):
            raise PageResponseError("figure_invalid_record", "Expected figure occurrence list")
        for entry in raw:
            figure = FigureOccurrence.model_validate(entry)
            result[figure.occurrence_id] = figure
    return result
