"""Preview policy changes without processing; execute only the reviewed frozen input."""

from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.inputs import CNKI_SOURCE_EXTENSIONS, OFFICE_SOURCE_EXTENSIONS
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope


class ReprocessingConflict(ValueError):
    """The previously reviewed processing plan no longer matches the current source."""


def runtime_availability(kb_dir, source_format):
    runtime = {
        "required": f".{source_format}" in OFFICE_SOURCE_EXTENSIONS | CNKI_SOURCE_EXTENSIONS,
        "available": True,
        "reason": None,
    }
    if f".{source_format}" in CNKI_SOURCE_EXTENSIONS:
        from ultrakb.cnki.runtime import processing_identity, require_runtime

        try:
            require_runtime(processing_identity())
        except ValueError as exc:
            runtime.update(available=False, reason=str(exc))
    elif runtime["required"]:
        from ultrakb.office.runtime import runtime_path, validate_runtime

        try:
            validate_runtime(runtime_path(kb_dir))
        except (OSError, ValueError) as exc:
            runtime.update(available=False, reason=str(exc))
    return runtime


def preview_reprocessing(
    kb_dir: Path, source_id: str, *, scope: KnowledgeScope | None = None
) -> dict:
    """Read availability and policy without conversion, worksheet parsing or model calls."""
    scope = resolve_scope(kb_dir, scope)
    from .storage_reprocessing import preview_stored_reprocessing

    return preview_stored_reprocessing(scope, source_id)


def reprocess_source(
    kb_dir: Path,
    source_id: str,
    *,
    version: str,
    scope: KnowledgeScope | None = None,
    context: ExecutionContext | None = None,
):
    """CAS a new request or resume only the current request identified by the same token."""
    scope = resolve_scope(kb_dir, scope)
    from .storage_reprocessing import reprocess_stored_source

    return reprocess_stored_source(scope, source_id, version=version, context=context)
