"""Publish an explicitly chosen retained target through the native fixture boundary."""

from native_publication_fixtures import publication_workspace


def publish_processed_source(kb_dir, source_id, *, preserve_summary=False):
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.application.sources import source_inventory
    from ultrakb.application.views import published_scope
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.knowledge_inputs import source_paths

    catalog = CatalogStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    for target in catalog.source_units(source_id):
        processing = catalog.processing(target.processing_id)
        with publication_workspace(kb_dir, target.unit_revision_id) as (wiki, target):
            if not preserve_summary:
                body = catalog.read_processed(
                    processing.processing_id, processing.body_path
                ).decode()
                _write_summary(
                    wiki,
                    target.doc_name,
                    body,
                    description=target.doc_name,
                    full_text=source_paths(target, processing)[-1][0],
                )
            _refresh_index(wiki)
    return next(item for item in source_inventory(kb_dir) if item["source_id"] == source_id)
