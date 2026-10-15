"""TXT/CSV preserve their input decisions through admission and source reading."""

import json

import pytest

pytest_plugins = ("test_pdf_readback", "block_fixtures", "processing_fixtures")


def test_txt_freezes_encoding_and_original_unicode_positions(kb_dir, tmp_path, processing_only):
    from ultrakb.documents import read_document_source

    original = "前言 e\u0301。\r\n# 这仍是文本\r\n![literal](missing.png)\r\n"
    path = tmp_path / "note.txt"
    path.write_bytes(original.encode("utf-16"))
    result = processing_only(path)
    assert result.source_id is not None
    path.unlink()
    saved = read_document_source(
        kb_dir, result.source_id, source_revision_id=result.source_revision_id
    )
    assert saved["content"] == original and not saved["assets"]
    assert saved["encoding"]["name"] == "utf-16-le" and saved["encoding"]["basis"] == "bom"
    selected = read_document_source(kb_dir, result.source_id, chars="3:5")
    assert selected["content"] == "e\u0301"
    assert selected["origin_locators"][0]["original_span"] == [4, 6]


def test_csv_retains_quoted_multiline_empty_and_literal_cells(kb_dir, tmp_path, processing_only):
    from ultrakb.api_models import DocumentSourceResponse
    from ultrakb.documents import read_document_source

    original = 'code,note,empty\r\n001,"a,b\r\n""quoted""",\r\n=1+1,2026-09-30,""\r\n'
    path = tmp_path / "data.csv"
    path.write_bytes(original.encode())
    result = processing_only(path)
    saved = read_document_source(kb_dir, result.source_id)
    cells = [item for item in saved["origin_locators"] if item.get("csv")]
    assert [item["csv"]["value"] for item in cells] == [
        "code",
        "note",
        "empty",
        "001",
        'a,b\r\n"quoted"',
        "",
        "=1+1",
        "2026-09-30",
        "",
    ]
    quoted = cells[4]
    assert quoted["csv"]["row"] == 2 and quoted["csv"]["column"] == 2
    assert quoted["csv"]["physical_lines"] == [2, 3]
    a, b = quoted["original_span"]
    assert original[a:b] == '"a,b\r\n""quoted"""'
    assert "001" in saved["content"] and 'a,b<br>"quoted"' in saved["content"]
    assert DocumentSourceResponse(**saved).encoding["name"] == "utf-8"


@pytest.mark.parametrize(
    "extension,body,expected",
    [
        ("txt", "hello\r\n世界", "hello\r\n世界"),
        ("csv", "hello,世界\r\n", "| Column 1 | Column 2 |\n| --- | --- |\n| hello | 世界 |\n"),
    ],
)
def test_text_formats_reuse_block_index_and_exact_revision_reads(
    kb_dir, tmp_path, block_model, monkeypatch, extension, body, expected, wide_short_prompt
):
    from click.testing import CliRunner

    from ultrakb.application.documents import import_document
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.cli import cli
    from ultrakb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 4096}}
        ),
    )
    path = tmp_path / f"sample.{extension}"
    path.write_bytes(body.encode())
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    path.unlink()
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    assert saved["content"] == expected and saved["execution_mode"] == "segmented"
    output = CliRunner().invoke(
        cli,
        [
            "--kb-dir",
            str(kb_dir),
            "source",
            result.source_id,
            "--source-revision",
            result.source_revision_id,
            "--blocks",
            "1",
        ],
    )
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["content"] == expected


@pytest.mark.parametrize(
    "extension,raw,error",
    [
        ("csv", b'head,value\n"unclosed,1\n', "CSV parse error"),
        ("txt", b"\xff\xfeA", "truncated data"),
        ("txt", b"binary\x00content", "NUL"),
    ],
)
def test_parse_failures_keep_the_source_without_publishing_empty_knowledge(
    kb_dir, tmp_path, pdf_model, extension, raw, error
):
    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source

    path = tmp_path / f"broken.{extension}"
    path.write_bytes(raw)
    result = import_document(kb_dir, path)
    assert result.status == "failed" and error in result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["knowledge_revision_id"] is None
    assert (kb_dir / saved["original_path"]).read_bytes() == raw


def test_watched_csv_does_not_reimport_its_managed_normalization(kb_dir, pdf_model):
    from test_native_watch import TaskSink, eventually

    from ultrakb.application.documents import import_document
    from ultrakb.runtime.watch import NativeWatch

    path = kb_dir / "raw/input.csv"
    path.write_bytes(b"code,value\n001,example\n")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.03, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        result = import_document(kb_dir, path)
        assert result.status == "added", result.message
        sink.finish("0", revision=result.input_version)
        scans = watch.view().scans
        eventually(lambda: watch.view().scans > scans + 3)
        assert len(sink.items) == 1 and sink.items["0"][0].source == str(path)
    finally:
        watch.stop()
        assert watch.join(5)


def test_csv_generated_column_labels_cannot_anchor_original_sections():
    from pageindex.index.block_policy import BlockContractError, BlockPolicy

    from ultrakb.csv_source import normalize_csv

    text, origins = normalize_csv("hello,world\n", 0)
    policy = BlockPolicy(
        {
            "unit_count": 1,
            "source": {
                "text": text,
                "blocks": [{"ordinal": 1, "source_spans": [[0, len(text)]]}],
                "origins": [origin.model_dump(mode="json") for origin in origins],
            },
        }
    )
    entry = {
        "structure": "1",
        "title": "Data",
        "title_origin": "generated",
        "physical_index": 1,
        "anchor": {"unit": 1, "part": "body", "range": [2, 10], "excerpt": "Column 1"},
    }
    assert policy.validate_entry(entry) == "generated_evidence"
    with pytest.raises(BlockContractError, match="Unverified content-block anchors"):
        policy.validate_order([entry])
    start = text.index("hello")
    entry["anchor"] = {"unit": 1, "part": "body", "range": [start, start + 5], "excerpt": "hello"}
    assert policy.valid(entry)


def test_csv_parse_failure_retains_its_successful_encoding_decision(kb_dir, tmp_path, pdf_model):
    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source

    path = tmp_path / "broken.csv"
    path.write_bytes('head,value\r\n"missing quote,123'.encode("utf-16"))
    result = import_document(kb_dir, path)
    assert result.status == "failed", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["encoding"]["name"] == "utf-16-le"
    assert any("CSV parse error" in item for item in saved["diagnostics"])


def test_large_csv_cell_keeps_exact_value_without_repeating_it_in_range_reads(
    kb_dir, tmp_path, processing_only
):
    import csv
    from pathlib import Path

    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 2400}}
        ),
    )
    value = (Path(__file__).parent / "fixtures/content-blocks.md").read_text("utf-8")
    path = tmp_path / "handbook.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        csv.writer(output).writerow([value])
    result = processing_only(path)
    saved = read_document_source(kb_dir, result.source_id)
    cells = [item["csv"] for item in saved["origin_locators"] if item.get("csv")]
    assert [cell["value"] for cell in cells] == [value]
    selected = read_document_source(kb_dir, result.source_id, blocks="2")
    assert any(item["kind"] == "table_header" for item in selected["units"][0]["display_context"])
    cell = next(item["csv"] for item in selected["origin_locators"] if item.get("csv"))
    assert "value" not in cell and cell["value_complete"] is False
