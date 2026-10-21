"""Read shared-wiki snapshots and retain pre-catalog source evidence."""

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ultrakb.knowledge_scope import KnowledgeScope

T = TypeVar("T")


def run_scoped_operation(
    operation: Callable[[], T],
    *,
    scope: KnowledgeScope | None,
    cancelled: Callable[[], bool] | None = None,
    read_only: bool = True,
) -> T:
    """Protect a local bound operation, including cancellable storage waits.

    Entrypoints supply their stop signal and join their worker. The application
    owns the generation and reclamation protection across the operation's reads.
    No SQL snapshot or write transaction spans the callback.
    """
    if scope is None or scope.knowledge is None:
        return operation()
    from ultrakb.storage.coordination import storage_stage

    with storage_stage(
        scope.kb_dir, scope.knowledge.identity, read_only=read_only, cancelled=cancelled
    ):
        return operation()


def storage_scope(
    kb_dir, identity, *, publication_id=None, view_id="legacy", cancelled=None
) -> KnowledgeScope:
    """Bind an exact public main-store identity, never the mutable worktree."""
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = kb_dir.expanduser().resolve()
    bound = KnowledgeStore(root, identity).bind(
        publication_id, view_id=view_id, cancelled=cancelled
    )
    return KnowledgeScope(root, root / "wiki", view_id, knowledge=bound)


def published_scope(
    kb_dir: Path, view_id: str = "legacy", *, publication_id: str | None = None, cancelled=None
) -> KnowledgeScope:
    """Pin the public pointer once without touching mutable writer state."""
    from ultrakb.storage.database import open_database

    with open_database(kb_dir, read_only=True) as db:
        identity = db.identity
    return storage_scope(
        kb_dir, identity, publication_id=publication_id, view_id=view_id, cancelled=cancelled
    )


def view_scope(
    kb_dir: Path, view_id: str, *, historical_revision: str | None = None, cancelled=None
) -> KnowledgeScope:
    from ultrakb.storage.database import open_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = kb_dir.resolve()
    with open_database(root, read_only=True) as db:
        identity = db.identity
    if historical_revision is not None:
        bound = KnowledgeStore(root, identity).bind_revision(
            historical_revision, view_id=view_id, cancelled=cancelled
        )
        return KnowledgeScope(root, root / "wiki", view_id, knowledge=bound, _historical=True)
    return storage_scope(root, identity, view_id=view_id, cancelled=cancelled)
