"""Workbook updates reconcile physical sheets and preserve per-unit publication facts."""

import pytest

pytest_plugins = ("test_workbook_import",)


def test_xls_rename_and_reorder_use_the_same_identity_reconciliation(kb_dir, tmp_path, pdf_model):
    from pathlib import Path
    from shutil import copyfile

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    fixtures = Path(__file__).parent / "fixtures/office"
    workbook = tmp_path / "workbook.xls"
    copyfile(fixtures / "typed-sheets.xls", workbook)
    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, workbook, metadata=metadata)
    original = {unit.name: unit.unit_id for unit in first.units}
    copyfile(fixtures / "renamed-sheets.xls", workbook)
    second = import_document(kb_dir, workbook, metadata=metadata)
    assert {unit.name: unit.unit_id for unit in second.units} == {
        "Renamed Alpha": original["Alpha"],
        "Beta": original["Beta"],
        "Gamma": original["Gamma"],
    }
    renamed = read_document_source(kb_dir, first.source_id, unit_id=original["Alpha"])
    assert renamed["sheet"]["ordinal"] == 2
    assert renamed["sheet"]["identity_basis"] == "unique_rename_in_complete_inventory"


def test_rename_and_reorder_keep_sheet_identity_without_using_table_position(
    kb_dir, three_sheets, pdf_model
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    original = {unit.name: unit.unit_id for unit in first.units}
    book = load_workbook(three_sheets)
    book["Alpha"].title = "Renamed Alpha"
    book.move_sheet("Gamma", offset=-2)
    book.save(three_sheets)
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    assert second.status == "added", second.message
    current = {unit.name: unit.unit_id for unit in second.units}
    assert current == {
        "Renamed Alpha": original["Alpha"],
        "Beta": original["Beta"],
        "Gamma": original["Gamma"],
    }
    renamed = read_document_source(kb_dir, first.source_id, unit_id=original["Alpha"])
    assert renamed["sheet"]["ordinal"] == 2
    assert renamed["sheet"]["identity_basis"] == "unique_rename_in_complete_inventory"


def test_confirmed_empty_and_deleted_sheets_retire_only_their_contributions(
    kb_dir, three_sheets, pdf_model
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_status
    from openkb.application.sources import source_inventory
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    units = {unit.name: unit for unit in first.units}
    book = load_workbook(three_sheets)
    book["Alpha"]["A1"] = "ALPHA_NEW_REVISION"
    book["Beta"]["A1"] = None
    del book["Gamma"]
    book.save(three_sheets)
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    assert second.status == "added", second.message
    assert {unit.name: unit.status for unit in second.units} == {
        "Alpha": "completed",
        "Beta": "empty",
        "Gamma": "retired",
    }
    inventory = source_inventory(kb_dir)[0]
    assert len(inventory["units"]) == 3
    empty = read_document_source(kb_dir, first.source_id, unit_id=units["Beta"].unit_id)
    assert empty["status"] == "empty" and "BETA_SHEET" not in empty["content"]
    retired = read_document_source(kb_dir, first.source_id, unit_id=units["Gamma"].unit_id)
    assert retired["validity"] == "withdrawn" and retired["status"] == "retired"
    assert retired["target_source_revision_id"] == second.source_revision_id
    pending = refresh_status(kb_dir, scope=view_scope(kb_dir, first.units[0].view_id))[
        "needs_refresh"
    ]
    assert any(
        reason["kind"] == "empty" for reason in pending[f"summaries/{units['Beta'].doc_name}.md"]
    )
    assert any(
        reason["kind"] == "withdrawn"
        for reason in pending[f"summaries/{units['Gamma'].doc_name}.md"]
    )


@pytest.mark.parametrize("new_version", [False, True])
def test_failed_and_objects_only_sheets_do_not_become_successful_empty_targets(
    kb_dir, three_sheets, pdf_model, tmp_path, new_version
):
    from zipfile import ZipFile

    from openpyxl import load_workbook
    from openpyxl.drawing.image import Image
    from PIL import Image as PillowImage

    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    original = {unit.name: unit for unit in first.units}
    book = load_workbook(three_sheets)
    book["Alpha"]["A1"] = "ALPHA_NEW"
    book["Beta"]["A1"] = None
    picture = tmp_path / "red.png"
    PillowImage.new("RGB", (10, 10), "red").save(picture)
    book["Beta"].add_image(Image(picture), "B2")
    book.save(three_sheets)
    with ZipFile(three_sheets) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet3.xml"] = b"<broken"
    with ZipFile(three_sheets, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    if new_version:
        metadata = metadata.model_copy(update={"applicable_versions": ("2",)})
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    assert second.status == "partial", second.message
    inventory = source_inventory(kb_dir)[0]
    units = {unit["name"]: unit for unit in inventory["units"]}
    assert units["Beta"]["content_state"] == "objects_only"
    assert units["Gamma"]["content_state"] == "parse_failed"
    for name in ("Beta", "Gamma"):
        assert units[name]["status"] == "failed"
        assert units[name]["successful_source_revision_id"] == (
            None if new_version else first.source_revision_id
        )
    if not new_version:
        old = read_document_source(kb_dir, first.source_id, unit_id=original["Gamma"].unit_id)
        assert old["source_revision_id"] == first.source_revision_id
        assert "GAMMA_SHEET" in old["content"]


def test_empty_new_version_does_not_withdraw_old_view(kb_dir, three_sheets, pdf_model):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    book = load_workbook(three_sheets)
    for sheet in book:
        sheet["A1"] = None
    book.save(three_sheets)
    second = import_document(
        kb_dir, three_sheets, metadata=metadata.model_copy(update={"applicable_versions": ("2",)})
    )
    assert all(unit.status == "empty" for unit in second.units)
    old = read_document_source(
        kb_dir,
        first.source_id,
        unit_id=first.units[0].unit_id,
        scope=view_scope(kb_dir, first.units[0].view_id),
    )
    assert old["source_revision_id"] == first.source_revision_id and old["validity"] == "current"


def test_workbook_removal_preview_names_owned_sheets(kb_dir, three_sheets, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.removal import preview_removal

    first = import_document(kb_dir, three_sheets)
    preview = preview_removal(kb_dir, first.source_id)
    labels = str(preview.plan.actions)
    assert all(unit.unit_id in labels for unit in first.units)


@pytest.mark.parametrize("deleted", [False, True])
def test_retirement_commit_failure_keeps_old_contribution_and_refresh_reason(
    kb_dir, three_sheets, pdf_model, monkeypatch, deleted
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_status
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.mutation import MutationSnapshot
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    beta = next(unit for unit in first.units if unit.name == "Beta")
    book = load_workbook(three_sheets)
    if deleted:
        del book["Beta"]
    else:
        book["Beta"]["A1"] = None
    book.save(three_sheets)
    commit = MutationSnapshot.mark_committed

    def fail_retirement(snapshot):
        if snapshot.operation == "retire-worksheet":
            raise OSError("Fixture lost retirement commit marker")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", fail_retirement)
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    assert second.status == "partial"
    old = read_document_source(kb_dir, first.source_id, unit_id=beta.unit_id)
    assert old["status"] == "failed" and old["validity"] == "needs_refresh"
    pending = refresh_status(kb_dir, scope=view_scope(kb_dir, beta.view_id))["needs_refresh"]
    assert not any(reason["kind"] == "empty" for reasons in pending.values() for reason in reasons)
    if deleted:
        from openkb.application.workbook_actions import retry_worksheet

        monkeypatch.setattr(MutationSnapshot, "mark_committed", commit)
        retried = retry_worksheet(kb_dir, first.source_id, beta.unit_id)
        assert len(retried.units) == 1 and retried.units[0].status == "retired"


@pytest.mark.parametrize("keep_anchor", [False, True])
def test_all_renamed_and_reordered_cannot_inherit_another_sheets_history(
    kb_dir, three_sheets, pdf_model, keep_anchor
):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    original = {unit.unit_id for unit in first.units}
    book = load_workbook(three_sheets)
    for sheet in book:
        if not keep_anchor or sheet.title != "Alpha":
            sheet.title = "New " + sheet.title
    book.move_sheet("New Gamma", offset=-1 if keep_anchor else -2)
    book.save(three_sheets)
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    current = {unit.unit_id for unit in second.units if unit.name.startswith("New ")}
    assert len(current) == (2 if keep_anchor else 3) and not current & original


def test_empty_sheet_clarification_finishes(kb_dir, three_sheets, pdf_model):
    from openpyxl import Workbook

    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook")
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    assert first.status == "added"
    empty = three_sheets.with_name("empty.xlsx")
    Workbook().save(empty)
    second = import_document(kb_dir, empty, metadata=metadata)
    assert second.status == "blocked"
    pending = list_version_reviews(kb_dir)
    assert len(pending) == 1
    supplement_version_reviews(
        kb_dir, {pending[0].review_id: SourceMetadata(applicable_versions=("2",))}
    )
    resumed = resume_version_review(kb_dir, pending[0].review_id)
    assert resumed.status == "added"
    assert list_version_reviews(kb_dir) == ()


def test_deleted_new_view_sheet_is_withdrawn(kb_dir, three_sheets, pdf_model):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    gamma = next(u for u in first.units if u.name == "Gamma")
    book = load_workbook(three_sheets)
    del book["Gamma"]
    book.save(three_sheets)
    second = import_document(
        kb_dir, three_sheets, metadata=metadata.model_copy(update={"applicable_versions": ("2",)})
    )
    read = read_document_source(kb_dir, first.source_id, unit_id=gamma.unit_id)
    assert read["target_source_revision_id"] == second.source_revision_id
    assert read["status"] == "retired"
    assert read["validity"] == "withdrawn"


def test_unextracted_background_picture_is_not_confirmed_empty(kb_dir, three_sheets, pdf_model):
    from io import BytesIO
    from zipfile import ZipFile

    from openpyxl import load_workbook
    from PIL import Image

    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Workbook", applicable_versions=("1",))
    import_document(kb_dir, three_sheets, metadata=metadata)
    book = load_workbook(three_sheets)
    book["Alpha"]["A1"] = None
    book.save(three_sheets)
    with ZipFile(three_sheets) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"</worksheet>",
        b'<picture xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        b'r:id="rBg"/></worksheet>',
    )
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rBg" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="/xl/media/background.png"/></Relationships>'
    )
    image = BytesIO()
    Image.new("RGB", (10, 10), "red").save(image, format="PNG")
    parts["xl/media/background.png"] = image.getvalue()
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b"</Types>", b'<Default Extension="png" ContentType="image/png"/></Types>'
    )
    with ZipFile(three_sheets, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    second = import_document(kb_dir, three_sheets, metadata=metadata)
    alpha = next(unit for unit in second.units if unit.name == "Alpha")
    assert alpha.status != "empty", (alpha, source_inventory(kb_dir))
