"""Read the actual recorded inputs of a page independently of its current validity."""

from pathlib import Path

from ultrakb.ingest_records import ImportUnit, KnowledgeRevision, UnitRevision
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.source_catalog import read_record
from ultrakb.source_records import SourceRevision
from ultrakb.unit_publication import read_head


def validated_input(
    kb_dir: Path, identity: str, view_id: str
) -> tuple[UnitRevision, SourceRevision]:
    """Validate immutable associations, without requiring the input to remain current."""
    unit = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
    named = read_record(kb_dir, "units", unit.unit_id, ImportUnit)
    # SDK tools run in another thread under the caller's exclusive KB lease.
    # These input records are immutable; reacquiring that lease here would deadlock.
    source = read_record(kb_dir, "source-revisions", unit.source_revision_id, SourceRevision)
    if named.source_id != source.source_id:
        raise ValueError("Knowledge input belongs to another source")
    from pydantic import TypeAdapter

    from ultrakb.source_records import ViewId

    TypeAdapter(ViewId).validate_python(view_id)
    return unit, source


def publication_dependencies(kb_dir, head, pages, before, inputs, *, wiki=None):
    dependencies = {page: tuple(sorted(inputs)) for page in pages if page.endswith(".md")}
    if head.knowledge_revision_id:
        directory = (
            kb_dir / ".ultrakb/knowledge" / head.view_id / "revisions" / head.knowledge_revision_id
        )
        previous = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
        if previous.view_id != head.view_id or previous.knowledge_revision_id != directory.name:
            raise ValueError("Knowledge evidence belongs to another snapshot")
        for page in dependencies:
            same = pages[page] == before.get(page)
            if not same and wiki and (directory / "wiki" / page).is_file():
                from ultrakb.figure_publication import figure_independent_body

                same = figure_independent_body(
                    (wiki / page).read_text()
                ) == figure_independent_body((directory / "wiki" / page).read_text())
            if page in previous.page_dependencies and same:
                dependencies[page] = previous.page_dependencies[page]
    return dependencies


def publication_originals(kb_dir, view_id, inputs, dependencies, wiki, pages) -> tuple[str, ...]:
    """Retain the artifact closure of current inputs, older page bases and figures."""
    from ultrakb.figure_publication import referenced_figure_inputs

    referenced = set(inputs) | {identity for values in dependencies.values() for identity in values}
    referenced.update(referenced_figure_inputs(wiki, pages))
    originals = set()
    for identity in referenced:
        _, source = validated_input(kb_dir, identity, view_id)
        originals.add(source.original)
        originals.update(asset.artifact for asset in source.assets if asset.artifact)
    return tuple(sorted(originals))


def page_evidence(scope: KnowledgeScope, page: str) -> dict:
    if scope.publication is not None:
        public = scope.publication
        dependencies = public.manifest.page_dependencies.get(page, ())
        reasons = public.head.needs_refresh.get(page, ())
        return {
            "validity": "needs_refresh" if reasons else "current",
            "knowledge_revision_id": public.knowledge_revision_id,
            "source_revision_ids": tuple(
                sorted(
                    {
                        validated_input(public.metadata_dir, identity, scope.view_id)[
                            1
                        ].source_revision_id
                        for identity in dependencies
                    }
                    | {reason.source_revision_id for reason in reasons if not dependencies}
                )
            ),
            "refresh_reasons": tuple(reason.model_dump(mode="json") for reason in reasons),
        }
    head = read_head(scope.kb_dir, scope.view_id)
    directory = (
        scope.wiki_dir.parent
        if scope.historical
        else (
            scope.kb_dir
            / ".ultrakb/knowledge"
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
                    if not scope.historical and not dependencies and scope.view_id == "legacy"
                }
            )
        ),
        "refresh_reasons": tuple(
            r.model_dump(mode="json") for r in head.needs_refresh.get(page, ())
        )
        if not scope.historical
        else (),
    }
