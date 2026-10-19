"""Real CAS durability/registration gaps are recoverable by explicit maintenance."""

import hashlib
import json
import sqlite3

import pytest

from ultrakb.storage.blobs import blob_path, put_blob
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage, try_reclamation
from ultrakb.storage.database import create_database
from ultrakb.storage.maintenance import Maintenance


@pytest.fixture
def interrupted_blob(tmp_path):
    root = tmp_path / "kb"
    identity = create_database(root)
    payload = b"Durable bytes whose registry transaction was interrupted."
    digest = hashlib.sha256(payload).hexdigest()
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_blob_registration BEFORE INSERT ON ukb_blobs "
            "BEGIN SELECT RAISE(ABORT, 'registration interrupted'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="registration interrupted"):
            put_blob(db, payload)
        db.connection.execute("DROP TRIGGER fail_blob_registration")
        assert db.connection.execute("SELECT digest FROM ukb_blobs").fetchall() == []
    assert blob_path(root, digest).read_bytes() == payload
    return root, identity, digest, payload


def test_registration_gap_is_previewed_and_reclaimed(interrupted_blob):
    root, identity, digest, payload = interrupted_blob
    maintenance = Maintenance(root, identity)
    preview = maintenance.preview()
    assert preview.candidate_count == 1
    assert preview.unregistered_count == 1
    assert preview.unregistered_bytes == len(payload)
    result = maintenance.collect()
    assert result.deleted_count == result.unregistered_deleted == 1
    assert result.deleted_bytes == len(payload)
    assert not blob_path(root, digest).exists()


def test_readmission_after_preview_protects_the_same_bytes(interrupted_blob, tmp_path):
    root, identity, digest, payload = interrupted_blob
    maintenance = Maintenance(root, identity)
    assert maintenance.preview().candidate_count == 1
    source = tmp_path / "original.md"
    source.write_bytes(payload)
    catalog = CatalogStore(root, identity)
    revision = catalog.admit(source, source_key="manual")
    result = maintenance.collect()
    assert result.deleted_count == 0
    assert catalog.read_original(revision.revision_id) == payload
    assert blob_path(root, digest).exists()


def test_physical_orphan_batches_obey_count_and_byte_limits(interrupted_blob):
    root, identity, first_digest, payload = interrupted_blob
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_blob_registration BEFORE INSERT ON ukb_blobs "
            "BEGIN SELECT RAISE(ABORT, 'registration interrupted'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            put_blob(db, payload + b"2")
        db.connection.execute("DROP TRIGGER fail_blob_registration")
    maintenance = Maintenance(root, identity)
    assert maintenance.collect(max_bytes=1).deleted_count == 0
    assert blob_path(root, first_digest).exists()
    preview = maintenance.preview(max_items=1)
    assert preview.candidate_count == 1 and preview.has_more
    first = maintenance.collect(max_items=1)
    second = maintenance.collect(max_items=1)
    assert first.deleted_count == second.deleted_count == 1
    assert first.deleted_bytes + second.deleted_bytes == 2 * len(payload) + 1


def test_unknown_paths_symlinks_and_busy_access_remain_untouched(interrupted_blob, tmp_path):
    root, identity, digest, payload = interrupted_blob
    directory = blob_path(root, digest).parent
    unknown = directory / "user-notes.txt"
    unknown.write_text("Keep unknown files")
    outside = tmp_path / "outside"
    outside.write_text("Never follow links")
    link = directory / (digest[:2] + "0" * 62)
    link.symlink_to(outside)
    maintenance = Maintenance(root, identity)
    with storage_stage(root, identity):
        assert maintenance.collect().status == "busy"
    assert blob_path(root, digest).read_bytes() == payload
    assert maintenance.collect().deleted_count == 1
    assert unknown.read_text() == "Keep unknown files"
    assert link.is_symlink() and outside.read_text() == "Never follow links"
    with try_reclamation(root, identity) as db:
        assert db is not None


def test_interrupted_physical_unlink_can_be_retried(interrupted_blob, monkeypatch):
    root, identity, digest, _ = interrupted_blob
    maintenance = Maintenance(root, identity)
    with monkeypatch.context() as fault:

        def fail_unlink(*args, **kwargs):
            raise OSError("unlink interrupted")

        fault.setattr("ultrakb.storage.physical_orphans.durable_unlink", fail_unlink)
        with pytest.raises(OSError, match="unlink interrupted"):
            maintenance.collect()
    assert blob_path(root, digest).exists()
    assert maintenance.collect().unregistered_deleted == 1


