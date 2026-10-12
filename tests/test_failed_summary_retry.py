"""A deliberate failed-response retry preserves paid work and stays bounded."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def reply(value):
    return 200, response({"role": "assistant", "content": json.dumps(value)})


@pytest.mark.parametrize("accepted_retry", [True, False])
def test_public_failed_response_retry_is_opt_in_and_consumed_once(
    kb_dir, model_service, accepted_retry
):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_retry import retry_source
    from openkb.model_task import read_task

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 6\n"
    )
    accepted = {"description": "Operating condition", "content": "Supervision required."}
    normal = kb_dir / "normal.txt"
    normal.write_text("Normal operation requires supervision.")
    model_service.replies.extend([reply(accepted), reply({})])
    context = ExecutionContext()
    assert import_document(kb_dir, normal, context=context).status == "added"
    task_id = context.model_task.record.task_id
    normal_bytes = (kb_dir / "wiki/summaries/normal.md").read_bytes()
    source = kb_dir / "failed.txt"
    source.write_text("Failed operation requires supervision.")
    model_service.replies.extend([reply({}), reply({})])
    failed = import_document(kb_dir, source, context=ExecutionContext(resume_model_task_id=task_id))
    assert failed.status == "failed"
    assert read_task(kb_dir, task_id).used == 4
    old_ledger = {p: p.read_bytes() for p in (kb_dir / ".openkb/usage/requests").glob("*.json")}
    assert retry_source(kb_dir, failed.source_id).status == "failed"
    assert len(model_service.requests) == 4
    model_service.replies.extend([reply(accepted), reply({})] if accepted_retry else [reply({})])

    retried = retry_source(kb_dir, failed.source_id, retry_failed_response=True)

    assert retried.status == ("added" if accepted_retry else "failed"), retried
    assert (
        len(model_service.requests)
        == read_task(kb_dir, task_id).used
        == (6 if accepted_retry else 5)
    )
    assert len(list((kb_dir / ".openkb/model-tasks").glob("*.json"))) == 1
    assert all(p.read_bytes() == value for p, value in old_ledger.items())
    assert (kb_dir / "wiki/summaries/normal.md").read_bytes() == normal_bytes
    if not accepted_retry:
        manifests = list((kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json"))
        claimed = [
            entry
            for path in manifests
            for entry in json.loads(path.read_text())["entries"].values()
            if entry.get("manual_retry_used")
        ]
        assert len(claimed) == 1
        assert claimed[0]["repair_used"] is True
        assert (
            claimed[0]["manual_retry"]["publication"]["target_revision_id"]
            == failed.units[0].target_revision_id
        )
        assert retry_source(kb_dir, failed.source_id, retry_failed_response=True).status == "failed"
        assert len(model_service.requests) == read_task(kb_dir, task_id).used == 5


def test_latest_failed_publication_cannot_be_paired_with_older_task_response(kb_dir, model_service):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_retry import retry_source
    from openkb.model_task import read_task

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 10\n"
    )
    path = kb_dir / "failed.txt"
    path.write_text("The operation requires supervision.")
    first = ExecutionContext()
    model_service.replies.extend([reply({}), reply({})])
    first_result = import_document(kb_dir, path, context=first)
    assert first_result.status == "failed"
    first_task = first.model_task.record.task_id
    second = ExecutionContext()
    model_service.replies.extend([reply({}), reply({})])
    second_result = import_document(kb_dir, path, context=second)
    assert second_result.status == "failed"
    second_task = second.model_task.record.task_id
    assert first_task != second_task
    assert first_result.source_id == second_result.source_id
    assert first_result.units[0].target_revision_id == second_result.units[0].target_revision_id
    assert read_task(kb_dir, first_task).used == read_task(kb_dir, second_task).used == 2
    before = len(model_service.requests)
    old_ledger = {p: p.read_bytes() for p in (kb_dir / ".openkb/usage/requests").glob("*.json")}
    model_service.replies.append(reply({}))

    result = retry_source(
        kb_dir,
        first_result.source_id,
        context=ExecutionContext(resume_model_task_id=first_task),
        retry_failed_response=True,
    )

    assert result.status == "failed"
    assert len(model_service.requests) == before
    assert read_task(kb_dir, first_task).used == read_task(kb_dir, second_task).used == 2
    assert all(p.read_bytes() == b for p, b in old_ledger.items())
    entries = [
        entry
        for p in (kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json")
        for entry in json.loads(p.read_text())["entries"].values()
    ]
    assert not any(entry.get("manual_retry_used") for entry in entries)
