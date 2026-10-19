"""Publish explicit compiler protocol fixtures through real native storage."""

from contextlib import contextmanager

from ultrakb.application.views import published_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_inputs import materialize_inputs, prepare_compiled_revision
from ultrakb.storage.compile_outcomes import CompileOutcome
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.pending_store import PendingStore
from ultrakb.storage.wiki_projection import ProjectionStore


@contextmanager
def publication_workspace(root, unit_revision_id, *, context=None, index_revisions=()):
    from processing_fixtures import source_processing_context

    base = published_scope(root).knowledge
    target = CatalogStore(root, base.identity).unit_revision(unit_revision_id)
    pending = PendingStore(root, base.identity)
    work = pending.enqueue(target.source_revision_id, unit_revision_id=unit_revision_id)
    work = pending.start(pending.claim(intent_id=work.intent_id))
    assert work is not None and work.status == "started"
    context = context or source_processing_context()
    with context.begin(root):
        execution = context.storage_execution
        pending.bind_execution(work, execution)
        checkpoints = CompileCheckpointStore(root, base.identity).for_work(
            execution.model_budget_id,
            object_id={"unit_revision_id": unit_revision_id, "publication_id": base.publication_id},
            conditions={"compiler": 3},
            execution_id=execution.execution_id,
        )
        with base.compile_workspace() as baseline:
            inputs = materialize_inputs(base, baseline, target)
            with compile_binding(
                checkpoints, view_id="legacy", document=target.doc_name, inputs=inputs
            ):
                yield baseline.directory, target
            store = KnowledgeStore(root, base.identity)
            store.publish(
                prepare_compiled_revision(
                    store,
                    base,
                    baseline,
                    target,
                    work,
                    index_revisions=index_revisions,
                    compile_outcome=CompileOutcome(quality_known=True),
                ),
                execution_id=execution.execution_id,
            )
            ProjectionStore(root, base.identity).sync()
        assert context.executor.calls == 0
