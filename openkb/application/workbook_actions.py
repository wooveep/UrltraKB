"""Explicit retries use retained worksheet targets and never require the user's file."""

from dataclasses import replace
from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.ingest_records import UnitRevision
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope
from openkb.lifecycle import read_lifecycle
from openkb.locks import kb_ingest_lock
from openkb.source_catalog import Admission, read_record, read_source, read_source_revision
from openkb.source_records import DiscoveryIntent
from openkb.unit_publication import list_source_units


def retry_worksheet(
    kb_dir: Path,
    source_id: str,
    unit_id: str,
    *,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> IngestResult:
    from openkb.application.ingestion import import_prepared_source
    from openkb.inputs import prepared_input

    context = context or ExecutionContext()
    root = kb_dir.resolve()
    with (
        read_lifecycle(root, cancelled=context.cancelled, on_wait=context.waiting),
        kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting),
    ):
        source = read_source(root, source_id)
        unit = next(
            (item for item in list_source_units(root, source_id) if item.unit_id == unit_id), None
        )
        if source.removed or unit is None or unit.key == "body":
            raise ValueError("Choose an available worksheet unit")
        revision = read_record(root, "unit-revisions", unit.target_revision_id, UnitRevision)
        if revision.source_revision_id != source.target_revision_id:
            raise ValueError("Worksheet target was superseded by another workbook revision")
        frozen = read_source_revision(root, revision.source_revision_id)
        intent = read_record(root, "discovery-intents", frozen.discovery_intent_id, DiscoveryIntent)
        with (
            prepared_input(root / frozen.original) as prepared,
            context.begin(root) as credentials,
            collect_compile_report() as compilation,
        ):
            result = import_prepared_source(
                root,
                prepared,
                admission=Admission(source, frozen, intent),
                bundle=credentials,
                context=context,
                scope=scope,
                retry_confirmed=True,
                unit_id=unit_id,
            )
            return replace(
                result,
                quality=tuple(compilation.quality),
                unfinished=result.unfinished + tuple(compilation.unfinished),
            )
