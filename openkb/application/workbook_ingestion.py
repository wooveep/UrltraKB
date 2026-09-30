"""Publish each worksheet through the native unit transaction, then aggregate receipts."""

import json
from dataclasses import replace

from openkb.ingest_result import IngestResult


def aggregate_units(results):
    if not results:
        raise ValueError("No worksheet outcomes to aggregate")
    good = [result for result in results if result.status in {"added", "skipped"}]
    status = (
        "partial"
        if len(good) != len(results)
        else "skipped"
        if all(result.status == "skipped" for result in results)
        else "added"
    )
    if not good:
        status = "failed" if any(result.status == "failed" for result in results) else "blocked"
    if any(result.status == "stopped" for result in results):
        status = "stopped"
    return replace(
        results[0],
        status=status,
        units=tuple(unit for result in results for unit in result.units),
        resources=tuple(dict.fromkeys(path for result in results for path in result.resources)),
        quality=tuple(dict.fromkeys(note for result in results for note in result.quality)),
        unfinished=tuple(dict.fromkeys(stage for result in results for stage in result.unfinished)),
        message="; ".join(result.message for result in results if result.message) or None,
    )


def import_workbook_units(kb_dir, prepared, *, admission, fingerprint, unit_id=None, **options):
    from openkb.application.ingestion import process_import_unit
    from openkb.workbooks.catalog import retain_workbook

    try:
        workbook = retain_workbook(kb_dir, admission, prepared)
        if workbook.error:
            raise ValueError(workbook.error)
    except Exception as exc:
        from openkb.mutation import RecoveryRequired

        if isinstance(exc, RecoveryRequired):
            raise
        return IngestResult(
            admission.source.identity,
            "failed",
            (str(kb_dir / admission.revision.original),),
            source_id=admission.source.source_id,
            source_revision_id=admission.revision.source_revision_id,
            unfinished=("sheet_inventory",),
            message=f"Workbook inventory: {exc}",
        )
    if not workbook.sheets:
        return IngestResult(
            admission.source.identity,
            "blocked",
            (str(kb_dir / admission.revision.original),),
            source_id=admission.source.source_id,
            source_revision_id=admission.revision.source_revision_id,
            unfinished=("sheet_inventory",),
            message="Workbook has no worksheets",
        )
    basis = json.loads(fingerprint)
    basis.pop("sheet", None)
    from openkb.ingest_records import UnitRevision
    from openkb.source_catalog import read_record
    from openkb.unit_publication import list_source_units

    selected_unit = None
    if unit_id is not None:
        selected_unit = next(
            (
                unit
                for unit in list_source_units(kb_dir, admission.source.source_id)
                if unit.unit_id == unit_id
            ),
            None,
        )
        if selected_unit is None:
            raise ValueError("Worksheet does not belong to this source")
    results = []
    for sheet in workbook.sheets:
        if selected_unit and sheet.key != selected_unit.key:
            continue
        selected = json.dumps(
            {**basis, "sheet": {"key": sheet.key, "policy": workbook.policy}}, sort_keys=True
        )
        if selected_unit:
            target = read_record(
                kb_dir, "unit-revisions", selected_unit.target_revision_id, UnitRevision
            )
            selected = target.processing_fingerprint
        results.append(
            process_import_unit(
                kb_dir, prepared, admission=admission, fingerprint=selected, sheet=sheet, **options
            )
        )
    return aggregate_units(results)
