"""Explicit view refresh: validity is separate from the immutable evidence shown."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope


@dataclass(frozen=True)
class SourceContributionTarget:
    source_id: str
    name: str
    source_revision_id: str
    target_generation: int
    removed: bool
    contribution_empty: bool


def read_source_contribution_target(kb_dir, source_id, *, scope=None):
    selected = resolve_scope(kb_dir, scope)
    if selected.historical:
        raise ValueError("Select the current view before confirming source contribution")
    with reading_scope(selected):
        assert selected.knowledge is not None
        from ultrakb.storage.catalog_store import CatalogStore

        source = CatalogStore(selected.kb_dir, selected.knowledge.identity).source(source_id)
        return SourceContributionTarget(
            source.source_id,
            source.label,
            source.revision_id,
            source.generation,
            source.removed,
            source.contribution_empty,
        )


def list_knowledge_history(
    kb_dir: Path,
    *,
    scope: KnowledgeScope,
    after=None,
    limit=63,
    publication_id=None,
    cancelled=None,
) -> tuple[dict, ...]:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    from ultrakb.storage.knowledge_history import require_limit

    require_limit(limit)
    if publication_id is not None and publication_id != scope.publication_id:
        from .views import storage_scope

        scope = storage_scope(
            scope.kb_dir,
            scope.knowledge.identity,
            publication_id=publication_id,
            view_id=scope.view_id,
        )
    from ultrakb.storage.knowledge_history import KnowledgeHistory

    from .knowledge_history import history_reading

    with history_reading(scope, cancelled):
        return KnowledgeHistory(scope.knowledge).list(after=after, limit=limit)


def read_refresh_proposal(
    kb_dir: Path, identity: str, *, scope: KnowledgeScope | None = None
) -> dict:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    kb_dir = kb_dir.resolve()
    from .storage_refresh import read_stored_refresh

    return read_stored_refresh(scope, identity)


@dataclass(frozen=True)
class RefreshProposalPage:
    proposals: tuple[dict, ...]
    next_after: str | None


def list_refresh_proposal_page(kb_dir: Path, *, scope=None, after=None, limit=63):
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir = kb_dir.resolve()
    from ultrakb.storage.refresh_store import RefreshStore

    proposals, cursor = RefreshStore(kb_dir, scope.knowledge.identity).summary_page(
        scope.view_id, after=after, limit=limit
    )
    return RefreshProposalPage(proposals, cursor)


def list_refresh_proposals(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[dict, ...]:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir = kb_dir.resolve()
    items, after = [], None
    while True:
        page = list_refresh_proposal_page(kb_dir, scope=scope, after=after)
        items.extend(page.proposals)
        if page.next_after is None:
            return tuple(items)
        after = page.next_after


def accept_refresh_proposal(
    kb_dir: Path, identity: str, *, version: str, scope: KnowledgeScope | None = None, context=None
):
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    kb_dir = kb_dir.resolve()
    from .storage_refresh import accept_stored_refresh

    return accept_stored_refresh(scope, identity, version=version, context=context)


def confirm_empty_source(
    kb_dir: Path,
    source_id: str,
    *,
    generation: int,
    scope: KnowledgeScope | None = None,
    context=None,
):
    """An explicit, generation-bound confirmation excludes this revision's contribution."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    kb_dir = kb_dir.resolve()
    from .storage_empty_source import confirm_stored_empty

    return confirm_stored_empty(scope, source_id, generation=generation, context=context)


async def refresh_knowledge_view(kb_dir: Path, *, scope: KnowledgeScope, context=None):
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    from .storage_refresh import refresh_stored_view

    return await refresh_stored_view(scope, context)


def refresh_status(kb_dir: Path, *, scope: KnowledgeScope) -> dict:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from ultrakb.storage.knowledge_validity import publication_validity

    return publication_validity(scope.knowledge)


async def refresh_selected_sources(
    kb_dir: Path,
    source_ids: list[str],
    *,
    scope: KnowledgeScope,
    context=None,
    page_paths: list[str] | None = None,
):
    """Never invoke document conversion, indexing, summary generation or planning."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    from .storage_refresh import refresh_stored_view

    return await refresh_stored_view(
        scope,
        context,
        selection=(tuple(source_ids), tuple(page_paths) if page_paths is not None else None),
    )
