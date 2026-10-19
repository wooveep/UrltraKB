"""Main-store recompilation reports committed units, paid usage and safe recovery."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_execution
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_recompilation_reports_the_exact_unit_and_actual_execution_usage(tmp_path):
    root, identity, target = compile_input(tmp_path)
    context = ExecutionContext()
    responses = [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        assert len(service.requests) == 2
    assert result.model_usage["current"]["actual_sends"] == 2
    assert result.model_usage["current"]["input_total"] == 240
    [unit] = result.units
    assert unit.unit_id == target.unit_id and unit.status == "completed"
    assert unit.target_revision_id == unit.successful_revision_id == target.unit_revision_id
    assert unit.successful_source_revision_id == target.source_revision_id
    assert unit.knowledge_revision_id == result.knowledge_revision_id
    assert unit.model_usage["current"]["actual_sends"] == 2
    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert detail.execution.state == "completed"
    assert detail.execution_usage.actual_sends == 2


@pytest.mark.asyncio
async def test_stopped_compiler_keeps_paid_response_and_resumes_the_original_budget(tmp_path):
    import asyncio
    from threading import Event

    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root, identity, target = compile_input(tmp_path)
    gate, stop = Event(), Event()
    context = ExecutionContext(cancelled=stop.is_set)
    original = storage_scope(root, identity)
    responses = [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses, gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        pending = asyncio.create_task(
            recompile_document(
                root,
                target.source_id,
                unit_id=target.unit_id,
                context=context,
                scope=original,
            )
        )
        try:
            for _ in range(300):
                if service.requests:
                    break
                await asyncio.sleep(0.01)
            assert len(service.requests) == 1
            stop.set()
        finally:
            gate.set()
        stopped = await pending
        assert stopped.status == "stopped", stopped.message
        previous = context.storage_execution
        detail = read_knowledge_execution(root, previous.execution_id)
        assert detail.execution.state == "cancelled"
        assert stopped.model_usage["current"]["actual_sends"] == 1
        assert storage_scope(root, identity).publication_id == original.publication_id
        [request] = UsageStore(root, identity).requests(execution_id=previous.execution_id).records
        assert b"The value is 17." in ReceiptStore(root, identity).read(request.request_attempt_id)
        stop.clear()
        resumed = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=original,
        )
        assert resumed.status == "compiled", resumed.message
        assert context.storage_execution.model_budget_id == previous.model_budget_id
        assert len(service.requests) == 2
        assert ExecutionStore(root, identity).budget(previous.model_budget_id).used == 2


@pytest.mark.asyncio
async def test_final_usage_confirmation_failure_keeps_committed_unit_and_publication(
    tmp_path, monkeypatch
):
    from ultrakb.storage.usage_store import UsageStore

    root, identity, target = compile_input(tmp_path)
    context = ExecutionContext()
    receipt = UsageStore.receipt

    def unavailable(*args, **options):
        receipt(*args, **options)
        raise OSError("Usage read failed after compilation was committed")

    responses = [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        monkeypatch.setattr(UsageStore, "receipt", unavailable)
        result = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        assert len(service.requests) == 2
    assert result.units[0].successful_revision_id == target.unit_revision_id
    assert (
        result.knowledge_revision_id
        == storage_scope(root, identity).knowledge.publication.revision_id
    )
    assert result.model_usage is None and "confirm usage" in result.unfinished
    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert detail.execution.state == "completed" and detail.execution_usage.actual_sends == 2


@pytest.mark.asyncio
async def test_recompile_rejects_a_stale_publication_before_new_model_work(tmp_path):
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, target = compile_input(tmp_path)
    original = storage_scope(root, identity)
    store = KnowledgeStore(root, identity)
    current = store.publish(
        store.prepare_changes(original.knowledge, {"index.md": b"Concurrent edit"})
    )
    context = ExecutionContext()
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=original,
        )
        assert result.status == "conflict", result.message
        assert not service.requests
    assert store.bind().publication_id == current.publication_id


@pytest.mark.asyncio
async def test_stopping_before_the_next_execution_does_not_report_the_previous_cost_as_current(
    tmp_path,
):
    from threading import Event

    root, identity, target = compile_input(tmp_path)
    stop = Event()
    context = ExecutionContext(cancelled=stop.is_set)
    responses = [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert first.status == "compiled"
        previous = context.storage_execution
        stop.set()
        stopped = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert stopped.status == "stopped"
        assert context.storage_execution == previous
        assert stopped.model_usage is None and stopped.units[0].model_usage is None
        assert read_knowledge_execution(root, previous.execution_id).execution.state == "completed"
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_failed_new_processing_reports_the_old_successful_basis_separately(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore

    root, identity, old = compile_input(tmp_path)
    responses = [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await recompile_document(
            root,
            old.source_id,
            unit_id=old.unit_id,
            scope=storage_scope(root, identity),
        )
        assert first.status == "compiled"
        catalog = CatalogStore(root, identity)
        changed = tmp_path / "fixture.txt"
        changed.write_text("The new value is 23.")
        source = catalog.admit(changed, source_key="fixture", expected=old.source_revision_id)
        processing = catalog.retain_processing(
            source.revision_id,
            {"fixture.md": changed},
            body_path="fixture.md",
            fingerprint="long-v1",
            operation_id="long-v1",
            decision=classify_markdown_tokens(6000),
        )
        target = catalog.plan_unit(
            processing.processing_id,
            key="body",
            doc_name="fixture",
            expected_generation=old.generation,
        )
        stopped = False

        def progress(event):
            nonlocal stopped
            if (
                event.get("event") == "activity"
                and event["data"].get("stage") == "compile.inputs.v1"
            ):
                stopped = True

        result = await recompile_document(
            root,
            old.source_id,
            unit_id=old.unit_id,
            scope=storage_scope(root, identity),
            context=ExecutionContext(cancelled=lambda: stopped, on_event=progress),
        )
        assert result.status == "stopped", result.message
        [unit] = result.units
        assert unit.successful_revision_id == old.unit_revision_id
        assert unit.target_revision_id == target.unit_revision_id
        assert unit.processing["length_class"] == "short"
        assert unit.target_processing["length_class"] == "long"
        assert unit.model_usage["current"]["actual_sends"] == 0
        assert unit.model_usage["cumulative"]["actual_sends"] == 2
        assert len(service.requests) == 2
