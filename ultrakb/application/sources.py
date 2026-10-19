"""Document inventory and original reading from committed source/unit records."""

from __future__ import annotations

from pathlib import Path

from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


def source_inventory(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> list[dict]:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    from .storage_inventory import source_inventory as stored_inventory

    return stored_inventory(scope)


def source_status(states, *, expected_count=0):
    statuses = {state.status for state in states}
    if not statuses:
        return "admitted"
    if len(states) < expected_count:
        statuses.add("pending")
    if len(statuses) == 1:
        return next(iter(statuses))
    finished = {"completed", "empty", "retired"}
    if statuses <= finished:
        return "completed"
    return "partial" if statuses & finished else "failed" if "failed" in statuses else "blocked"


def read_admitted_source(
    kb_dir: Path,
    identifier: str,
    *,
    source_revision_id: str | None = None,
    unit_id: str | None = None,
    scope: KnowledgeScope | None = None,
    page_range: str | None = None,
    char_range: str | None = None,
    block_range: str | None = None,
) -> dict | None:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from ultrakb.source_pages import validate_source_ranges

    validate_source_ranges(page_range, char_range, block_range)
    kb_dir.resolve()
    from .storage_sources import read_stored_source

    return read_stored_source(
        scope,
        identifier,
        unit_id=unit_id,
        source_revision_id=source_revision_id,
        pages=page_range,
        chars=char_range,
        blocks=block_range,
    )
