"""Capacity failure is an actionable import result, retaining paid work and receipts."""

import json

import pytest


@pytest.fixture
def retained_index(tmp_path, monkeypatch):
    """Assemble historical domain state, never a successful model response."""
    import hashlib
    from pathlib import Path

    import pymupdf
    from error_transport import ErrorTransport
    from test_condb_pageindex import indexed_document

    from ultrakb import config
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.views import view_scope
    from ultrakb.condb_storage import ConDBPageIndexStorage
    from ultrakb.index_checkpoint import checkpoint_root, reusable_index
    from ultrakb.index_location import IndexLocation
    from ultrakb.indexer import IndexResult, _convert_pdf_to_pages, _write_long_doc_artifacts
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_usage import begin_request, finish_request, usage_context
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.normalization import (
        normalization_fingerprint,
        restore_normalization,
        retain_normalization,
    )
    from ultrakb.source_catalog import admit_source_revision
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        plan_import_units,
        prepare_compile_view,
        record_unit_failure,
    )

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    source = tmp_path / "manual.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text(
            (72, 72), "Before maintenance, close the inlet. Inspect the seal before reopening."
        )
        pdf.save(source)
    kb = tmp_path / "kb"
    with ErrorTransport(status=401) as service:
        initialize_kb(
            kb,
            model="deepseek/deepseek-flash",
            api_key="offline-capacity-key",
            openai_api_base=service.url,
            seed_environment=False,
        )
        values = config.load_config(kb / ".ultrakb/config.yaml")
        values.update(
            model_capacity={**template_capacity(CONNECTION_TEMPLATES[2]), "max_input_tokens": 5000},
            pdf_short_max_pages=0,
            language="en",
            model_policy={"max_calls": 8, "retries": 0, "backoff": 0},
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
                    kb, admission, prepared, fingerprint, unit=unit, scope=scope, bundle=bundle
                )
                with prepare_compile_view(kb) as view:
                    working = view.scope.wiki_dir.parent
                    converted = restore_normalization(directory, normalized, working)
                    assert converted.is_long_doc
                    index_input = converted.pdf_path or converted.raw_path
                    assert index_input.suffix == ".pdf"

                    def store_history():
                        location = IndexLocation.package(working / "index")
                        pages = _convert_pdf_to_pages(
                            index_input, unit.doc_name, working / "wiki/sources/images"
                        )
                        document = indexed_document(location.inputs_root, name=unit.doc_name)
                        Path(document["file_path"]).write_bytes(index_input.read_bytes())
                        document["metadata"]["source_digest"] = hashlib.sha256(
                            index_input.read_bytes()
                        ).hexdigest()
                        document["pages"] = pages
                        document["structure"][0]["text"] = pages[0]["content"]
                        tree = {
                            key: document[key]
                            for key in ("doc_name", "doc_description", "structure")
                        }
                        with ConDBPageIndexStorage(location) as storage:
                            storage.get_or_create_collection("default")
                            storage.save_document("default", "document", document)
                        _write_long_doc_artifacts(
                            tree, pages, unit.doc_name, "document", kb, scope=view.scope
                        )
                        return IndexResult("document", document["doc_description"], tree)

                    reusable_index(
                        kb,
                        admission,
                        revision,
                        normalized,
                        index_input,
                        working,
                        store_history,
                    )
                    marker = (
                        checkpoint_root(kb, admission.source.source_id)
                        / revision.unit_revision_id
                        / "checkpoint.json"
                    )
                    state = state.model_copy(
                        update={
                            "completed_stages": ("conversion", "indexing"),
                            "recovery_point": marker.relative_to(kb).as_posix(),
                        }
                    )
                    record_unit_failure(
                        kb, state, "compilation", ValueError("Historical interruption")
                    )
                # A historical billing receipt is ledger input, not generated model content.
                with usage_context(
                    source_id=admission.source.source_id,
                    source_revision_id=admission.revision.source_revision_id,
                    unit_id=unit.unit_id,
                    unit_revision_id=revision.unit_revision_id,
                ):
                    # Restore matching historical consumption in the task ledger.
                    context.executor.reserve("document_index")
                    request = begin_request("deepseek/deepseek-flash", "document_index.summary")
                    finish_request(
                        request,
                        {"usage": {"prompt_tokens": 41, "completion_tokens": 13}},
                        send_status="sent",
                    )
        assert context.executor.calls == 1 and service.requests == []
        yield kb, admission, state, marker, service, context.model_task.record.task_id


def _capacity_worker(*args):
    """Inject only a negative compiler boundary; the real SDK guard rejects it."""
    from unittest.mock import patch

    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.llm_execution import ModelRequest, active_executor
    from ultrakb.runtime.worker import run_unit

    async def rejected(doc_name, summary_path, *unused, **kwargs):
        reader = CompileSourceWindows(summary_path.parent.parent, doc_name)
        window = next(reader.iter_document_windows(fits=lambda _: True))
        messages = summary_messages(
            {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}, doc_name, window
        )
        # Capacity rejection must not depend on how verbose the current prompt is.
        measure = active_executor().request_measure("compile")
        messages.append({"role": "user", "content": "capacity-check " * (measure.input_limit + 1)})
        await active_executor().acomplete(
            "compile",
            ModelRequest(
                messages,
                operation="compile",
                stage="compile.document-summary:1",
                generation_options={"response_format": {"type": "json_object"}},
            ),
        )
        raise AssertionError("The complete request must fail before any transport send")

    with patch("ultrakb.agent.compiler.compile_long_doc", rejected):
        run_unit(*args)


