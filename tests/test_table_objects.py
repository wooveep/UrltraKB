"""Native tables are compilation objects; cell positions remain source evidence."""

import json

import pytest

from openkb.application.documents import import_document
from tests.document_fixtures import write_docx
from tests.http_model_fixture import evidence_response


def write_table(path, rows):
    if path.suffix == ".docx":
        write_docx(
            path,
            "<w:tbl>"
            + "".join(
                "<w:tr>"
                + ("<w:trPr><w:tblHeader/></w:trPr>" if index == 0 else "")
                + "".join(f"<w:tc><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:tc>" for v in row)
                + "</w:tr>"
                for index, row in enumerate(rows)
            )
            + "</w:tbl>",
        )
    elif path.suffix == ".pptx":
        from pptx import Presentation
        from pptx.util import Inches

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        table = slide.shapes.add_table(
            len(rows), len(rows[0]), Inches(1), Inches(1), Inches(7), Inches(4)
        ).table
        table.first_row = True
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
        deck.save(path)
    elif path.suffix == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table

        book = Workbook()
        sheet = book.active
        sheet.title = "Limits"
        for row in rows:
            sheet.append(row)
        sheet.add_table(
            Table(displayName="Limits", ref=f"A1:{get_column_letter(len(rows[0]))}{len(rows)}")
        )
        book.save(path)
    else:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        for r in range(len(rows) + 1):
            page.draw_line((50, 50 + 30 * r), (450, 50 + 30 * r))
        for c in range(len(rows[0]) + 1):
            page.draw_line((50 + 200 * c, 50), (50 + 200 * c, 50 + 30 * len(rows)))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                page.insert_text((55 + 200 * c, 70 + 30 * r), value)
        doc.save(path)
        doc.close()


@pytest.mark.parametrize("suffix", [".docx", ".pdf", ".pptx", ".xlsx"])
def test_small_table_is_one_object_without_model_extracting_each_cell(
    kb_dir, tmp_path, model_service, suffix
):
    path = tmp_path / ("limits" + suffix)
    rows = [("Parameter", "Required value"), ("timeout", "37 seconds"), ("retries", "3")]
    write_table(path, rows)
    requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        requests.append(payload)
        output = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, result in zip(payload["units"], output["units"]):
                if unit["kind"] in {"image", "heading"}:
                    result.update(
                        facts=[], empty_reason="Source navigation without additional facts"
                    )
                else:
                    result["facts"][0]["topic"] = unit["text"]
        return output

    model_service.respond = respond
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    assert not [
        unit
        for request in requests
        if request["stage"] == "facts"
        for unit in request["units"]
        if unit["kind"] == "table"
    ]
    plans = [request for request in requests if request["stage"] == "planning"]
    assert sum(len(request["topics"]) for request in plans) == 1
    generated = [request for request in requests if request["stage"] == "generation"]
    assert len(generated) == 1
    assert {item["text"] for item in generated[0]["evidence"]} == {v for row in rows for v in row}
    assert len(generated[0]["table_objects"]) == 1


def test_large_spreadsheet_batches_repeat_headers_and_keep_complete_rows(
    kb_dir, tmp_path, model_service
):
    from collections import Counter

    from openpyxl.utils.cell import coordinate_to_tuple

    path = tmp_path / "large.xlsx"
    rows = [("Name", "Limit", "Unit")] + [(f"item-{i}", str(i), "seconds") for i in range(45)]
    write_table(path, rows)
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        output = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit in output["units"]:
                unit.update(facts=[], empty_reason="Worksheet heading")
        if payload["stage"] == "generation":
            generated.append(payload)
        return output

    model_service.respond = respond
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert len(generated) > 1
    addresses = []
    for batch in generated:
        (obj,) = batch["table_objects"]
        assert {header["text"] for header in obj["headers"]} == set(rows[0])
        cells = [item["location"]["cell_address"] for item in batch["evidence"]]
        positions = [coordinate_to_tuple(cell) for cell in cells]
        assert set(Counter(row for row, col in positions).values()) == {3}
        assert obj["row_offset"] == min(row for row, col in positions) - 1
        assert obj["columns"] == [1, 2, 3]
        addresses.extend(cells)
    assert len(addresses) == len(set(addresses)) == len(rows) * 3


