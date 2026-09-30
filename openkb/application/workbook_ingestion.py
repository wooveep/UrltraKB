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
    from openkb.ingest_records import UnitRevision
    from openkb.source_catalog import read_record
    from openkb.unit_publication import list_source_units
    from openkb.workbooks.catalog import retain_workbook

    for unit in list_source_units(kb_dir, admission.source.source_id):
        target = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        if target.source_revision_id == admission.revision.source_revision_id:
            saved_basis = json.loads(target.processing_fingerprint)
            saved_basis.pop("sheet", None)
            fingerprint = json.dumps(saved_basis, sort_keys=True)
            break

    try:
        workbook = retain_workbook(kb_dir, admission, prepared)
        if workbook.error:
            raise ValueError(workbook.error)
    except Exception as exc:
        from openkb.mutation import RecoveryRequired
        from openkb.processing_policy import ReprocessingRequired

        if isinstance(exc, RecoveryRequired):
            raise
        return IngestResult(
            admission.source.identity,
            "blocked" if isinstance(exc, ReprocessingRequired) else "failed",
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
        if not any(sheet.key == selected_unit.key for sheet in workbook.sheets):
            if not options["assessment"].missing_fields:
                from openkb.workbooks.lifecycle import retire_missing_sheets

                context = options.get("context")
                return aggregate_units(
                    retire_missing_sheets(
                        kb_dir,
                        admission,
                        workbook,
                        fingerprint,
                        view_id=options["scope"].view_id,
                        unit_id=selected_unit.unit_id,
                        retry_confirmed=options.get("retry_confirmed", False),
                        check_stop=context.check_stop if context else lambda: None,
                    )
                )
            from openkb.application.ingestion import result_from_publication
            from openkb.unit_publication import read_unit_publication

            state = read_unit_publication(kb_dir, selected_unit.unit_id, options["scope"].view_id)
            return result_from_publication(
                kb_dir,
                admission,
                state,
                status="skipped" if state.status == "retired" else "blocked",
            )
    results = []
    if selected_unit is None and not options["assessment"].missing_fields:
        from openkb.workbooks.lifecycle import retire_missing_sheets

        context = options.get("context")
        results.extend(
            retire_missing_sheets(
                kb_dir,
                admission,
                workbook,
                fingerprint,
                view_id=options["scope"].view_id,
                check_stop=context.check_stop if context else lambda: None,
            )
        )
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
