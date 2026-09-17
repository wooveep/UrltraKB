"""Retired knowledge-base views release their Qt storage and late reads stay inert."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from openkb.desktop.documents import DocumentsDialog
from openkb.desktop.source_review import SourceReview
from openkb.desktop.window import Workbench


class DeferredIO:
    def __init__(self):
        self.pending = []
        self.operations = []

    def submit(self, operation, callback, **kwargs):
        self.pending.append((callback, kwargs))
        self.operations.append(operation)


@pytest.mark.parametrize("view", ["inventory", "source_review", "settings_checks"])
def test_retired_source_views_release_their_storage(kb_dir, view, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Workbench(history_dir=kb_dir / "tasks")
    actual_io = window.io
    deferred = DeferredIO()
    window.io = deferred
    window.kb = kb_dir
    retired = []
    try:
        for _ in range(4):
            if view == "inventory":
                window.workspaces.activate("资料")
                panel, host = window.workspaces.panels["资料"]
                window.workspaces.reset()
            elif view == "source_review":
                panel = host = SourceReview(window, kb_dir, "1" * 32)
                panel.show()
                panel.reject()
            else:
                window.workspaces.activate("设置")
                panel, host = window.workspaces.panels["当前知识库"]
                deferred.pending[-1][0](deferred.operations[-1](), None)
                # Ignore the persistent global-settings view's initial read.
                deferred.pending.clear()
                deferred.operations.clear()
                ocr = panel.fields["parsing"]
                ocr.execution.setCurrentIndex(ocr.execution.findData("service"))
                ocr.check_capability()
                panel.fields["image_understanding"].verify()
                panel.repair()
                ocr.installer.start()
                install = deferred.operations[-1]
                window.workspaces.reset()
            retired.append((panel, host))
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        assert not window.findChildren(DocumentsDialog)
        assert not window.findChildren(SourceReview)
        assert all(not isValid(panel) and not isValid(host) for panel, host in retired)
        for callback, options in deferred.pending:
            if "obsolete" in options:
                assert options["obsolete"]()
            callback({"documents": []}, None)
        if view == "settings_checks":
            assert ocr.installer.stop.is_set()

            # Simulate one last progress report from work already in flight.
            def late_progress(*args, **kwargs):
                assert kwargs["cancelled"]()
                kwargs["progress"]("fixture", 1, 2)

            monkeypatch.setattr("openkb.desktop.ocr_installation.install_ocr", late_progress)
            install()
    finally:
        window.io = actual_io
        window.request_quit()
        import time

        deadline = time.monotonic() + 10
        while not window.shutdown_complete() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.shutdown_complete()
        window.timer.stop()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