def test_capacity_and_response_retry_do_not_split_a_row_or_merged_cell():
    from openkb.agent.evidence_retry import split_generation, split_generation_response

    def pair(row, column, rowspan=1):
        return (
            {"table_object": {"id": "table", "row": row, "column": column, "rowspan": rowspan}},
            {},
        )

    merged = [pair(1, 1, 2), pair(1, 2), pair(2, 2)]
    for split in (split_generation, split_generation_response):
        assert split(merged) == []
        assert split(merged + [pair(3, 1), pair(3, 2)]) == [merged, [pair(3, 1), pair(3, 2)]]


@pytest.mark.parametrize("changed_prompt", [False, True])
def test_upgrade_reuses_only_unchanged_prose_from_a_mixed_old_table_batch(
    kb_dir, tmp_path, model_service, monkeypatch, changed_prompt
):
    import yaml

    from openkb.agent import table_objects
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.agent.table_recovery import PRE_TABLE_MODULES
    from openkb.application.source_actions import continue_source

    path = tmp_path / "old.docx"
    write_docx(
        path,
        "<w:p><w:r><w:t>Required release is 7.</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Timeout</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>37 seconds</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    current_key = CompilationCheckpoints._key_record
    config_path = kb_dir / ".openkb/config.yaml"
    original_config = config_path.read_text()
    config = yaml.safe_load(original_config)
    config["processing"]["max_requests"] = 1
    config_path.write_text(yaml.safe_dump(config))

    def previous_key(self, system, payload, **kwargs):
        record = current_key(self, system, payload, **kwargs)
        if payload.get("stage") == "facts":
            record.update(
                message_format=PRE_TABLE_MODULES["evidence_units"],
                stage_implementation=PRE_TABLE_MODULES,
            )
        return record

    with monkeypatch.context() as old:
        old.setattr(table_objects, "source_table_objects", lambda parsed, source: {})
        old.setattr(CompilationCheckpoints, "_key_record", previous_key)
        first = import_document(kb_dir, path)
    assert first.reason == "request_budget_exhausted", first
    config_path.write_text(original_config)
    before = len(model_service)
    if changed_prompt:
        from openkb.agent import evidence_facts

        monkeypatch.setattr(
            evidence_facts, "FACTS_SYSTEM", evidence_facts.FACTS_SYSTEM + "\nNew extraction policy."
        )
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert second.parse_id == first.parse_id
    calls = [json.loads(call["messages"][-1]["content"]) for call in model_service[before:]]
    facts = [call for call in calls if call["stage"] == "facts"]
    assert bool(facts) == changed_prompt
    assert all(unit["kind"] != "table" for call in facts for unit in call["units"])


def test_unmarked_worksheet_regions_and_embedded_tables_keep_distinct_identity():
    from types import SimpleNamespace

    from openkb.agent.table_objects import source_table_objects
    from openkb.evidence import BlockDraft

    blocks = []
    for number, address in enumerate(["B3", "C3", "B4", "C4", "B9", "C9", "B10", "C10"]):
        draft = BlockDraft(
            "value",
            "table",
            {"kind": "xlsx", "sheet": "Sheet", "sheet_index": 1, "cell_address": address},
        )
        blocks.append(SimpleNamespace(id=str(number), **draft.__dict__))
    objects = source_table_objects(SimpleNamespace(blocks=blocks), SimpleNamespace(id="source"))
    assert {obj["position"]["range"] for obj in objects.values()} == {"B3:C4", "B9:C10"}
    assert len({obj["id"] for obj in objects.values()}) == 2

    for number in (1, 2):
        draft = BlockDraft(
            "same",
            "table",
            {
                "kind": "docx",
                "paragraph": 1,
                "attachment": {
                    "part": f"ole{number}.bin",
                    "name": "same.docx",
                    "blob": "a" * 64,
                    "position": {"kind": "docx", "table": 1, "row": 1, "cell": 1},
                },
            },
        )
        blocks.append(SimpleNamespace(id=f"embedded{number}", **draft.__dict__))
    objects = source_table_objects(SimpleNamespace(blocks=blocks), SimpleNamespace(id="source"))
    assert objects["embedded1"]["id"] != objects["embedded2"]["id"]
