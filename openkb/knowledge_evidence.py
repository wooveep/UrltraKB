"""Read the actual recorded inputs of a page independently of its current validity."""

from pathlib import Path

from openkb.ingest_records import ImportUnit, KnowledgeRevision, UnitRevision
from openkb.knowledge_scope import KnowledgeScope
from openkb.source_catalog import read_record
from openkb.source_records import SourceRevision
from openkb.unit_publication import read_head
from openkb.view_records import VersionAnnotation


def validated_input(
    kb_dir: Path, identity: str, view_id: str
) -> tuple[UnitRevision, SourceRevision, VersionAnnotation | None]:
    """Validate immutable associations, without requiring the input to remain current."""
    unit = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
    named = read_record(kb_dir, "units", unit.unit_id, ImportUnit)
    # SDK tools run in another thread under the caller's exclusive KB lease.
    # These input records are immutable; reacquiring that lease here would deadlock.
    source = read_record(kb_dir, "source-revisions", unit.source_revision_id, SourceRevision)
    if named.source_id != source.source_id:
        raise ValueError("Knowledge input belongs to another source")
    annotation = (
        read_record(kb_dir, "annotations", unit.annotation_id, VersionAnnotation)
        if unit.annotation_id
        else None
    )
    if annotation is None:
        if view_id != "legacy":
            raise ValueError("Versioned knowledge input is missing its annotation")
    elif (
        annotation.source_revision_id != source.source_revision_id
        or annotation.source_id != source.source_id
        or annotation.view_id != view_id
    ):
        raise ValueError("Knowledge annotation belongs to another input or view")
    return unit, source, annotation


def publication_dependencies(kb_dir, head, pages, before, inputs):
    dependencies = {page: tuple(sorted(inputs)) for page in pages if page.endswith(".md")}
    if head.knowledge_revision_id:
        directory = (
            kb_dir / ".openkb/knowledge" / head.view_id / "revisions" / head.knowledge_revision_id
        )
        previous = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
        if previous.view_id != head.view_id or previous.knowledge_revision_id != directory.name:
            raise ValueError("Knowledge evidence belongs to another snapshot")
        for page in dependencies:
            if page in previous.page_dependencies and (pages[page] == before.get(page)):
                dependencies[page] = previous.page_dependencies[page]
    return dependencies


def page_evidence(scope: KnowledgeScope, page: str) -> dict:
    head = read_head(scope.kb_dir, scope.view_id)
    directory = (
        scope.wiki_dir.parent
        if scope.read_only
        else (
            scope.kb_dir
            / ".openkb/knowledge"
            / scope.view_id
            / "revisions"
            / str(head.knowledge_revision_id)
        )
    )
    manifest = (
        KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
        if scope.read_only or head.knowledge_revision_id
        else None
    )
    if manifest and (
        manifest.view_id != scope.view_id or manifest.knowledge_revision_id != directory.name
    ):
        raise ValueError("Knowledge evidence belongs to another snapshot")
    dependencies = manifest.page_dependencies.get(page, ()) if manifest else ()
    return {
        "validity": "historical"
        if scope.read_only
        else "needs_refresh"
        if page in head.needs_refresh
        else "current",
        "knowledge_revision_id": manifest.knowledge_revision_id if manifest else None,
        "source_revision_ids": tuple(
            sorted(
                {
                    validated_input(scope.kb_dir, identity, scope.view_id)[1].source_revision_id
                    for identity in dependencies
                }
                | {
                    r.source_revision_id
                    for r in head.needs_refresh.get(page, ())
                    if not scope.read_only and not dependencies and scope.view_id == "legacy"
                }
            )
        ),
        "refresh_reasons": tuple(
            r.model_dump(mode="json") for r in head.needs_refresh.get(page, ())
        )
        if not scope.read_only
        else (),
    }
