"""Resume current source work from retained inputs, preserving completed units."""

from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.ingest_result import IngestResult
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


class RetryTargetChanged(ValueError):
    pass


def retry_source(
    kb_dir: Path,
    source_id: str,
    *,
    unit_id: str | None = None,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
    retry_failed_response: bool = False,
    source_revision_id: str | None = None,
    unit_revision_id: str | None = None,
) -> IngestResult:
    scope = resolve_scope(kb_dir, scope)
    from ultrakb.storage.compile_summary_recovery import summary_recovery_requested

    from .storage_source_retry import retry_stored_source

    with summary_recovery_requested(retry_failed_response):
        return retry_stored_source(
            scope,
            source_id,
            unit_id=unit_id,
            context=context,
            source_revision_id=source_revision_id,
            unit_revision_id=unit_revision_id,
        )
