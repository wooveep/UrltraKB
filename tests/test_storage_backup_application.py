"""Shared maintenance operations preserve a portable store and its recovery state."""

import json

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.pages import read_page
from ultrakb.application.views import storage_scope


def test_backup_and_restore_preserve_the_kb_configuration_pair_and_fixed_knowledge(tmp_path):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge

    root, identity, _ = question_knowledge(tmp_path)
    credentials = b"LLM_API_KEY=backup-fixture-key\nOPENAI_API_BASE=http://127.0.0.1:1\n"
    (root / ".env").write_bytes(credentials)
    configuration = (root / ".ultrakb/config.yaml").read_bytes()
    backup = backup_knowledge(root, tmp_path / "backup", scope=storage_scope(root, identity))
    assert backup.coverage["configuration"] == "complete"
    restored = restore_knowledge(tmp_path / "backup", tmp_path / "restored")
    assert restored.identity.generation != identity.generation
    assert (tmp_path / "restored/.env").read_bytes() == credentials
    assert (tmp_path / "restored/.ultrakb/config.yaml").read_bytes() == configuration
    scope = storage_scope(tmp_path / "restored", restored.identity)
    assert (
        read_page(tmp_path / "restored", "sources/fixture.md", scope=scope).body
        == "The value is 17."
    )


@pytest.mark.parametrize("damage", ["bytes", "manifest"])
def test_restore_rejects_damaged_configuration_before_creating_the_destination(tmp_path, damage):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge

    root, identity, _ = question_knowledge(tmp_path)
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup_knowledge(root, archive, scope=storage_scope(root, identity))
    if damage == "bytes":
        (archive / ".ultrakb/config.yaml").write_text("model: unexpected\n")
    else:
        manifest = json.loads((archive / "backup.json").read_text())
        manifest["files"][0]["path"] = "../escape"
        (archive / "backup.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore_knowledge(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape").exists()


def test_backup_restores_paused_changes_deletions_and_unknown_files_without_accepting_them(
    tmp_path,
):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.application.wiki_worktree import accept_external_edit, begin_external_edit
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, initial = question_knowledge(tmp_path)
    assert ProjectionStore(root, identity).sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "index.md").write_text("Manual draft")
    (edit.directory / "sources/fixture.md").unlink()
    (edit.directory / "unknown.md").write_text("Unaccepted new page")
    knowledge = KnowledgeStore(root, identity)
    previous = knowledge.bind()
    knowledge.publish(
        knowledge.prepare_revision(
            {
                **{path: previous.read(path) for path in previous.files()},
                "new.md": b"Published while paused",
            },
            expected=initial.publication_id,
        )
    )
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    result = backup_knowledge(root, archive, scope=storage_scope(root, identity))
    assert result.coverage["worktree"] == "complete"
    restored = restore_knowledge(archive, destination)
    restarted = begin_external_edit(destination, restored.identity)
    assert restarted.edit_id == edit.edit_id
    assert restarted.baseline == edit.baseline
    assert (restarted.directory / "index.md").read_text() == "Manual draft"
    assert not (restarted.directory / "sources/fixture.md").exists()
    assert (restarted.directory / "unknown.md").read_text() == "Unaccepted new page"
    assert ProjectionStore(destination, restored.identity).sync().status == "paused"
    scope = storage_scope(destination, restored.identity)
    assert read_page(destination, "sources/fixture.md", scope=scope).body == "The value is 17."
    accepted = accept_external_edit(
        destination, restored.identity, restarted.edit_id, editor_stopped=True
    )
    assert accepted.status == "accepted"
    current = storage_scope(destination, restored.identity)
    assert read_page(destination, "index.md", scope=current).body == "Manual draft"
    assert read_page(destination, "new.md", scope=current).body == "Published while paused"
