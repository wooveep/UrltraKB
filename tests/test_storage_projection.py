"""Master publication and recoverable worktree synchronization have separate outcomes."""

import pytest


def test_published_content_survives_worktree_failure_and_restarts_sync(tmp_path, monkeypatch):
    from ultrakb.storage import wiki_projection as projection
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision(
        {"index.md": b"old", "images/a.png": b"pixels"}, expected=None
    )
    knowledge.publish(first)
    worktree = projection.ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    assert (root / "wiki/index.md").read_bytes() == b"old"
    second = knowledge.prepare_revision(
        {"index.md": b"new", "images/a.png": b"pixels"}, expected=first.publication_id
    )
    knowledge.publish(second)

    def disk_failure(*args):
        raise OSError("injected worktree write failure")

    with monkeypatch.context() as broken:
        broken.setattr(projection, "atomic_write_bytes", disk_failure)
        assert worktree.sync().status == "pending"
    assert knowledge.bind().read("index.md") == b"new"
    assert (root / "wiki/index.md").read_bytes() == b"old"
    assert projection.ProjectionStore(root, identity).sync().status == "synced"
    assert (root / "wiki/index.md").read_bytes() == b"new"
    assert worktree.sync(plan_id=first.sync_plan_id).status == "obsolete"
    assert (root / "wiki/index.md").read_bytes() == b"new"


@pytest.mark.parametrize("delete", [False, True])
def test_matching_target_does_not_confirm_an_unfinished_directory_sync(
    tmp_path, monkeypatch, delete
):
    from ultrakb import locks
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(knowledge.prepare_revision({"index.md": b"complete"}, expected=None))
    worktree = ProjectionStore(root, identity)
    if delete:
        assert worktree.sync().status == "synced"
        knowledge.publish(knowledge.prepare_revision({}, expected=knowledge.bind().publication_id))

    def failed_directory_sync(path):
        raise OSError("injected interrupted directory synchronization")

    with monkeypatch.context() as failed:
        failed.setattr(locks, "_fsync_directory", failed_directory_sync)
        assert worktree.sync().status == "pending"
        if delete:
            assert not (root / "wiki/index.md").exists()
        else:
            assert (root / "wiki/index.md").read_bytes() == b"complete"
        assert worktree.sync().status == "pending"
    assert worktree.sync().status == "synced"


def test_replacement_before_confirmation_recovers_without_replacing_again(tmp_path, monkeypatch):
    from ultrakb.storage import wiki_projection as projection
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    prepared = knowledge.prepare_revision({"index.md": b"complete"}, expected=None)
    knowledge.publish(prepared)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_confirmation BEFORE INSERT ON ukb_projection_applied "
            "BEGIN SELECT RAISE(ABORT, 'injected crash after replacement'); END"
        )
    worktree = projection.ProjectionStore(root, identity)
    result = worktree.sync()
    assert result.status == "pending"
    assert result.error_code == "IntegrityError"
    assert (root / "wiki/index.md").read_bytes() == b"complete"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_confirmation")

    def unexpected_replacement(*args):
        raise AssertionError("The complete target must only be confirmed")

    monkeypatch.setattr(projection, "atomic_write_bytes", unexpected_replacement)
    assert worktree.sync().status == "synced"
    assert knowledge.publish(prepared).publication_id == prepared.publication_id


def test_changed_applied_baseline_requires_reprepare_and_unknown_edits_are_preserved(tmp_path):
    import pytest

    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionConflict, ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"old"}, expected=None)
    knowledge.publish(first)
    stale = knowledge.prepare_revision({"index.md": b"new"}, expected=first.publication_id)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    with pytest.raises(ProjectionConflict):
        knowledge.publish(stale)
    assert knowledge.bind().publication_id == first.publication_id
    fresh = knowledge.prepare_revision({"index.md": b"new"}, expected=first.publication_id)
    knowledge.publish(fresh)
    (root / "wiki/index.md").write_bytes(b"external edit")
    result = worktree.sync()
    assert result.status == "conflict"
    assert result.conflict_count == 1
    assert (root / "wiki/index.md").read_bytes() == b"external edit"
    assert knowledge.bind().read("index.md") == b"new"


