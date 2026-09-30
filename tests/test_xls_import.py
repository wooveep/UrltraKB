"""Independent binary XLS fixture exercises the common worksheet contract."""

from pathlib import Path

import pytest

pytest_plugins = ("test_workbook_import",)


@pytest.mark.parametrize("suffix", ["xls", "xlsx"])
def test_numeric_zero_mask_keeps_sign_outside_digit_places(kb_dir, tmp_path, pdf_model, suffix):
    from openpyxl import Workbook

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    if suffix == "xls":
        path = Path(__file__).parent / "fixtures/office/negative-sheets.xls"
    else:
        book = Workbook()
        book.active.title = "Alpha"
        book.active["B2"] = -42
        book.active["B2"].number_format = "00000"
        path = tmp_path / "negative.xlsx"
        book.save(path)
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    alpha = next(unit for unit in result.units if unit.name == "Alpha")
    body = read_document_source(kb_dir, result.source_id, unit_id=alpha.unit_id, cells="B2")
    assert body["content"] == 'B2: "-00042"'


def test_binary_xls_publishes_typed_cached_cells_with_honest_formula_capabilities(
    kb_dir, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = Path(__file__).parent / "fixtures/office/typed-sheets.xls"
    assert path.read_bytes()[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    result = import_document(kb_dir, path)
    assert result.status == "added" and len(result.units) == 3, result
    alpha = next(unit for unit in result.units if unit.name == "Alpha")
    body = read_document_source(kb_dir, result.source_id, unit_id=alpha.unit_id)
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
    selected = read_document_source(
        kb_dir, result.source_id, unit_id=alpha.unit_id, cells="A1,IV65530"
    )
    assert "0012" in selected["content"] and "XLS_TAIL_VALUE" in selected["content"]
    gamma = next(unit for unit in result.units if unit.name == "Gamma")
    assert (
        read_document_source(kb_dir, result.source_id, unit_id=gamma.unit_id)["sheet"]["state"]
        == "hidden"
    )


def test_binary_xls_api_partial_retry_preserves_other_sheets(kb_dir, pdf_model, monkeypatch):
    from collections import Counter

    import litellm
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.config import register_kb_alias

    register_kb_alias("xls", kb_dir)
    original = litellm.completion
    calls = Counter()
    failing = True

    def complete(**kwargs):
        prompt = str(kwargs["messages"])
        for marker in ("0012", "XLS_BETA", "XLS_GAMMA"):
            if marker in prompt:
                calls[marker] += 1
                if failing and marker == "XLS_BETA":
                    raise ValueError("Fixture model refuses Beta")
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", complete)
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
