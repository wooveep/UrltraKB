"""Actual native parsers retain structural positions and expose uncertain pages."""

import pymupdf

from openkb.evidence import Evidence, ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore


def source_version(kb, source):
    with prepared_input(source) as ready:
        return SourceStore(kb).intake(ready)


def test_native_pdf_preserves_physical_pages_and_requests_only_uncertain_ocr(kb_dir, tmp_path):
    file = tmp_path / "mixed.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 40), "NATIVE fact: pressure is 37 kPa.")
    doc.new_page()
    raster = pymupdf.open()
    raster.new_page().insert_text((40, 40), "SCANNED fact: timeout is 42 seconds.")
    page = doc.new_page()
    page.insert_image(page.rect, stream=raster[0].get_pixmap().tobytes("png"))
    doc.save(file)
    raster.close()
    doc.close()
    version = source_version(kb_dir, file)
    result = parse_document(kb_dir, version)
    assert len(result.quality) == 3
    assert result.quality[0]["status"] == "verified"
    assert [q["page"] for q in result.quality if q["status"] == "needs_review"] == [2]
    assert (
        result.quality[2]["reason"]
        == "pdf_image_ocr_notice:system_ocr_unavailable:open_ocr_settings"
    )
    first = result.blocks[0]
    assert first.location["page"] == 1
    store = ParseStore(kb_dir)
    ref = Evidence(version.source_id, version.id, result.id, first.id)
    assert "37 kPa" in store.read(ref, max_chars=100).text
    assert not store.complete(version, result)
    assert parse_document(kb_dir, version).id == result.id


def test_docx_maps_headings_paragraphs_table_cells_and_merged_header(kb_dir, tmp_path):
    from tests.document_fixtures import write_docx

    file = tmp_path / "structured.docx"
    write_docx(
        file,
        """
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Installation</w:t></w:r></w:p>
      <w:p><w:r><w:t>Requires version 7.</w:t></w:r></w:p>
      <w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>
        <w:tr><w:trPr><w:tblHeader/></w:trPr>
          <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
            <w:p><w:r><w:t>Command parameters</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:p><w:r><w:t>--timeout</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr>
      </w:tbl>
      <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Exceptions</w:t></w:r></w:p>
      <w:p><w:r><w:t>Do not retry authentication failures.</w:t></w:r></w:p>
    """,
    )
    version = source_version(kb_dir, file)
    parsed = parse_document(kb_dir, version)
    assert all("page" not in block.location for block in parsed.blocks)
    table_blocks = [b for b in parsed.blocks if b.kind == "table"]
    assert any(b.location["row"] == 2 and b.location["cell"] == 1 for b in table_blocks)
    assert any("Command parameters" in b.context for b in table_blocks)
    assert parsed.blocks[-1].location["headings"] == ["Installation", "Exceptions"]
    assert ParseStore(kb_dir).complete(version, parsed)


def test_native_pdf_table_cells_retain_headers_and_physical_coordinates(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document

    file = tmp_path / "parameters.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for y in (50, 80, 110):
            page.draw_line((40, y), (360, y))
        for x in (40, 200, 360):
            page.draw_line((x, 50), (x, 110))
        for x, y, text in (
            (50, 70, "Parameter"),
            (210, 70, "Value"),
            (50, 100, "--timeout"),
            (210, 100, "42 seconds"),
        ):
            page.insert_text((x, y), text)
        pdf.save(file)
    imported = import_document(kb_dir, file)
    parsed = ParseStore(kb_dir).load(imported.parse_id)
    cells = [b for b in parsed.blocks if b.kind == "table"]
    timeout = next(b for b in cells if b.location["row"] == 2 and b.location["cell"] == 2)
    text = ParseStore(kb_dir).read(
        Evidence(imported.source_id, imported.input_version, parsed.id, timeout.id), max_chars=200
    )
    assert text.text == "42 seconds"
    assert "Parameter" in text.context and "Value" in text.context
    assert timeout.location["page"] == 1
    assert timeout.location["bbox"] == [200.0, 80.0, 360.0, 110.0]


def test_docx_footnote_exceptions_are_bound_to_the_referencing_paragraph(kb_dir, tmp_path):
    from tests.document_fixtures import write_docx

    file = tmp_path / "notes.docx"
    write_docx(
        file,
        '<w:p><w:r><w:t>Retry network failures.</w:t><w:footnoteReference w:id="1"/></w:r></w:p>',
        footnotes='<w:footnote w:id="1"><w:p><w:r><w:t>Except authentication failures.'
        "</w:t></w:r></w:p></w:footnote>",
    )
    version = source_version(kb_dir, file)
    parsed = parse_document(kb_dir, version)
    reference = Evidence(version.source_id, version.id, parsed.id, parsed.blocks[0].id)
    content = ParseStore(kb_dir).read(reference, max_chars=1000)
    assert "Except authentication failures." in content.text
    assert content.location["paragraph"] == 1 and "page" not in content.location


def test_vector_diagram_with_readable_caption_is_retained_when_ocr_is_unavailable(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document

    file = tmp_path / "circuit.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 40), "Circuit diagram")
        page.draw_circle((100, 140), 30)
        page.draw_rect((220, 120, 280, 160))
        page.draw_line((130, 140), (220, 140))
        pdf.save(file)
    result = import_document(kb_dir, file)
    assert result.source_intake == "saved"
    assert result.knowledge_compilation == "completed"
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert parsed.quality[0]["status"] == "verified"
    assert (
        parsed.quality[0]["reason"]
        == "pdf_image_ocr_notice:system_ocr_unavailable:open_ocr_settings"
    )
    assert any(block.assets for block in parsed.blocks)
    assert list((kb_dir / "wiki/summaries").glob("*.md"))
