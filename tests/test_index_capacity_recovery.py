"""A blocked index keeps normalized originals, historical usage and its recovery marker."""

import json

import pytest
from error_transport import ErrorTransport

pytestmark = pytest.mark.integration


@pytest.fixture
def retained_normalization(tmp_path, monkeypatch):
    import pymupdf

    from ultrakb import config
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.views import view_scope
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_usage import begin_request, finish_request, usage_context
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.normalization import normalization_fingerprint, retain_normalization
    from ultrakb.source_catalog import admit_source_revision, record_path
    from ultrakb.unit_publication import begin_unit_attempt, plan_import_units, record_unit_failure

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    source = tmp_path / "manual.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text(
            (72, 72), "检修前关闭进水阀，检查密封后重新开启。", fontname="china-s"
        )
        pdf.save(source)
    kb = tmp_path / "kb"
    with ErrorTransport(status=401) as service:
        initialize_kb(
            kb,
            model="deepseek/deepseek-flash",
            api_key="offline-index-key",
            openai_api_base=service.url,
            seed_environment=False,
        )
        values = config.load_config(kb / ".ultrakb/config.yaml")
        values.update(
            model_capacity={**template_capacity(CONNECTION_TEMPLATES[2]), "max_input_tokens": 96},
            pdf_short_max_pages=0,
            model_policy={"max_calls": 8, "retries": 2, "backoff": 0},
        )
        config.save_config(kb / ".ultrakb/config.yaml", values)
        context = ExecutionContext()
        with kb_ingest_lock(kb / ".ultrakb"), context.begin(kb) as bundle:
            with prepared_input(source) as prepared:
                admission = admit_source_revision(kb, prepared)
                scope = view_scope(kb, "legacy")
                fingerprint = normalization_fingerprint(
                    kb,
                    scope=scope,
                    bundle=bundle,
                    source_revision=admission.revision,
                    doc_name=admission.source.doc_name,
                )
                unit, revision = plan_import_units(kb, admission, fingerprint)
                state, runnable = begin_unit_attempt(kb, unit, revision)
                assert runnable
                context.bind_model_input(
                    admission.source.source_id, admission.revision.source_revision_id
                )
                directory, normalized = retain_normalization(
                    kb, admission, prepared, fingerprint, scope=scope, bundle=bundle, unit=unit
                )
                marker = record_path(kb, "normalizations", normalized.normalization_id)
                state = state.model_copy(
                    update={
                        "completed_stages": ("conversion",),
                        "recovery_point": marker.relative_to(kb).as_posix(),
                    }
                )
                record_unit_failure(kb, state, "indexing", ValueError("Historical interruption"))
                # Historical ledger input; no generated model reply is supplied.
                with usage_context(
                    source_id=admission.source.source_id,
                    source_revision_id=admission.revision.source_revision_id,
                    unit_id=unit.unit_id,
                    unit_revision_id=revision.unit_revision_id,
                ):
                    context.executor.reserve("document_index")
                    request = begin_request("deepseek/deepseek-flash", "document_index.structure")
                    finish_request(
                        request,
                        {"usage": {"prompt_tokens": 41, "completion_tokens": 13}},
                        send_status="sent",
                    )
        assert context.executor.calls == 1 and service.requests == []
        yield kb, admission, state, marker, directory, service, context.model_task.record.task_id


def test_index_capacity_block_preserves_history_and_normalization_through_worker_restart(
    retained_normalization, tmp_path
):
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.model_task import read_task
    from ultrakb.runtime.records import UnitResult
    from ultrakb.runtime.requests import RetrySource
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.unit_publication import read_head, read_unit_publication

    kb, admission, state, marker, directory, service, model_task_id = retained_normalization
    retained = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    retained[marker] = marker.read_bytes()
    original = (kb / admission.revision.original).read_bytes()
    head = read_head(kb)
    manual = live_scope(kb).wiki_dir / "concepts/operator-note.md"
    content = "# 人工维护\n必须保留现场确认的检修步骤。\n"
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(manual, content)
    history = tmp_path / "tasks"
    manager = TaskManager(history_dir=history, max_workers=1)
    try:
        task_id = manager.submit(kb, [RetrySource(admission.source.source_id)])
        task = manager.wait(task_id, timeout=30)
        assert task.processes_reaped and task.state == "blocked"
        [result] = task.results
        assert result.status == "blocked" and "ContextCapacityError" in result.error
        assert "Complete document_index request" in result.error
        assert result.unfinished == ("indexing",)
        [outcome] = result.import_units
        assert outcome.completed_stages == ("conversion",)
        assert "conversion" in task.completed_stages
        assert outcome.recovery_point == state.recovery_point
        assert outcome.error_type == "ContextCapacityError"
        assert result.resources and outcome.model_usage
        assert result.model_usage["current"]["actual_sends"] == 0
        assert result.model_usage["current"]["requests"] == 1
        assert result.model_usage["current"]["unsent_attempts"] == 1
        assert result.model_usage["current"]["usage_complete"]
        assert result.model_usage["current"]["in_flight_requests"] == 0
        assert result.model_usage["cumulative"]["input_total"] == 41
        assert result.model_usage["cumulative"]["output_total"] == 13
        [model_task] = result.model_usage["model_tasks"]
        assert model_task["task_id"] == model_task_id
        assert model_task["actual_sends"] == 1 and model_task["limit"] == 8
        assert read_task(kb, model_task_id).used == 1
        assert read_head(kb) == head and manual.read_text() == content
        assert all(path.read_bytes() == data for path, data in retained.items())
        assert (kb / admission.revision.original).read_bytes() == original
        assert not list((kb / ".ultrakb/index-checkpoints").rglob("checkpoint.json"))
        saved = read_unit_publication(kb, state.unit_id)
        assert saved.status == "blocked" and saved.unfinished == ("indexing",)
        assert saved.recovery_point == state.recovery_point
        assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
        assert service.requests == []
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
    restarted = TaskManager(history_dir=history)
    try:
        restored = restarted.get(task_id)
        assert restored.results == task.results
        assert restored.model_usage == task.model_usage
        assert restored.completed_stages == task.completed_stages
    finally:
        restarted.shutdown(stop=True)
        assert restarted.join(10)
