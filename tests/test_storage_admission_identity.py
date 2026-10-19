"""Admission and its pre-admission stage association share one durable boundary."""

import json
from contextlib import contextmanager

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.database import Database

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("previous_import", [False, True])
def test_lost_admission_acknowledgement_keeps_the_actual_original_in_task_details(
    tmp_path, monkeypatch, previous_import
):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "original.txt"
    source.write_text("The actual value is VALUE_17.")
    original = source.read_bytes()
    transaction, injected = Database.transaction, False

    @contextmanager
    def lose_acknowledgement(database):
        nonlocal injected
        with transaction(database) as connection:
            yield connection
        if (
            not injected
            and database.connection.execute(
                "SELECT 1 FROM ukb_stage_attempts s JOIN ukb_source_revisions r "
                "ON r.revision_id = s.source_revision_id WHERE s.stage_code = 'import.admit.v1' "
                "AND s.state = 'started' AND r.state = 'ready' LIMIT 1"
            ).fetchone()
        ):
            injected = True
            raise OSError("Admission commit acknowledgement lost")

    context = ExecutionContext(storage_identity=identity)
    prior_context = ExecutionContext(storage_identity=identity)
    responses = [
        {"content": json.dumps({"description": "Value", "content": "VALUE_17"})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        if previous_import:
            prior = import_document(
                root, source, context=prior_context, scope=storage_scope(root, identity)
            )
            assert prior.status == "added", prior.message
        monkeypatch.setattr(Database, "transaction", lose_acknowledgement)
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
    assert injected and result.status == "blocked"
    source.unlink()
    detail = read_knowledge_task(root, context.storage_execution.task_id)
    stage = next(stage for stage in detail.stages if stage.stage_code == "import.admit.v1")
    assert stage.state == "failed" and stage.error_code == "OSError"
    assert stage.input.name == "original.txt" and stage.input.original_bytes == len(original)
    assert CatalogStore(root, identity).read_original(stage.source_revision_id) == original
    assert len(service.requests) == (2 if previous_import else 0)
    if previous_import:
        from ultrakb.storage.execution_store import ExecutionStore

        executions = ExecutionStore(root, identity)
        assert (
            executions.source_execution(stage.source_revision_id)
            == prior_context.storage_execution.execution_id
        )
        assert executions.budget(prior_context.storage_execution.model_budget_id).used == 2


def test_unchanged_input_keeps_the_same_managed_identity_for_distinct_admission_stages(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "original.txt"
    source.write_text("The actual value is VALUE_17.")
    responses = [
        {"content": json.dumps({"description": "Value", "content": "VALUE_17"})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    stages, results = [], []
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        for _ in range(2):
            context = ExecutionContext(storage_identity=identity)
            results.append(
                import_document(root, source, context=context, scope=storage_scope(root, identity))
            )
            detail = read_knowledge_task(root, context.storage_execution.task_id)
            stages.append(next(s for s in detail.stages if s.stage_code == "import.admit.v1"))
    assert [result.status for result in results] == ["added", "skipped"]
    assert (
        stages[0].source_revision_id
        == stages[1].source_revision_id
        == results[0].source_revision_id
    )
    assert (
        stages[0].observation.input.temporary_input_id
        != stages[1].observation.input.temporary_input_id
    )
    assert all(stage.input is not None for stage in stages)
    assert len(service.requests) == 2
