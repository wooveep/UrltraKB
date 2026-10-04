"""Source operations account for every sheet in the retained workbook inventory."""

import asyncio
from zipfile import ZipFile

import pytest

pytest_plugins = ("test_office_import", "test_workbook_import")


def test_workbook_version_correction_moves_all_retained_sheets(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.source_catalog import read_record, read_source
    from openkb.unit_publication import list_source_units, read_unit_publication
    from openkb.view_records import SourceMetadata, VersionAnnotation

    first = import_document(kb_dir, three_sheets)
    assert first.status == "added", first.message
    assert len(first.units) == 3
    review = review_source_version(kb_dir, first.source_id)
    metadata = SourceMetadata(product="Fixture", family="Sheets", applicable_versions=("1",))
    supplement_version_reviews(kb_dir, {review.review_id: metadata})
    three_sheets.unlink()

    def no_reparse(*args, **kwargs):
        pytest.fail("Version correction reparsed the frozen workbook")

    monkeypatch.setattr("openkb.workbooks.xlsx.read_xlsx", no_reparse)
    resumed = resume_version_review(kb_dir, review.review_id)
    assert resumed.status == "added", resumed.message
    assert {unit.name for unit in resumed.units if unit.status == "completed"} == {
        "Alpha",
        "Beta",
        "Gamma",
    }
    assert {unit.unit_id for unit in resumed.units} == {unit.unit_id for unit in first.units}
    views = {unit.view_id for unit in resumed.units}
    assert len(views) == 1 and views != {first.units[0].view_id}
    source = read_source(kb_dir, first.source_id)
    annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
    assert annotation.metadata == metadata
    assert all(
        read_unit_publication(kb_dir, unit.unit_id, annotation.view_id).status == "completed"
        for unit in list_source_units(kb_dir, source.source_id)
    )


def test_embedded_workbook_recovery_requires_retry_for_uncreated_sheets(
    kb_dir, writer_document, three_sheets, pdf_model, monkeypatch
):
    import openkb.application.ingestion as ingestion
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending, retry_pending_job
    from openkb.source_catalog import read_source
    from openkb.source_changes import source_view
    from openkb.unit_publication import list_source_units, read_unit_publication

    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/book.xlsx", three_sheets.read_bytes())
    import_document(kb_dir, writer_document)
    process_pending(kb_dir, max_jobs=1)
    original = ingestion.process_import_unit
    count = 0

    def die_before_next_unit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise SystemExit("Worker termination between sheet publications")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingestion, "process_import_unit", die_before_next_unit)
    with pytest.raises(SystemExit):
        process_pending(kb_dir, max_jobs=1)
    monkeypatch.setattr(ingestion, "process_import_unit", original)
    process_pending(kb_dir)
    job = next(row for row in pending_status(kb_dir)["jobs"] if row["kind"] == "import")
    units = list_source_units(kb_dir, job["source_id"])
    assert len(units) == 1
    view_id = source_view(kb_dir, read_source(kb_dir, job["source_id"]))
    published = read_unit_publication(kb_dir, units[0].unit_id, view_id)
    assert job["status"] == "interrupted"
    assert process_pending(kb_dir)["processed"] == 0
    retry_pending_job(kb_dir, job["id"])
    process_pending(kb_dir)
    recovered = next(row for row in pending_status(kb_dir)["jobs"] if row["id"] == job["id"])
    assert recovered["status"] == "completed"
    assert len(list_source_units(kb_dir, job["source_id"])) == 3
    assert read_unit_publication(kb_dir, units[0].unit_id, view_id) == published


