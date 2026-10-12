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


def planning_navigation(structure: list[dict]) -> list[dict]:
    """Expose retained topic locators and hierarchy without repeating subtrees."""
    result: list[dict] = []

    def visit(nodes: list[dict], parent_id: str | None = None) -> None:
        for node in nodes:
            entry = {
                key: node[key]
                for key in ("node_id", "title", "start_index", "end_index")
                if key in node
            }
            summary = node.get("summary")
            if isinstance(summary, str) and summary.strip():
                entry["navigation_summary"] = summary
            if parent_id is not None:
                entry["parent_node_id"] = parent_id
            result.append(entry)
            visit(node.get("nodes", []), node.get("node_id", parent_id))

    visit(structure)
    return result


def published_navigation(
    kb_dir: Path,
    view_id: str,
    doc_name: str,
    *,
    knowledge_revision_id: str | None = None,
    source_revision_id: str | None = None,
) -> dict:
    """Caller holds the KB lease; compiler child tasks must not reacquire it."""
    from openkb.ingest_records import ImportUnit, KnowledgeRevision, UnitPublication, UnitRevision
    from openkb.source_catalog import read_record
    from openkb.unit_publication import publication_id, read_head

    if knowledge_revision_id:
        # Follow the selected snapshot's publication chain, including retained
        # historical originals. A live head cannot supply another revision's tree.
        seen = set()
        while knowledge_revision_id and knowledge_revision_id not in seen:
            seen.add(knowledge_revision_id)
            directory = kb_dir / ".openkb/knowledge" / view_id / "revisions" / knowledge_revision_id
            manifest = KnowledgeRevision.model_validate_json(
                (directory / "manifest.json").read_bytes()
            )
            if (
                manifest.knowledge_revision_id != knowledge_revision_id
                or manifest.view_id != view_id
            ):
                raise ValueError("Navigation snapshot belongs to another publication")
            if manifest.unit_revision_id:
                revision = read_record(
                    kb_dir, "unit-revisions", manifest.unit_revision_id, UnitRevision
                )
                unit = read_record(kb_dir, "units", revision.unit_id, ImportUnit)
                if unit.doc_name == doc_name and revision.source_revision_id == source_revision_id:
                    return {
                        "unit_id": unit.unit_id,
                        "unit_revision_id": revision.unit_revision_id,
                        "source_revision_id": revision.source_revision_id,
                        "knowledge_revision_id": manifest.knowledge_revision_id,
                        "structure": stored_navigation(directory / "index", manifest.index_ref)
                        if manifest.index_ref
                        else [],
                    }
            knowledge_revision_id = manifest.base_revision_id
        return {}
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
