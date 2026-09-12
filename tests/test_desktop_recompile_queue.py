"""Recompile submission must not wait for an unrelated document's write lease."""

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from openkb.desktop.documents import DocumentsDialog
from openkb.desktop.io import LocalIO
from openkb.locks import kb_ingest_lock


def pump(app, predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def inventory(kb_dir, monkeypatch):
    app = QApplication.instance() or QApplication([])
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps(
            {
                "legacy-a": {"name": "a.md", "doc_name": "a", "type": "md"},
                "legacy-b": {"name": "b.md", "doc_name": "b", "type": "md"},
            }
        )
    )
    window = QWidget()
    requests = []
    window.io = LocalIO()
    window.manager = SimpleNamespace(
        submit=lambda kb, units: requests.append(tuple(units)) or str(len(requests)),
        get=lambda task: SimpleNamespace(state="queued"),
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    panel = DocumentsDialog(window, kb_dir)
    panel.show()
    assert pump(app, lambda: panel.table.rowCount() == 2)
    yield app, panel, requests
    panel.done(0)
    window.io.stop()
    assert pump(app, window.io.stopped)
    window.close()


def test_selected_legacy_source_can_queue_while_another_document_holds_kb(inventory, kb_dir):
    app, panel, requests = inventory
    entered, release = threading.Event(), threading.Event()

    def writer():
        with kb_ingest_lock(kb_dir / ".openkb"):
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(2)
        panel.table.selectRow(0)
        panel.recompile_selected.click()
        assert pump(app, lambda: bool(requests), seconds=0.8), (
            "No task was submitted while the other document was running"
        )
        assert requests[0][0].file_hash == "legacy-a"
        assert requests[0][0].source_revision and requests[0][0].version is None
    finally:
        release.set()
        thread.join(2)


def test_panel_can_queue_another_source_after_its_first_submission(inventory):
    app, panel, requests = inventory
    panel.table.selectRow(0)
    panel.recompile_selected.click()
    assert pump(app, lambda: len(requests) == 1)
    assert not panel.recompile_selected.isEnabled()
    panel.recompile(all_docs=False)
    assert len(requests) == 1
    panel.table.selectRow(1)
    assert panel.recompile_selected.isEnabled(), "The first task disabled unrelated sources"
    panel.recompile_selected.click()
    assert pump(app, lambda: len(requests) == 2)