@pytest.mark.parametrize("delete_sheets", [False, True])
def test_all_confirmed_empty_sheets_refresh_retires_current_but_retains_history(
    kb_dir, three_sheets, pdf_model, delete_sheets
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_knowledge_view, refresh_status
    from openkb.application.views import view_scope
    from openkb.unit_publication import read_head, wiki_versions
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Sheets", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    assert first.status == "added"
    scope = view_scope(kb_dir, first.units[0].view_id)
    head = read_head(kb_dir, scope.view_id)
    history = view_scope(kb_dir, scope.view_id, historical_revision=head.knowledge_revision_id)
    old_pages = wiki_versions(kb_dir, history.wiki_dir)
    book = load_workbook(three_sheets)
    for sheet in book:
        sheet["A1"] = None
    if delete_sheets:
        del book["Beta"]
        del book["Gamma"]
    book.save(three_sheets)
    updated = import_document(kb_dir, three_sheets, metadata=metadata)
    assert updated.status == "added", updated.message
    assert len(updated.units) == 3
    assert all(unit.status in {"empty", "retired"} for unit in updated.units)
    status = refresh_status(kb_dir, scope=scope)
    assert not status["effective_inputs"] and status["needs_refresh"]
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "completed", result.message
    assert read_head(kb_dir, scope.view_id).inputs == {}
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert not list((scope.wiki_dir / "summaries").glob("*.md"))
    assert wiki_versions(kb_dir, history.wiki_dir) == old_pages


def test_refresh_does_not_retire_unfinished_workbook_sheets(kb_dir, three_sheets, pdf_model):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_knowledge_view
    from openkb.application.views import view_scope
    from openkb.unit_publication import read_head, wiki_versions
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Sheets", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    book = load_workbook(three_sheets)
    book["Alpha"]["A1"] = "ALPHA_UPDATED"
    book["Beta"]["A1"] = None
    book.save(three_sheets)
    with ZipFile(three_sheets) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet3.xml"] = b"<broken"
    with ZipFile(three_sheets, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    updated = import_document(kb_dir, three_sheets, metadata=metadata)
    assert updated.status == "partial"
    assert {unit.name: unit.status for unit in updated.units} == {
        "Alpha": "completed",
        "Beta": "empty",
        "Gamma": "failed",
    }
    scope = view_scope(kb_dir, first.units[0].view_id)
    head, pages = read_head(kb_dir, scope.view_id), wiki_versions(kb_dir, scope.wiki_dir)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "blocked", result.message
    assert read_head(kb_dir, scope.view_id) == head
    assert wiki_versions(kb_dir, scope.wiki_dir) == pages


def test_empty_workbook_version_correction_uses_retained_sheet_input(
    kb_dir, three_sheets, pdf_model
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        read_version_review,
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    book = load_workbook(three_sheets)
    for sheet in book:
        sheet["A1"] = None
    book.save(three_sheets)
    first = import_document(kb_dir, three_sheets)
    assert len(first.units) == 3 and all(unit.status == "empty" for unit in first.units)
    review = review_source_version(kb_dir, first.source_id)
    supplement_version_reviews(
        kb_dir,
        {review.review_id: SourceMetadata(product="Fixture", applicable_versions=("1",))},
    )
    resumed = resume_version_review(kb_dir, review.review_id)
    assert resumed.status == "added", resumed.message
    assert len(resumed.units) == 3 and all(unit.status == "empty" for unit in resumed.units)
    assert read_version_review(kb_dir, review.review_id).status == "completed"


def test_workbook_version_review_stays_ready_until_every_sheet_is_processed(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import openkb.application.ingestion as ingestion
    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        read_version_review,
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    first = import_document(kb_dir, three_sheets)
    review = review_source_version(kb_dir, first.source_id)
    supplement_version_reviews(
        kb_dir,
        {review.review_id: SourceMetadata(product="Fixture", applicable_versions=("1",))},
    )
    original = ingestion.process_import_unit
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise SystemExit("Worker disappeared after the first corrected sheet")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingestion, "process_import_unit", interrupted)
    with pytest.raises(SystemExit):
        resume_version_review(kb_dir, review.review_id)
    assert read_version_review(kb_dir, review.review_id).status == "ready"
    monkeypatch.setattr(ingestion, "process_import_unit", original)
    resumed = resume_version_review(kb_dir, review.review_id)
    assert resumed.status == "added", resumed.message
    assert read_version_review(kb_dir, review.review_id).status == "completed"
