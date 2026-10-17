"""The real desktop entry point explicitly initializes supported existing KBs."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_workbench_explicit_publication_baseline_is_cancellable(kb_dir):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['_exercise_baseline'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(kb_dir),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_baseline(target):
    import shutil
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest

    from ultrakb import config
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.published_knowledge import current_pointer, initialize_publication

    config.GLOBAL_CONFIG_DIR = target.parent / (target.name + "-settings")
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "config.yaml"
    from ultrakb.desktop.window import Workbench

    shown = target.with_name(target.name + "-shown")
    unknown = target.with_name(target.name + "-unknown")
    damaged = target.with_name(target.name + "-damaged")
    shutil.copytree(target, shown)
    shutil.copytree(target, unknown)
    shutil.copytree(target, damaged)
    atomic_write_text(shown / "wiki/concepts/first.md", "Previously displayed body")
    atomic_write_text(target / "wiki/concepts/first.md", "Existing supported knowledge")
    atomic_write_text(unknown / ".ultrakb/format.json", '{"format":"unknown","version":999}')
    atomic_write_text(current_pointer(damaged), '{"publication_id":"missing-record"}')
    with kb_ingest_lock(shown / ".ultrakb"):
        initialize_publication(shown)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = Workbench(history_dir=target / "desktop-test-tasks")
    window.show()
    warnings = []
    dismiss = QTimer()

    def dismiss_warning():
        for widget in app.topLevelWidgets():
            if isinstance(widget, QtWidgets.QMessageBox) and widget.isVisible():
                warnings.append(widget.text())
                widget.accept()

    dismiss.timeout.connect(dismiss_warning)
    dismiss.start(10)

    def until(predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), f"Timed out; warnings={warnings}"

    def dialog():
        return next(
            (
                widget
                for widget in app.topLevelWidgets()
                if isinstance(widget, QtWidgets.QDialog)
                and widget.isVisible()
                and widget.windowTitle() == "建立公开阅读基线"
            ),
            None,
        )

    def click(owner, text):
        button = next(
            button for button in owner.findChildren(QtWidgets.QPushButton) if button.text() == text
        )
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    entered, release = threading.Event(), threading.Event()

    def writer():
        with kb_ingest_lock(target / ".ultrakb"):
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = None
        try:
            # This path is reachable even when the desktop has no selected KB.
            window.open_knowledge_base(target)
            until(lambda: dialog() is not None)
            assert window.kb is None
            assert not current_pointer(target).exists()
            click(dialog(), "取消")
            until(lambda: dialog() is None)

            window.open_knowledge_base(shown)
            until(lambda: window.page is not None)
            previous_id = window.read_scope.publication_id
            writing = pool.submit(writer)
            assert entered.wait(5)

            window.open_knowledge_base(target)
            until(lambda: dialog() is not None)
            pending = dialog()
            click(pending, "建立公开阅读基线")
            until(
                lambda: "等待"
                in " ".join(label.text() for label in pending.findChildren(QtWidgets.QLabel))
            )
            assert not current_pointer(target).exists()
            assert window.kb == shown
            assert window.read_scope.publication_id == previous_id
            assert "Previously displayed body" in window.reader.toPlainText()
            click(pending, "取消")
            until(lambda: dialog() is None)
            release.set()
            writing.result(5)
            QTest.qWait(350)
            assert not current_pointer(target).exists()
            assert window.kb == shown
            assert window.read_scope.publication_id == previous_id
            assert "Previously displayed body" in window.reader.toPlainText()

            # Retrying requires another explicit click; a cancelled wait cannot initialize it.
            window.open_knowledge_base(target)
            until(lambda: dialog() is not None)
            assert not current_pointer(target).exists()
            click(dialog(), "建立公开阅读基线")
            until(lambda: window.kb == target and window.page is not None, seconds=8)
            assert current_pointer(target).exists()
            assert window.page.body == "Existing supported knowledge"
            assert window.page.publication_id == window.read_scope.publication_id
            assert "Existing supported knowledge" in window.reader.toPlainText()
            assert warnings == []

            window.open_knowledge_base(unknown)
            until(lambda: bool(warnings))
            assert dialog() is None
            assert not current_pointer(unknown).exists()
            assert window.kb == target
            assert "Existing supported knowledge" in window.reader.toPlainText()
            assert "Unsupported knowledge-base format" in warnings[-1]

            warning_count = len(warnings)
            window.open_knowledge_base(damaged)
            until(lambda: len(warnings) > warning_count)
            assert dialog() is None
            assert current_pointer(damaged).read_text() == '{"publication_id":"missing-record"}'
            assert window.kb == target
            assert "Existing supported knowledge" in window.reader.toPlainText()
        finally:
            release.set()
            if writing is not None:
                writing.result(5)
            for widget in app.topLevelWidgets():
                if isinstance(widget, QtWidgets.QDialog):
                    widget.reject()
            window.request_quit()
            until(window.shutdown_complete, seconds=10)
            window.timer.stop()
            dismiss.stop()
