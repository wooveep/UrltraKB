"""An unsendable native index request keeps conversion and budget through worker restart."""

import pymupdf
from error_transport import ErrorTransport

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_application_task
from ultrakb.application.views import published_scope
from ultrakb.config import save_config
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.runtime.requests import RetrySource
from ultrakb.runtime.tasks import TaskManager
from ultrakb.storage.catalog_store import CatalogStore


def test_index_capacity_block_preserves_normalization_through_worker_restart(kb_dir, tmp_path):
    save_config(
        kb_dir / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "pdf_short_max_pages": 0,
            "model_capacity": {"max_input_tokens": 96, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": 8, "retries": 0},
        },
    )
    source = tmp_path / "manual.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "The value is 17.")
        pdf.save(source)
    original = source.read_bytes()
    publish_pages(
        kb_dir, {"concepts/operator-note.md": b"# Human note\nKeep the reviewed procedure.\n"}
    )
    scope = published_scope(kb_dir)
    context = ExecutionContext()
    with ErrorTransport(status=401) as service:
        (kb_dir / ".env").write_text(
            f"LLM_API_KEY=offline-index-key\nOPENAI_API_BASE={service.url}\n"
        )
        result = import_document(kb_dir, source, context=context)
        assert result.status == "blocked", result.message
        [unit] = result.units
        assert unit.error_type == "IndexCapacityError"
        source.unlink()
        catalog = CatalogStore(kb_dir, scope.knowledge.identity)
        target = catalog.unit(unit.unit_id)
        processing = catalog.processing(target.processing_id)
        retained = {
            file.path: catalog.read_processed(processing.processing_id, file.path)
            for file in processing.files
        }
        manager = TaskManager(history_dir=tmp_path / "history")
        try:
            task = manager.submit(kb_dir, [RetrySource(result.source_id)])
            final = manager.wait(task, timeout=30)
            assert final.state == "blocked" and final.processes_reaped, final
            assert final.results[0].import_units[0].error_type == "IndexCapacityError"
            detail = read_application_task(kb_dir, task)
            assert detail.task.budget.model_budget_id == context.storage_execution.model_budget_id
            assert detail.task.budget.used == 0
            assert detail.task.usage is None and not detail.requests
            assert catalog.unit(unit.unit_id) == target
            assert catalog.read_original(result.source_revision_id) == original
            assert {
                path: catalog.read_processed(processing.processing_id, path) for path in retained
            } == retained
            assert published_scope(kb_dir).publication_id == scope.publication_id
            assert not service.requests
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
        restarted = TaskManager(history_dir=tmp_path / "history")
        try:
            assert restarted.get(task).results == final.results
            assert restarted.get(task).processes_reaped
        finally:
            restarted.shutdown(stop=True)
            assert restarted.join(10)
