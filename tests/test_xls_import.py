"""Independent binary XLS fixture exercises the common worksheet contract."""

from pathlib import Path

import pytest

pytest_plugins = ("test_office_import", "block_fixtures", "workbook_fixtures")


def retained_worksheet(path, sheet, directory):
    """Use the real sparse reader, frozen conversion and source-selection pipeline."""
    import json

    from ultrakb.inputs import prepared_input
    from ultrakb.text_source import read_text_selection
    from ultrakb.workbooks.normalization import convert_sheet

    with prepared_input(path) as prepared:
        converted = convert_sheet(prepared, sheet, "worksheet", directory)
    saved = json.loads(converted.source_path.with_suffix(".content.json").read_text())
    return read_text_selection(saved)


@pytest.mark.parametrize("suffix", ["xls", "xlsx"])
def test_numeric_zero_mask_keeps_sign_outside_digit_places(tmp_path, suffix):
    from openpyxl import Workbook

    from ultrakb.workbooks.selection import select_cells
    from ultrakb.workbooks.xls import read_xls
    from ultrakb.workbooks.xlsx import read_xlsx

    if suffix == "xls":
        path = Path(__file__).parent / "fixtures/office/negative-sheets.xls"
    else:
        book = Workbook()
        book.active.title = "Alpha"
        book.active["B2"] = -42
        book.active["B2"].number_format = "00000"
        path = tmp_path / "negative.xlsx"
        book.save(path)
    sheets = (read_xls if suffix == "xls" else read_xlsx)(path)
    alpha = next(sheet for sheet in sheets if sheet.name == "Alpha")
    body = select_cells(retained_worksheet(path, alpha, tmp_path / "retained"), "B2")
    assert body["content"] == 'B2: "-00042"'


def test_binary_xls_retains_typed_cached_cells_with_honest_formula_capabilities(tmp_path):
    from ultrakb.workbooks.selection import select_cells
    from ultrakb.workbooks.xls import read_xls

    path = Path(__file__).parent / "fixtures/office/typed-sheets.xls"
    assert path.read_bytes()[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    sheets = read_xls(path)
    assert len(sheets) == 3 and all(sheet.error is None for sheet in sheets)
    alpha = next(sheet for sheet in sheets if sheet.name == "Alpha")
    body = retained_worksheet(path, alpha, tmp_path / "alpha")
    cells = {
        item["sheet_cell"]["cell"]["coordinate"]: item["sheet_cell"]["cell"]
        for item in body["origin_locators"]
        if item.get("sheet_cell")
    }
    assert cells["A1"]["value"] == "0012" and cells["B2"]["display"] == "00042"
    assert cells["D2"]["value"] == "2025-01-02T00:00:00"
    assert cells["B2"]["hidden_row"] and cells["B2"]["hidden_column"]
    assert cells["E2"]["formula_status"] == "unavailable" and cells["E2"]["formula"] is None
    assert cells["E2"]["cache_status"] == "available" and cells["E2"]["cached"] == 43
    assert cells["F2"]["cached"] == "cached text" and cells["G2"]["cached"] is True
    assert "XLS_TAIL_VALUE" in body["content"] and len(body["content"]) < 2000
    selected = select_cells(body, "A1,IV65530")
    assert "0012" in selected["content"] and "XLS_TAIL_VALUE" in selected["content"]
    gamma = next(sheet for sheet in sheets if sheet.name == "Gamma")
    assert retained_worksheet(path, gamma, tmp_path / "gamma")["sheet"]["state"] == "hidden"


def test_binary_xls_api_partial_retry_preserves_other_sheets(kb_dir, pdf_model, monkeypatch):
    from collections import Counter
    from contextlib import contextmanager

    import litellm
    from fastapi.testclient import TestClient
    from office_publication_fixtures import publish_workbook
    from test_workbook_import import worksheet_prompt_markers

    from ultrakb.api import create_app
    from ultrakb.api_helpers import published_input
    from ultrakb.application.sources import source_inventory
    from ultrakb.config import register_kb_alias, resolve_credential_bundle

    register_kb_alias("xls", kb_dir)
    original = litellm.completion
    calls = Counter()
    failing = True
    prepared = []

    @contextmanager
    def arrange_published_sheets(*args, **kwargs):
        # The actual uploaded path already owns this source identity. Arrange
        # completed Alpha/Gamma domain publications before normal ingestion;
        # the API still receives the genuine owned input and produces its own
        # partial result from the real Beta transport failure.
        with published_input(*args, **kwargs) as owned:
            result = publish_workbook(
                kb_dir,
                owned.path,
                bundle=resolve_credential_bundle(kb_dir),
                sheet_names=("Alpha", "Gamma"),
                record_model_task=False,
            )
            assert result.status == "added" and len(result.units) == 2
            assert all(unit.status == "completed" for unit in result.units)
            prepared.append(result)
            yield owned

    def complete(**kwargs):
        for marker in worksheet_prompt_markers(
            kwargs["messages"], ("0012", "XLS_BETA", "XLS_GAMMA")
        ):
            calls[marker] += 1
            if failing and marker == "XLS_BETA":
                raise ValueError("Local transport rejects Beta")
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr("ultrakb.api_helpers.published_input", arrange_published_sheets)
    path = Path(__file__).parent / "fixtures/office/typed-sheets.xls"
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "xls", "stream": "false"},
            files={"files": (path.name, path.read_bytes())},
        )
        assert response.status_code == 200, response.text
        imported = response.json()["files"][0]
        assert imported["status"] == "partial", imported
        assert imported["source_id"] == prepared[0].source_id
        assert imported["source_revision_id"] == prepared[0].source_revision_id
        previous = {unit.name: unit for unit in prepared[0].units}
        for unit in imported["units"]:
            if unit["name"] in previous:
                assert unit["unit_id"] == previous[unit["name"]].unit_id
                assert unit["knowledge_revision_id"] == previous[unit["name"]].knowledge_revision_id
                assert unit["model_usage"]["current"]["requests"] == 0
        beta = next(unit for unit in imported["units"] if unit["name"] == "Beta")
        before = calls.copy()
        failing = False
        response = client.post(
            "/api/v1/document/retry-worksheet",
            json={"kb": "xls", "source_id": imported["source_id"], "unit_id": beta["unit_id"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "added"
        assert calls["XLS_BETA"] > before["XLS_BETA"]
        assert calls["0012"] == before["0012"] and calls["XLS_GAMMA"] == before["XLS_GAMMA"]
        for unit in source_inventory(kb_dir)[0]["units"]:
            if unit["name"] in previous:
                assert unit["unit_id"] == previous[unit["name"]].unit_id
                assert unit["knowledge_revision_id"] == previous[unit["name"]].knowledge_revision_id
