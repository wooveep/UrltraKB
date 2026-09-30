"""A workbook publishes each worksheet independently at public application boundaries."""

from collections import Counter

import pytest

pytest_plugins = ("test_office_import",)


@pytest.fixture
def three_sheets(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "Alpha"
    book.active["A1"] = "ALPHA_SHEET"
    book.create_sheet("Beta")["A1"] = "BETA_SHEET"
    book.create_sheet("Gamma")["A1"] = "GAMMA_SHEET"
    book["Gamma"].sheet_state = "hidden"
    path = tmp_path / "three.xlsx"
    book.save(path)
    return path


def test_one_sheet_failure_keeps_other_publications_and_retry_skips_them(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source

    original = litellm.completion
    calls = Counter()
    fail = True

    def completion(**kwargs):
        prompt = str(kwargs["messages"])
        for sheet in ("ALPHA_SHEET", "BETA_SHEET", "GAMMA_SHEET"):
            if sheet in prompt:
                calls[sheet] += 1
                if fail and sheet == "BETA_SHEET":
                    raise ValueError("Fixture model rejects only Beta")
        return original(**kwargs)

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    first = import_document(kb_dir, three_sheets)
    assert first.status == "partial", first.message
    units = {unit.name: unit for unit in first.units}
    assert {name: item.status for name, item in units.items()} == {
        "Alpha": "completed",
        "Beta": "failed",
        "Gamma": "completed",
    }
    retained = read_document_source(kb_dir, first.source_id, unit_id=units["Gamma"].unit_id)
    assert retained["sheet"]["state"] == "hidden" and "GAMMA_SHEET" in retained["content"]
    before = calls.copy()
    fail = False
    resumed = import_document(kb_dir, three_sheets)
    assert resumed.status == "added", resumed.message
    assert calls["ALPHA_SHEET"] == before["ALPHA_SHEET"]
    assert calls["GAMMA_SHEET"] == before["GAMMA_SHEET"]
    assert calls["BETA_SHEET"] > before["BETA_SHEET"]
    assert units["Gamma"].knowledge_revision_id == next(
        item.knowledge_revision_id for item in resumed.units if item.name == "Gamma"
    )
    sources = source_inventory(kb_dir)
    assert len(sources) == 1 and len(sources[0]["units"]) == 3


def test_segmented_sheet_keeps_cell_locations_and_recompiles_offline(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import asyncio
    import json
    from types import SimpleNamespace

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    original = litellm.completion
    structures = []

    def completion(**kwargs):
        prompt = str(kwargs["messages"])
        if "CONTENT BLOCK STRUCTURE" not in prompt:
            return original(**kwargs)
        structures.append(prompt)
        marker, start = next(
            (marker, start)
            for marker, start in (("ALPHA_SHEET", 40), ("BETA_SHEET", 39), ("GAMMA_SHEET", 39))
            if marker in prompt
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            [
                                {
                                    "structure": "1",
                                    "title": "Worksheet",
                                    "title_origin": "generated",
                                    "physical_index": 1,
                                    "anchor": {
                                        "unit": 1,
                                        "part": "body",
                                        "range": [start, start + len(marker)],
                                        "excerpt": marker,
                                    },
                                }
                            ]
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    imported = import_document(kb_dir, three_sheets)
    assert imported.status == "added", imported.message
    gamma = next(unit for unit in imported.units if unit.name == "Gamma")
    saved = read_document_source(kb_dir, imported.source_id, unit_id=gamma.unit_id, blocks="1")
    assert saved["execution_mode"] == "segmented" and saved["length_class"] == "short"
    assert saved["sheet"]["name"] == "Gamma"
    assert any(
        item.get("sheet_cell", {}).get("cell", {}).get("coordinate") == "A1"
        for item in saved["origin_locators"]
    )
    before = len(structures)
    three_sheets.unlink()
    selected = select_recompilation(
        kb_dir, imported.source_id, confirmation=True, unit_id=gamma.unit_id
    )
    result = asyncio.run(
        recompile_document(
            kb_dir, imported.source_id, unit_id=gamma.unit_id, version=selected.version
        )
    )
    assert result.status == "compiled", result.message
    assert len(structures) == before
    again = read_document_source(kb_dir, imported.source_id, unit_id=gamma.unit_id, blocks="1")
    assert again["content"] == saved["content"]


def test_sparse_sheet_preserves_types_formulas_hidden_cells_and_disjoint_ranges(
    kb_dir, tmp_path, pdf_model
):
    from datetime import date
    from zipfile import ZIP_DEFLATED, ZipFile

    from openpyxl import Workbook
    from openpyxl.styles import Font

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "sparse.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Typed values"
    sheet["A1"] = "0012"
    sheet["B2"] = 42
    sheet["B2"].number_format = "00000"
    sheet["D2"] = date(2025, 1, 2)
    sheet["E2"] = "=B2+1"
    sheet["F2"] = "=B2+2"
    sheet["G2"] = True
    sheet["XFD100000"] = "TAIL_VALUE"
    sheet["XFD1048576"].font = Font(bold=True)
    sheet.row_dimensions[2].hidden = True
    sheet.column_dimensions["B"].hidden = True
    sheet.merge_cells("H3:J4")
    book.save(path)
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b'<c r="E2"><f>B2+1</f><v></v></c>', b'<c r="E2"><f>B2+1</f><v>43</v></c>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    saved = read_document_source(kb_dir, imported.source_id, unit_id=imported.units[0].unit_id)
    cells = {
        item["sheet_cell"]["cell"]["coordinate"]: item["sheet_cell"]["cell"]
        for item in saved["origin_locators"]
        if item.get("sheet_cell")
    }
    assert len(cells) == 7 and saved["characters"] < 2000
    assert cells["A1"]["value"] == "0012" and cells["B2"]["display"] == "00042"
    assert cells["D2"]["value"] == "2025-01-02T00:00:00" and cells["D2"]["data_type"] == "d"
    assert cells["E2"]["formula"] == "=B2+1" and cells["E2"]["cached"] == 43
    assert cells["F2"]["cache_status"] == "missing" and cells["F2"]["cached"] is None
    assert cells["G2"]["value"] is True
    assert cells["B2"]["hidden_row"] and cells["B2"]["hidden_column"]
    assert saved["sheet"]["merged_ranges"] == ["H3:J4"]
    selected = read_document_source(
        kb_dir, imported.source_id, unit_id=imported.units[0].unit_id, cells="A1:B2,XFD100000"
    )
    assert "0012" in selected["content"] and "TAIL_VALUE" in selected["content"]
    assert "=B2+1" not in selected["content"]
    assert len(selected["source_spans"]) == 3


def test_workbook_upload_cli_and_single_sheet_recompilation(kb_dir, three_sheets, pdf_model):
    import asyncio
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.application.sources import source_inventory
    from openkb.cli import cli
    from openkb.config import register_kb_alias

    register_kb_alias("sheets", kb_dir)
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/v1/add",
            data={"kb": "sheets", "stream": "false"},
            files={"files": ("three.xlsx", three_sheets.read_bytes())},
        ).json()["files"][0]
        assert result["status"] == "added", result
        gamma = next(item for item in result["units"] if item["name"] == "Gamma")
        response = client.post(
            "/api/v1/document/source",
            json={
                "kb": "sheets",
                "hash": result["source_id"],
                "unit_id": gamma["unit_id"],
                "cells": "A1",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["sheet"]["name"] == "Gamma"
    output = CliRunner().invoke(
        cli,
        [
            "--kb-dir",
            str(kb_dir),
            "source",
            result["source_id"],
            "--unit",
            gamma["unit_id"],
            "--cells",
            "A1",
        ],
    )
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["content"] == response.json()["content"]
    before = {
        unit["unit_id"]: unit["knowledge_revision_id"]
        for unit in source_inventory(kb_dir)[0]["units"]
    }
    selected = select_recompilation(kb_dir, result["source_id"], confirmation=True)
    again = asyncio.run(
        recompile_document(
            kb_dir, result["source_id"], unit_id=gamma["unit_id"], version=selected.version
        )
    )
    assert again.status == "compiled", again.message
    after = {
        unit["unit_id"]: unit["knowledge_revision_id"]
        for unit in source_inventory(kb_dir)[0]["units"]
    }
    assert after.pop(gamma["unit_id"]) != before.pop(gamma["unit_id"])
    assert after == before


def test_retry_one_failed_sheet_uses_retained_input_after_original_is_removed(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.workbook_actions import retry_worksheet

    original = litellm.completion
    failed = {"BETA_SHEET", "GAMMA_SHEET"}
    calls = Counter()

    def completion(**kwargs):
        prompt = str(kwargs["messages"])
        for name in ("ALPHA_SHEET", "BETA_SHEET", "GAMMA_SHEET"):
            if name in prompt:
                calls[name] += 1
                if name in failed:
                    raise ValueError("Fixture refuses this sheet")
        return original(**kwargs)

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    first = import_document(kb_dir, three_sheets)
    assert first.status == "partial", first.message
    beta = next(unit for unit in first.units if unit.name == "Beta")
    three_sheets.unlink()
    before = calls.copy()
    failed.remove("BETA_SHEET")
    result = retry_worksheet(kb_dir, first.source_id, beta.unit_id)
    assert result.status == "added" and len(result.units) == 1
    assert result.units[0].target_revision_id == beta.target_revision_id
    assert calls["BETA_SHEET"] > before["BETA_SHEET"]
    assert calls["ALPHA_SHEET"] == before["ALPHA_SHEET"]
    assert calls["GAMMA_SHEET"] == before["GAMMA_SHEET"]


def test_historical_workbook_lists_only_its_published_sheet_names(kb_dir, three_sheets, pdf_model):
    from openpyxl import load_workbook

    from openkb.application.documents import import_document
    from openkb.application.query_views import resolve_query_views
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Worksheets", applicable_versions=("1.0",))
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    pinned = next(
        view.scope
        for view in resolve_query_views(kb_dir, "Read all worksheets").views
        if view.view_id == first.units[0].view_id
    )
    book = load_workbook(three_sheets)
    book["Alpha"].title = "Renamed"
    book.create_sheet("Delta")["A1"] = "NEW_SHEET"
    book.save(three_sheets)
    updated = import_document(kb_dir, three_sheets, metadata=metadata)
    assert updated.status == "added", updated.message
    historical = source_inventory(kb_dir, scope=pinned)[0]
    assert {unit["name"] for unit in historical["units"]} == {"Alpha", "Beta", "Gamma"}
    alpha = next(unit for unit in first.units if unit.name == "Alpha")
    source = read_document_source(kb_dir, first.source_id, unit_id=alpha.unit_id, scope=pinned)
    assert {unit["name"] for unit in source["available_units"]} == {"Alpha", "Beta", "Gamma"}
    assert source["sheet"]["name"] == "Alpha"


def test_workbook_version_clarification_resumes_all_retained_sheets(
    kb_dir, three_sheets, pdf_model
):
    import shutil

    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Worksheets")
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    second_path = three_sheets.with_name("another.xlsx")
    shutil.copyfile(three_sheets, second_path)
    second = import_document(kb_dir, second_path, metadata=metadata)
    assert first.status == "added" and second.status == "blocked"
    assert len(second.units) == 3
    pending = list_version_reviews(kb_dir)
    assert len(pending) == 1
    second_path.unlink()
    supplement_version_reviews(
        kb_dir, {pending[0].review_id: SourceMetadata(applicable_versions=("2.0",))}
    )
    resumed = resume_version_review(kb_dir, pending[0].review_id)
    assert resumed.status == "added", resumed.message
    assert {unit.name for unit in resumed.units if unit.status == "completed"} == {
        "Alpha",
        "Beta",
        "Gamma",
    }
    assert list_version_reviews(kb_dir) == ()
