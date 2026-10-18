"""Controlled external editing uses the last applied files across master publications."""


def test_pause_survives_restart_and_publishing_does_not_touch_editor_files(tmp_path):
    import hashlib

    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"applied"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(
        {"index.md": b"master ahead"}, expected=first.publication_id
    )
    knowledge.publish(second)
    edit = begin_external_edit(root, identity)
    (edit.directory / "index.md").write_bytes(b"editor draft")
    assert worktree.sync().status == "paused"
    restarted = begin_external_edit(root, identity)
    assert restarted.edit_id == edit.edit_id
    assert restarted.baseline == {"index.md": hashlib.sha256(b"applied").hexdigest()}
    third = knowledge.prepare_revision({"index.md": b"latest"}, expected=second.publication_id)
    knowledge.publish(third)
    assert ProjectionStore(root, identity).sync().status == "paused"
    assert (root / "wiki/index.md").read_bytes() == b"editor draft"
    assert knowledge.bind().read("index.md") == b"latest"


def test_accept_merges_disjoint_master_changes_and_preserves_conflicting_drafts(tmp_path):
    from ultrakb.application.wiki_worktree import accept_external_edit, begin_external_edit
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"a.md": b"a0", "b.md": b"b0"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "a.md").write_bytes(b"manual a")
    second = knowledge.prepare_revision(
        {"a.md": b"a0", "b.md": b"model b"}, expected=first.publication_id
    )
    knowledge.publish(second)
    accepted = accept_external_edit(root, identity, edit.edit_id, editor_stopped=True)
    assert accepted.status == "accepted"
    assert knowledge.bind().read("a.md") == b"manual a"
    assert knowledge.bind().read("b.md") == b"model b"
    assert worktree.sync().status == "synced"
    assert (root / "wiki/b.md").read_bytes() == b"model b"
    another = begin_external_edit(root, identity)
    (edit.directory / "a.md").write_bytes(b"next draft")
    newer = knowledge.prepare_revision(
        {"a.md": b"concurrent", "b.md": b"model b"}, expected=accepted.publication_id
    )
    knowledge.publish(newer)
    result = accept_external_edit(root, identity, another.edit_id, editor_stopped=True)
    assert result.status == "conflict"
    assert result.conflicts == ("a.md",)
    assert knowledge.bind().publication_id == newer.publication_id
    assert worktree.sync().status == "paused"
    assert (root / "wiki/a.md").read_bytes() == b"next draft"


def test_discard_requires_stopped_editor_and_keeps_unknown_files_until_explicit_choice(tmp_path):
    import pytest

    from ultrakb.application.wiki_worktree import begin_external_edit, discard_external_edit
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"a.md": b"old", "deleted.md": b"old"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "a.md").unlink()
    (edit.directory / "unknown.md").write_bytes(b"unknown draft")
    (edit.directory / "deleted.md").unlink()
    second = knowledge.prepare_revision({"a.md": b"latest"}, expected=first.publication_id)
    knowledge.publish(second)
    with pytest.raises(ValueError, match="stopped"):
        discard_external_edit(root, identity, edit.edit_id, editor_stopped=False)
    result = discard_external_edit(root, identity, edit.edit_id, editor_stopped=True)
    assert result.status == "conflict"
    assert result.conflicts == ("unknown.md",)
    assert worktree.sync().status == "paused"
    discarded = discard_external_edit(
        root, identity, edit.edit_id, editor_stopped=True, discard_unknown=("unknown.md",)
    )
    assert discarded.status == "discarded"
    assert discarded.publication_id == second.publication_id
    assert knowledge.bind().publication_id == second.publication_id
    assert worktree.sync().status == "synced"
    assert (root / "wiki/a.md").read_bytes() == b"latest"
    assert not (root / "wiki/unknown.md").exists()
    assert not (root / "wiki/deleted.md").exists()
    next_edit = begin_external_edit(root, identity)
    assert set(next_edit.baseline) == {"a.md"}


def test_new_publication_keeps_a_pending_explicit_discard_authorization(tmp_path):
    from ultrakb.application.wiki_worktree import begin_external_edit, discard_external_edit
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"applied"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "index.md").write_bytes(b"discarded draft")
    (edit.directory / "unknown.md").write_bytes(b"discarded creation")
    assert (
        discard_external_edit(
            root, identity, edit.edit_id, editor_stopped=True, discard_unknown=("unknown.md",)
        ).status
        == "discarded"
    )
    second = knowledge.prepare_revision({"index.md": b"master"}, expected=first.publication_id)
    knowledge.publish(second)
    assert worktree.sync().status == "synced"
    assert (root / "wiki/index.md").read_bytes() == b"master"
    assert not (root / "wiki/unknown.md").exists()
