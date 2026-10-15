"""Exercise source processing and reading with generation explicitly unadmitted."""

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class ProcessedSource:
    source_id: str
    source_revision_id: str


@pytest.fixture
def processing_only(kb_dir):
    from ultrakb.application.documents import import_document
    from ultrakb.application.sources import source_inventory
    from ultrakb.documents import read_document_source
    from ultrakb.llm_execution import ModelBudgetExceeded

    def process(source, *, context=None, known_source_ids=None, **options):
        context = context or source_processing_context()
        executor = context.executor
        assert executor is not None and executor.policy.max_calls == 0
        previous = (
            set(known_source_ids)
            if known_source_ids is not None
            else {item["source_id"] for item in source_inventory(kb_dir)}
        )
        with pytest.raises(ModelBudgetExceeded):
            import_document(kb_dir, source, context=context, **options)
        assert executor.calls == 0
        [saved] = [item for item in source_inventory(kb_dir) if item["source_id"] not in previous]
        result = read_document_source(kb_dir, saved["source_id"])
        assert result["knowledge_revision_id"] is None
        return ProcessedSource(result["source_id"], result["source_revision_id"])

    return process


def source_processing_context(**options):
    """Execute admission/conversion/discovery while forbidding model sends."""
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    return ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
            ModelCallPolicy(max_calls=0, retries=0),
        ),
        **options,
    )


@pytest.fixture
def processing_context():
    return source_processing_context()


def publish_processed_text(kb_dir, source_id):
    """Publish retained original text as domain state, without a model response."""
    from ultrakb.source_catalog import read_source, read_source_revision

    source = read_source(kb_dir, source_id)
    assert read_source_revision(kb_dir, source.target_revision_id).source_format in {
        "txt",
        "md",
        "markdown",
    }
    return publish_processed_source(kb_dir, source_id)


def publish_processed_source(kb_dir, source_id):
    """Publish genuine retained text/pages/sheets without generating a model reply."""
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.config import resolve_credential_bundle
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.normalization import normalization_id, read_normalization, restore_normalization
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.source_catalog import read_admission, read_record, read_source
    from ultrakb.source_map import freeze_pdf_map, freeze_text_map
    from ultrakb.unit_publication import (
        list_source_units,
        prepare_compile_view,
        publish_unit_revision,
        read_unit_publication,
    )

    context = source_processing_context()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        admission = read_admission(kb_dir, read_source(kb_dir, source_id))
        if admission.revision.source_format in {"xls", "xlsx"}:
            from office_publication_fixtures import publish_workbook

            result = publish_workbook(
                kb_dir,
                kb_dir / admission.revision.original,
                bundle=resolve_credential_bundle(kb_dir),
                admission=admission,
                retry_confirmed=True,
            )
            assert result.status == "added", result.message
            assert result.source_id == source_id
            assert result.source_revision_id == admission.revision.source_revision_id
            assert context.executor.calls == 0
            return result
        [unit] = list_source_units(kb_dir, source_id)
        revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        state = read_unit_publication(kb_dir, unit.unit_id)
        directory, normalized = read_normalization(
            kb_dir,
            normalization_id(revision.source_revision_id, revision.processing_fingerprint),
        )
        with prepare_compile_view(kb_dir) as view:
            converted = restore_normalization(directory, normalized, view.scope.wiki_dir.parent)
            assert not converted.is_long_doc
            body = (converted.source_path or converted.raw_path).read_text("utf-8")
            wiki = view.scope.wiki_dir
            atomic_write_text(wiki / f"sources/{unit.doc_name}.md", body)
            _write_summary(wiki, unit.doc_name, body, description=unit.doc_name)
            _refresh_index(wiki)
            physical = converted.pdf_path or (
                kb_dir / admission.revision.original
                if admission.revision.source_format == "pdf"
                else None
            )
            state = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.md",
                source_map=freeze_pdf_map(wiki, unit.doc_name, physical)
                if physical
                else freeze_text_map(wiki, unit.doc_name),
                processing=converted.processing,
                normalized_format="pdf" if physical else "markdown",
                is_long=False,
            )
        assert state.status == "completed"
        assert context.executor.calls == 0
        return state


def finish_recovered_sources(kb_dir, parent_source_id, context):
    """Discover actual objects, publish their retained originals, then reconcile jobs."""
    from ultrakb.application.pending import process_pending
    from ultrakb.source_catalog import list_sources
    from ultrakb.unit_publication import list_source_units, read_unit_publication

    process_pending(kb_dir, context=context)
    for source in list_sources(kb_dir):
        if source.source_id != parent_source_id and not source.removed:
            units = list_source_units(kb_dir, source.source_id)
            states = [read_unit_publication(kb_dir, unit.unit_id) for unit in units]
            if units and all(
                state.status == "completed"
                and state.successful_revision_id == unit.target_revision_id
                for unit, state in zip(units, states, strict=True)
            ):
                continue
            publish_processed_source(kb_dir, source.source_id)
    result = process_pending(kb_dir, context=context)
    assert context.executor.calls == 0
    return result
