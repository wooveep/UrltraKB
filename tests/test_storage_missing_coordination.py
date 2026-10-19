"""A lost active sidecar uses verified backup restore, never an implicit WAL rewrite."""

from pathlib import Path

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
from ultrakb.storage.database import StorageFormatError, database_path, open_database
from ultrakb.storage.knowledge_store import KnowledgeStore


@pytest.mark.parametrize("missing", ["wal", "shm"])
def test_lost_active_coordination_leaves_original_bytes_untouched_and_restores_backup(
    tmp_path, missing
):
    root, identity, _ = question_knowledge(tmp_path)
    bound = KnowledgeStore(root, identity).bind()
    backup = tmp_path / "backup"
    backup_knowledge(root, backup)
    path = database_path(root)
    Path(str(path) + "-" + missing).unlink()
    before = {
        item.name: item.read_bytes()
        for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if item.exists()
    }
    with pytest.raises(StorageFormatError, match="restore a verified backup"):
        bound.read("sources/fixture.md")
    assert before == {
        item.name: item.read_bytes()
        for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if item.exists()
    }
    destination = tmp_path / "restored"
    result = restore_knowledge(backup, destination)
    assert result.status == "completed" and result.identity.generation != identity.generation
    assert (
        KnowledgeStore(destination, result.identity).bind().read("sources/fixture.md")
        == b"The value is 17."
    )
    with pytest.raises(StorageFormatError):
        with open_database(destination, expected=identity):
            pytest.fail("A restored generation accepted the old callback identity")
