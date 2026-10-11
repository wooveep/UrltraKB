"""Read shared-wiki snapshots and retain pre-catalog source evidence."""

from pathlib import Path

from openkb.knowledge_scope import KnowledgeScope, live_scope
from openkb.locks import kb_ingest_lock, kb_read_lock


def map_legacy_sources(kb_dir: Path) -> dict:
    """Record retained legacy evidence without changing wiki pages or invoking a model."""
    from openkb.ingest_diagnostics import failure_reason
    from openkb.legacy_sources import admit_legacy_snapshot
    from openkb.state import HashRegistry

    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        sources, unavailable = [], {}
        for identity, metadata in HashRegistry(root / ".openkb/hashes.json").all_entries().items():
            try:
                if not isinstance(metadata, dict):
                    raise ValueError("Invalid legacy source metadata")
                sources.append(admit_legacy_snapshot(root, identity, metadata).source_id)
            except (OSError, ValueError) as exc:
                unavailable[identity] = failure_reason(exc)
        return {"source_ids": sources, "unavailable": unavailable}


def view_scope(
    kb_dir: Path, view_id: str, *, historical_revision: str | None = None
) -> KnowledgeScope:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        from pydantic import TypeAdapter

        from openkb.source_records import ViewId

        TypeAdapter(ViewId).validate_python(view_id)
        if historical_revision is None:
            return live_scope(root, view_id)
        from openkb.ingest_records import KnowledgeRevision
        from openkb.source_records import RecordId

        TypeAdapter(RecordId).validate_python(historical_revision)
        directory = root / ".openkb/knowledge" / view_id / "revisions" / historical_revision
        scope = KnowledgeScope(root, directory / "wiki", view_id)
        manifest = KnowledgeRevision.model_validate_json(
            (directory / "manifest.json").read_text("utf-8")
        )
        if manifest.view_id != view_id or manifest.knowledge_revision_id != historical_revision:
            raise ValueError("Knowledge revision belongs to another view")
        return scope
