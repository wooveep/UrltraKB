"""Published DOCX coverage distinguishes retained images, OCR and visual understanding."""

import hashlib
import json

import pytest
import requests

from openkb.application.documents import import_document
from tests.docx_attachment_fixtures import attached_docx, docx_with_parts
from tests.test_cloud_ocr import cloud_settings
from tests.test_docx_images import _png


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("image_kind", ["drawing", "vml"])
def test_successful_image_transcription_does_not_complete_its_neighbor(
    kb_dir, tmp_path, monkeypatch, model_service, nested, image_kind
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    originals = [_png("white"), _png("black")]
    source = docx_with_parts(
        tmp_path / "images.docx",
        "<w:p><w:r><w:t>Follow the pictured command.</w:t>"
        + "".join(
            (
                '<w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
                'drawingml/2006/wordprocessingDrawing" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<a:graphic><a:graphicData><pic:pic><pic:blipFill>"
                f'<a:blip r:embed="{name}"/></pic:blipFill></pic:pic>'
                "</a:graphicData></a:graphic></wp:inline></w:drawing>"
            )
            if image_kind == "drawing"
            else (
                '<w:pict xmlns:v="urn:schemas-microsoft-com:vml" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<v:shape><v:textbox><w:txbxContent><w:p>"
                '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>'
                "<w:t>UNRELATED TEXTBOX</w:t></w:r></w:p></w:txbxContent>"
                "</v:textbox></v:shape>"
                f'<v:shape><v:imagedata r:id="{name}"/></v:shape></w:pict>'
            )
            for name in ("first", "second")
        )
        + "</w:r></w:p>",
        parts={"word/media/first.png": originals[0], "word/media/second.png": originals[1]},
        relationships="".join(
            f'<Relationship Id="{name}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="media/{name}.png"/>'
            for name in ("first", "second")
        ),
    )
    if nested:
        source = attached_docx(tmp_path / "outer.docx", source.read_bytes())
    submissions = []

    def service(self, method, url, **kwargs):
        if method.upper() == "POST":
            submissions.append(url)
            value = (
                {"code": 0, "data": {"jobId": "recognized"}}
                if len(submissions) == 1
                else {"code": 12001, "data": {}}
            )
        elif url.endswith("/recognized"):
            value = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/result"},
                },
            }
        else:
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "Restart control 9473", "images": {}},
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Restart control 9473",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", service)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", (result.reason, result.warnings)
    assets = {row["id"]: row for row in result.coverage["assets"]}
    first, second = (assets[hashlib.sha256(data).hexdigest()] for data in originals)
    assert first["transcription"] == "available"
    assert second["transcription"] == "pending"
    assert first["understanding"] == second["understanding"] == "pending"
    assert result.coverage["status"] == "partial"
    if image_kind == "vml":
        positions = [
            row["location"]
            for row in result.coverage["ranges"]
            if row["block_id"] in first["blocks"] + second["blocks"]
        ]
        for location in positions:
            while "attachment" in location:
                location = location["attachment"]["position"]
            assert "paragraph" not in location and "headings" not in location
        assert any(
            row["reason"].endswith("docx_image_position_unavailable")
            for row in result.coverage["issues"]
        )
        image_units = []
        for request in model_service:
            try:
                payload = json.loads(request["messages"][-1]["content"])
            except ValueError:
                continue
            if payload.get("stage") == "facts":
                image_units.extend(unit for unit in payload["units"] if unit["kind"] == "image")
        assert image_units
        assert all(not unit["headings"] and not unit["heading_evidence"] for unit in image_units)
    before = list(submissions)
    assert import_document(kb_dir, source).status == "skipped"
    assert submissions == before
