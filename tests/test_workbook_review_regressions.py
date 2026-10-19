"""Recovered workbooks keep every planned sheet and independent accepted results."""

from zipfile import ZipFile

import pytest
from test_pending_empty_html import html_model_transport  # noqa: F401


@pytest.fixture
def model_context():
    from ultrakb.application.execution import ExecutionContext

    return ExecutionContext()


pytest_plugins = ("pending_fixtures", "workbook_fixtures")
pytestmark = pytest.mark.integration


def test_embedded_workbook_recovery_preserves_independent_sheet_publications(
    kb_dir, writer_document, three_sheets, model_context, monkeypatch
):
    from native_catalog_fixtures import list_source_units, unit_inventory

    import ultrakb.application.storage_workbooks as ingestion
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import (
        pending_status,
        process_pending,
        read_pending_job,
        retry_pending_job,
    )

    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/book.xlsx", three_sheets.read_bytes())
    parent = import_document(kb_dir, writer_document, context=model_context)
    assert parent.status == "added", parent.message
    process_pending(kb_dir, max_jobs=1, context=model_context)
    original = ingestion.compile_stored_unit
    count = 0

    def die_before_next_unit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise SystemExit("Worker termination between sheet publications")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingestion, "compile_stored_unit", die_before_next_unit)
    with pytest.raises(SystemExit):
        process_pending(kb_dir, max_jobs=1, context=model_context)
    monkeypatch.setattr(ingestion, "compile_stored_unit", original)
    job = next(row for row in pending_status(kb_dir)["jobs"] if row["filename"] == "book.xlsx")
    job = read_pending_job(kb_dir, job["id"])
    assert job["status"] == "interrupted", job
    units = list_source_units(kb_dir, job["source_id"])
    assert len(units) == 3  # Native planning retains the whole inventory before the first send.
    [first] = [
        unit for unit in units if unit_inventory(kb_dir, unit.unit_id)["successful_revision_id"]
    ]
    accepted = unit_inventory(kb_dir, first.unit_id)["successful_revision_id"]
    assert accepted == first.unit_revision_id
    retry_pending_job(kb_dir, job["id"])
    recovered = process_pending(kb_dir, context=model_context)
    result = read_pending_job(kb_dir, job["id"])
    assert result["status"] == "completed", (result, recovered)
    assert len(list_source_units(kb_dir, job["source_id"])) == 3
    assert unit_inventory(kb_dir, first.unit_id)["successful_revision_id"] == accepted
