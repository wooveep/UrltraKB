"""Actual package parts produce independent ordinary Sources across OOXML hosts."""

import io
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

pytest_plugins = ("pending_fixtures", "processing_fixtures")


@pytest.mark.parametrize("host, prefix", [("docx", "word"), ("pptx", "ppt"), ("xlsx", "xl")])
def test_chart_workbook_in_any_host_uses_actual_payload_type_and_frozen_bytes(
    kb_dir,
    tmp_path,
    writer_document,
    pdf_model,
    host,
    prefix,
    processing_only,
    processing_context,
    live_llm,
):
    from processing_fixtures import finish_recovered_sources

    from ultrakb.application.pending import pending_status
    from ultrakb.application.removal import remove_document
    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

    path = tmp_path / ("host." + host)
    if host == "docx":
        shutil.copyfile(writer_document, path)
    elif host == "pptx":
        shutil.copyfile(Path(__file__).parent / "fixtures/office/slides.pptx", path)
    else:
        outer = Workbook()
        outer.active["A1"] = "Outer body"
        outer.save(path)
    nested = Workbook()
    nested.active.title = "Chart data"
    nested.active["B2"] = "Recovered independent marker"
    payload = io.BytesIO()
    nested.save(payload)
    with ZipFile(path, "a") as package:
        package.writestr(prefix + "/embeddings/chart-data.bin", payload.getvalue())
    calls_before = len(live_llm.calls)
    parent = processing_only(path)
    assert pending_status(kb_dir)["discovery_pending"] == 1
    remove_document(kb_dir, parent.source_id)
    path.unlink()
    finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    recovered = next(s for s in list_sources(kb_dir) if s.source_id != parent.source_id)
    source = read_document_source(kb_dir, recovered.source_id)
    assert recovered.name == "chart-data.xlsx"
    assert source["status"] == "completed"
    assert "Recovered independent marker" in source["content"]
    assert (kb_dir / source["original_path"]).read_bytes() == payload.getvalue()
    assert pending_status(kb_dir)["runnable"] == 0
    assert len(live_llm.calls) == calls_before


def test_distinct_parts_keep_identity_and_diagnostics_do_not_hide_complete_files(
    kb_dir, writer_document, physical_pdf, pdf_model, processing_only, processing_context, live_llm
):
    from processing_fixtures import finish_recovered_sources

    from ultrakb.application.pending import pending_status
    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/first.pdf", physical_pdf.read_bytes())
        package.writestr("custom/second.bin", physical_pdf.read_bytes())
        package.writestr("word/embeddings/private.bin", b"PRIVATE\x00FORMAT")
        package.writestr("word/embeddings/preview.png", b"\x89PNG\r\n\x1a\npreview")
        package.writestr("word/embeddings/broken.docx", b"PK\x03\x04truncated")
        package.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="one" Type="https://example.invalid/package" '
                'Target="../custom/second.bin"/>'
                '<Relationship Id="two" Type="https://example.invalid/package" '
                'Target="../custom/second.bin"/>'
                '<Relationship Id="remote" Type="https://example.invalid/oleObject" '
                'Target="https://example.invalid/linked.doc" TargetMode="External"/>'
                "</Relationships>"
            ),
        )
    calls_before = len(live_llm.calls)
    parent = processing_only(writer_document)
    result = finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    assert result["imports_pending"] == 0
    recovered = [s for s in list_sources(kb_dir) if s.source_id != parent.source_id]
    assert sorted(s.name for s in recovered) == ["first.pdf", "second.pdf"]
    assert len({s.source_id for s in recovered}) == 2
    for source in recovered:
        saved = read_document_source(kb_dir, source.source_id)
        assert saved["status"] == "completed"
        assert (kb_dir / saved["original_path"]).read_bytes() == physical_pdf.read_bytes()
    statuses = {c["outcome"] for c in pending_status(kb_dir)["checkpoints"]}
    assert statuses == {
        "recovered",
        "private_object",
        "preview",
        "corrupt_object",
        "external_reference",
    }
    assert len(live_llm.calls) == calls_before


def test_complete_binary_workbook_in_ooxml_is_detected_as_xls(
    kb_dir, writer_document, pdf_model, processing_only, processing_context, live_llm
):
    from processing_fixtures import finish_recovered_sources

    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

    payload = (Path(__file__).parent / "fixtures/office/typed-sheets.xls").read_bytes()
    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/old.bin", payload)
    calls_before = len(live_llm.calls)
    parent = processing_only(writer_document)
    result = finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    assert result["imports_pending"] == 0
    recovered = next(s for s in list_sources(kb_dir) if s.source_id != parent.source_id)
    assert recovered.name == "old.xls"
    saved = read_document_source(kb_dir, recovered.source_id)
    assert saved["status"] == "completed"
    assert (kb_dir / saved["original_path"]).read_bytes() == payload
    assert len(live_llm.calls) == calls_before


@pytest.mark.parametrize("host", ["docx", "pptx", "xlsx", "doc", "xls", "ppt"])
def test_real_ole_package_preserves_counted_native_text_file(
    kb_dir, pdf_model, host, processing_only, processing_context, live_llm
):
    from processing_fixtures import finish_recovered_sources

    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

    calls_before = len(live_llm.calls)
    parent = processing_only(Path(__file__).parent / ("fixtures/office/package-text." + host))
    result = finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    assert result["imports_pending"] == 0
    child = next(s for s in list_sources(kb_dir) if s.source_id != parent.source_id)
    saved = read_document_source(kb_dir, child.source_id)
    expected = (
        "This is the contents of a simple ascii text file."
        if host in {"docx", "doc"}
        else "This is a simple ascii contents of this simple text file."
    )
    assert saved["content"] == expected
    assert saved["status"] == "completed"
    assert (kb_dir / saved["original_path"]).read_text("ascii") == expected
    assert len(live_llm.calls) == calls_before
