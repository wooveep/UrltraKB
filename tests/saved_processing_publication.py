"""Publish explicit fixture knowledge while retaining a real native SDK index."""


def publish_saved_processing(kb, path, *, bundle, indexed=False):
    from publication_fixtures import publish_document

    if not indexed:
        return publish_document(kb, path, None, bundle=bundle)

    from native_catalog_fixtures import catalog
    from processing_fixtures import source_processing_context

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.storage_indexing import prepare_document_index
    from ultrakb.application.views import published_scope
    from ultrakb.storage.compile_inputs import materialize_inputs

    retained = import_document(kb, path, context=source_processing_context(bundle=bundle))
    assert retained.source_id is not None
    store = catalog(kb)
    [unit] = store.source_units(retained.source_id)
    processing = store.processing(unit.processing_id)
    base = published_scope(kb).knowledge
    context = ExecutionContext(credential_bundle=bundle)
    with context.begin(kb) as credentials, base.compile_workspace() as baseline:
        materialize_inputs(base, baseline, unit)
        index = prepare_document_index(base, unit, processing, baseline, context, credentials, None)
    assert context.executor.calls > 0
    return publish_document(kb, path, None, bundle=bundle, index_revisions=(index,))
