"""Native capacity rejection keeps accepted indexes and prior costs across worker restart."""

import json

from index_recovery_fixtures import index_ids

from ultrakb.application.knowledge_tasks import read_application_task
from ultrakb.application.views import published_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.index_store import IndexStore

pytest_plugins = ("index_recovery_fixtures",)


def _capacity_worker(*args):
    from unittest.mock import patch

    from ultrakb.llm_execution import ModelRequest, active_executor
    from ultrakb.runtime.worker import run_unit

    async def reject(*unused, **kwargs):
        executor = active_executor()
        messages = [{"role": "user", "content": "capacity-check " * 100000}]
        await executor.acomplete(
            "compile", ModelRequest(messages, operation="compile", stage="capacity-boundary")
        )
        raise AssertionError("Capacity rejection must precede transport")

    with patch("ultrakb.agent.compiler.compile_long_doc", reject):
        run_unit(*args)


def test_capacity_failure_survives_worker_receipt_and_restart(
    retained_index, tmp_path, monkeypatch
):
    import ultrakb.runtime.tasks as tasks
    from ultrakb.runtime.records import UnitResult
    from ultrakb.runtime.requests import RetrySource

    data = retained_index
    monkeypatch.setattr(tasks, "run_unit", _capacity_worker)
    history = tmp_path / "tasks"
    manager = tasks.TaskManager(history_dir=history, max_workers=1)
    try:
        task = manager.submit(data.root, [RetrySource(data.target.source_id)])
        view = manager.wait(task, timeout=30)
        assert view.state == "blocked" and view.processes_reaped, view
        [result] = view.results
        assert (
            result.status == "blocked"
            and result.import_units[0].error_type == "ContextCapacityError"
        )
        assert result.model_usage["current"]["actual_sends"] == 0
        assert result.model_usage["cumulative"]["actual_sends"] == data.sent
        assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
        detail = read_application_task(data.root, task)
        assert detail.task.budget.model_budget_id == data.context.storage_execution.model_budget_id
        assert detail.task.budget.used == data.sent
        assert published_scope(data.root).publication_id == data.publication_id
        assert index_ids(data.root, data.identity, data.target.unit_revision_id) == (data.index,)
        assert IndexStore(data.root, data.identity).document(data.index) == data.document
        assert (
            CatalogStore(data.root, data.identity).read_original(data.target.source_revision_id)
            == data.original
        )
        assert len(data.service.requests) == data.sent
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
    restarted = tasks.TaskManager(history_dir=history)
    try:
        assert restarted.get(task).results == view.results
        assert restarted.get(task).processes_reaped
    finally:
        restarted.shutdown(stop=True)
        assert restarted.join(10)


def test_small_import_capacity_failure_retains_completed_conversion(kb_dir, tmp_path):
    from ultrakb.application.documents import import_document
    from ultrakb.application.sources import source_inventory
    from ultrakb.config import save_config

    save_config(
        kb_dir / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "model_capacity": {"max_input_tokens": 96, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    source = tmp_path / "safety.md"
    original = b"# Safety\nClose the inlet before inspecting the seal.\n"
    source.write_bytes(original)
    before = published_scope(kb_dir)
    result = import_document(kb_dir, source)
    assert result.status == "blocked", result.message
    [unit] = result.units
    assert unit.error_type == "IndexCapacityError"
    catalog = CatalogStore(kb_dir, before.knowledge.identity)
    retained = catalog.unit(unit.unit_id)
    processing = catalog.processing(retained.processing_id)
    assert catalog.read_original(result.source_revision_id) == original
    assert b"Close the inlet" in catalog.read_processed(
        processing.processing_id, processing.body_path
    )
    assert source.read_bytes() == original
    assert (
        source_inventory(kb_dir)[0]["units"][0]["target_revision_id"] == retained.unit_revision_id
    )
    assert published_scope(kb_dir).publication_id == before.publication_id
    assert result.model_usage["current"]["actual_sends"] == 0
