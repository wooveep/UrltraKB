"""Bounded retained conversation history, versioned deletion and explicit export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope

if TYPE_CHECKING:
    from ultrakb.storage.wiki_projection import ProjectionResult


@dataclass(frozen=True)
class SessionResult:
    status: Literal["deleted", "exported", "missing", "conflict"]
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    publication_id: str | None = None
    projection: ProjectionResult | None = None
    candidate_revision_id: str | None = None
    reason_code: str | None = None
    unfinished: tuple[str, ...] = ()


def list_conversations(
    kb_dir: Path,
    *,
    view_id: str | None = None,
    scope: KnowledgeScope | None = None,
    limit: int = 63,
    after: str | None = None,
    context: ExecutionContext | None = None,
) -> list[dict]:
    """List a bounded page of the selected view's retained conversations."""

    scope = resolve_scope(kb_dir, scope)
    from ultrakb.storage.session_catalog import SessionCatalog

    scope = resolve_scope(kb_dir, scope)
    assert scope.knowledge is not None
    if view_id is not None and view_id != scope.view_id:
        raise ValueError("Conversation scope and view do not match")
    return SessionCatalog(scope.kb_dir, scope.knowledge.identity).list(
        view_id=scope.view_id,
        limit=limit,
        after=after,
        cancelled=context.cancelled if context else None,
    )


def resolve_conversation(
    kb_dir: Path, query: str, *, view_id: str | None = None, scope: KnowledgeScope | None = None
) -> str | None:
    scope = resolve_scope(kb_dir, scope)
    from ultrakb.storage.session_catalog import SessionCatalog

    scope = resolve_scope(kb_dir, scope)
    assert scope.knowledge is not None
    if view_id is not None and view_id != scope.view_id:
        raise ValueError("Conversation scope and view do not match")
    return SessionCatalog(scope.kb_dir, scope.knowledge.identity).resolve(
        query, view_id=scope.view_id
    )


def delete_conversation(
    kb_dir: Path,
    session_id: str,
    *,
    version: str | None = None,
    context: ExecutionContext | None = None,
    view_id: str | None = None,
    scope: KnowledgeScope | None = None,
) -> SessionResult:
    root = kb_dir.resolve()
    scope = resolve_scope(root, scope)
    from ultrakb.storage.session_catalog import SessionCatalog

    scope = resolve_scope(root, scope)
    assert scope.knowledge is not None
    if view_id is not None and view_id != scope.view_id:
        raise ValueError("Conversation scope and view do not match")

    def check_cancelled():
        if context:
            context.check_stop()
        return False

    status = SessionCatalog(root, scope.knowledge.identity).delete(
        session_id,
        view_id=scope.view_id,
        expected=version,
        cancelled=check_cancelled,
        on_wait=context.waiting if context else None,
    )
    return SessionResult(
        status, changes=(f"deleted conversation: {session_id}",) if status == "deleted" else ()
    )


def export_conversation(
    kb_dir: Path,
    session: str,
    name: str | None = None,
    *,
    unique: bool = True,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> SessionResult:
    """Export completed history without replacing previous exports.

    Originals are read from the main store under the owning session lock.
    """
    scope = resolve_scope(kb_dir, scope)
    from .storage_session_export import export_stored_conversation

    return export_stored_conversation(scope, session, name, unique=unique, context=context)


def retain_pending_conversation(
    root,
    session_id,
    submission_id,
    message,
    *,
    identity,
    new=False,
    after_turn=0,
    order=None,
    view_id="legacy",
    cancelled=None,
    on_wait=None,
):
    """Recover accepted desktop intent without executing a model or creating a run."""
    from ultrakb.storage.database import open_database
    from ultrakb.storage.session_store import SessionStore

    with open_database(root, expected=identity, read_only=True):
        pass
    return SessionStore(root, identity).queue_submission(
        session_id,
        submission_id,
        message,
        view_id=view_id,
        new=new,
        after_turn=after_turn,
        order=order,
        cancelled=cancelled,
        on_wait=on_wait,
    )