@pytest.mark.parametrize("boundary", ["wire", "preflight"])
def test_capacity_failure_survives_worker_receipt_and_restart(
    retained_index, tmp_path, monkeypatch, boundary
):
    import ultrakb.runtime.tasks as tasks
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.model_task import read_task
    from ultrakb.runtime.records import UnitResult
    from ultrakb.runtime.requests import RetrySource
    from ultrakb.unit_publication import read_head, read_unit_publication

    kb, admission, state, marker, service, model_task_id = retained_index
    retained = marker.read_bytes()
    original = (kb / admission.revision.original).read_bytes()
    head = read_head(kb)
    manual = live_scope(kb).wiki_dir / "concepts/operator-note.md"
    human_content = "# Operator review\nKeep this manually maintained procedure.\n"
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(manual, human_content)
    if boundary == "wire":
        monkeypatch.setattr(tasks, "run_unit", _capacity_worker)
    history = tmp_path / "tasks"
    manager = tasks.TaskManager(history_dir=history, max_workers=1)
    try:
        task_id = manager.submit(kb, [RetrySource(admission.source.source_id)])
        task = manager.wait(task_id, timeout=30)
        assert task.processes_reaped and task.state == "blocked"
        [result] = task.results
        assert result.status == "blocked" and "ContextCapacityError" in result.error
        assert "Operation failed" not in result.error
        assert result.unfinished == ("compilation",)
        [outcome] = result.import_units
        assert outcome.completed_stages == ("conversion", "indexing")
        assert set(outcome.completed_stages) <= set(task.completed_stages)
        assert outcome.recovery_point == state.recovery_point
        assert outcome.error_type == "ContextCapacityError"
        assert result.resources and outcome.model_usage
        assert result.model_usage["current"]["requests"] == 0
        assert result.model_usage["current"]["actual_sends"] == 0
        assert result.model_usage["cumulative"]["input_total"] == 41
        assert result.model_usage["cumulative"]["output_total"] == 13
        assert outcome.model_usage["cumulative"]["total_known"] == 54
        [model_task] = result.model_usage["model_tasks"]
        assert model_task["task_id"] == model_task_id
        assert model_task["actual_sends"] == 1 and model_task["limit"] == 8
        assert read_task(kb, model_task_id).used == 1
        assert read_head(kb) == head
        assert manual.read_text() == human_content
        assert marker.read_bytes() == retained
        assert (kb / admission.revision.original).read_bytes() == original
        persisted = read_unit_publication(kb, state.unit_id)
        assert persisted.status == "blocked" and persisted.unfinished == ("compilation",)
        assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
        assert service.requests == []
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
    restarted = tasks.TaskManager(history_dir=history)
    try:
        restored = restarted.get(task_id)
        assert restored.results == task.results
        assert restored.model_usage == task.model_usage
        assert set(state.completed_stages) <= set(restored.completed_stages)
    finally:
        restarted.shutdown(stop=True)
        assert restarted.join(10)


def test_short_import_capacity_failure_retains_completed_conversion(kb_dir, monkeypatch):
    from ultrakb import config
    from ultrakb.application.documents import import_document
    from ultrakb.model_tokenizer import ContextCapacityError
    from ultrakb.normalization import NormalizedInput, read_normalization
    from ultrakb.unit_publication import read_head, read_unit_publication

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", kb_dir / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", kb_dir / "profile/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    source = kb_dir / "raw/safety.md"
    source.write_text("# Safety\nClose the inlet before inspecting the seal.\n")
    before = source.read_bytes()
    head = read_head(kb_dir)
    starts = []

    async def rejected(*args, **kwargs):
        starts.append(1)
        raise ContextCapacityError("Complete compile request exceeds captured model capacity")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", rejected)
    result = import_document(kb_dir, source)
    assert result.status == "blocked" and result.unfinished == ("compilation",)
    [outcome] = result.units
    assert outcome.completed_stages == ("conversion",)
    assert outcome.recovery_point
    marker = kb_dir / outcome.recovery_point
    retained = NormalizedInput.model_validate_json(marker.read_text())
    directory, restored = read_normalization(kb_dir, retained.normalization_id)
    assert restored == retained and directory.is_dir() and not restored.is_long
    assert outcome.error_type == "ContextCapacityError"
    assert read_unit_publication(kb_dir, outcome.unit_id).completed_stages == ("conversion",)
    assert starts == [1] and read_head(kb_dir) == head
    assert source.read_bytes() == before
    assert result.model_usage["current"]["requests"] == 0
