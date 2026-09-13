"""Cloud layout evidence must not borrow a neighboring figure on the same page."""

import io
import json

import pytest
import requests
from PIL import Image

from openkb.application.documents import import_document
from openkb.evidence import ParseStore
from tests.test_cloud_ocr import cloud_settings, scanned_pdf


@pytest.mark.parametrize("transcription", [False, True, "repetitive"])
def test_each_layout_block_retains_only_its_own_figure(
    kb_dir, tmp_path, monkeypatch, model_service, transcription
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    source = tmp_path / "paired.pdf"
    scanned_pdf(source)
    left_name = "imgs/img_in_image_box_10_20_30_40.jpg" if transcription else "left.png"
    right_name = "imgs/img_in_image_box_50_60_70_80.jpg" if transcription else "right.png"
    left = f"![Read mechanism]({left_name})"
    right = f"![Write mechanism]({right_name})"
    recognized = "Signal\nArrow\n" * 120 if transcription == "repetitive" else "Read mechanism"

    def service(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        if method.upper() == "POST":
            value = {"code": 0, "data": {"jobId": "paired"}}
        elif url.endswith("/paired"):
            value = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/result"},
                },
            }
        elif url.endswith((".png", ".jpg")):
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), "red" if url.endswith(left_name) else "blue").save(
                buffer, format="PNG"
            )
            response._content = buffer.getvalue()
            return response
        else:
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": left + "\n" + recognized + "\n" + right,
                                "images": {
                                    name: "https://assets.example.test/" + name
                                    for name in (left_name, right_name)
                                },
                            },
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "image",
                                        "block_content": recognized if transcription else left,
                                        "block_bbox": [10, 20, 30, 40],
                                    },
                                    {
                                        "block_id": 1,
                                        "block_label": "text",
                                        "block_content": "Keep both captions.",
                                    },
                                    {
                                        "block_id": 2,
                                        "block_label": "image",
                                        "block_content": "Write mechanism"
                                        if transcription
                                        else right,
                                        "block_bbox": [50, 60, 70, 80],
                                    },
                                ]
                            },
                        }
                    ]
                }
            }
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", service)
    result = import_document(kb_dir, source)
    parsed = ParseStore(kb_dir).load(result.parse_id)
    blocks = [row for row in parsed.blocks if row.context.startswith("OCR layout block")]
    assert len(blocks) == 3
    assert len(blocks[0].assets) == len(blocks[2].assets) == 1
    assert blocks[0].assets != blocks[2].assets
    assert blocks[1].assets == ()
    if transcription == "repetitive":
        from openkb.sources import SourceStore

        store = SourceStore(kb_dir)
        assert all(recognized not in store.asset(block.blob).read_text() for block in parsed.blocks)
        assert result.coverage["status"] == "partial"
        assert any(
            "ocr_repetitive_transcription" in row["reason"] for row in result.coverage["issues"]
        )
