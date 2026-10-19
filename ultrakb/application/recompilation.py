"""Recompile existing knowledge with one recoverable transaction per document."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.config import (
    LlmCredentialBundle,
)
from ultrakb.ingest_result import ImportUnitOutcome
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope

LONG_DOC_TYPES = frozenset({"long_pdf", "pageindex_cloud"})


def is_long_doc(meta: dict) -> bool:
    return meta.get("type") in LONG_DOC_TYPES


@dataclass(frozen=True)
class RecompileTarget:
    file_hash: str
    name: str
    doc_name: str
    kind: str
    execution_mode: str | None = None
    unit_id: str | None = None


@dataclass(frozen=True)
class RecompileSelection:
    status: Literal["ready", "invalid", "empty", "not_found", "multiple"]
    targets: tuple[RecompileTarget, ...] = ()
    version: str | None = None


def select_recompilation(
    kb_dir: Path,
    identifier: str | None = None,
    *,
    all_docs: bool = False,
    confirmation: bool = False,
    scope: KnowledgeScope | None = None,
    unit_id: str | None = None,
) -> RecompileSelection:
    scope = resolve_scope(kb_dir, scope)
    if bool(identifier) == all_docs or (unit_id is not None and all_docs):
        return RecompileSelection("invalid")
    from .storage_recompilation import select_retained

    return select_retained(
        scope, identifier, all_docs=all_docs, confirmation=confirmation, unit_id=unit_id
    )


def refresh_schema(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> bool:
    """Explicitly publish the current schema through the main-store protocol."""
    from .storage_recompilation import refresh_compilation_schema

    _, _, changed = refresh_compilation_schema(resolve_scope(kb_dir, scope))
    return changed


@dataclass(frozen=True)
class RecompileResult:
    status: Literal[
        "compiled", "skipped", "failed", "rejected", "conflict", "blocked", "stopped", "partial"
    ]
    name: str = ""
    kind: str = "short"
    message: str | None = None
    error_type: str | None = None
    elapsed: float | None = None
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    version: str | None = None
    quality: tuple[str, ...] = ()
    quality_known: bool | None = None
    execution_mode: str | None = None
    units: tuple[ImportUnitOutcome, ...] = ()
    model_usage: dict | None = None
    knowledge_revision_id: str | None = None
    publication_id: str | None = None
    proposal_id: str | None = None


async def recompile_document(
    kb_dir: Path,
    file_hash: str,
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    model: str | None = None,
    max_concurrency: int | None = None,
    version: str | None = None,
    scope: KnowledgeScope | None = None,
    unit_id: str | None = None,
) -> RecompileResult:
    """Reload the exact document under its lease; never convert or index again.

    Every entrypoint captures settings under the lease. A batch reuses its context.
    """
    scope = resolve_scope(kb_dir, scope)
    context = context or ExecutionContext()
    if bundle is not None:
        if context.snapshot is not None and context.snapshot.values()["credentials"] != asdict(
            bundle
        ):
            raise ValueError("Recompilation credentials differ from the captured execution")
        if context.executor is not None and context.executor.bindings.credentials() != bundle:
            raise ValueError("Recompilation credentials differ from the active executor")
        context.credential_bundle = LlmCredentialBundle(**asdict(bundle))
    from .storage_compilation import recompile_stored

    return await recompile_stored(
        scope,
        file_hash,
        unit_id=unit_id,
        context=context,
        version=version,
        model=model,
        max_concurrency=max_concurrency,
    )
