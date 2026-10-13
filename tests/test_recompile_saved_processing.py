"""Ordinary recompilation consumes saved processing, independent of today's adapters."""

import asyncio
import hashlib
import shutil
import zipfile

import pytest
import test_presentation_import as slide_fixtures
from openpyxl import Workbook

from openkb.application.documents import import_document
from openkb.application.recompilation import recompile_document, select_recompilation
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.documents import read_document_source

pytest_plugins = ("block_fixtures", "test_pdf_readback", "cnki_fixtures")
frozen_slide_conversion = slide_fixtures.frozen_slide_conversion
single_slide_model = slide_fixtures.single_slide_model


def test_recompile_uses_saved_worksheet_without_reading_original_again(
    kb_dir, tmp_path, block_model, monkeypatch
):
    path = tmp_path / "manual.xlsx"
    book = Workbook()
    book.active["A1"] = "hello"
    book.active["B1"] = "Retained worksheet"
    book.save(path)
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    requests = len(block_model)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Original workbook must not be read during recompilation")

    monkeypatch.setattr("openkb.workbooks.xlsx.read_xlsx", unavailable)
    path.unlink()
    selection = select_recompilation(kb_dir, imported.source_id, confirmation=True)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id, version=selection.version))
    assert result.status == "compiled", result.message
    assert not calls
    assert len(block_model) > requests
    after = read_document_source(kb_dir, imported.source_id)
    assert after["content"] == before["content"]
    assert after["processing"] == before["processing"]


@pytest.mark.parametrize("limit", [10, 0])
def test_recompile_reuses_slide_processing_with_no_office_or_new_tree(
    kb_dir, tmp_path, frozen_slide_conversion, single_slide_model, monkeypatch, limit
):
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": limit})
    )
    path = frozen_slide_conversion(tmp_path / "manual.pptx", "Original rule.")
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    normalized = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (kb_dir / ".openkb/normalized").rglob("*")
        if p.is_file()
    }
    tree_calls = sum("PHYSICAL SLIDE STRUCTURE" in str(r) for r in single_slide_model)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Recompilation must not use today's Office runtime")

    monkeypatch.setattr("openkb.office.convert.convert_office", forbidden)
    monkeypatch.setattr("openkb.office.runtime.processing_identity", forbidden)
    path.unlink()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 1 - int(bool(limit))}),
    )
    selection = select_recompilation(kb_dir, imported.source_id, confirmation=True)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id, version=selection.version))
    assert result.status == "compiled", result.message
    assert not calls
    assert sum("PHYSICAL SLIDE STRUCTURE" in str(r) for r in single_slide_model) == tree_calls
    after = read_document_source(kb_dir, imported.source_id)
    for field in ("content", "processing", "office", "source_revision_id"):
        assert after[field] == before[field]
    assert after["knowledge_revision_id"] != before["knowledge_revision_id"]
    assert normalized == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (kb_dir / ".openkb/normalized").rglob("*")
        if p.is_file()
    }


def test_recompile_uses_retained_cnki_without_preparing_original_again(
    kb_dir, cnki_source, pdf_model, monkeypatch
):
    imported = import_document(kb_dir, cnki_source)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Recompilation must not prepare CNKI again")

    monkeypatch.setattr("openkb.cnki.convert.prepare_cnki", forbidden)
    monkeypatch.setattr("openkb.cnki.runtime.processing_identity", forbidden)
    cnki_source.unlink()
    selection = select_recompilation(kb_dir, imported.source_id, confirmation=True)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id, version=selection.version))
    assert result.status == "compiled", result.message
    assert not calls
    after = read_document_source(kb_dir, imported.source_id)
    for field in ("content", "processing", "cnki", "source_revision_id"):
        assert after[field] == before[field]


def test_missing_saved_index_does_not_rebuild_from_the_original(
    kb_dir, tmp_path, frozen_slide_conversion, single_slide_model, monkeypatch
):
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    path = frozen_slide_conversion(tmp_path / "manual.pptx", "Original rule.")
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    unit = imported.units[0]
    snapshot = (
        kb_dir / ".openkb/knowledge" / unit.view_id / "revisions" / unit.knowledge_revision_id
    )
    summary = (kb_dir / f"wiki/summaries/{unit.doc_name}.md").read_bytes()
    calls = len(single_slide_model)
    shutil.rmtree(snapshot / "index")

    def forbidden(*args, **kwargs):
        pytest.fail("A missing saved index cannot authorize original processing")

    monkeypatch.setattr("openkb.office.convert.convert_office", forbidden)
    monkeypatch.setattr("openkb.office.runtime.processing_identity", forbidden)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id))
    assert result.status == "failed", result.message
    assert len(single_slide_model) == calls
    assert (kb_dir / f"wiki/summaries/{unit.doc_name}.md").read_bytes() == summary


def test_docx_recompile_uses_saved_writer_pdf_with_no_office(
    kb_dir, tmp_path, frozen_slide_conversion, block_model, monkeypatch
):
    from openkb.locks import atomic_write_text
    from openkb.office.convert import convert_office
    from openkb.office.records import OfficeConversion
    from openkb.state import HashRegistry

    # A fixed external Office result, with real retained provenance/digest checks.
    template = frozen_slide_conversion(tmp_path / "output-template.pptx", "hello Writer rule.")

    def convert(kb_dir, source, output, **kwargs):
        receipt = convert_office(kb_dir, template, output, **kwargs)
        record = OfficeConversion.model_validate_json(receipt.read_text()).model_copy(
            update={
                "input_digest": HashRegistry.hash_file(source),
                "filter": "writer_pdf_Export",
                "detected_filter": "MS Word 2007 XML",
                "print_hidden_text": False,
                "slides": [],
            }
        )
        atomic_write_text(receipt, record.model_dump_json())
        return receipt

    monkeypatch.setattr("openkb.office.convert.convert_office", convert)
    path = tmp_path / "manual.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>hello Writer rule.</w:t></w:r></w:p></w:body></w:document>",
        )
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)

    def forbidden(*args, **kwargs):
        pytest.fail("Saved Writer processing must not invoke Office")

    monkeypatch.setattr("openkb.office.convert.convert_office", forbidden)
    monkeypatch.setattr("openkb.office.runtime.processing_identity", forbidden)
    path.unlink()
    result = asyncio.run(recompile_document(kb_dir, imported.source_id))
    assert result.status == "compiled", result.message
    after = read_document_source(kb_dir, imported.source_id)
    for field in ("content", "office", "processing", "source_revision_id"):
        assert after[field] == before[field]
