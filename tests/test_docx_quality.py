"""DOCX formatting diagnostics must not masquerade as missing source content."""

import pytest

from openkb.evidence import ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx


def _parse(kb_dir, tmp_path, body):
    file = tmp_path / "formatting.docx"
    write_docx(file, body)
    with prepared_input(file) as ready:
        source = SourceStore(kb_dir).intake(ready)
    return source, parse_document(kb_dir, source)


def test_table_formatting_warning_does_not_block_preserved_content(kb_dir, tmp_path):
    source, parsed = _parse(
        kb_dir,
        tmp_path,
        "<w:tbl><w:tr><w:tblPrEx/><w:tc><w:p><w:r>"
        "<w:t>Timeout is 42 seconds.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    block = parsed.blocks[0]
    assert SourceStore(kb_dir).asset(block.blob).read_text() == "Timeout is 42 seconds."
    assert block.location["table"] == 1 and block.location["cell"] == 1
    assert ParseStore(kb_dir).complete(source, parsed)
    assert parsed.quality == [
        {
            "status": "verified",
            "reason": "docx_conversion_warning:An unrecognised element was ignored: w:tblPrEx",
        }
    ]


@pytest.mark.parametrize(
    "unknown",
    [
        "<w:unsupported><w:t>Required recovery instruction.</w:t></w:unsupported>",
        '<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" ProgID="Package"/>',
        '<v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:path/></v:shape>',
    ],
)
def test_missing_content_warnings_still_block_compilation(kb_dir, tmp_path, unknown):
    source, parsed = _parse(
        kb_dir, tmp_path, f"<w:p><w:r><w:t>Preserved text.</w:t>{unknown}</w:r></w:p>"
    )
    assert not ParseStore(kb_dir).complete(source, parsed)
    assert any(row["status"] == "needs_review" for row in parsed.quality)


def test_reused_inline_image_notices_keep_every_original_position(kb_dir, tmp_path):
    from openkb.agent.dependency_preflight import known_omissions
    from tests.docx_attachment_fixtures import docx_with_parts
    from tests.test_docx_images import _png

    drawing = (
        "<w:p><w:r><w:t>Read the pictured operation.</w:t><w:drawing><wp:inline "
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="same"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    path = docx_with_parts(
        tmp_path / "repeated.docx",
        drawing * 2,
        parts={"word/media/same.png": _png("white")},
        relationships='<Relationship Id="same" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/same.png"/>',
    )
    store = SourceStore(kb_dir)
    with prepared_input(path) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    notices = [row for row in parsed.quality if "image_ocr_notice:" in row["reason"]]
    assert {row["location"]["paragraph"] for row in notices} == {1, 2}
    omissions = [row for row in known_omissions(parsed) if row["stage"] == "parsing"]
    assert {row["block"] for row in omissions} == {block.id for block in parsed.blocks}
