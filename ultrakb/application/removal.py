"""Shared document removal: stable previews, journaled cleanup, and retryable results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


@dataclass
class RemoveAction:
    """One planned step surfaced in the remove preview/summary."""

    tag: str
    target: str


@dataclass
class RemovePlan:
    """Structured preview of what ``remove`` will do (no side effects yet).

    Built by ``_build_remove_plan``; consumed by the CLI (for printing the
    preview) and by ``_execute_remove_plan`` (for actually doing it).
    """

    name: str
    doc_name: str
    doc_type: str
    file_hash: str
    actions: list[RemoveAction]
    concept_deletes: list[str]
    entity_deletes: list[str]
    raw_path: Path | None
    cleanup_pageindex: bool
    pageindex_doc_id: str | None
    summary_path: Path
    source_md: Path
    source_json: Path
    images_dir: Path
    kept_raw: Path | None = None
    cleanup_deferred: bool = False
    keep_images: bool = False


@dataclass
class RemoveResult:
    """Outcome of executing a :class:`RemovePlan`.

    ``status="partial"`` means PageIndex cleanup raised and the registry
    entry was deliberately kept so the user can retry — mirroring the CLI.
    """

    status: Literal["removed", "partial"]
    name: str
    doc_name: str
    actions: list[RemoveAction]
    concepts_deleted: list[str]
    entities_deleted: list[str]
    lint_files_changed: int
    lint_ghosts_removed: int
    pageindex_message: str | None
    pageindex_error: str | None
    message: str
    changes: tuple[str, ...] = ()
    needs_repair: bool = False
    error_type: str | None = None


def run_remove_for_api(
    kb_dir: Path,
    identifier: str,
    *,
    keep_raw: bool = False,
    keep_empty: bool = False,
    dry_run: bool = False,
    scope: KnowledgeScope | None = None,
    version: str | None = None,
) -> dict:
    """Resolve ``identifier`` and run remove under the KB ingest lock.

    Shared entry point for the REST ``/api/v1/remove`` endpoint. Resolve +
    plan + execute all run inside ``kb_ingest_lock`` so concurrent
    add/remove can't interleave (matching the CLI's ``@_with_kb_lock``).

    Returns a dict whose ``status`` is one of ``not_found``, ``multiple``,
    ``dry_run``, ``removed``, ``partial``.
    """
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from dataclasses import asdict

    preview = preview_removal(kb_dir, identifier, scope=scope)
    if preview.status != "ready" and (dry_run or version is None):
        return {
            "status": preview.status,
            "candidates": [{"name": name} for name in preview.candidates],
        }
    if dry_run:
        assert preview.plan is not None
        return {
            "status": "dry_run",
            "name": preview.plan.name,
            "doc_name": preview.plan.doc_name,
            "actions": [asdict(action) for action in preview.plan.actions],
            "version": preview.version,
            "publication_id": scope.publication_id,
        }
    outcome = remove_document(
        kb_dir,
        identifier,
        version=version if version is not None else preview.version,
        scope=scope,
    )
    return {
        **(asdict(outcome.result) if outcome.result else {}),
        "status": outcome.status,
        "retained": outcome.retained,
        "unfinished": outcome.unfinished,
        "publication_id": outcome.publication_id,
        "projection_pending": outcome.projection_pending,
        "message": outcome.message or (outcome.result.message if outcome.result else None),
    }


@dataclass(frozen=True)
class RemovalPreview:
    status: Literal["ready", "not_found", "multiple"]
    version: str = ""
    plan: RemovePlan | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemovalOutcome:
    status: str
    preview: RemovalPreview
    result: RemoveResult | None = None
    retained: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    publication_id: str | None = None
    projection_pending: bool = False
    message: str | None = None
    error_type: str | None = None


def preview_removal(
    kb_dir: Path,
    identifier: str,
    *,
    keep_raw: bool = False,
    keep_empty: bool = False,
    scope: KnowledgeScope | None = None,
) -> RemovalPreview:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    from .storage_removal import preview_stored_removal

    return preview_stored_removal(scope, identifier)


def remove_document(
    kb_dir: Path,
    identifier: str,
    *,
    keep_raw: bool = False,
    keep_empty: bool = False,
    version: str | None = None,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> RemovalOutcome:
    """Revalidate a confirmed preview before cleaning either durable phase.

    CLI/REST can omit a version because their existing adapters confirm under
    one lease or submit an immediate request. Native confirmation supplies the
    preview version; any changed input requires another explicit confirmation.
    """
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    from .storage_removal import remove_stored_source

    return remove_stored_source(scope, identifier, version=version, context=context)
