"""Cross-process budgets, ambiguous parent membership and streamed usage regressions."""

import importlib
import json
import time
from unittest.mock import patch

import pytest


def _budget_worker(*args):
    from ultrakb.runtime.worker import run_unit

    def complete(root, source, *, context, **kwargs):
        from ultrakb.application.documents import DocumentResult
        from ultrakb.locks import kb_ingest_lock

        with kb_ingest_lock(root / ".ultrakb"), context.begin(root):
            context.executor.reserve("compile")
            context.executor.reserve("compile")
        return DocumentResult(str(source), "added", ())

    with patch("ultrakb.application.documents.import_document", complete):
        run_unit(*args)


def test_spawned_batch_preserves_one_total_budget(kb_dir, monkeypatch):
    from ultrakb.model_task import ModelTask
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 5\n"
    )
    sources = []
    for index in range(4):
        source = kb_dir.parent / f"budget-{index}.md"
        source.write_text(f"# Plant {index}\nPlant care.")
        sources.append(ImportFile(str(source)))
    monkeypatch.setattr("ultrakb.runtime.tasks.run_unit", _budget_worker)
    manager = TaskManager(history_dir=kb_dir / "task-history")
    try:
        task_id = manager.submit(kb_dir, sources)
        result = manager.wait(task_id, timeout=40)
        records = [
            ModelTask.model_validate_json(p.read_text())
            for p in (kb_dir / ".ultrakb/model-tasks").glob("*.json")
        ]
        assert len(records) == 1
        assert records[0].used == 5
        assert result.succeeded == 2
        assert len(result.results) == 3
        assert result.state == "stopped"
        resumed = manager.wait(manager.submit(kb_dir, sources[2:], retry_of=task_id), timeout=20)
        assert resumed.state == "stopped" and resumed.succeeded == 0
        record = ModelTask.model_validate_json(
            next((kb_dir / ".ultrakb/model-tasks").glob("*.json")).read_text()
        )
        assert record.used == 5 and len(record.execution_task_ids) == 2
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_larger_call_limit_does_not_renew_expired_deadline(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_execution import ModelDeadlineExceeded
    from ultrakb.locks import atomic_write_json, kb_ingest_lock
    from ultrakb.model_task import task_path

    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text("model: openai/gpt-4o-mini\nmodel_policy:\n  max_calls: 5\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), first.begin(kb_dir):
        first.executor.reserve("compile")
    record = first.model_task.record.model_copy(update={"deadline_at": time.time() - 1})
    atomic_write_json(task_path(kb_dir, record.task_id), record.model_dump())
    config.write_text("model: openai/gpt-4o-mini\nmodel_policy:\n  max_calls: 10\n")
    resumed = ExecutionContext(resume_model_task_id=record.task_id)
    with kb_ingest_lock(kb_dir / ".ultrakb"), pytest.raises(ModelDeadlineExceeded):
        with resumed.begin(kb_dir):
            pytest.fail("Expanded calls renewed an expired deadline")
    assert resumed.executor.calls == 1


@pytest.mark.asyncio
async def test_unanchored_neighbor_requires_parent_membership_verification(monkeypatch, live_llm):
    from pageindex.index.physical_sections import SectionBoundary, prepare_entries

    live_llm.install(monkeypatch)
    engine = importlib.import_module("pageindex.index.page_index")
    pages = [("1 Gardening\n1.1 Roses\nCare.\n", 10), ("Cooking\n2.1 Sauces\nStir.\n", 10)]
    boundary = SectionBoundary.from_node(
        {
            "title": "Gardening",
            "start_index": 1,
            "end_index": 2,
            "anchor": {"unit": 1, "range": [0, 11]},
        }
    )
    entries = prepare_entries(
        [{"title": "Sauces", "physical_index": 2, "list_index": 0}], pages, boundary=boundary
    )
    calls = []
    original = engine.llm_acompletion

    async def verify(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.check_title_appearance(entries[0], pages, boundary=boundary)
    assert result["answer"] == "no"
    assert len(calls) == 1


def test_cancellation_receipt_keeps_final_anthropic_stream_usage(kb_dir):
    import httpx

    from ultrakb.llm_usage import read_requests
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.llm_usage_transport import observe_model_call

    chunks = [
        {
            "type": "message_start",
            "message": {
                "id": "anthropic-final",
                "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 3},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 27}},
    ]
    body = "\n\n".join("data: " + json.dumps(chunk) for chunk in chunks)
    with import_usage_execution(kb_dir, "cancelled"):
        with observe_model_call("fixture", "answer") as observed:
            identity = observed.sending()
            observed.received(identity, httpx.Response(200, content=body))
            observed.finish(state="cancelled")
    [record] = read_requests(kb_dir)
    assert record.output_total == 27
    assert record.cached_input == 3
    assert record.usage_status == "known"
    assert record.business_state == "cancelled"
