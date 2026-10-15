"""Model readers receive proved cell identity without rewriting archived text."""

import copy
import json

from ultrakb.agent.evidence_payload import project_evidence, slice_evidence
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.source_contract import NATIVE_PAGE_POLICY
from ultrakb.source_pages import read_page_selection

TABLE = (
    "<table>\n<tr><td>Category</td><td>Feature</td><td>Description</td></tr>\n"
    '<tr><td rowspan="3">Group</td><td rowspan="2">Backup</td><td>First rule</td></tr>\n'
    "<tr><td>Second rule</td></tr>\n"
    "<tr><td>Other feature</td><td></td></tr>\n</table>"
)


def native_page(body, page=1):
    spans, cursor = [], 0
    while (start := body.find("<table>", cursor)) >= 0:
        end = body.index("</table>", start) + len("</table>")
        spans.append({"role": "table", "span": [start, end]})
        cursor = end
    return {
        "page": page,
        "unit_kind": "page",
        "content": body,
        "source_structure_policy": NATIVE_PAGE_POLICY,
        "content_spans": spans,
        "image_reference_spans": [],
        "images": [],
    }


def source(body=TABLE, *, native=True):
    page = native_page(body) if native else {"page": 1, "unit_kind": "page", "content": body}
    return read_page_selection([page])


def test_original_message_supplies_explicit_merged_cell_identity(kb_dir):
    pages = [native_page(TABLE)]
    path = kb_dir / "wiki/sources/manual.json"
    path.write_text(json.dumps(pages))
    original = path.read_bytes()

    message = CompileSourceWindows(kb_dir / "wiki", "manual").current_original_message()
    evidence = json.loads(message["content"].split("\n", 1)[1])
    table = evidence["native_tables"][0]
    cells = {cell["id"]: cell for cell in table["cells"]}

    assert evidence["content"] == source()["content"]
    assert table["rows"][2] == {"row": 3, "cells": ["r2c1", "r2c2", "r3c3"]}
    assert cells["r2c1"]["rows"] == [2, 4] and cells["r2c1"]["value"] == "Group"
    assert cells["r2c2"]["rows"] == [2, 3] and cells["r2c2"]["value"] == "Backup"
    assert cells["r3c3"]["columns"] == [3, 3]
    assert cells["r4c3"]["kind"] == "cell" and cells["r4c3"]["value"] == ""
    assert "not counts of domain items" in evidence["table_interpretation"]
    assert path.read_bytes() == original


def test_partial_read_keeps_native_cell_ids_without_backfilling_unread_values():
    selected = source()
    before = copy.deepcopy(selected)
    start = selected["content"].index("<tr><td>Second rule")
    end = start + len("<tr><td>Second rule</td></tr>")

    evidence = slice_evidence(selected, start, end)
    table = evidence["native_tables"][0]
    cells = {cell["id"]: cell for cell in table["cells"]}

    assert table["rows"] == [{"row": 3, "cells": ["r2c1", "r2c2", "r3c3"]}]
    assert cells["r3c3"]["value"] == "Second rule"
    assert cells["r2c2"]["value_coverage"] == "unread"
    assert "value" not in cells["r2c2"] and "value" not in cells["r2c1"]
    location = cells["r2c2"]["native_location"]["page_spans"][0]
    assert location["page"] == 1
    assert "Backup" in TABLE[location["start"] : location["end"]]
    assert table["coverage"] == "partial"
    assert evidence["content"] == selected["content"][start:end]
    assert selected == before


def test_literal_fenced_table_is_not_promoted_to_source_cell_structure():
    evidence = project_evidence(source("```html\n" + TABLE + "\n```", native=False))
    assert "native_tables" not in evidence


def test_table_metadata_participates_in_payload_admission():
    seen = []

    def fits(payload):
        seen.append(payload)
        return "native_tables" not in payload

    selected = source()
    evidence = project_evidence(selected, offset=selected["content"].index("<table>"), fits=fits)
    assert evidence["coverage"] == "budget_exceeded"
    assert any("native_tables" in payload for payload in seen)


def test_small_window_continuations_preserve_original_text_and_cell_identity():
    selected = source("Before\n\n" + TABLE + "\n\nAfter")
    offset, windows = 0, []
    while offset is not None:
        part = project_evidence(selected, offset=offset, budget=2000, allow_partial=True)
        assert part["content"] and len(json.dumps(part, ensure_ascii=False)) <= 2000
        assert part["continuation"] is None or part["continuation"] > offset
        windows.append(part)
        offset = part["continuation"]
    assert "".join(part["content"] for part in windows) == selected["content"]
    assert len(windows) >= 2
    assert any(
        cell["value_coverage"] == "unread"
        for part in windows
        for table in part.get("native_tables", [])
        for cell in table["cells"]
    )
    for part in windows:
        for table in part.get("native_tables", []):
            for cell in table["cells"]:
                if cell["id"] == "r2c2":
                    assert cell["rows"] == [2, 3] and cell["columns"] == [2, 2]


def test_table_identity_is_scoped_to_original_physical_page():
    pages = [
        native_page(TABLE, 1),
        native_page(TABLE.replace("Backup", "Restore"), 2),
    ]
    evidence = project_evidence(read_page_selection(pages))
    assert len(evidence["native_tables"]) == 2
    for table, page, value in zip(evidence["native_tables"], (1, 2), ("Backup", "Restore")):
        assert table["native_location"]["page_spans"][0]["page"] == page
        cell = next(cell for cell in table["cells"] if cell["id"] == "r2c2")
        assert cell["value"] == value
        assert cell["native_location"]["page_spans"][0]["page"] == page


def test_document_windows_parse_tables_once_including_adjacent_context(kb_dir, monkeypatch):
    from ultrakb.agent import table_evidence

    body = "\n\n".join([TABLE] * 5)
    pages = [native_page(body)]
    expected_content = read_page_selection(pages)["content"]
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps(pages))
    calls = []
    initialize = table_evidence._Parser.__init__

    def count(parser, text, base):
        calls.append(base)
        initialize(parser, text, base)

    monkeypatch.setattr(table_evidence._Parser, "__init__", count)
    windows = list(
        CompileSourceWindows(kb_dir / "wiki", "manual").iter_document_windows(
            fits=lambda payload: len(json.dumps(payload, ensure_ascii=False)) <= 8000
        )
    )
    assert len(windows) >= 2
    assert "".join(window["content"] for window in windows) == expected_content
    assert len(calls) == len(set(calls)) == 5
    assert all(len(json.dumps(window, ensure_ascii=False)) <= 8000 for window in windows)
