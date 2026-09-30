"""Durable workbook-level and per-unit failures are visible through public adapters."""

import pytest

pytest_plugins = ("test_workbook_import",)


@pytest.mark.parametrize("empty_inventory", [False, True])
def test_inventory_failure_survives_import_and_keeps_old_body(
    kb_dir, three_sheets, pdf_model, empty_inventory
):
    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Worksheets", applicable_versions=("1",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    if empty_inventory:
        import re
        from zipfile import ZipFile

        with ZipFile(three_sheets) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        parts["xl/workbook.xml"] = re.sub(
            b"<sheets>.*?</sheets>", b"<sheets/>", parts["xl/workbook.xml"]
        )
        with ZipFile(three_sheets, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
    else:
        three_sheets.write_bytes(b"invalid workbook")
    failed = import_document(kb_dir, three_sheets, metadata=metadata)
    assert failed.status == "failed"
    read = read_document_source(kb_dir, first.source_id, unit_id=first.units[0].unit_id)
    assert read["status"] == "failed" and "inventory" in read["message"].lower()
    assert read["source_revision_id"] == first.source_revision_id
    assert read["target_source_revision_id"] == failed.source_revision_id
    assert source_inventory(kb_dir)[0]["status"] == "failed"
    assert all(unit["status"] == "completed" for unit in source_inventory(kb_dir)[0]["units"])
    explicit = read_document_source(
        kb_dir,
        first.source_id,
        unit_id=first.units[0].unit_id,
        source_revision_id=failed.source_revision_id,
    )
    assert explicit["status"] == "failed" and "inventory" in explicit["message"].lower()
    old = read_document_source(
        kb_dir,
        first.source_id,
        unit_id=first.units[0].unit_id,
        source_revision_id=first.source_revision_id,
    )
    assert old["status"] == "completed" and old["message"] is None


def test_invalid_xlsx_cell_coordinate_never_publishes(kb_dir, tmp_path, pdf_model):
    from openpyxl import Workbook

    from openkb.application.documents import import_document

    workbook = Workbook()
    workbook.active["XFE1"] = "Outside the XLSX column limit"
    path = tmp_path / "invalid.xlsx"
    workbook.save(path)
    result = import_document(kb_dir, path)
    assert result.status == "failed" and result.units[0].successful_revision_id is None


def test_partial_recompile_has_per_sheet_outcomes_and_is_not_counted_skipped(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import asyncio

    import litellm
    from click.testing import CliRunner

    from openkb.api_recompile import iter_recompile
    from openkb.application.documents import import_document
    from openkb.cli import cli

    imported = import_document(kb_dir, three_sheets)
    original = litellm.completion

    def complete(**kwargs):
        if "BETA_SHEET" in str(kwargs["messages"]):
            raise ValueError("Fixture rejects Beta recompile")
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", complete)

    async def run():
        return [event async for event in iter_recompile(kb_dir, imported.source_id)]

    final = asyncio.run(run())[-1]
    assert final.get("partial") == 1 and final["skipped"] == 0
    assert {item["status"] for item in final["docs"][0]["units"]} == {"completed", "failed"}
    cli_result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "recompile", imported.source_id])
    assert cli_result.exit_code == 0, cli_result.output
    assert "[PARTIAL]" in cli_result.output and "Beta" in cli_result.output
