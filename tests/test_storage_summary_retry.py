"""Explicit failed-summary recovery keeps native receipts and the original budget."""

import json

import pytest
from test_storage_conversation_writes import StreamTransport

from ultrakb.application.document_actions import document_inventory
from ultrakb.application.document_batches import preview_document_batch
from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_execution
from ultrakb.application.source_retry import retry_source
from ultrakb.config import save_config


def failed_summary(kb, service, tmp_path, *, max_calls=4):
    save_config(
        kb / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": max_calls, "retries": 0},
        },
    )
    (kb / ".env").write_text(f"LLM_API_KEY=offline-summary-key\nOPENAI_API_BASE={service.url}\n")
    original = tmp_path / "rule.md"
    original.write_text("# Rule\n\nClose the inlet before maintenance.")
    context = ExecutionContext()
    result = import_document(kb, original, context=context)
    assert result.status == "failed", result.message
    original.unlink()
    assert len(service.requests) == 2
    return result, context.storage_execution


def test_failed_summary_preview_uses_retained_compiler_outcome(kb_dir, tmp_path):
    with StreamTransport([{"content": "invalid summary"}] * 2) as service:
        failed_summary(kb_dir, service, tmp_path)
    [row] = document_inventory(kb_dir)
    preview = preview_document_batch(kb_dir, [row.identity], action="retry")
    assert preview.retry_targets[0].failed_summary is True
    assert "summary" in row.document["units"][0]["unfinished"]


@pytest.mark.parametrize("repaired", [True, False])
def test_explicit_summary_recovery_allows_one_extra_send_without_resetting_budget(
    kb_dir,
    tmp_path,
    repaired,
):
    responses = [{"content": "invalid summary"}] * 2 + (
        [
            {
                "content": json.dumps(
                    {"description": "Rule", "content": "Close the inlet before maintenance."}
                )
            },
            {"content": '{"concepts":[],"entities":[]}'},
        ]
        if repaired
        else [{"content": "still invalid summary"}]
    )
    with StreamTransport(responses) as service:
        failed, original = failed_summary(kb_dir, service, tmp_path)
        [unit] = failed.units
        options = {
            "unit_id": unit.unit_id,
            "source_revision_id": failed.source_revision_id,
            "unit_revision_id": unit.target_revision_id,
        }
        ordinary = retry_source(kb_dir, failed.source_id, **options)
        assert ordinary.status == "failed" and len(service.requests) == 2
        result = retry_source(kb_dir, failed.source_id, retry_failed_response=True, **options)
        assert result.status == ("added" if repaired else "failed"), result.message
        assert len(service.requests) == (4 if repaired else 3)
        repeated = retry_source(kb_dir, failed.source_id, retry_failed_response=True, **options)
        assert repeated.status == ("skipped" if repaired else "failed"), repeated.message
        assert len(service.requests) == (4 if repaired else 3)
    detail = read_knowledge_execution(kb_dir, original.execution_id)
    assert detail.task.budget.model_budget_id == original.model_budget_id
    assert detail.task.budget.used == detail.task.usage.actual_sends == (4 if repaired else 3)


@pytest.mark.parametrize("entry", ["cli", "http", "worker"])
def test_three_entrypoints_forward_explicit_summary_recovery(kb_dir, tmp_path, entry, monkeypatch):
    responses = [{"content": "invalid summary"}] * 2 + [
        {"content": '{"description":"Rule","content":"Close the inlet before maintenance."}'},
        {"content": '{"concepts":[],"entities":[]}'},
    ]
    with StreamTransport(responses) as service:
        failed, original = failed_summary(kb_dir, service, tmp_path)
        [unit] = failed.units
        if entry == "cli":
            from click.testing import CliRunner

            from ultrakb.cli import cli

            output = CliRunner().invoke(
                cli,
                [
                    "--kb-dir",
                    str(kb_dir),
                    "retry-source",
                    failed.source_id,
                    "--retry-failed-response",
                ],
            )
            assert output.exit_code == 0 and '"status": "added"' in output.output, output.output
        elif entry == "http":
            from fastapi.testclient import TestClient

            from ultrakb.api import create_app
            from ultrakb.config import register_kb_alias

            register_kb_alias("summary-retry", kb_dir)
            monkeypatch.setenv("ULTRAKB_API_TOKEN", "summary-retry-token")
            with TestClient(
                create_app(), headers={"Authorization": "Bearer summary-retry-token"}
            ) as client:
                output = client.post(
                    "/api/v1/document/retry",
                    json={
                        "kb": "summary-retry",
                        "source_id": failed.source_id,
                        "source_revision_id": failed.source_revision_id,
                        "unit_revision_id": unit.target_revision_id,
                        "retry_failed_response": True,
                    },
                )
            assert output.status_code == 200 and output.json()["status"] == "added", output.text
        else:
            from ultrakb.runtime.requests import RetrySource
            from ultrakb.runtime.tasks import TaskManager

            manager = TaskManager(history_dir=tmp_path / "task-history")
            try:
                task = manager.submit(
                    kb_dir,
                    [
                        RetrySource(
                            failed.source_id,
                            source_revision_id=failed.source_revision_id,
                            unit_revision_id=unit.target_revision_id,
                            retry_failed_response=True,
                        )
                    ],
                )
                result = manager.wait(task, timeout=30)
                assert result.succeeded == 1, result
            finally:
                manager.shutdown(stop=True)
                assert manager.join(10)
        assert len(service.requests) == 4
    detail = read_knowledge_execution(kb_dir, original.execution_id)
    assert detail.task.budget.model_budget_id == original.model_budget_id
    assert detail.task.budget.used == detail.task.usage.actual_sends == 4
