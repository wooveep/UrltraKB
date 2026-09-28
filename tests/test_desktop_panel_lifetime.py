"""Retired knowledge-base views release their Qt storage and late reads stay inert."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from openkb.desktop.documents import DocumentsDialog
from openkb.desktop.window import Workbench


class DeferredIO:
    def __init__(self):
        self.pending = []
        self.operations = []

    def submit(self, operation, callback, **kwargs):
        self.pending.append((callback, kwargs))
        self.operations.append(operation)


@pytest.mark.parametrize("view", ["inventory", "settings"])
def test_retired_source_views_release_their_storage(kb_dir, view):
    # Other unit tests legitimately own a QCoreApplication. Widgets require
    # their own QApplication, which cannot replace that process-wide singleton.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_check'](Path(sys.argv[2]), sys.argv[3])",
            str(Path(__file__).resolve()),
            str(kb_dir),
            view,
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _check(kb_dir, view):
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
            else:
                window.workspaces.activate("设置")
                panel, host = window.workspaces.panels["当前知识库"]
                window.workspaces.reset()
            retired.append((panel, host))
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        assert not window.findChildren(DocumentsDialog)
        assert all(not isValid(panel) and not isValid(host) for panel, host in retired)
        for callback, options in deferred.pending:
            if options.get("kb") is None:
                continue  # Global settings intentionally survive knowledge-base switches.
            if "obsolete" in options:
                assert options["obsolete"]()
            if options.get("obsolete", lambda: False)():
                callback({"documents": []}, None)
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
