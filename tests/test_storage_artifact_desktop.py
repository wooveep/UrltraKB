"""Real Qt I/O reads one retained artifact catalog, never the mutable copy."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_storage_artifact_exports import artifact_writer

from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def _png(color):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(1, 1, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_desktop_artifact_paging_read_preview_and_export_use_the_selected_master(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    root, identity, _, save = artifact_writer(tmp_path)
    save("output/decks/selected/index.html", b'<link href="theme.css"><p>Accepted deck v1</p>')
    save("output/decks/selected/theme.css", b"p { color: blue }")
    save("output/decks/selected/picture.png", _png("red"))
    save("output/assets/shared.css", b"p { font-size: 20px }")
    for index in range(65):
        suffix = "MD" if index == 0 else "md"
        save(f"output/notes/{index:03}.{suffix}", f"# Retained note {index}".encode())
    save(
        "output/notes/000.MD",
        b"# Retained note 0\n![Retained](../decks/selected/picture.png)\n"
        b"![Unknown](../decks/selected/unaccepted.png)",
    )
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(knowledge.bind(), {"reports/quality.md": b"# Accepted report v1"})
    )
    draft = root / "output/decks/selected"
    draft.mkdir(parents=True)
    (draft / "index.html").write_text("Unaccepted physical draft")
    (draft / "theme.css").write_text("Unaccepted physical style")
    (draft / "picture.png").write_bytes(_png("blue"))
    (draft / "unaccepted.png").write_bytes(_png("blue"))
    (root / ".ultrakb/config.yaml").unlink()
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
            "runpy.run_path(sys.argv[1])['_exercise_artifact_desktop'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert (draft / "index.html").read_text() == "Unaccepted physical draft"
    assert (draft / "theme.css").read_text() == "Unaccepted physical style"


def _exercise_artifact_desktop(root):
    import logging
    import time
    import zipfile
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from urllib.error import HTTPError
    from urllib.parse import urljoin
    from urllib.request import urlopen

    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication, QWidget

    import ultrakb.desktop.artifacts as artifacts
    import ultrakb.storage.blobs as blobs
    from ultrakb.application.views import storage_scope
    from ultrakb.desktop.io import LocalIO
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.coordination import try_reclamation
    from ultrakb.storage.database import open_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.maintenance import Maintenance

    logging.disable(logging.CRITICAL)
    with open_database(root, read_only=True) as db:
        identity = db.identity
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.scope = live_scope(root)
    window.read_scope = storage_scope(root, identity)
    window.io = LocalIO(window)
    dialog = artifacts.ArtifactsDialog(window, root)
    assert dialog.scope is window.read_scope
    window.read_scope = None
    previews = []
    original_open = artifacts.QDesktopServices.openUrl
    original_directory = artifacts.QFileDialog.getExistingDirectory
    destination = root.parent / "artifact-exports"
    destination.mkdir()
    artifacts.QDesktopServices.openUrl = lambda url: previews.append(url.toString()) or True

    def until(predicate, seconds=8):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), f"Artifact I/O did not finish: {dialog.status.text()}"

    def select(path):
        item = next(
            dialog.items.item(index)
            for index in range(dialog.items.count())
            if dialog.items.item(index).data(Qt.ItemDataRole.UserRole) == path
        )
        dialog.items.setCurrentItem(item)

    try:
        until(lambda: dialog.more_button.isEnabled())
        assert dialog.items.count() == 63
        epoch = dialog._epoch
        store = ArtifactStore(root, identity)
        execution = ExecutionStore(root, identity).begin(
            "artifact", fingerprint="later-artifact-publication", max_calls=0
        )
        for path, payload in {
            "output/decks/selected/index.html": b"<p>Accepted deck v2</p>",
            "output/decks/selected/theme.css": b"p { color: red }",
            "output/decks/selected/picture.png": _png("green"),
            "output/decks/selected/late.md": b"A later accepted attachment",
            "output/later.md": b"A later accepted artifact",
        }.items():
            assert (
                store.save(
                    path,
                    payload,
                    expected_epoch=store.epoch(),
                    execution_id=execution.execution_id,
                    evidence_publication_id=dialog.scope.publication_id,
                ).status
                == "saved"
            )
        knowledge = KnowledgeStore(root, identity)
        knowledge.publish(
            knowledge.prepare_changes(
                knowledge.bind(), {"reports/quality.md": b"# Accepted report v2"}
            )
        )
        dialog.load_more()
        until(lambda: not dialog._page_loading)
        assert dialog.items.count() == 68 and dialog._epoch == epoch
        assert not dialog.more_button.isEnabled()
        select("output/decks/selected")
        until(lambda: "Accepted deck v1" in dialog.source.toPlainText())
        assert dialog.files.count() == 3
        dialog.preview()
        until(lambda: len(previews) == 1)
        with urlopen(previews[0], timeout=2) as response:
            assert response.read() == b'<link href="theme.css"><p>Accepted deck v1</p>'
            assert "base-uri 'self'" in response.headers["Content-Security-Policy"]
        with urlopen(urljoin(previews[0], "theme.css"), timeout=2) as response:
            assert response.read() == b"p { color: blue }"
        with urlopen(urljoin(previews[0], "../../assets/shared.css"), timeout=2) as response:
            assert response.read() == b"p { font-size: 20px }"
        with pytest.raises(HTTPError) as missing:
            urlopen(urljoin(previews[0], "unaccepted.png"), timeout=2)
        assert missing.value.code == 404
        select("output/notes/000.MD")
        until(lambda: "Retained note 0" in dialog.source.toPlainText())
        assets = dialog.reader._snapshot_assets
        assert assets is not None
        prefix = assets.base_url.rsplit("/", 2)[0]
        picture_url = prefix + "/decks/selected/picture.png"
        assert assets.image(picture_url) == store.read(
            "output/decks/selected/picture.png", epoch=epoch
        ), (assets.base_url, picture_url, tuple(assets.images))
        assert assets.image(prefix + "/decks/selected/unaccepted.png") is None
        picture = dialog.reader.loadResource(
            QTextDocument.ResourceType.ImageResource.value, QUrl(picture_url)
        )
        assert picture.toImage().pixelColor(0, 0).name() == "#ff0000"
        select("wiki/reports/quality.md")
        until(lambda: dialog.source.toPlainText() == "# Accepted report v1")
        select("output/decks/selected")
        until(lambda: "Accepted deck v1" in dialog.source.toPlainText())

        def directory_with_concurrent_refresh(*args):
            dialog.refresh()
            until(lambda: not dialog._page_loading)
            return str(destination)

        artifacts.QFileDialog.getExistingDirectory = directory_with_concurrent_refresh
        dialog.export()
        until(lambda: "已导出新副本" in dialog.status.text())
        with zipfile.ZipFile(destination / "selected.zip") as archive:
            assert archive.namelist() == [
                "selected/index.html",
                "selected/picture.png",
                "selected/theme.css",
            ]
            assert archive.read("selected/theme.css") == b"p { color: blue }"
        until(lambda: dialog._epoch != epoch and not dialog._page_loading)
        select("output/decks/selected")
        until(lambda: "Accepted deck v2" in dialog.source.toPlainText())
        assert dialog.files.count() == 4
        until(window.io.stopped)
        dialog.preview()
        end = time.monotonic() + 3
        while not dialog._pending_previews and time.monotonic() < end:
            time.sleep(0.01)  # Deliberately do not deliver queued UI callbacks.
        assert len(dialog._pending_previews) == 1
        undelivered = dialog._pending_previews[0]
        select("output/notes/001.md")
        until(window.io.stopped)
        assert undelivered.stopped.is_set() and not undelivered.thread.is_alive()
        entered, release = Event(), Event()
        original_copy = blobs.copy_blob

        class SlowOutput:
            def __init__(self, output):
                self.output = output

            def write(self, payload):
                entered.set()
                assert release.wait(3)
                return self.output.write(payload)

        def slow_copy(db, digest, output, *, cancelled=None):
            return original_copy(db, digest, SlowOutput(output), cancelled=cancelled)

        blobs.copy_blob = slow_copy
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(urlopen, urljoin(previews[0], "theme.css"), timeout=4)
                assert entered.wait(2)
                assert Maintenance(root, identity).collect(max_items=1).status == "busy"
                began = time.monotonic()
                dialog.close_previews()
                assert time.monotonic() - began < 0.5
                assert not window.io.stopped()
                release.set()
                until(window.io.stopped)
                with pytest.raises(HTTPError) as cancelled:
                    reading.result(timeout=2)
                assert cancelled.value.code == 503
        finally:
            release.set()
            blobs.copy_blob = original_copy
        with try_reclamation(root, identity) as db:
            assert db is not None
            dialog.refresh()
            end = time.monotonic() + 0.15
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)
            assert not window.io.stopped()
            window.io.stop()
            until(window.io.stopped, seconds=2)
            dialog.close()
        with pytest.raises(OSError):
            urlopen(previews[0], timeout=1)
    finally:
        artifacts.QDesktopServices.openUrl = original_open
        artifacts.QFileDialog.getExistingDirectory = original_directory
        dialog.close()
        window.io.stop()
        until(window.io.stopped, seconds=2)
