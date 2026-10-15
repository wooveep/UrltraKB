"""Retained partial workbook and unprocessed source, with zero model sends."""

import hashlib
import json

from office_publication_fixtures import publish_workbook
from openpyxl import Workbook

from ultrakb.inputs import prepared_input
from ultrakb.locks import kb_ingest_lock
from ultrakb.source_catalog import admit_source_revision, read_record
from ultrakb.unit_publication import (
    begin_unit_attempt,
    list_source_units,
    plan_import_units,
    record_unit_failure,
)


def partial_workbook(kb, path, *, bundle):
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.workbooks.catalog import retain_workbook

    workbook = Workbook()
    workbook.active.title = "Alpha"
    workbook.active["A1"] = "Available review policy"
    for name in ("Beta", "Gamma"):
        workbook.create_sheet(name)["A1"] = f"Unavailable {name} policy"
    with kb_ingest_lock(kb / ".ultrakb"):
        workbook.save(path)
        with prepared_input(path) as prepared:
            admission = admit_source_revision(kb, prepared)
    published = publish_workbook(
        kb, path, admission=admission, bundle=bundle, sheet_names=["Alpha"], record_model_task=False
    )
    with kb_ingest_lock(kb / ".ultrakb"), prepared_input(path) as prepared:
        retained = retain_workbook(kb, admission, prepared)
        [first] = list_source_units(kb, admission.source.source_id)
        basis = json.loads(
            read_record(
                kb, "unit-revisions", first.target_revision_id, UnitRevision
            ).processing_fingerprint
        )
        for sheet in retained.sheets:
            if sheet.name == "Alpha":
                continue
            selected = json.dumps(
                {**basis, "sheet": {"key": sheet.key, "policy": retained.policy}}, sort_keys=True
            )
            unit, revision = plan_import_units(
                kb,
                admission,
                selected,
                key=sheet.key,
                name=sheet.name,
                doc_name=admission.source.doc_name
                + "-sheet-"
                + hashlib.sha256(sheet.key.encode()).hexdigest()[:12],
            )
            if sheet.name == "Beta":
                state, _ = begin_unit_attempt(kb, unit, revision)
                record_unit_failure(
                    kb, state, "conversion", OSError("Local worksheet delivery failed")
                )
    return published.source_id


def unprocessed_source(kb, path):
    from ultrakb.locks import atomic_write_text

    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(path, "# Retained original\n\nNo normalized body yet.")
        with prepared_input(path) as prepared:
            return admit_source_revision(kb, prepared).source.source_id
