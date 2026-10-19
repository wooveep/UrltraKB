"""Exercise source processing and reading with generation explicitly unadmitted."""

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class ProcessedSource:
    source_id: str
    source_revision_id: str
    unit_revision_id: str
    doc_name: str


@pytest.fixture
def processing_only(kb_dir):
    from ultrakb.application.documents import import_document
    from ultrakb.application.sources import source_inventory
    from ultrakb.application.views import published_scope
    from ultrakb.documents import read_document_source
    from ultrakb.storage.catalog_store import CatalogStore

    def process(source, *, context=None, known_source_ids=None, **options):
        context = context or source_processing_context(bundle=options.get("bundle"))
        executor = context.executor
        assert executor is not None and executor.policy.max_calls == 0
        previous = (
            set(known_source_ids)
            if known_source_ids is not None
            else {item["source_id"] for item in source_inventory(kb_dir)}
        )
        imported = import_document(kb_dir, source, context=context, **options)
        assert imported.status == "blocked", imported
        assert executor.calls == 0
        [saved] = [item for item in source_inventory(kb_dir) if item["source_id"] not in previous]
        target = imported.units[0].target_revision_id
        result = read_document_source(kb_dir, saved["source_id"], unit_revision_id=target)
        assert result is not None, imported
        assert result["knowledge_revision_id"] is None
        identity = published_scope(kb_dir).knowledge.identity
        unit = CatalogStore(kb_dir, identity).unit_revision(target)
        return ProcessedSource(
            result["source_id"], result["source_revision_id"], target, unit.doc_name
        )

    return process


def source_processing_context(*, bundle=None, **options):
    """Execute admission/conversion/discovery while forbidding model sends."""
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    return ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", bundle or LlmCredentialBundle()),
            ModelCallPolicy(max_calls=0, retries=0),
        ),
        **options,
    )


@pytest.fixture
def processing_context():
    return source_processing_context()


def publish_processed_text(kb_dir, source_id):
    return publish_processed_source(kb_dir, source_id)


def publish_processed_source(kb_dir, source_id):
    """Publish retained input as explicit fixture data without a model response."""
    from types import SimpleNamespace

    from native_publication_fixtures import publication_workspace

    from ultrakb.agent.compiler import _write_summary
    from ultrakb.application.views import published_scope
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.knowledge_inputs import source_paths

    bound = published_scope(kb_dir).knowledge
    catalog = CatalogStore(kb_dir, bound.identity)
    targets = catalog.source_units(source_id)
    assert targets
    for target in targets:
        processing = catalog.processing(target.processing_id)
        body = catalog.read_processed(processing.processing_id, processing.body_path).decode()
        with publication_workspace(kb_dir, target.unit_revision_id) as (wiki, target):
            original = source_paths(target, processing)[-1][0]
            _write_summary(
                wiki, target.doc_name, body, description=target.doc_name, full_text=original
            )
            _refresh_index(wiki)
    publication = published_scope(kb_dir).knowledge.publication
    return SimpleNamespace(
        status="completed",
        source_id=source_id,
        unit_id=target.unit_id,
        successful_revision_id=target.unit_revision_id,
        knowledge_revision_id=publication.revision_id,
    )


def finish_recovered_sources(kb_dir, parent_source_id, context):
    from ultrakb.application.pending import process_pending
    from ultrakb.application.sources import source_inventory
    from ultrakb.application.views import published_scope
    from ultrakb.storage.catalog_store import CatalogStore

    process_pending(kb_dir, context=context)
    bound = published_scope(kb_dir).knowledge
    catalog = CatalogStore(kb_dir, bound.identity)
    for source in source_inventory(kb_dir):
        if source["source_id"] == parent_source_id or source.get("removed"):
            continue
        units = catalog.source_units(source["source_id"])
        if units and not all(u.unit_revision_id in bound.unit_revisions() for u in units):
            publish_processed_source(kb_dir, source["source_id"])
    result = process_pending(kb_dir, context=context)
    assert context.executor.calls == 0
    return result
