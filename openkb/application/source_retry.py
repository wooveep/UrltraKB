"""Resume current source work from retained inputs, preserving completed units."""

from dataclasses import replace
from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.application.retained_inputs import frozen_source_input
from openkb.compilation_report import collect_compile_report
from openkb.compile_recovery import explicit_compile_retry
from openkb.ingest_records import UnitRevision
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.llm_usage_execution import track_import_usage
from openkb.locks import kb_ingest_lock
from openkb.source_catalog import read_admission, read_record, read_source
from openkb.unit_publication import list_source_units


@track_import_usage
def retry_source(
    kb_dir: Path,
    source_id: str,
    *,
    unit_id: str | None = None,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
    retry_failed_response: bool = False,
) -> IngestResult:
    from openkb.application.ingestion import import_prepared_source

    root = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        source = read_source(root, source_id)
        admission = read_admission(root, source)
        if source.removed or admission.revision.original_kind != "original":
            raise ValueError(
                "Choose a current source with a retained original; "
                "preview reprocessing for legacy data"
            )
        if scope:
            scope = resolve_scope(root, scope, writable=True)
        if unit_id:
            unit = next(
                (item for item in list_source_units(root, source_id) if item.unit_id == unit_id),
                None,
            )
            if unit is None or unit.key == "body":
                raise ValueError("Choose an available worksheet unit")
            revision = read_record(root, "unit-revisions", unit.target_revision_id, UnitRevision)
            if revision.source_revision_id != source.target_revision_id:
                raise ValueError("Worksheet target was superseded by another workbook revision")
        from openkb.model_task import source_task

        if context.executor is None and context.resume_model_task_id is None:
            context.resume_model_task_id = source_task(root, source_id, source.target_revision_id)
        with (
            context.begin(root) as credentials,
            collect_compile_report() as compilation,
            explicit_compile_retry(
                root,
                source_id,
                retry_failed_response=retry_failed_response,
                view_id=scope.view_id if scope else "legacy",
            ),
        ):
            result = import_prepared_source(
                root,
                frozen_source_input(root, source, admission.revision),
                admission=admission,
                bundle=credentials,
                context=context,
                on_event=context.on_event,
                scope=scope,
                retry_confirmed=True,
                unit_id=unit_id,
            )
        return replace(
            result,
            quality=tuple(dict.fromkeys((*result.quality, *compilation.quality))),
            unfinished=tuple(dict.fromkeys((*result.unfinished, *compilation.unfinished))),
        )
