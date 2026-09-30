"""Retire confirmed empty/deleted worksheet contributions with their refresh reasons."""

import json
from typing import Literal

from openkb.ingest_records import ImportUnit
from openkb.locks import LockCancelled
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.source_catalog import read_record, read_source, record_path, write_record
from openkb.source_changes import exclude_source_inputs, source_change_heads
from openkb.unit_publication import (
    begin_unit_attempt,
    list_source_units,
    plan_import_units,
    read_unit_publication,
    record_unit_failure,
)


def retire_sheet(
    kb_dir, admission, unit, state, kind: Literal["empty", "withdrawn"], *, check_stop=lambda: None
):
    check_stop()
    from openkb.lifecycle import current_generation

    source = read_source(kb_dir, admission.source.source_id)
    current = read_unit_publication(kb_dir, unit.unit_id, state.view_id)
    if (
        current_generation(kb_dir) != admission.discovery_intent.kb_generation
        or source.removed
        or source.target_revision_id != admission.revision.source_revision_id
        or source.target_generation != admission.source.target_generation
        or read_record(kb_dir, "units", unit.unit_id, ImportUnit) != unit
        or current.attempt_id != state.attempt_id
    ):
        raise ValueError("Worksheet target changed before retirement")
    changed = exclude_source_inputs(
        kb_dir, source, kind, view_id=state.view_id, unit_ids={unit.unit_id}
    )
    completed = state.model_copy(
        update={
            "status": "empty" if kind == "empty" else "retired",
            "stage": "retired",
            "error_type": None,
            "message": (
                "Confirmed empty worksheet"
                if kind == "empty"
                else "Worksheet absent from complete inventory"
            )
            + "; current contribution withdrawn, historical publication retained.",
        }
    )
    records = {
        record_path(kb_dir, "sources", source.source_id): changed,
        record_path(kb_dir, "publications", state.publication_id): completed,
        record_path(kb_dir, "attempts", state.attempt_id): completed,
        **source_change_heads(
            kb_dir, changed, kind, unit_ids={unit.unit_id}, view_id=state.view_id
        ),
    }
    with mutation_scope(kb_dir, list(records), operation="retire-worksheet"):
        for path, record in records.items():
            write_record(path, record)
        check_stop()
    return completed


def retire_missing_sheets(
    kb_dir,
    admission,
    workbook,
    fingerprint,
    *,
    view_id,
    check_stop=lambda: None,
    unit_id=None,
    retry_confirmed=False,
):
    from openkb.application.ingestion import result_from_publication
    from openkb.workbooks.catalog import unit_name

    keys = {sheet.key for sheet in workbook.sheets}
    results = []
    for previous in list_source_units(kb_dir, admission.source.source_id):
        if unit_id is not None and previous.unit_id != unit_id:
            continue
        if previous.key == "body" or previous.key in keys:
            continue
        check_stop()
        selected = json.dumps(
            {
                **json.loads(fingerprint),
                "sheet": {"key": previous.key, "policy": workbook.policy, "retirement": "absent"},
            },
            sort_keys=True,
        )
        unit, revision = plan_import_units(
            kb_dir,
            admission,
            selected,
            key=previous.key,
            name=unit_name(kb_dir, previous, previous.target_revision_id, previous.doc_name),
        )
        state, runnable = begin_unit_attempt(
            kb_dir,
            unit,
            revision,
            view_id=view_id,
            discovery_intent=admission.discovery_intent,
            retry_confirmed=retry_confirmed,
        )
        if runnable:
            try:
                state = retire_sheet(
                    kb_dir, admission, unit, state, "withdrawn", check_stop=check_stop
                )
            except RecoveryRequired:
                raise
            except LockCancelled as exc:
                record_unit_failure(kb_dir, state, "retirement", exc)
                raise
            except Exception as exc:
                failed = record_unit_failure(kb_dir, state, "retirement", exc)
                results.append(result_from_publication(kb_dir, admission, failed, status="failed"))
                continue
        results.append(
            result_from_publication(
                kb_dir,
                admission,
                state,
                status="added"
                if runnable
                else "skipped"
                if state.status == "retired"
                else "blocked",
            )
        )
    return results
