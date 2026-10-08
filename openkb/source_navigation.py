"""Read published PageIndex navigation and identity without indexing again."""

from pathlib import Path

from openkb.condb_storage import ConDBPageIndexStorage
from openkb.index_location import IndexLocation


def stored_navigation(index_root: Path, doc_id: str) -> list[dict]:
    if not (index_root / "index.json").is_file():
        return []
    with ConDBPageIndexStorage(IndexLocation.package(index_root)) as storage:
        for collection in storage.list_collections():
            structure = storage.get_document_structure(collection, doc_id)
            if structure:
                return structure
    return []


def published_navigation(kb_dir: Path, view_id: str, doc_name: str) -> dict:
    """Caller holds the KB lease; compiler child tasks must not reacquire it."""
    from openkb.ingest_records import ImportUnit, KnowledgeRevision, UnitPublication, UnitRevision
    from openkb.source_catalog import read_record
    from openkb.unit_publication import publication_id, read_head

    for identity in read_head(kb_dir, view_id).inputs.values():
        revision = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
        unit = read_record(kb_dir, "units", revision.unit_id, ImportUnit)
        if unit.doc_name != doc_name:
            continue
        published = read_record(
            kb_dir, "publications", publication_id(unit.unit_id, view_id), UnitPublication
        )
        if not published or published.successful_revision_id != identity:
            return {}
        directory = (
            kb_dir
            / ".openkb/knowledge"
            / view_id
            / "revisions"
            / str(published.knowledge_revision_id)
        )
        manifest = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_bytes())
        return {
            "unit_id": unit.unit_id,
            "unit_revision_id": identity,
            "source_revision_id": revision.source_revision_id,
            "knowledge_revision_id": manifest.knowledge_revision_id,
            "structure": stored_navigation(directory / "index", manifest.index_ref)
            if manifest.index_ref
            else [],
        }
    return {}
