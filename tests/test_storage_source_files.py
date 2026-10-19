"""The desktop opens exact retained source bytes after the external file is gone."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_pending_empty_html import html_model_transport  # noqa: F401

pytest_plugins = ("pending_fixtures",)
pytestmark = pytest.mark.integration


def test_desktop_opens_retained_original_and_pdf_from_the_selected_revision(
    kb_dir, writer_document, tmp_path
):
    pytest.importorskip("PySide6.QtWidgets")
    from native_catalog_fixtures import catalog

    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source

    original = writer_document.read_bytes()
    imported = import_document(kb_dir, writer_document)
    assert imported.status == "added", imported.message
    source = read_document_source(kb_dir, imported.source_id)
    expected = tmp_path / "expected-source-files.json"
    expected.write_text(
        json.dumps(
            {
                "source_id": imported.source_id,
                "unit_revision_id": source["unit_revision_id"],
                "original": hashlib.sha256(original).hexdigest(),
                "pdf": source["office"]["pdf_digest"],
            }
        )
    )
    writer_document.write_bytes(b"A later external draft must never be opened")
    catalog(kb_dir).admit(writer_document, source_key=str(writer_document.resolve()))
    writer_document.unlink()
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
            "runpy.run_path(sys.argv[1])['_exercise_source_files']"
            "(Path(sys.argv[2]), Path(sys.argv[3]))",
            str(Path(__file__).resolve()),
            str(kb_dir),
            str(expected),
        ],
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert process.returncode == 0, process.stdout + process.stderr


def _exercise_source_files(root, expected_path):
    import time
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
    from shiboken6 import isValid

    import ultrakb.desktop.source_reader as readers
    from ultrakb.desktop.io import LocalIO
    from ultrakb.documents import read_document_view
    from ultrakb.storage.coordination import try_reclamation

    expected = json.loads(expected_path.read_text())
    display = read_document_view(
        root, expected["source_id"], unit_revision_id=expected["unit_revision_id"]
    )
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.io = LocalIO(window)
    window.read_scope = display.scope
    window.appearance = SimpleNamespace(dark=False)
    window.zoom = SimpleNamespace(currentData=lambda: 1)
    window._follow_link = lambda url: None
    dialog = readers.show_source(window, root, display)
    opened = []
    original_open = readers.QDesktopServices.openUrl
    readers.QDesktopServices.openUrl = lambda url: opened.append(Path(url.toLocalFile())) or True

    def until(predicate, seconds=5):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), "Source I/O did not finish"

    try:
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        assert "打开导入原件" in buttons
        assert "打开导入时生成的 PDF" in buttons
        window.read_scope = None  # The reader owns the original captured selection.
        for label, kind in (
            ("打开导入原件", "original"),
            ("打开导入时生成的 PDF", "pdf"),
        ):
            previous = len(opened)
            buttons[label].click()
            until(lambda: len(opened) > previous)
            assert hashlib.sha256(opened[-1].read_bytes()).hexdigest() == expected[kind]
            assert not opened[-1].is_relative_to(root)
        until(window.io.stopped)
        with try_reclamation(root, display.scope.knowledge.identity) as db:
            assert db is not None
            buttons["打开导入原件"].click()
            end = time.monotonic() + 0.15
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)
            assert len(opened) == 2 and not window.io.stopped()
            dialog.close()
            until(window.io.stopped, seconds=2)
        assert len(opened) == 2
        assert all(not path.exists() for path in opened)
    finally:
        readers.QDesktopServices.openUrl = original_open
        if isValid(dialog):
            dialog.close()
        window.io.stop()
        until(window.io.stopped, seconds=2)
