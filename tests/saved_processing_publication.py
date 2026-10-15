"""Prepare saved source/index state while reserving compiler generation for the test."""


def publish_saved_processing(kb, path, *, bundle, indexed=False):
    from publication_fixtures import publish_document

    if not indexed:
        return publish_document(kb, path, None, bundle=bundle)

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.ingestion import result_from_publication
    from ultrakb.application.views import view_scope
    from ultrakb.index_checkpoint import reusable_index
    from ultrakb.indexer import index_long_document
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_usage import usage_context
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.normalization import (
        normalization_fingerprint,
        restore_normalization,
        retain_normalization,
    )
    from ultrakb.source_catalog import admit_source_revision
    from ultrakb.source_map import freeze_pdf_map
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        plan_import_units,
        prepare_compile_view,
        publish_unit_revision,
    )

    # The actual PageIndex build is needed by cache/restore assertions. Its model
    # requests use the normal captured executor and genuine shared gateway;
    # the fixture publishes its validated navigation without compiling it twice.
    context = ExecutionContext()
    with kb_ingest_lock(kb / ".ultrakb"), context.begin(kb), prepared_input(path) as prepared:
        admission = admit_source_revision(kb, prepared)
        scope = view_scope(kb, "legacy")
        fingerprint = normalization_fingerprint(
            kb,
            scope=scope,
            bundle=bundle,
            source_revision=admission.revision,
            doc_name=admission.source.doc_name,
        )
        unit, revision = plan_import_units(kb, admission, fingerprint)
        state, runnable = begin_unit_attempt(
            kb, unit, revision, discovery_intent=admission.discovery_intent
        )
        assert runnable
        context.bind_model_input(admission.source.source_id, admission.revision.source_revision_id)
        directory, normalized = retain_normalization(
            kb, admission, prepared, fingerprint, unit=unit, scope=scope, bundle=bundle
        )
        with (
            usage_context(
                unit_id=unit.unit_id,
                unit_revision_id=revision.unit_revision_id,
                attempt_id=state.job_id,
            ),
            prepare_compile_view(kb) as view,
        ):
            working = view.scope.wiki_dir.parent
            converted = restore_normalization(directory, normalized, working)
            assert converted.is_long_doc and converted.pdf_path is not None
            index_input = converted.pdf_path
            if index_input.with_suffix(".okpi").is_file():
                index_input = index_input.with_suffix(".okpi")
            indexed_result = reusable_index(
                kb,
                admission,
                revision,
                normalized,
                index_input,
                working,
                lambda: index_long_document(
                    index_input,
                    kb,
                    doc_name=unit.doc_name,
                    scope=view.scope,
                    storage_path=working / "index",
                ),
            )
            state = publish_unit_revision(
                kb,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.json",
                source_map=freeze_pdf_map(view.scope.wiki_dir, unit.doc_name, converted.pdf_path),
                processing=converted.processing,
                normalized_format="pdf",
                is_long=True,
                index_ref=indexed_result.doc_id,
            )
        assert state.status == "completed"
        assert context.executor.calls > 0
        return result_from_publication(kb, admission, state, status="added")
