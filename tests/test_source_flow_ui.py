"""Clickable native stages route to real source content and explicit actions."""

import os
from types import SimpleNamespace

import pytest

from tests.test_source_artifacts import source_run as source_fixture

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from openkb.desktop.documents import DocumentsDialog
from openkb.desktop.source_review import SourceReview
from openkb.runtime.source_activity import SourceActivity

source_run = source_fixture


@pytest.fixture
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


class ImmediateIO:
    def __init__(self):
        self.errors = []

    def submit(self, function, callback, **kwargs):
        try:
            value = function()
        except Exception as error:
            self.errors.append(error)
            callback(None, error)
        else:
            callback(value, None)


class Manager:
    def __init__(self):
        self.requests, self.activity = [], None

    def source_activity(self, *args):
        return self.activity

    def tasks(self):
        return ()

    def submit(self, kb, requests):
        self.requests.extend(requests)
        self.activity = SourceActivity("b" * 32, "queued", "queued", ())
        return "b" * 32

    def get(self, task):
        return SimpleNamespace(state="queued")


@pytest.fixture
def window(app, kb_dir):
    window = QWidget()
    window.io, window.manager, window.kb = ImmediateIO(), Manager(), kb_dir
    yield window
    for child in window.findChildren(SourceReview):
        child.done(0)
    for child in window.findChildren(DocumentsDialog):
        child.done(0)
    window.close()


def test_stage_clicks_show_saved_content_and_do_not_submit_work(window, kb_dir, source_run):
    result, _, calls = source_run(stop=True)
    before = list(calls)
    panel = SourceReview(window, kb_dir, result.source_id)
    panel.show()
    assert panel._selected_stage == "generation"
    assert panel.records.count() == 1 and "待校验草稿" in panel.records.currentText()
    for key, tab in [
        ("intake", 0),
        ("parsing", 1),
        ("facts", 5),
        ("planning", 5),
        ("generation", 5),
        ("publication", 6),
    ]:
        QTest.mouseClick(panel.flow.buttons[key], Qt.MouseButton.LeftButton)
        assert panel._selected_stage == key and panel.tabs.currentIndex() == tab
        assert panel.flow.buttons[key].isChecked()
    panel.flow.buttons["facts"].click()
    assert "37 kPa" in panel.record_text.toPlainText()
    assert panel.stage_buttons["continue"].isVisible()
    assert not panel.stage_buttons["reparse"].isVisible()
    assert not window.manager.requests and calls == before
    assert not window.io.errors
    panel.stage_buttons["continue"].click()
    assert window.manager.requests[0].source_id == result.source_id
    assert window.manager.requests[0].version_id == result.input_version
    assert not panel.stage_buttons["continue"].isEnabled()


def test_inventory_single_selection_exposes_flow_and_multiselect_hides_it(
    window, kb_dir, source_run
):
    result, _, _ = source_run(stop=True)
    source_run()
    panel = DocumentsDialog(window, kb_dir)
    panel.show()
    assert panel.table.rowCount() == 2
    panel.table.selectRow(0)
    assert panel.source_flow.isVisible()
    assert any(step.current for step in panel.source_flow._steps)
    from PySide6.QtCore import QItemSelectionModel

    panel.table.selectionModel().select(
        panel.table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert not panel.source_flow.isVisible()
    assert panel.recompile_selected.isEnabled() and not window.manager.requests
    assert not window.io.errors


def test_keyboard_changes_stage_and_completed_source_disables_continue(window, kb_dir, source_run):
    result, _, _ = source_run()
    panel = SourceReview(window, kb_dir, result.source_id)
    panel.show()
    panel.flow.buttons["publication"].setFocus()
    QTest.keyClick(panel.flow.buttons["publication"], Qt.Key.Key_Space)
    assert panel.published_pages.count() > 0
    assert not panel.stage_buttons["continue"].isEnabled()
    assert panel.open_published.isEnabled()
    assert all(step.state == "completed" for step in panel.flow._steps)
    assert not window.io.errors


def test_published_omissions_stay_visible_and_can_be_continued(window, kb_dir, source_run):
    from dataclasses import replace

    from openkb.application.source_history import record_source_result

    result, _, _ = source_run()
    record_source_result(
        kb_dir,
        replace(
            result,
            omissions=(
                {
                    "stage": "generation",
                    "reason": "knowledge_evidence_mismatch",
                    "items": ["concepts/excluded"],
                },
            ),
        ),
    )
    panel = SourceReview(window, kb_dir, result.source_id)
    panel.show()
    assert panel.stage_buttons["continue"].isEnabled()
    steps = {step.key: step for step in panel.flow._steps}
    assert steps["generation"].state == "review"
    assert "已排除 1 项" in steps["generation"].progress
    assert steps["publication"].state == "completed"
    panel.stage_buttons["continue"].click()
    assert window.manager.requests[0].source_id == result.source_id
    assert not window.io.errors


def test_late_artifact_response_cannot_replace_the_newly_selected_stage(window, kb_dir, source_run):
    result, _, _ = source_run()
    panel = SourceReview(window, kb_dir, result.source_id)
    pending = []

    def defer(operation, callback, **kwargs):
        pending.append((callback, operation()))

    window.io.submit = defer
    panel.show_stage("facts")
    panel.show_stage("planning")
    assert len(pending) == 2
    callback, value = pending[1]
    callback(value, None)
    planned = panel.record_text.toPlainText()
    assert "涵盖主题" in planned
    callback, value = pending[0]
    callback(value, None)
    assert panel._selected_stage == "planning" and panel.record_text.toPlainText() == planned


def test_inventory_preserves_selection_after_refresh_and_resets_stage_for_another_source(
    window, kb_dir, source_run
):
    source_run(stop=True)
    source_run()
    panel = DocumentsDialog(window, kb_dir)
    panel.table.selectRow(0)
    first = panel.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    panel.reload()
    selected = panel.table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert panel.table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole) == first
    panel.source_flow.select("intake")
    panel.table.selectRow(1)
    assert panel.source_flow._selected == next(s.key for s in panel.source_flow._steps if s.current)
