"""Explicit fixture knowledge from actual native processing, without a model send."""

from dataclasses import replace

from native_publication_fixtures import publication_workspace
from processing_fixtures import source_processing_context

from ultrakb.agent.compiler import _write_concept, _write_summary
from ultrakb.application.documents import import_document
from ultrakb.application.views import published_scope
from ultrakb.locks import atomic_write_text
from ultrakb.selected_refresh import _refresh_index
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_inputs import source_paths


def publish_document(
    kb,
    path,
    page_body,
    *,
    bundle,
    shared_body=None,
    shared_sources=None,
    concept_pages=None,
    expected_status="completed",
    index_revisions=(),
):
    context = source_processing_context(bundle=bundle)
    retained = import_document(kb, path, context=context)
    assert retained.source_id is not None and context.executor.calls == 0, retained
    catalog = CatalogStore(kb, published_scope(kb).knowledge.identity)
    [unit] = catalog.source_units(retained.source_id)
    processing = catalog.processing(unit.processing_id)
    with publication_workspace(kb, unit.unit_revision_id, index_revisions=index_revisions) as (
        wiki,
        target,
    ):
        body = catalog.read_processed(processing.processing_id, processing.body_path).decode()
        _write_summary(
            wiki,
            target.doc_name,
            body if page_body is None else page_body,
            description=target.doc_name,
            full_text=source_paths(target, processing)[-1][0],
        )
        if shared_body is not None:
            own = f"summaries/{target.doc_name}.md"
            sources = list(dict.fromkeys([*(shared_sources or ()), own]))
            _write_concept(wiki, "shared", shared_body, sources[-1], False, "Shared policies")
            from ultrakb.agent.compiler import _prepend_source_to_frontmatter

            shared = wiki / "concepts/shared.md"
            for source in sources[:-1]:
                atomic_write_text(
                    shared, _prepend_source_to_frontmatter(shared.read_text(), source)
                )
        for name, content in (concept_pages or {}).items():
            _write_concept(wiki, name, content, f"summaries/{target.doc_name}.md", False, name)
        _refresh_index(wiki)
    assert expected_status == "completed", "Review behavior uses the native proposal use case"
    publication = published_scope(kb).knowledge.publication
    return replace(
        retained,
        status="added",
        unfinished=(),
        message=None,
        units=tuple(
            replace(
                item,
                status="completed",
                error_type=None,
                unfinished=(),
                knowledge_revision_id=publication.revision_id,
            )
            for item in retained.units
        ),
    )


def fail_document(kb, path, *, bundle):
    context = source_processing_context(bundle=bundle)
    result = import_document(kb, path, context=context)
    assert result.source_id is not None and context.executor.calls == 0, result
    assert result.status in {"blocked", "failed", "partial"}, result
    return result
