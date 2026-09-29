"""Document inventory and original reading from committed source/unit records."""

from __future__ import annotations

import json
from pathlib import Path

from openkb.file_state import contained_paths
from openkb.ingest_records import KnowledgeRevision, UnitPublication, UnitRevision
from openkb.locks import kb_read_lock
from openkb.source_catalog import list_sources, read_record, read_source_revision
from openkb.unit_publication import list_source_units, read_unit_publication


def _publication(kb_dir: Path, unit_id: str) -> UnitPublication | None:
    try:
        return read_unit_publication(kb_dir, unit_id)
    except FileNotFoundError:
        return None


def _manifest(kb_dir: Path, state: UnitPublication) -> tuple[Path, KnowledgeRevision] | None:
    if state.knowledge_revision_id is None:
        return None
    directory = (
        kb_dir / ".openkb/knowledge" / state.view_id / "revisions" / state.knowledge_revision_id
    )
    contained_paths(kb_dir, [directory])
    manifest = KnowledgeRevision.model_validate_json(
        (directory / "manifest.json").read_text("utf-8")
    )
    if (
        manifest.knowledge_revision_id != state.knowledge_revision_id
        or manifest.view_id != state.view_id
        or manifest.unit_revision_id != state.successful_revision_id
    ):
        raise ValueError("Knowledge revision does not match its publication")
    return directory, manifest


def _historical_manifest(
    kb_dir: Path, state: UnitPublication, source_revision_id: str
) -> tuple[Path, KnowledgeRevision] | None:
    """Follow committed ancestry, never substitute today's normalized source."""
    identity = state.knowledge_revision_id
    visited = set()
    while identity:
        if identity in visited:
            raise ValueError("Knowledge revision ancestry contains a cycle")
        visited.add(identity)
        directory = kb_dir / ".openkb/knowledge" / state.view_id / "revisions" / identity
        contained_paths(kb_dir, [directory])
        manifest = KnowledgeRevision.model_validate_json(
            (directory / "manifest.json").read_text("utf-8")
        )
        if manifest.knowledge_revision_id != identity or manifest.view_id != state.view_id:
            raise ValueError("Knowledge revision does not match its location")
        if manifest.unit_revision_id:
            used = read_record(kb_dir, "unit-revisions", manifest.unit_revision_id, UnitRevision)
            if used.unit_id == state.unit_id and used.source_revision_id == source_revision_id:
                return directory, manifest
        identity = manifest.base_revision_id
    return None


def source_inventory(kb_dir: Path) -> list[dict]:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        documents = []
        for source in list_sources(root):
            if source.removed:
                continue
            revision = read_source_revision(root, source.target_revision_id)
            units = list_source_units(root, source.source_id)
            states = [state for unit in units if (state := _publication(root, unit.unit_id))]
            actual = _manifest(root, states[0]) if states else None
            documents.append(
                {
                    "hash": source.source_id,
                    "source_id": source.source_id,
                    "legacy_hash": source.legacy_hash,
                    "source_revision_id": revision.source_revision_id,
                    "name": source.name,
                    "type": revision.source_format,
                    "doc_name": source.doc_name,
                    "display_type": "pageindex"
                    if actual and actual[1].execution_mode == "segmented"
                    else "short",
                    "status": states[0].status if states else "admitted",
                    "units": [state.model_dump(mode="json") for state in states],
                    "message": states[0].message if states else None,
                    "pages": None,
                    "original_path": revision.original,
                }
            )
        return documents


def read_admitted_source(
    kb_dir: Path, identifier: str, *, source_revision_id: str | None = None
) -> dict | None:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        sources = list_sources(root)
        source = next((source for source in sources if source.source_id == identifier), None)
        if source is None:
            return None
        units = list_source_units(root, source.source_id)
        state = _publication(root, units[0].unit_id) if units else None
        target = read_source_revision(root, source_revision_id or source.target_revision_id)
        if target.source_id != source.source_id:
            raise ValueError("Source revision belongs to another document")
        actual = _manifest(root, state) if state else None
        # The current source body belongs to its last successful input, even if a
        # newer input failed. Explicit history never falls back to different bytes.
        if actual and state and state.successful_revision_id:
            used = read_record(root, "unit-revisions", state.successful_revision_id, UnitRevision)
            if used.unit_id != state.unit_id:
                raise ValueError("Successful revision belongs to another unit")
            if source_revision_id is None or source_revision_id == used.source_revision_id:
                target = read_source_revision(root, used.source_revision_id)
                if target.source_id != source.source_id:
                    raise ValueError("Successful source revision belongs to another document")
            else:
                actual = _historical_manifest(root, state, source_revision_id)
        content = (
            state.message
            if state and state.message
            else "Frozen original retained; no published body for this revision."
        )
        pages = None
        base_path = root / target.original
        contained_paths(root, [base_path])
        artifact_dir = root / ".openkb/artifacts" / target.digest
        if base_path.resolve().parent != artifact_dir or artifact_dir.resolve() != artifact_dir:
            raise ValueError("Frozen original moved outside its artifact directory")
        if actual:
            directory, manifest = actual
            if manifest.normalized_source is None:
                raise ValueError("Published source revision is missing its body reference")
            base_path = directory / "wiki" / manifest.normalized_source
            contained_paths(root, [base_path])
            if not base_path.resolve().is_relative_to(directory / "wiki"):
                raise ValueError("Source body is outside its knowledge snapshot")
            if base_path.suffix == ".json":
                from openkb.documents import _render_pages

                page_list = json.loads(base_path.read_text("utf-8"))
                if not isinstance(page_list, list):
                    raise ValueError("Stored source must contain a page list")
                content = _render_pages(page_list)
                pages = len(page_list)
            else:
                content = base_path.read_text("utf-8")
        return {
            "hash": source.source_id,
            "source_id": source.source_id,
            "original_kind": target.original_kind,
            "source_revision_id": target.source_revision_id,
            "target_source_revision_id": source.target_revision_id,
            "knowledge_revision_id": actual[1].knowledge_revision_id if actual else None,
            "name": source.name,
            "doc_name": source.doc_name,
            "type": target.source_format,
            "format": "markdown",
            "content": content,
            "pages": pages,
            "original_path": target.original,
            "base_path": base_path.parent.relative_to(root).as_posix(),
            "status": state.status if state else "admitted",
            "message": state.message if state else None,
            "error_type": state.error_type if state else None,
        }
