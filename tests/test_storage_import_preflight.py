"""Failed input freezing and preflight remain queryable without admitted sources."""

import hashlib

import pytest
import test_storage_task_entrypoints as task_cases
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def test_rejected_input_retains_its_actual_preflight_failure_and_frozen_digest(tmp_path):
    root, identity, publication = question_knowledge(tmp_path)
    source = tmp_path / "empty.html"
    source.write_text("<html><body><script>nothing readable</script></body></html>")
    original = source.read_bytes()
    context = ExecutionContext()
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
        assert not service.requests
    assert result.status == "skipped" and "no readable body" in result.message
    assert result.source_id is None and result.source_revision_id is None
    source.unlink()
    detail = read_knowledge_task(
        root, context.storage_execution.task_id, identity=identity, limit=128
    )
    stages = {stage.stage_code: stage for stage in detail.stages}
    freeze, preflight = stages["import.freeze.v1"], stages["import.preflight.v1"]
    assert freeze.state == "completed"
    assert preflight.state == "failed" and preflight.error_code == "ImportTextRejected"
    assert preflight.observation.input.input_name == "empty.html"
    assert preflight.observation.input.input_bytes == len(original)
    assert preflight.observation.input.input_digest == hashlib.sha256(original).hexdigest()
    assert preflight.observation.input.temporary_input_id
    assert (
        freeze.observation.input.temporary_input_id
        == preflight.observation.input.temporary_input_id
    )
    assert preflight.source_revision_id is None
    assert detail.task.budget.used == 0
    assert storage_scope(root, identity).publication_id == publication.publication_id


def test_qt_groups_earlier_input_stages_with_the_admitted_original(imported_task, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.knowledge_tasks import fill_stage_tree

    root, task_id, _ = imported_task
    detail = read_knowledge_task(root, task_id, limit=128)
    observations = [
        stage.observation.input
        for stage in detail.stages
        if stage.stage_code in {"import.freeze.v1", "import.preflight.v1", "import.admit.v1"}
    ]
    assert len(observations) == 3
    assert len({original.temporary_input_id for original in observations}) == 1
    app = widgets.QApplication.instance() or widgets.QApplication([])
    tree = widgets.QTreeWidget()
    try:
        fill_stage_tree(tree, detail.stages)
        assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())].count(
            "guide.md"
        ) == 1
    finally:
        tree.close()
        tree.deleteLater()
        app.processEvents()


def test_missing_input_retains_a_failed_freeze_without_claiming_a_digest(tmp_path):
    root, identity, publication = question_knowledge(tmp_path)
    context = ExecutionContext()
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(
            root, tmp_path / "missing.md", context=context, scope=storage_scope(root, identity)
        )
        assert not service.requests
    assert result.status == "blocked" and result.source_id is None
    detail = read_knowledge_task(
        root, context.storage_execution.task_id, identity=identity, limit=128
    )
    stages = {stage.stage_code: stage for stage in detail.stages}
    freeze = stages["import.freeze.v1"]
    assert freeze.state == "failed"
    assert freeze.observation.input.input_name == "missing.md"
    assert freeze.observation.input.input_bytes is None
    assert freeze.observation.input.input_digest is None
    assert "import.preflight.v1" not in stages and "import.admit.v1" not in stages
    assert detail.task.budget.used == 0
    assert storage_scope(root, identity).publication_id == publication.publication_id
