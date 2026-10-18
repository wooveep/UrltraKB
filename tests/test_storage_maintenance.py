"""Crash and lease witnesses use the same physical store as the v2 factory."""

import concurrent.futures


def test_reclamation_rechecks_references_and_returns_busy_during_access(tmp_path):
    from ultrakb.storage.blobs import put_blob, read_bytes
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    maintenance = Maintenance(root, identity)
    with storage_stage(root, identity) as db:
        orphan = put_blob(db, b"Unreferenced temporary calculation")
    preview = maintenance.preview()
    assert preview.candidate_count == 1
    source = tmp_path / "manual.md"
    source.write_bytes(b"Unreferenced temporary calculation")
    CatalogStore(root, identity).admit(source, source_key="manual")
    with storage_stage(root, identity) as db:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
            busy = workers.submit(maintenance.collect).result(timeout=5)
        assert busy.status == "busy" and busy.deleted_bytes == 0
    result = maintenance.collect()
    assert result.status == "completed" and result.deleted_bytes == 0
    with storage_stage(root, identity, read_only=True) as db:
        assert read_bytes(db, orphan.digest) == b"Unreferenced temporary calculation"


def test_backup_restores_independent_originals_with_new_generation(tmp_path):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import StorageFormatError, create_database, open_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    source = tmp_path / "manual.md"
    source.write_text("Original facts survive backup and source deletion.")
    revision = CatalogStore(root, identity).admit(source, source_key="manual")
    backup = tmp_path / "backup"
    Maintenance(root, identity).backup(backup)
    source.unlink()
    restored = tmp_path / "restored"
    new_identity = Maintenance.restore(backup, restored)
    assert new_identity.generation != identity.generation
    assert CatalogStore(restored, new_identity).read_original(revision.revision_id) == (
        b"Original facts survive backup and source deletion."
    )
    import pytest

    with pytest.raises(StorageFormatError):
        with open_database(restored, expected=identity):
            pytest.fail("A pre-restore callback entered the new generation")


def test_collection_never_upgrades_a_stage_owned_by_its_caller(tmp_path):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    with storage_stage(root, identity):
        result = Maintenance(root, identity).collect()
    assert result.status == "busy" and result.deleted_count == 0


def test_interrupted_retirement_rejects_new_references_and_resumes(tmp_path, monkeypatch):
    import pytest

    from ultrakb.storage.blobs import BlobRetiring, put_blob
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    source = tmp_path / "manual.md"
    source.write_text("Retirement must finish before this can be accepted again.")
    with storage_stage(root, identity) as db:
        put_blob(db, source.read_bytes())
    maintenance = Maintenance(root, identity)

    def interrupted_unlink(*args, **kwargs):
        raise OSError("simulated interruption")

    with monkeypatch.context() as failed:
        failed.setattr("ultrakb.storage.maintenance.durable_unlink", interrupted_unlink)
        with pytest.raises(OSError, match="interruption"):
            maintenance.collect()
    with pytest.raises(BlobRetiring):
        CatalogStore(root, identity).admit(source, source_key="manual")
    result = maintenance.collect()
    assert result.deleted_count == 1
    assert result.deleted_bytes == source.stat().st_size
    catalog = CatalogStore(root, identity)
    admitted = catalog.admit(source, source_key="manual")
    assert catalog.read_original(admitted.revision_id) == source.read_bytes()


def test_restored_backup_allows_readmission_of_previously_unreferenced_bytes(tmp_path):
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    payload = b"An interrupted admission left these bytes without a business reference."
    with storage_stage(root, identity) as db:
        put_blob(db, payload)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    Maintenance(root, identity).backup(backup)
    new_identity = Maintenance.restore(backup, restored)
    source = tmp_path / "original.md"
    source.write_bytes(payload)
    catalog = CatalogStore(restored, new_identity)
    revision = catalog.admit(source, source_key="readmitted")
    assert catalog.read_original(revision.revision_id) == payload


def test_restored_active_database_uses_wal(tmp_path):
    from ultrakb.storage.database import create_database, open_database
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    Maintenance(root, identity).backup(backup)
    new_identity = Maintenance.restore(backup, restored)
    with open_database(restored, expected=new_identity, read_only=True) as db:
        assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
