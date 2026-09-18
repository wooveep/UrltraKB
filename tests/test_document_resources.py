"""Document work drains active batches and preserves recovery at resource limits."""

import json

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


def test_memory_pressure_stops_before_model_work_without_losing_original(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from openkb import resource_budget

    monkeypatch.setattr(
        resource_budget,
        "memory_sample",
        lambda: {"available": 256 * 1024**2, "resident": 192 * 1024**2, "private": None},
    )
    path = tmp_path / "resource.md"
    path.write_text("# Maintenance\n\nRestart only after making a verified backup.")
    calls = []

    def respond(body):
        calls.append(body)
        return evidence_response(json.loads(body["messages"][-1]["content"]))

    model_service.respond = respond
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "unfinished"
    assert result.reason == "resource_memory_insufficient"
    assert result.input_version and result.source_id
    assert calls == []
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_shared_range_cache_evicts_by_bytes_and_is_released(monkeypatch):
    from openkb import resource_budget

    monkeypatch.setattr(resource_budget, "CACHE_BYTES", 2200)
    with resource_budget.resource_scope() as budget:
        first = budget.read("first", lambda: "a" * 1000)
        assert budget.read("first", lambda: "wrong") is first
        budget.read("second", lambda: "b" * 1000)
        budget.read("third", lambda: "c" * 1000)
        assert "first" not in budget.cache
        assert budget.cache_bytes <= 2200
        with resource_budget.resource_scope() as nested:
            assert nested is budget
    assert not budget.cache


def test_attachment_and_retry_share_the_parent_request_allowance(kb_dir, tmp_path, model_service):
    import yaml

    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager
    from tests.document_fixtures import write_docx
    from tests.docx_attachment_fixtures import attached_docx

    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["processing"]["max_requests"] = 3
    config.write_text(yaml.safe_dump(settings))
    child = tmp_path / "instructions.docx"
    write_docx(child, "<w:p><w:r><w:t>Child-only recovery port 9473.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    history = tmp_path / "history"
    manager = TaskManager(history_dir=history)
    try:
        parent_id = manager.submit(kb_dir, [ImportFile(str(parent))])
        parent_view = manager.wait(parent_id, timeout=60)
        assert len(parent_view.child_task_ids) == 1
        child_view = manager.wait(parent_view.child_task_ids[0], timeout=60)
        assert child_view.results[0].document.reason == "request_budget_exhausted"
        before = len(model_service)
        # A retry keeps the original family, including after the manager restarts.
        manager.shutdown(stop=True)
        assert manager.join(10)
        manager = TaskManager(history_dir=history)
        repeated = manager.submit(kb_dir, [ImportFile(str(child))], retry_of=child_view.id)
        retry_view = manager.wait(repeated, timeout=60)
        assert retry_view.results[0].document.reason == "request_budget_exhausted"
        assert len(model_service) == before == 3
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_new_continue_task_keeps_the_original_family_allowance(kb_dir, tmp_path, model_service):
    import yaml

    from openkb.runtime.requests import ContinueSource, ImportFile, ReparseSource
    from openkb.runtime.tasks import TaskManager

    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["processing"]["max_requests"] = 1
    config.write_text(yaml.safe_dump(settings))
    original = tmp_path / "bounded.md"
    original.write_text("# Recovery\n\nRestart only after verifying the backup.")
    history = tmp_path / "continue-history"
    manager = TaskManager(history_dir=history)
    try:
        task_id = manager.submit(kb_dir, [ImportFile(str(original))])
        first = manager.wait(task_id, timeout=60).results[0].document
        assert first.reason == "request_budget_exhausted"
        manager.shutdown(stop=True)
        assert manager.join(10)
        manager = TaskManager(history_dir=history)
        for request in (
            ReparseSource(first.source_id, first.input_version),
            ContinueSource(first.source_id, first.input_version),
            ImportFile(str(original)),
        ):
            followup = manager.submit(kb_dir, [request])
            result = manager.wait(followup, timeout=60).results[0].document
            if not isinstance(request, ReparseSource):
                assert result.reason == "request_budget_exhausted"
        assert result.reason == "request_budget_exhausted"
        assert len(model_service) == 1
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
