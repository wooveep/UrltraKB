"""OCR policy through the shared settings and document-processing interfaces."""

import subprocess

import pymupdf
import pytest

from openkb.application.documents import import_document
from openkb.application.settings import (
    apply_kb_config_patch,
    read_settings_view,
)
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.evidence import ParseStore
from openkb.sources import SourceStore


def test_new_settings_use_system_ocr_and_allow_turning_recognition_off(kb_dir):
    defaults = read_settings_view(kb_dir).values.parsing.ocr
    assert defaults.backend == "system"
    assert defaults.policy == "auto"
    assert defaults.device == "auto"
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": {"ocr": {"policy": "off"}}}),
    )
    assert read_settings_view(kb_dir).values.parsing.ocr.policy == "off"


def test_ocr_off_retains_scanned_original_without_launching_an_engine(
    kb_dir, tmp_path, monkeypatch
):
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().draw_circle((150, 150), 50)
        pdf.save(path)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": {"ocr": {"policy": "off"}}}),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("OCR launched a process"))
    result = import_document(kb_dir, path)
    assert result.source_intake == "saved"
    assert result.knowledge_compilation != "completed"
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert parsed.profile["ocr"]["policy"] == "off"
    assert any("ocr_disabled" in row["reason"] for row in parsed.quality)
    assert parsed.blocks and all(block.kind == "image" for block in parsed.blocks)
    store = SourceStore(kb_dir)
    assert store.original(store.version(result.input_version)).read_bytes() == path.read_bytes()


def test_selected_page_can_force_another_engine_without_changing_defaults(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import json

    import requests

    from openkb.application.source_actions import reprocess_source_page
    from tests.test_cloud_ocr import cloud_settings

    cloud_settings(kb_dir)
    saved = read_settings_view(kb_dir).values.parsing.model_dump()
    saved["ocr"].update(policy="off", backend="system")
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": saved}))
    monkeypatch.setenv("TEST_OCR_TOKEN", "isolated-ocr-key")
    path = tmp_path / "two-pages.pdf"
    with pymupdf.open() as pdf:
        for text in ("First page native text.", "Second page native text."):
            pdf.new_page().insert_text((40, 40), text)
        pdf.save(path)
    first = import_document(kb_dir, path)
    old = ParseStore(kb_dir).load(first.parse_id)
    requests_seen = []

    def service(self, method, url, **kwargs):
        requests_seen.append(method)
        if method == "POST":
            value = {"code": 0, "data": {"jobId": "page-two"}}
        elif url.endswith("page-two"):
            value = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://ocr.example.test/output.jsonl"},
                },
            }
        else:
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "Recognized page two.", "images": {}},
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Recognized page two.",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(value).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "request", service)
    retried = reprocess_source_page(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        parse_id=first.parse_id,
        page=2,
        engine="cloud",
    )
    current = ParseStore(kb_dir).load(retried.parse_id)
    assert requests_seen == ["POST", "GET", "GET"]
    assert read_settings_view(kb_dir).values.parsing.model_dump() == saved
    assert retried.input_version == first.input_version
    assert ParseStore(kb_dir).load(first.parse_id) == old
    assert [b for b in current.blocks if b.location["page"] == 1] == [
        b for b in old.blocks if b.location["page"] == 1
    ]
    assert any(
        "Recognized page two" in SourceStore(kb_dir).asset(b.blob).read_text()
        for b in current.blocks
    )


@pytest.mark.parametrize(
    "selection",
    [
        {"installation": "a" * 64},
        {"execution": "service", "service": {"endpoint": "http://127.0.0.1:8080"}},
    ],
)
def test_new_minimal_local_selection_is_not_migrated_to_system(kb_dir, selection):
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"parsing": {"ocr": {"backend": "local", **selection}}}
        ),
    )
    assert read_settings_view(kb_dir).values.parsing.ocr.backend == "local"


def test_ocr_off_docx_with_only_image_stays_unfinished(kb_dir, tmp_path, monkeypatch):
    from PIL import Image

    from tests.docx_attachment_fixtures import docx_with_parts

    image = tmp_path / "drawing.png"
    Image.new("RGB", (300, 200), "red").save(image)
    source = tmp_path / "drawing.docx"
    body = (
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="picture"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    docx_with_parts(
        source,
        body,
        parts={"word/media/picture.png": image.read_bytes()},
        relationships='<Relationship Id="picture" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/picture.png"/>',
    )
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": {"ocr": {"policy": "off"}}})
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("OCR was launched"))
    result = import_document(kb_dir, source)
    assert result.source_intake == "saved"
    assert result.knowledge_compilation != "completed"
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert any("readable_text_absent" in q["reason"] for q in parsed.quality)
