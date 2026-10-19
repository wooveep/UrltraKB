"""Read exact retained source text and locations from the main store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.source_pages import PageRangeError

if TYPE_CHECKING:
    from ultrakb.application.reading_assets import ReadingAssets


def read_document_source(
    kb_dir: Path,
    file_hash: str,
    *,
    scope: KnowledgeScope | None = None,
    source_revision_id: str | None = None,
    unit_id: str | None = None,
    unit_revision_id: str | None = None,
    cells: str | None = None,
    pages: str | None = None,
    chars: str | None = None,
    blocks: str | None = None,
    part: str | None = None,
) -> dict[str, Any] | None:
    """Return the ingested source text for the document identified by hash.

    Returns ``None`` when the hash is unknown OR its source file is missing,
    so the caller maps both to a 404. ``format`` is always ``"markdown"``;
    ``pages`` is the verified physical page count, or ``None`` when the saved
    source lacks a physical-page map. This is independent of compilation mode.
    """
    from ultrakb.application.sources import read_admitted_source
    from ultrakb.application.storage_sources import source_reading_units
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.source_pages import validate_source_ranges

    validate_source_ranges(pages, chars, blocks)
    scope = resolve_scope(kb_dir, scope)
    if cells is not None and any(value is not None for value in (pages, chars, blocks, part)):
        raise PageRangeError(
            "Select cells separately from pages, characters, blocks or slide parts"
        )
    if unit_revision_id is not None:
        from ultrakb.application.storage_sources import read_retained_input
        from ultrakb.knowledge_scope import resolve_scope

        selected = resolve_scope(kb_dir, scope)
        if selected.historical or selected.knowledge is None:
            raise ValueError("Retained inputs require an explicit current database context")
        admitted = read_retained_input(
            selected.kb_dir,
            file_hash,
            unit_revision_id,
            identity=selected.knowledge.identity,
            pages=pages,
            chars=chars,
            blocks=blocks,
        )
        admitted["view_id"] = selected.view_id
        if (
            unit_id is not None
            and admitted["unit_id"] != unit_id
            or (
                source_revision_id is not None
                and admitted["source_revision_id"] != source_revision_id
            )
        ):
            raise ValueError("The retained input differs from the selected source or unit")
    else:
        admitted = read_admitted_source(
            kb_dir,
            file_hash,
            source_revision_id=source_revision_id,
            unit_id=unit_id,
            scope=scope,
            page_range=pages,
            char_range=chars,
            block_range=blocks,
        )
    if admitted is not None:
        admitted["available_units"] = source_reading_units(scope, file_hash)
        from ultrakb.source_pages import select_page_part
        from ultrakb.workbooks.selection import select_cells

        return select_cells(select_page_part(admitted, part), cells)
    return None


@dataclass(frozen=True)
class DocumentView:
    source: dict[str, Any]
    assets: "ReadingAssets"
    scope: KnowledgeScope


def read_document_view(kb_dir, file_hash, *, scope=None, **selection):
    """Capture source text and immutable image bytes on the application worker."""
    from ultrakb.application.reading_assets import capture_source_assets
    from ultrakb.knowledge_scope import resolve_scope

    scope = resolve_scope(kb_dir, scope)
    source = read_document_source(kb_dir, file_hash, scope=scope, **selection)
    if source is None:
        return None
    return DocumentView(source, capture_source_assets(scope, source), scope)
