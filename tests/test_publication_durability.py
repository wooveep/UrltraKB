"""Publication precedes working-copy deletion; failed directory sync stays pending."""

import pytest

from ultrakb.application.views import published_scope
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_projection import ProjectionStore


@pytest.mark.parametrize("interrupted", [False, True])
def test_projection_flushes_retired_directory_before_acknowledging_deletion(
    kb_dir, monkeypatch, interrupted
):
    import ultrakb.locks as locks

    base = published_scope(kb_dir).knowledge
    store = KnowledgeStore(kb_dir, base.identity)
    store.publish(store.prepare_changes(base, {"concepts/retired.md": b"Previous committed rule"}))
    projection = ProjectionStore(kb_dir, base.identity)
    assert projection.sync().status == "synced"
    current = store.bind()
    deleted = kb_dir / "wiki/concepts/retired.md"
    publication = store.publish(store.prepare_changes(current, {"concepts/retired.md": None}))
    assert deleted.exists()
    with pytest.raises(FileNotFoundError):
        store.bind().read("concepts/retired.md")
    assert store.receipt(publication.publication_id).revision_id == publication.revision_id
    real_sync, real_confirm = locks._fsync_directory, ProjectionStore._confirm
    synchronized = []

    def sync(directory):
        if directory == deleted.parent and not deleted.exists():
            if interrupted:
                raise OSError("Injected directory synchronization failure")
        result = real_sync(directory)
        if not deleted.exists():
            synchronized.append(directory)
        return result

    def confirm(self, db, owner, slot, plan, generation, path, digest, size):
        if path == "concepts/retired.md":
            assert next(parent for parent in deleted.parents if parent.exists()) in synchronized
            assert not db.connection.in_transaction
        return real_confirm(self, db, owner, slot, plan, generation, path, digest, size)

    with monkeypatch.context() as fault:
        fault.setattr(locks, "_fsync_directory", sync)
        fault.setattr(ProjectionStore, "_confirm", confirm)
        result = projection.sync()
    if interrupted:
        assert result.status == "pending" and result.error_code == "OSError"
        assert store.bind().publication_id == publication.publication_id
        assert not deleted.exists()
        with storage_stage(kb_dir, base.identity, read_only=True) as db:
            assert (
                db.connection.execute(
                    "SELECT digest FROM ukb_projection_applied WHERE path = 'concepts/retired.md'"
                ).fetchone()[0]
                is not None
            )
        interrupted = False
        with monkeypatch.context() as recovery:
            recovery.setattr(locks, "_fsync_directory", sync)
            recovery.setattr(ProjectionStore, "_confirm", confirm)
            result = projection.sync()
    assert result.status == "synced"
    assert not deleted.exists()
    assert next(parent for parent in deleted.parents if parent.exists()) in synchronized
    assert store.bind().publication_id == publication.publication_id
    with storage_stage(kb_dir, base.identity, read_only=True) as db:
        assert db.connection.execute(
            "SELECT digest FROM ukb_projection_applied WHERE path = 'concepts/retired.md'"
        ).fetchone() == (None,)
