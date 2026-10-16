"""Explicit retries use retained worksheet targets and never require the user's file."""

from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.ingest_result import IngestResult
from ultrakb.knowledge_scope import KnowledgeScope


def retry_worksheet(
    kb_dir: Path,
    source_id: str,
    unit_id: str,
    *,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
    source_revision_id: str | None = None,
    unit_revision_id: str | None = None,
    retry_failed_response: bool = False,
) -> IngestResult:
    from ultrakb.application.source_retry import retry_source

    return retry_source(
        kb_dir,
        source_id,
        unit_id=unit_id,
        context=context,
        scope=scope,
        source_revision_id=source_revision_id,
        unit_revision_id=unit_revision_id,
        retry_failed_response=retry_failed_response,
    )