def test_unknown_receipt_ownership_protects_unregistered_files(interrupted_blob):
    root, identity, digest, payload = interrupted_blob
    pending = root / ".ultrakb/receipts/pending"
    pending.mkdir(parents=True)
    (pending / "unreadable.json").write_text("{broken")
    maintenance = Maintenance(root, identity)
    assert maintenance.preview().blocked_reason == "unresolved_pending_receipts"
    assert maintenance.collect().status == "blocked"
    assert blob_path(root, digest).read_bytes() == payload


def test_readmission_between_directory_scan_and_exclusive_lease_is_rechecked(
    interrupted_blob, tmp_path, monkeypatch
):
    from contextlib import contextmanager

    import ultrakb.storage.maintenance as module

    root, identity, _, payload = interrupted_blob
    source = tmp_path / "original.md"
    source.write_bytes(payload)
    actual = module.try_reclamation
    retained = []

    @contextmanager
    def admit_before_lease(*args, **kwargs):
        retained.append(CatalogStore(root, identity).admit(source, source_key="late"))
        with actual(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(module, "try_reclamation", admit_before_lease)
    assert Maintenance(root, identity).collect().deleted_count == 0
    assert CatalogStore(root, identity).read_original(retained[0].revision_id) == payload


@pytest.mark.parametrize("operation", ["preview", "collect"])
def test_directory_discovery_holds_no_content_or_ingest_lease(
    interrupted_blob, monkeypatch, operation
):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from pathlib import Path
    from threading import Event

    from ultrakb.locks import _Lease

    root, identity, digest, _ = interrupted_blob
    entered, release = Event(), Event()
    actual = os.scandir

    @contextmanager
    def slow_directory(path):
        with actual(path) as entries:
            if Path(path) == blob_path(root, digest).parent:
                entered.set()
                assert release.wait(5)
            yield entries

    monkeypatch.setattr("ultrakb.storage.physical_orphans.os.scandir", slow_directory)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(getattr(Maintenance(root, identity), operation))
        try:
            assert entered.wait(5)
            with try_reclamation(root, identity) as db:
                assert db is not None
                lease = _Lease(root / ".ultrakb/ingest.lock", True)
                try:
                    assert lease.try_acquire()
                finally:
                    lease.release()
        finally:
            release.set()
            pending.result(timeout=5)


@pytest.mark.parametrize("adapter", ["cli", "http", "desktop"])
def test_all_maintenance_adapters_reclaim_registration_gaps(interrupted_blob, adapter, monkeypatch):
    root, identity, digest, _ = interrupted_blob
    if adapter == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        result = CliRunner().invoke(cli, ["--kb-dir", str(root), "storage", "collect"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["unregistered_deleted"] == 1
    elif adapter == "http":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app
        from ultrakb.config import register_kb_alias

        monkeypatch.setenv("ULTRAKB_API_TOKEN", "orphan-test-token")
        register_kb_alias("orphan-test", root)
        with TestClient(create_app()) as client:
            result = client.post(
                "/api/v1/storage/collect",
                json={
                    "kb": "orphan-test",
                    "kb_id": identity.kb_id,
                    "generation": identity.generation,
                },
                headers={"Authorization": "Bearer orphan-test-token"},
            )
            assert result.status_code == 200, result.text
            assert result.json()["unregistered_deleted"] == 1
    else:
        import time

        from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

        widgets = pytest.importorskip("PySide6.QtWidgets")
        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageMaintenanceDialog(root)

        def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert predicate(), dialog.status.text()

        try:
            until(lambda: dialog.overview is not None and not dialog.busy)
            assert dialog.overview.garbage.unregistered_count == 1
            assert "未登记" in dialog.body.toPlainText()
            dialog.collect()
            until(lambda: dialog.last_result is not None and not dialog.busy)
            assert dialog.last_result.unregistered_deleted == 1
        finally:
            dialog.reject()
            app.processEvents()
    assert not blob_path(root, digest).exists()
