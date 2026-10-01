"""An anomalous extraction cannot advance sources or spend model tokens."""

import pytest

from openkb.application.documents import import_document
from openkb.source_catalog import list_sources

pytest_plugins = ("block_fixtures",)


def test_repeated_cjk_extraction_is_rejected_before_admission(kb_dir, tmp_path, block_model):
    path = tmp_path / "unreadable.md"
    original = "参" * 32 + "7项信创云计算\n" + "参" * 40 + "标准\n" + "参" * 35 + "技术资料"
    path.write_text(original, encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "rejected"
    assert "repeated_cjk" in result.message
    assert result.source_id is None and not list_sources(kb_dir)
    assert block_model == []
    assert path.read_text("utf-8") == original
    assert result.model_usage["current"]["requests"] == 0


def test_normal_repetition_remains_readable(kb_dir, tmp_path, block_model):
    path = tmp_path / "normal.md"
    path.write_text("hello\n" + "哈哈，服务正常。\n" * 50 + "0" * 100 + "-" * 100, encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    assert result.source_id is not None


def test_pdf_is_rejected_before_segmented_indexing(kb_dir, tmp_path, block_model):
    import pymupdf

    path = tmp_path / "unreadable.pdf"
    with pymupdf.open() as pdf:
        for _ in range(20):
            page = pdf.new_page()
            page.insert_textbox((40, 40, 550, 400), ("参" * 24 + "技术\n") * 4, fontname="china-s")
        pdf.save(path)
    result = import_document(kb_dir, path)
    assert result.status == "rejected"
    assert "page[1]" in result.message
    assert not list_sources(kb_dir) and not block_model


def test_workbook_cells_are_checked_before_source_admission(kb_dir, tmp_path, block_model):
    from openpyxl import Workbook

    book = Workbook()
    book.active["A1"] = ("参" * 30 + "损坏\n") * 4
    path = tmp_path / "unreadable.xlsx"
    book.save(path)
    result = import_document(kb_dir, path)
    assert result.status == "rejected", result.message
    assert not list_sources(kb_dir) and not block_model
    assert result.model_usage["current"]["requests"] == 0


def test_office_extraction_rejection_identifies_conversion_and_preserves_original(
    kb_dir, tmp_path, block_model, monkeypatch
):
    import pymupdf

    def convert(kb_dir, path, output, **kwargs):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_textbox((40, 40, 550, 400), ("参" * 24 + "技术\n") * 4, fontname="china-s")
            pdf.save(output)
        return output.with_suffix(".office.json")

    monkeypatch.setattr("openkb.office.convert.convert_office", convert)
    monkeypatch.setattr("openkb.office.runtime.processing_identity", lambda _: {"fixture": True})
    monkeypatch.setattr("openkb.office.slide_content.read_slides", lambda _: [])
    path = tmp_path / "conversion.docx"
    original = b"Frozen Office fixture"
    path.write_bytes(original)
    result = import_document(kb_dir, path)
    assert result.status == "rejected", result.message
    assert "Office conversion/extraction result" in result.message
    assert not list_sources(kb_dir) and not block_model
    assert path.read_bytes() == original


def test_catalog_boundary_checks_the_assessment_against_frozen_bytes(kb_dir, tmp_path):
    from openkb.import_text import preflight_import_text
    from openkb.inputs import prepared_input
    from openkb.source_catalog import admit_source_revision

    path = tmp_path / "changed.md"
    path.write_text("hello", encoding="utf-8")
    with prepared_input(path) as prepared:
        assessment = preflight_import_text(kb_dir, prepared)
        prepared.path.write_text(("参" * 30 + "损坏\n") * 4, encoding="utf-8")
        with pytest.raises(ValueError, match="changed after extraction"):
            admit_source_revision(kb_dir, prepared, text_assessment=assessment)
    assert not list_sources(kb_dir)


def test_rejected_replacement_keeps_the_previous_source_target_and_knowledge(
    kb_dir, tmp_path, block_model
):
    from openkb.application.sources import source_inventory
    from openkb.source_catalog import read_source

    path = tmp_path / "replace.md"
    path.write_text("hello original", encoding="utf-8")
    initial = import_document(kb_dir, path)
    before = source_inventory(kb_dir)
    target = read_source(kb_dir, initial.source_id)
    called = len(block_model)
    path.write_text(("参" * 30 + "损坏\n") * 4, encoding="utf-8")
    rejected = import_document(kb_dir, path)
    assert rejected.status == "rejected"
    assert len(block_model) == called
    assert source_inventory(kb_dir) == before
    assert read_source(kb_dir, initial.source_id) == target
    assert len(list_sources(kb_dir)) == 1


def test_legacy_recompile_rejects_anomalous_retained_text_before_admission(kb_dir, block_model):
    import asyncio
    import json

    from openkb.application.recompilation import recompile_document

    original = ("参" * 30 + "损坏\n") * 4
    (kb_dir / "wiki/sources/old.md").write_text(original, encoding="utf-8")
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"old-hash": {"doc_name": "old", "name": "old.md", "type": "md"}})
    )
    result = asyncio.run(recompile_document(kb_dir, "old-hash"))
    assert result.error_type == "ImportTextRejected"
    assert not list_sources(kb_dir) and not block_model
    assert result.model_usage["current"]["requests"] == 0
    assert (kb_dir / "wiki/sources/old.md").read_text("utf-8") == original


def test_runtime_recompile_preserves_the_text_rejection_result(kb_dir, block_model, monkeypatch):
    from openkb.application.execution import ExecutionContext
    from openkb.application.recompilation import RecompileResult
    from openkb.llm_usage import usage_receipt
    from openkb.runtime.records import UnitIdentity
    from openkb.runtime.requests import RecompileDocument
    from openkb.runtime.worker import _execute

    async def rejected(*args, **kwargs):
        return RecompileResult(
            "rejected",
            message="Repeated CJK text",
            error_type="ImportTextRejected",
            quality=("import_text_rejected",),
            unfinished=("text_preflight",),
            model_usage=usage_receipt(kb_dir),
        )

    monkeypatch.setattr("openkb.application.recompilation.recompile_document", rejected)
    result = _execute(
        RecompileDocument("old-hash", "confirmed-version"),
        UnitIdentity("a" * 32, "1", str(kb_dir), "b" * 64),
        ExecutionContext(),
    )
    assert result.status == "failed"
    assert "ImportTextRejected" in result.error
    assert result.quality == ("import_text_rejected",)
    assert result.unfinished == ("text_preflight",)
    assert result.model_usage["current"]["requests"] == 0
    assert not list_sources(kb_dir) and not block_model
