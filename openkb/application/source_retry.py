"""Resume current source work from retained inputs, preserving completed units."""

from dataclasses import replace
from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.application.retained_inputs import frozen_source_input
from openkb.application.sources import source_view_id
from openkb.compilation_report import collect_compile_report
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
) -> IngestResult:
    from openkb.application.ingestion import import_prepared_source

    root = kb_dir.resolve()
    context = context or ExecutionContext()
    with (
        kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting),
        context.begin(root) as credentials,
        collect_compile_report() as compilation,
    ):
        source = read_source(root, source_id)
        admission = read_admission(root, source)
        if source.removed or admission.revision.original_kind != "original":
            raise ValueError(
                "Choose a current source with a retained original; "
                "preview reprocessing for legacy data"
            )
        if scope:
            scope = resolve_scope(root, scope, writable=True)
            if scope.view_id != source_view_id(root, source):
                raise ValueError("Source belongs to another knowledge view")
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
            unfinished=result.unfinished + tuple(compilation.unfinished),
        )
