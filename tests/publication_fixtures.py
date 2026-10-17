"""Build publication domain state from actual retained inputs, without a model response.

These fixtures exercise storage and refresh boundaries. Model generation is covered
separately by live compiler and refresh requests; no compiler result is impersonated.
"""

from ultrakb.locks import atomic_write_text, kb_ingest_lock


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
):
    from ultrakb.agent.compiler import _write_concept, _write_summary
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.ingestion import result_from_publication
    from ultrakb.application.views import view_scope
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.normalization import (
        normalization_fingerprint,
        restore_normalization,
        retain_normalization,
    )
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.source_catalog import admit_source_revision
    from ultrakb.source_map import freeze_pdf_map, freeze_text_map
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        plan_import_units,
        prepare_compile_view,
        publish_unit_revision,
    )

    context = ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", bundle), ModelCallPolicy(max_calls=0, retries=0)
        )
    )
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
        directory, normalized = retain_normalization(
            kb, admission, prepared, fingerprint, unit=unit, scope=scope, bundle=bundle
        )
        with prepare_compile_view(kb) as view:
            converted = restore_normalization(directory, normalized, view.scope.wiki_dir.parent)
            wiki = view.scope.wiki_dir
            source = wiki / f"sources/{unit.doc_name}.md"
            if not source.exists():
                atomic_write_text(source, converted.raw_path.read_text("utf-8"))
            _write_summary(
                wiki,
                unit.doc_name,
                source.read_text("utf-8") if page_body is None else page_body,
                description=unit.doc_name,
            )
            if shared_body is not None:
                sources = shared_sources or [f"summaries/{unit.doc_name}.md"]
                _write_concept(wiki, "shared", shared_body, sources[-1], False, "Shared policies")
                from ultrakb.agent.compiler import _prepend_source_to_frontmatter

                shared = wiki / "concepts/shared.md"
                for source_page in sources[:-1]:
                    atomic_write_text(
                        shared,
                        _prepend_source_to_frontmatter(shared.read_text("utf-8"), source_page),
                    )
            for name, body in (concept_pages or {}).items():
                _write_concept(wiki, name, body, f"summaries/{unit.doc_name}.md", False, name)
            _refresh_index(wiki)
            physical = converted.pdf_path or (
                kb / admission.revision.original
                if admission.revision.source_format == "pdf"
                else None
            )
            source_map = (
                freeze_pdf_map(wiki, unit.doc_name, physical)
                if physical is not None
                else freeze_text_map(wiki, unit.doc_name)
            )
            state = publish_unit_revision(
                kb,
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
        assert state.status == expected_status, state
        assert context.executor.calls == 0
        status = "blocked" if state.status == "awaiting_confirmation" else "added"
        return result_from_publication(kb, admission, state, status=status)


def fail_document(kb, path, *, bundle):
    """Retain conversion, then record a local delivery failure before any model send."""
    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )
    from ultrakb.source_catalog import list_sources, read_admission
    from ultrakb.unit_publication import (
        list_source_units,
        read_unit_publication,
        record_unit_failure,
    )

    context = ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", bundle), ModelCallPolicy(max_calls=0, retries=0)
        )
    )
    try:
        import_document(kb, path, context=context)
    except ModelBudgetExceeded:
        pass
    else:
        raise AssertionError("Generation must stop at the unsent budget gate")
    source = next(
        source for source in list_sources(kb) if source.identity == str(path.relative_to(kb))
    )
    [unit] = list_source_units(kb, source.source_id)
    state = read_unit_publication(kb, unit.unit_id)
    assert context.executor.calls == 0
    with kb_ingest_lock(kb / ".ultrakb"):
        state = record_unit_failure(kb, state, "delivery", ValueError("Local delivery interrupted"))
    from ultrakb.application.ingestion import result_from_publication

    return result_from_publication(kb, read_admission(kb, source), state, status="failed")