@pytest.mark.parametrize("intermediate", [b"v2", b"longer second target", None])
def test_later_publication_recovers_an_unconfirmed_predecessor_without_rollback(
    tmp_path, intermediate
):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"v1"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(
        {"index.md": intermediate} if intermediate is not None else {},
        expected=first.publication_id,
    )
    knowledge.publish(second)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_confirmation BEFORE UPDATE ON ukb_projection_applied "
            "BEGIN SELECT RAISE(ABORT, 'injected crash after replacement'); END"
        )
    assert worktree.sync().status == "pending"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_confirmation")
    third = knowledge.prepare_revision({"index.md": b"v3"}, expected=second.publication_id)
    knowledge.publish(third)
    fourth = knowledge.prepare_revision({"index.md": b"v4"}, expected=third.publication_id)
    knowledge.publish(fourth)
    assert worktree.sync().status == "synced"
    assert (root / "wiki/index.md").read_bytes() == b"v4"
    assert worktree.sync(plan_id=second.sync_plan_id).status == "obsolete"
    assert (root / "wiki/index.md").read_bytes() == b"v4"


def test_sealed_sync_items_cannot_be_removed_before_retirement(tmp_path):
    import sqlite3

    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    prepared = knowledge.prepare_revision({"index.md": b"complete"}, expected=None)
    with storage_stage(root, identity) as db:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
            connection.execute(
                "DELETE FROM ukb_sync_items WHERE plan_id = ?", (prepared.sync_plan_id,)
            )
    knowledge.publish(prepared)


def test_unconfirmed_creation_is_removed_by_a_newer_publication(tmp_path):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"v1"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(
        {"index.md": b"v1", "new.md": b"new file"}, expected=first.publication_id
    )
    knowledge.publish(second)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_new_confirmation BEFORE INSERT ON ukb_projection_applied "
            "WHEN NEW.path = 'new.md' BEGIN SELECT RAISE(ABORT, 'injected creation crash'); END"
        )
    assert worktree.sync().status == "pending"
    assert (root / "wiki/new.md").read_bytes() == b"new file"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_new_confirmation")
    third = knowledge.prepare_revision({"index.md": b"v3"}, expected=second.publication_id)
    knowledge.publish(third)
    assert worktree.sync().status == "synced"
    assert not (root / "wiki/new.md").exists()
    assert (root / "wiki/index.md").read_bytes() == b"v3"


@pytest.mark.parametrize("reverse", [False, True])
def test_managed_files_can_change_between_directory_and_file_paths(tmp_path, reverse):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    old, new = ({"guides/intro.md": b"old"}, {"guides": b"new"})
    if reverse:
        old, new = new, old
    first = knowledge.prepare_revision(old, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(new, expected=first.publication_id)
    knowledge.publish(second)
    assert worktree.sync().status == "synced"
    assert (root / "wiki" / next(iter(new))).read_bytes() == next(iter(new.values()))


@pytest.mark.parametrize("reverse", [False, True])
def test_skipping_an_intermediate_file_directory_change_keeps_latest_projection(tmp_path, reverse):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first_files, skipped_files = ({"guides/intro.md": b"v1"}, {"guides": b"v2"})
    if reverse:
        first_files, skipped_files = skipped_files, first_files
    first = knowledge.prepare_revision(first_files, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    skipped = knowledge.prepare_revision(skipped_files, expected=first.publication_id)
    knowledge.publish(skipped)
    final_path = next(iter(first_files))
    final = knowledge.prepare_revision({final_path: b"v3"}, expected=skipped.publication_id)
    knowledge.publish(final)
    assert worktree.sync().status == "synced"
    assert (root / "wiki" / final_path).read_bytes() == b"v3"
