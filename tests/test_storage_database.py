"""P0 crash/format boundary witnesses for the v2 store (not yet enabled)."""

import sqlite3

import pytest


def test_unrecognized_directory_is_not_initialized_by_open(tmp_path):
    from ultrakb.storage.database import StorageFormatError, open_database

    state = tmp_path / ".ultrakb"
    state.mkdir()
    (state / "format.json").write_text('{"version": 1}')
    before = set(state.iterdir())
    with pytest.raises(StorageFormatError):
        with open_database(tmp_path):
            pytest.fail("Old format was opened")
    assert set(state.iterdir()) == before


def test_independent_commit_survives_later_publication_failure(tmp_path):
    from ultrakb.storage.database import create_database, open_database

    identity = create_database(tmp_path)
    with open_database(tmp_path, expected=identity) as db:
        with db.transaction() as connection:
            connection.execute("CREATE TABLE witness (value TEXT NOT NULL)")
        with db.transaction() as connection:
            connection.execute("INSERT INTO witness VALUES ('accepted response')")
        with pytest.raises(RuntimeError, match="publication failed"):
            with db.transaction() as connection:
                connection.execute("INSERT INTO witness VALUES ('unpublished')")
                raise RuntimeError("publication failed")
    with open_database(tmp_path, expected=identity, read_only=True) as db:
        assert db.connection.execute("SELECT value FROM witness").fetchall() == [
            ("accepted response",)
        ]


def test_nested_transaction_cannot_claim_an_independent_commit(tmp_path):
    from ultrakb.storage.database import create_database, open_database

    create_database(tmp_path)
    with open_database(tmp_path) as db:
        with db.transaction():
            with pytest.raises(RuntimeError, match="top-level"):
                with db.transaction():
                    pytest.fail("Nested durability was accepted")


@pytest.mark.parametrize("version", ["3.44.5", "3.49.2", "3.50.6", "3.51.2"])
def test_unsafe_runtime_is_rejected_before_creating_state(tmp_path, monkeypatch, version):
    from ultrakb.storage.database import UnsupportedSQLite, create_database

    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    with pytest.raises(UnsupportedSQLite):
        create_database(tmp_path)
    assert not (tmp_path / ".ultrakb").exists()


def test_read_only_stage_cannot_create_orphan_payloads(tmp_path):
    import hashlib

    from ultrakb.storage.blobs import blob_path, put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database

    identity = create_database(tmp_path)
    payload = b"A read-only reader must not write CAS content."
    with storage_stage(tmp_path, identity, read_only=True) as db:
        with pytest.raises((RuntimeError, PermissionError)):
            put_blob(db, payload)
    assert not blob_path(tmp_path, hashlib.sha256(payload).hexdigest()).exists()


def test_unconfirmed_blob_retry_must_reestablish_durability(tmp_path, monkeypatch):
    from ultrakb.storage.blobs import put_blob, read_bytes
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database

    identity = create_database(tmp_path)

    def failed_directory_sync(path):
        raise OSError("Directory synchronization did not complete")

    with storage_stage(tmp_path, identity) as db:
        with monkeypatch.context() as failed:
            failed.setattr("ultrakb.locks._fsync_directory", failed_directory_sync)
            with pytest.raises(OSError, match="synchronization"):
                put_blob(db, b"Retained bytes from an unconfirmed rename")
            with pytest.raises(OSError, match="synchronization"):
                put_blob(db, b"Retained bytes from an unconfirmed rename")
        assert db.connection.execute("SELECT count(*) FROM ukb_blobs").fetchone()[0] == 0
        blob = put_blob(db, b"Retained bytes from an unconfirmed rename")
        assert read_bytes(db, blob.digest) == b"Retained bytes from an unconfirmed rename"


def test_last_client_close_does_not_checkpoint_the_active_wal(tmp_path):
    from ultrakb.storage.database import create_database, database_path, open_database

    identity = create_database(tmp_path)
    wal = database_path(tmp_path).with_name("kb.sqlite-wal")
    with open_database(tmp_path, expected=identity) as db:
        with db.transaction() as connection:
            connection.execute("CREATE TABLE accepted (value TEXT NOT NULL)")
            connection.execute("INSERT INTO accepted VALUES ('durable in WAL')")
        retained_size = wal.stat().st_size
        assert retained_size > 0
    assert wal.exists() and wal.stat().st_size == retained_size
    with open_database(tmp_path, expected=identity, read_only=True) as db:
        assert db.connection.execute("SELECT value FROM accepted").fetchone()[0] == "durable in WAL"


def test_unsupported_store_with_missing_sidecars_is_rejected_without_creating_them(tmp_path):
    from ultrakb.storage.database import (
        StorageFormatError,
        create_database,
        database_path,
        open_database,
    )

    create_database(tmp_path)
    connection = sqlite3.connect(database_path(tmp_path))
    connection.execute("UPDATE ukb_meta SET required_capabilities = '[\"unknown\"]'")
    connection.commit()
    connection.close()
    before = set((tmp_path / ".ultrakb").iterdir())
    with pytest.raises(StorageFormatError):
        with open_database(tmp_path):
            pytest.fail("Unsupported capabilities were accepted")
    assert set((tmp_path / ".ultrakb").iterdir()) == before


def test_live_wal_capability_changes_are_not_hidden_by_the_format_probe(tmp_path):
    from ultrakb.storage.database import (
        StorageFormatError,
        create_database,
        database_path,
        open_database,
    )

    create_database(tmp_path)
    writer = sqlite3.connect(database_path(tmp_path))
    try:
        writer.execute("UPDATE ukb_meta SET required_capabilities = '[\"unknown\"]'")
        writer.commit()
        with pytest.raises(StorageFormatError):
            with open_database(tmp_path, read_only=True):
                pytest.fail("The active WAL was ignored during capability validation")
    finally:
        writer.close()
