"""Publish already processed native inputs at the production storage boundary."""


def publish_processed_source(kb_dir, source_id, *, preserve_summary=False):
    from processing_fixtures import source_processing_context

    from ultrakb.agent.compiler import _write_summary
    from ultrakb.application.ingestion import result_from_publication
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.mutation import _copy_file_atomic
    from ultrakb.normalization import normalization_id, read_normalization, restore_normalization
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.source_catalog import read_admission, read_record, read_source
    from ultrakb.source_map import freeze_pdf_map, freeze_text_map
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        list_source_units,
        prepare_compile_view,
        publish_unit_revision,
    )

    context = source_processing_context()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        admission = read_admission(kb_dir, read_source(kb_dir, source_id))
        [unit] = list_source_units(kb_dir, source_id)
        revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        assert revision.source_revision_id == admission.revision.source_revision_id
        state, runnable = begin_unit_attempt(
            kb_dir,
            unit,
            revision,
            discovery_intent=admission.discovery_intent,
            retry_confirmed=True,
        )
        assert runnable
        directory, normalized = read_normalization(
            kb_dir, normalization_id(revision.source_revision_id, revision.processing_fingerprint)
        )
        with prepare_compile_view(kb_dir) as view:
            converted = restore_normalization(directory, normalized, view.scope.wiki_dir.parent)
            assert not converted.is_long_doc and converted.source_path is not None
            wiki = view.scope.wiki_dir
            summary = f"summaries/{unit.doc_name}.md"
            if preserve_summary:
                _copy_file_atomic(live_scope(kb_dir).wiki_dir / summary, wiki / summary)
            else:
                _write_summary(
                    wiki,
                    unit.doc_name,
                    converted.source_path.read_text("utf-8"),
                    description=unit.doc_name,
                )
            _refresh_index(wiki)
            physical = converted.pdf_path or (
                kb_dir / admission.revision.original
                if admission.revision.source_format == "pdf"
                else None
            )
            source_map = (
                freeze_pdf_map(wiki, unit.doc_name, physical)
                if physical is not None
                else freeze_text_map(wiki, unit.doc_name)
            )
            state = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.md",
                source_map=source_map,
                processing=converted.processing,
                normalized_format="markdown",
                is_long=False,
            )
        assert state.status == "completed", state
        assert context.executor.calls == 0
        return result_from_publication(kb_dir, admission, state, status="added")
