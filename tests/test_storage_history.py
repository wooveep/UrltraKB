"""History lists and reads retain exact committed publications without snapshots."""

import pytest
from test_storage_source_removal import retained_publication

from ultrakb.application.pages import read_page, save_page
from ultrakb.application.refresh import list_knowledge_history
from ultrakb.application.views import storage_scope
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def test_history_is_bounded_and_keeps_its_selected_publication(tmp_path):
    root, identity, _, first = retained_publication(tmp_path)
    page = read_page(root, "summaries/fixture.md", scope=first)
    saved = save_page(root, page.path, "The value is 23.", version=page.version, scope=first)
    assert saved.status == "saved"
    current = storage_scope(root, identity)
    [newest] = list_knowledge_history(root, scope=current, limit=1)
    assert newest["publication_id"] == current.publication_id
    assert newest["change_kind"] == "manual"
    assert "pages" not in newest
    [older] = list_knowledge_history(root, scope=current, after=current.publication_id, limit=1)
    assert older["publication_id"] == first.publication_id
    [fixed] = list_knowledge_history(root, scope=first, limit=1)
    assert fixed["publication_id"] == first.publication_id
    with pytest.raises(ValueError, match="limit|bounded"):
        list_knowledge_history(root, scope=current, limit=0)


def test_history_reads_exact_pages_and_images_after_worktree_changes(tmp_path):
    from ultrakb.application.knowledge_history import history_pages, read_history_page

    root, identity, _, first = retained_publication(tmp_path)
    store = KnowledgeStore(root, identity)
    prepared = store.prepare_changes(
        first.knowledge,
        {
            "summaries/picture.md": b"![figure](../images/figure.png)",
            "images/figure.png": b"old-image",
        },
    )
    store.publish(prepared)
    pictured = storage_scope(root, identity)
    revision = pictured.knowledge.publication.revision_id
    store.publish(store.prepare_changes(pictured.knowledge, {"images/figure.png": b"new-image"}))
    current = storage_scope(root, identity)
    (root / "wiki/summaries").mkdir(parents=True, exist_ok=True)
    (root / "wiki/summaries/picture.md").write_text("An external draft")
    values = history_pages(root, revision, scope=current, limit=1)
    assert len(values) == 1
    more = history_pages(root, revision, scope=current, after=values[0], limit=128)
    assert "summaries/picture.md" in (*values, *more)
    page, assets = read_history_page(root, revision, "summaries/picture.md", scope=current)
    assert page.publication_id == pictured.publication_id
    assert page.knowledge_revision_id == revision
    assert page.validity == "historical"
    assert "figure" in page.body and "external" not in page.body
    assert (
        assets.image(f"ultrakb-knowledge://{pictured.publication_id}/images/figure.png")
        == b"old-image"
    )
    assert storage_scope(root, identity).publication_id == current.publication_id
    with pytest.raises(FileNotFoundError):
        read_history_page(root, "0" * 32, "summaries/picture.md", scope=current)


@pytest.mark.parametrize("adapter", ["cli", "http"])
def test_history_adapters_read_the_same_retained_revision(tmp_path, monkeypatch, adapter):
    root, identity, _, first = retained_publication(tmp_path)
    old = read_page(root, "summaries/fixture.md", scope=first)
    assert (
        save_page(root, old.path, "A replacement", version=old.version, scope=first).status
        == "saved"
    )
    revision_id = first.knowledge.publication.revision_id
    if adapter == "cli":
        import json

        from click.testing import CliRunner

        import ultrakb.cli as cli

        monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
        monkeypatch.setattr(cli, "_selected_scope", lambda *args: storage_scope(root, identity))
        result = CliRunner().invoke(cli.cli, ["refresh", "history", "--limit", "1"])
        assert result.exit_code == 0, result.output
        [metadata] = json.loads(result.output)
        assert metadata["publication_id"] == storage_scope(root, identity).publication_id
        result = CliRunner().invoke(
            cli.cli, ["refresh", "read-history", revision_id, "summaries/fixture.md"]
        )
        assert result.exit_code == 0, result.output
        page = json.loads(result.output)
    else:
        from fastapi.testclient import TestClient

        import ultrakb.api as api
        import ultrakb.api_refresh as api_refresh
        import ultrakb.api_views as api_views

        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
        monkeypatch.setattr(api_refresh, "_resolve_kb", lambda *args: root)
        monkeypatch.setattr(api_views, "view_scope", lambda *args: storage_scope(root, identity))
        with TestClient(api.create_app()) as client:
            response = client.get(
                "/api/v1/refresh/history",
                params={"kb": "fixture", "limit": 1, "publication_id": first.publication_id},
            )
            assert response.status_code == 200, response.text
            assert response.json()[0]["publication_id"] == first.publication_id
            response = client.get(
                "/api/v1/refresh/history/page",
                params={
                    "kb": "fixture",
                    "knowledge_revision_id": revision_id,
                    "path": "summaries/fixture.md",
                },
            )
            assert response.status_code == 200, response.text
            page = response.json()
            absent = client.get(
                "/api/v1/refresh/history/page",
                params={
                    "kb": "fixture",
                    "knowledge_revision_id": "0" * 32,
                    "path": "summaries/fixture.md",
                },
            )
            assert absent.status_code == 404, absent.text
            invalid = client.get(
                "/api/v1/refresh/history/pages",
                params={
                    "kb": "fixture",
                    "knowledge_revision_id": revision_id,
                    "after": "../escape",
                },
            )
            assert invalid.status_code == 400, invalid.text
    assert page["content"] == old.content
    assert page["source_revision_ids"] == list(old.source_revision_ids)
    assert page["publication_id"] == first.publication_id
    assert page["validity"] == "historical"


def test_desktop_history_loads_main_store_and_cancels_a_waiting_read(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip("PySide6.QtWidgets")
    root, identity, _, _ = retained_publication(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
            "runpy.run_path(sys.argv[1])['_exercise_history_desktop']"
            "(Path(sys.argv[2]), sys.argv[3], sys.argv[4])",
            str(Path(__file__).resolve()),
            str(root),
            identity.kb_id,
            identity.generation,
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_history_desktop(root, kb_id, generation):
    import logging
    import time
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QWidget

    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.knowledge_history import KnowledgeHistoryDialog
    from ultrakb.storage.coordination import try_reclamation
    from ultrakb.storage.database import DatabaseIdentity

    logging.disable(logging.CRITICAL)
    identity = DatabaseIdentity(kb_id, generation)
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.read_scope = storage_scope(root, identity)
    window.io = LocalIO(window)
    window.appearance = SimpleNamespace(dark=False)
    window.zoom = SimpleNamespace(currentData=lambda: 1.0)
    dialog = KnowledgeHistoryDialog(window, root, "legacy")

    def until(predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.revisions.count() > 0 and dialog.pages.count() > 0)
        row = next(
            i
            for i in range(dialog.pages.count())
            if dialog.pages.item(i).text() == "summaries/fixture.md"
        )
        dialog.pages.setCurrentRow(row)
        until(lambda: "17" in dialog.reader.toPlainText())
        until(window.io.stopped)
        with try_reclamation(root, identity) as db:
            assert db is not None
            dialog.load_more_pages()
            # This operation must already be waiting for the shared reclaim lease.
            deadline = time.monotonic() + 0.15
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert not window.io.stopped()
            dialog.done(0)
            window.io.stop()
            until(window.io.stopped, seconds=2)
    finally:
        dialog.reader.stop_rendering()
        window.io.stop()
        until(window.io.stopped, seconds=2)
