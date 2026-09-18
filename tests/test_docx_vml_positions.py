"""Original XML ownership survives Mammoth's detached VML representation."""

import hashlib

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import read_source_evidence
from openkb.evidence import Evidence
from tests.docx_attachment_fixtures import docx_with_parts
from tests.test_docx_images import _png


@pytest.mark.parametrize("title", [None, "", "Required control"])
def test_repeated_inline_vml_image_retains_each_original_paragraph(
    kb_dir, tmp_path, model_service, title
):
    image = _png("white", size=(32, 16))
    attribute = f' o:title="{title}"' if title is not None else ""
    picture = (
        '<w:pict xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<v:shape style="height:16pt;width:32pt"><v:imagedata r:id="same"'
        f"{attribute}/></v:shape></w:pict>"
    )
    source = docx_with_parts(
        tmp_path / "positions.docx",
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Install</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>Before.</w:t>{picture}<w:t>After.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Recovery</w:t></w:r></w:p>"
        f"<w:p><w:r>{picture}</w:r></w:p>",
        parts={"word/media/image.png": image},
        relationships='<Relationship Id="same" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image.png"/>',
    )
    original_bytes = source.read_bytes()
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result.reason
    original = next(
        row for row in result.coverage["assets"] if row["id"] == hashlib.sha256(image).hexdigest()
    )
    locations = [
        row["location"]
        for row in result.coverage["ranges"]
        if row["block_id"] in original["blocks"]
    ]
    assert locations == [
        {"kind": "docx", "paragraph": 2, "headings": ["Install"]},
        {"kind": "docx", "paragraph": 4, "headings": ["Recovery"]},
    ]
    assert original["understanding"] == "pending"
    views = [
        read_source_evidence(
            kb_dir,
            Evidence(result.source_id, result.input_version, result.parse_id, block),
            max_chars=4096,
        )
        for block in original["blocks"]
    ]
    first = next(view for view in views if view.location["paragraph"] == 2)
    assert first.text.startswith("Before.![") and first.text.endswith("After.")
    assert source.read_bytes() == original_bytes
    for view in views:
        relation = view.context_data["image_relations"][0]
        assert relation["original_asset"] == hashlib.sha256(image).hexdigest()
        if title is None:
            assert "source_alt" not in relation
        else:
            assert relation["source_alt"] == title
    assert not any(
        row["reason"].endswith("docx_image_position_unavailable")
        for row in result.coverage["issues"]
    )


@pytest.mark.parametrize("custom_type", [False, True])
def test_unrecognized_vml_transform_remains_unlocated(kb_dir, tmp_path, model_service, custom_type):
    image = _png("white", size=(32, 16))
    definition = (
        '<w:p><w:r><w:pict xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:shapetype id="_x0000_t75"><v:path v="m0,0l21600,0,10800,21600xe"/>'
        "</v:shapetype></w:pict></w:r></w:p>"
        if custom_type
        else ""
    )
    transform = 'type="#_x0000_t75"' if custom_type else 'rotation="90"'
    source = docx_with_parts(
        tmp_path / "transformed.docx",
        definition + "<w:p><w:r><w:t>Visible instructions.</w:t><w:pict "
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<v:shape {transform}><v:imagedata r:id="image"/></v:shape>'
        "</w:pict></w:r></w:p>",
        parts={"word/media/image.png": image},
        relationships='<Relationship Id="image" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image.png"/>',
    )
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result.reason
    assert any(
        row["reason"].endswith("docx_image_position_unavailable")
        for row in result.coverage["issues"]
    )
    original = next(
        row for row in result.coverage["assets"] if row["id"] == hashlib.sha256(image).hexdigest()
    )
    assert all(
        "paragraph" not in row["location"]
        for row in result.coverage["ranges"]
        if row["block_id"] in original["blocks"]
    )
