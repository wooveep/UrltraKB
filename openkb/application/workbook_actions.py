"""Explicit retries use retained worksheet targets and never require the user's file."""

from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope


def retry_worksheet(
    kb_dir: Path,
    source_id: str,
    unit_id: str,
    *,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> IngestResult:
    from openkb.application.source_retry import retry_source

    return retry_source(kb_dir, source_id, unit_id=unit_id, context=context, scope=scope)
