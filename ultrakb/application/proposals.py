"""Review and explicitly accept a durable candidate without repeating model work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.ingest_result import IngestResult
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


@dataclass(frozen=True)
class ProposalView:
    proposal_id: str
    source_id: str
    unit_id: str
    view_id: str
    status: str
    version: str
    conflicts: tuple[str, ...]
    diff: str


@dataclass(frozen=True)
class ProposalPage:
    proposals: tuple[ProposalView, ...]
    next_after: str | None = None


def list_proposal_page(kb_dir, *, scope=None, after=None, limit=63):
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Proposal pages require a limit of 1..128")
    from .storage_proposals import page_stored_proposals

    return page_stored_proposals(scope, after=after, limit=limit)


def read_proposal(
    kb_dir: Path, proposal_id: str, *, scope: KnowledgeScope | None = None
) -> ProposalView:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    from .storage_proposals import read_stored_proposal

    return read_stored_proposal(scope, proposal_id)


def list_proposals(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[ProposalView, ...]:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    from .storage_proposals import list_stored_proposals

    return list_stored_proposals(scope)


def accept_proposal(
    kb_dir: Path,
    proposal_id: str,
    *,
    version: str,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> IngestResult:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    from .storage_proposals import accept_stored_proposal

    return accept_stored_proposal(scope, proposal_id, version=version, context=context)
