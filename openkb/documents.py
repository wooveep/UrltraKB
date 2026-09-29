"""Read the ingested source text for a document (REST ``/document/source``).

The Documents pane lists ingested docs by hash; this resolves a hash to the
converted full text under ``wiki/sources/``. Short docs are stored as
``<doc_name>.md``; long docs as ``<doc_name>.json`` — a per-page
``list[{page, content, images}]`` (see ``indexer._write_long_doc_artifacts``).
Read-only: sources are ``Do not modify directly`` artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openkb.cli import _LONG_DOC_TYPES
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.source_pages import PageRangeError
from openkb.state import HashRegistry


def _render_pages(pages: list[dict[str, Any]]) -> str:
    from openkb.source_pages import read_page_selection

    return read_page_selection(pages)["content"]


def _resolve_source_file(
    kb_dir: Path, meta: dict, doc_name: str, *, scope: KnowledgeScope | None = None
) -> Path | None:
    """Resolve a document's source file, guarding against path traversal.

    Prefers the registry's stored ``source_path`` (a KB-relative posix path),
    then falls back to the ``wiki/sources/<doc_name>.{md,json}`` convention
    (older entries carry no ``source_path``). Returns ``None`` when nothing
    resolves to an existing file inside ``wiki/sources/``.
    """
    scope = resolve_scope(kb_dir, scope)
    sources_dir = (scope.wiki_dir / "sources").resolve()
    candidates: list[Path] = []
    stored = meta.get("source_path")
    if stored:
        candidates.append(kb_dir / stored)
    # Long docs are stored as ``<doc_name>.json`` (per-page), short docs as
    # ``<doc_name>.md``. Order the by-convention fallbacks by THIS doc's type so
    # two docs sharing a doc_name (one short, one long) each resolve to their
    # own file rather than whichever extension is tried first.
    exts = (".json", ".md") if meta.get("type") in _LONG_DOC_TYPES else (".md", ".json")
    candidates.extend(sources_dir / f"{doc_name}{ext}" for ext in exts)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(sources_dir):
            return resolved
    return None


def read_document_source(
    kb_dir: Path,
    file_hash: str,
    *,
    scope: KnowledgeScope | None = None,
    source_revision_id: str | None = None,
    pages: str | None = None,
    chars: str | None = None,
) -> dict[str, Any] | None:
    """Return the ingested source text for the document identified by hash.

    Returns ``None`` when the hash is unknown OR its source file is missing,
    so the caller maps both to a 404. ``format`` is always ``"markdown"``;
    ``pages`` is the verified physical page count, or ``None`` when the saved
    source lacks a physical-page map. This is independent of compilation mode.
    """
    from openkb.application.sources import read_admitted_source

    admitted = read_admitted_source(
        kb_dir,
        file_hash,
        source_revision_id=source_revision_id,
        scope=scope,
        page_range=pages,
        char_range=chars,
    )
    if admitted is not None:
        return admitted
    if chars is not None:
        raise PageRangeError("Legacy source has no frozen character map")
    scope = resolve_scope(kb_dir, scope)
    if scope.view_id != "legacy":
        return None
    registry = HashRegistry(kb_dir / ".openkb" / "hashes.json")
    meta = registry.get(file_hash)
    if meta is None:
        return None

    doc_name = meta.get("doc_name") or Path(meta.get("name", "")).stem
    source = _resolve_source_file(kb_dir, meta, doc_name, scope=scope)
    if source is None:
        return None

    selection = {}
    if source.suffix == ".json":
        from openkb.source_pages import read_page_selection

        selection = read_page_selection(json.loads(source.read_text(encoding="utf-8")), pages)
        content = selection["content"]
        page_count: int | None = selection["pages"]
    else:
        if pages is not None:
            raise PageRangeError("This retained source has no physical-page map")
        content = source.read_text(encoding="utf-8")
        page_count = None

    return {
        "hash": file_hash,
        "name": meta.get("name", doc_name),
        "doc_name": doc_name,
        "type": meta.get("type", "unknown"),
        "format": "markdown",
        "content": content,
        "pages": page_count,
        "base_path": (scope.wiki_dir if source.suffix == ".json" else source.parent)
        .relative_to(kb_dir.resolve())
        .as_posix(),
        **selection,
    }
