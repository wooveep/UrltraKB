"""The ordinary adapters and native preview use the same managed CNKI source."""

import importlib
import json
import os
import shutil
import subprocess
import sys

import pytest
from click.testing import CliRunner
from test_import_entrypoints import _model_boundary_worker

pytest_plugins = ("cnki_fixtures", "test_pdf_readback")


@pytest.mark.parametrize("entry", ["cli_directory", "api", "desktop", "watch"])
def test_cnki_adapters_publish_a_single_source_with_an_internal_pdf(
    kb_dir, cnki_source, pdf_model, monkeypatch, entry
):
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.documents import read_document_source

    cli = importlib.import_module("openkb.cli")
    monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
    external = kb_dir / "input" / cnki_source.name
    external.parent.mkdir()
    shutil.move(cnki_source, external)
    original = external.read_bytes()
    resources = None
    if entry == "desktop":
        from openkb.runtime.requests import ImportFile
        from openkb.runtime.tasks import TaskManager

        monkeypatch.setattr("openkb.runtime.tasks.run_unit", _model_boundary_worker)
        manager = TaskManager(history_dir=kb_dir / "task-history")
        try:
            identity = manager.submit(kb_dir, [ImportFile(str(external))])
            result = manager.wait(identity, timeout=30)
            assert result.succeeded == 1, result
            assert result.processes_reaped
            resources = result.results[0].resources
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
    elif entry == "api":
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/add",
                data={"kb": "fixture", "stream": "false"},
                files=[("files", (external.name, original, "application/octet-stream"))],
            )
        assert response.status_code == 200, response.text
        assert response.json()["added_count"] == 1
    else:
        command = ["add", str(external.parent)]
        if entry == "watch":
            watched = kb_dir / "raw" / external.name
            shutil.copy2(external, watched)
            monkeypatch.setattr(
                "openkb.watcher.watch_directory",
                lambda raw, callback, **options: callback([str(watched)]),
            )
            command = ["watch"]
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), *command])
        assert result.exit_code == 0, result.output
    documents = get_kb_list(kb_dir)["documents"]
    assert len(documents) == 1
    source = read_document_source(kb_dir, documents[0]["source_id"], pages="1")
    assert source["name"] == external.name
    assert source["cnki"]["internal_format"] == "KDH"
    assert "Original CNKI content." in source["content"]
    assert (kb_dir / source["original_path"]).read_bytes() == original
    assert (kb_dir / source["internal_pdf_path"]).is_file()
    assert "office" not in source
    if resources is not None:
        assert str(kb_dir / source["internal_pdf_path"]) in resources
    assert not external.with_suffix(".pdf").exists()


def test_native_watch_does_not_queue_derived_pdf(kb_dir, cnki_source, pdf_model):
    import time

    from test_native_watch import TaskSink, eventually

    from openkb.application.documents import import_document
    from openkb.runtime.watch import NativeWatch

    source = kb_dir / "raw" / cnki_source.name
    shutil.move(cnki_source, source)
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.03, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        result = import_document(kb_dir, source)
        assert result.status == "added", result.message
        sink.finish("0", revision=result.input_version)
        time.sleep(0.15)
        assert len(sink.items) == 1
    finally:
        watch.stop()
        assert watch.join(5)


def test_native_source_reader_opens_cnki_pdf_without_office_metadata(
    kb_dir, cnki_source, pdf_model, tmp_path
):
    pytest.importorskip("PySide6")
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    imported = import_document(kb_dir, cnki_source)
    source = read_document_source(kb_dir, imported.source_id)
    payload = tmp_path / "source.json"
    payload.write_text(json.dumps(source))
    code = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QLabel
from PySide6.QtGui import QDesktopServices
from openkb.desktop.source_reader import SourceReader
app = QApplication([])
opened = []
QDesktopServices.openUrl = lambda url: opened.append(url.toLocalFile()) or True
root = Path(sys.argv[1])
source = json.loads(Path(sys.argv[2]).read_text())
window = QWidget()
window.appearance = SimpleNamespace(dark=False)
window.zoom = SimpleNamespace(currentData=lambda: 1.0)
reader = SourceReader(window, root, source)
button = next(button for button in reader.findChildren(QPushButton)
              if '生成的 PDF' in button.text())
button.click()
assert opened == [str(root / source['internal_pdf_path'])]
assert any('KDH' in label.text() for label in reader.findChildren(QLabel))
reader.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(kb_dir), str(payload)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
