"""The public diagnostic and repair workflow uses only retained native facts."""

import json

import pytest

from ultrakb.application.repair import (
    inspect_knowledge_base,
    read_diagnostic_page,
    repair_knowledge_base,
)
from ultrakb.config import save_config
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import StorageFormatError, create_database, database_path
from ultrakb.storage.knowledge_store import KnowledgeStore


def _knowledge(tmp_path):
    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"language": "en"})
    store = KnowledgeStore(root, identity)
    publication = store.publish(
        store.prepare_revision({"index.md": b"# Retained\n"}, expected=None)
    )
    return root, identity, store, publication


def test_diagnostic_reads_committed_pages_without_a_working_copy(tmp_path):
    root, identity, _, _ = _knowledge(tmp_path)
    assert not (root / "wiki").exists()
    (root / "raw").mkdir()
    (root / "raw/draft.md").write_text("unadmitted draft")
    path = database_path(root)
    before = (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes())
    diagnostic = inspect_knowledge_base(root)
    assert diagnostic.pages == ("index.md",)
    assert not diagnostic.needs_repair
    assert read_diagnostic_page(root, "index.md") == "# Retained\n"
    assert (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes()) == before


def test_only_explicit_repair_rebuilds_missing_navigation(tmp_path):
    from ultrakb.storage.navigation_store import RecoveryRequired

    root, identity, store, publication = _knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "DELETE FROM ukb_revision_navigation WHERE revision_id = ?", (publication.revision_id,)
        )
    with pytest.raises(RecoveryRequired, match="navigation"):
        store.bind()
    diagnostic = inspect_knowledge_base(root)
    assert diagnostic.needs_repair
    assert diagnostic.pages == ("index.md",)
    assert read_diagnostic_page(root, "index.md") == "# Retained\n"
    with pytest.raises(RecoveryRequired, match="navigation"):
        store.bind()
    repaired = repair_knowledge_base(root)
    assert repaired.repaired, repaired
    assert store.bind().publication_id == publication.publication_id
    assert store.bind().read("index.md") == b"# Retained\n"
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM ukb_usage_requests").fetchone()[0] == 0


@pytest.mark.parametrize("entrypoint", ["cli", "http", "desktop"])
def test_explicit_navigation_repair_is_available_without_reading_or_model_configuration(
    tmp_path, monkeypatch, entrypoint
):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, identity, store, publication = _knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as sql:
        sql.execute("DELETE FROM ukb_revision_navigation")
    if entrypoint == "cli":
        response = CliRunner().invoke(cli, ["--kb-dir", str(root), "storage", "repair-navigation"])
        assert response.exit_code == 0, response.output
        result = json.loads(response.output)
    elif entrypoint == "http":
        register_kb_alias("repair-test", root)
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "repair-test-token")
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/storage/repair-navigation",
                headers={"Authorization": "Bearer repair-test-token"},
                json={
                    "kb": "repair-test",
                    "kb_id": identity.kb_id,
                    "generation": identity.generation,
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
    else:
        import time
        from dataclasses import asdict

        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        widgets = pytest.importorskip("PySide6.QtWidgets")
        from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageMaintenanceDialog(root)

        def wait():
            deadline = time.monotonic() + 5
            while dialog.busy and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert not dialog.busy

        try:
            wait()
            dialog.repair_navigation()
            wait()
            assert dialog.last_result is not None, dialog.body.toPlainText()
            result = asdict(dialog.last_result)
        finally:
            dialog.done(0)
            deadline = time.monotonic() + 5
            while not dialog.io.stopped() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert dialog.io.stopped()
    assert result["status"] == "completed"
    assert result["publication_id"] == publication.publication_id
    assert store.bind().publication_id == publication.publication_id
    assert store.bind().read("index.md") == b"# Retained\n"
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM ukb_usage_requests").fetchone()[0] == 0


def test_repair_recovers_only_the_settings_pair_and_preserves_paused_edits(tmp_path):
    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.configuration_lock import configuration_lock_path
    from ultrakb.locks import file_write_lock
    from ultrakb.mutation import repair_marker, snapshot_paths

    root, identity, _, _ = _knowledge(tmp_path)
    # Sync is a separate explicit operation; a pending projection is not missing knowledge.
    from ultrakb.storage.wiki_projection import ProjectionStore

    assert ProjectionStore(root, identity).sync().status == "synced"
    begin_external_edit(root, identity)
    (root / "wiki/index.md").write_text("human editor draft")
    config = root / ".ultrakb/config.yaml"
    with file_write_lock(configuration_lock_path(root)):
        interrupted = snapshot_paths(root, [config, root / ".env"], operation="settings")
        save_config(config, {"language": "zh"})
        repair_marker(root).write_text("{}")
    repaired = repair_knowledge_base(root)
    assert repaired.repaired, repaired
    assert repaired.projection_status == "paused"
    assert "en" in config.read_text()
    assert not interrupted.journal_path.exists()
    assert not repair_marker(root).exists()
    assert (root / "wiki/index.md").read_text() == "human editor draft"
    assert read_diagnostic_page(root, "index.md") == "# Retained\n"


def test_repair_retains_unknown_journals_without_touching_published_content(tmp_path):
    root, _, store, publication = _knowledge(tmp_path)
    journal = root / ".ultrakb/journal/unknown.json"
    journal.parent.mkdir(parents=True)
    original = json.dumps({"operation": "publish", "publication": {"old": "pointer"}}).encode()
    journal.write_bytes(original)
    result = repair_knowledge_base(root)
    assert not result.repaired
    assert journal.read_bytes() == original
    assert store.bind().publication_id == publication.publication_id
    assert read_diagnostic_page(root, "index.md") == "# Retained\n"


@pytest.mark.parametrize(
    "operation", [inspect_knowledge_base, repair_knowledge_base, read_diagnostic_page]
)
def test_diagnostic_and_repair_refuse_old_format_before_writes(tmp_path, operation):
    root = tmp_path / "old"
    config = root / ".ultrakb/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("language: en\n")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(StorageFormatError):
        operation(root, "index.md") if operation is read_diagnostic_page else operation(root)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
