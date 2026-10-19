"""Document conversion, indexing and knowledge compilation shared by adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.ingest_result import ImportUnitOutcome, describe_ingest
from ultrakb.ingest_result import IngestResult as DocumentResult
from ultrakb.inputs import PreparedInput
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope

logger = logging.getLogger(__name__)


def add_single_file(
    file_path: Path,
    kb_dir: Path,
    *,
    stage: bool = True,
    bundle=None,
    context: ExecutionContext | None = None,
    report=logger.info,
    on_event: Callable[[dict], None] | None = None,
    prepared: PreparedInput | None = None,
    origin_url: str | None = None,
    scope: KnowledgeScope | None = None,
    download_remote_assets: bool | None = None,
) -> Literal["added", "skipped", "failed", "blocked", "partial", "stopped", "rejected"]:
    """Compatibility projection of the shared import use case's status."""
    result = import_document(
        kb_dir,
        file_path,
        stage=stage,
        bundle=bundle,
        context=context,
        report=report,
        on_event=on_event,
        prepared=prepared,
        origin_url=origin_url,
        scope=scope,
        download_remote_assets=download_remote_assets,
    )
    for line in describe_ingest(result):
        report(line)
    return result.status


@dataclass
class AddFileResult:
    """Structured add outcome for the REST API.

    Projects the shared ``import_document`` outcome with
    the original filename and a human-readable message, which the API's
    ``/add`` endpoint surfaces per file in its JSON/SSE response.
    """

    original_name: str
    saved_path: str | None
    status: str
    message: str
    source_id: str | None = None
    source_revision_id: str | None = None
    units: tuple[ImportUnitOutcome, ...] = ()
    discovery_pending: int | None = None
    imports_pending: int | None = None
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    model_usage: dict | None = None


def _add_for_api(
    file_path: Path,
    kb_dir: Path,
    *,
    bundle=None,
    context: ExecutionContext | None = None,
    source_root: Path | None = None,
    scope: KnowledgeScope | None = None,
    download_remote_assets: bool | None = None,
) -> AddFileResult:
    """Run the locked add pipeline and return a structured result for the API.

    Import preparation, locking and business work belong to ``import_document``.
    On ``skipped`` the upload owner checks whether its raw copy can be discarded.
    """
    result = import_document(
        kb_dir,
        file_path,
        bundle=bundle,
        context=context,
        source_root=source_root,
        scope=scope,
        download_remote_assets=download_remote_assets,
    )
    status_str = result.status
    if status_str == "skipped":
        message = f"Already in knowledge base: {file_path.name}"
    elif status_str == "failed":
        message = result.message or f"Failed to add: {file_path.name} (see server logs)"
    elif status_str != "added":
        message = result.message or f"Import {status_str}: {file_path.name}"
    else:
        message = f"Added: {file_path.name}"
    return AddFileResult(
        original_name=file_path.name,
        saved_path=str(file_path) if status_str == "added" else None,
        status=status_str,
        message=message,
        source_id=result.source_id,
        source_revision_id=result.source_revision_id,
        units=result.units,
        discovery_pending=result.discovery_pending,
        imports_pending=result.imports_pending,
        quality=result.quality,
        unfinished=result.unfinished,
        model_usage=result.model_usage,
    )


def import_document(
    kb_dir: Path,
    source: Path,
    *,
    bundle=None,
    on_event: Callable[[dict], None] | None = None,
    context: ExecutionContext | None = None,
    origin_url: str | None = None,
    source_root: Path | None = None,
    stage: bool = True,
    prepared: PreparedInput | None = None,
    report=logger.info,
    scope: KnowledgeScope | None = None,
    download_remote_assets: bool | None = None,
) -> DocumentResult:
    """Process one complete item and report only resources actually retained."""
    if download_remote_assets is not None and type(download_remote_assets) is not bool:
        raise ValueError("download_remote_assets must be a boolean")
    scope = resolve_scope(kb_dir, scope)
    from .storage_ingestion import import_stored_document

    return import_stored_document(
        scope,
        source,
        bundle=bundle,
        context=context,
        prepared=prepared,
        origin_url=origin_url,
        source_root=source_root,
        on_event=on_event,
        download_remote_assets=download_remote_assets,
    )
