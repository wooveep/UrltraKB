"""Cloud OCR layout stays readable in the original PDF coordinate system."""

import copy
import json

import pymupdf
import pytest
import requests

from openkb.application.documents import import_document
from openkb.application.source_actions import (
    inspect_source_parse,
    read_source_evidence,
    reparse_source,
)
from openkb.application.source_history import source_status
from openkb.evidence import Evidence
from tests.test_cloud_ocr import cloud_settings


@pytest.fixture
def cloud_layout(kb_dir, tmp_path, monkeypatch, model_service):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    result = {
        "width": 400,
        "height": 600,
        "model_settings": {"use_doc_preprocessor": False},
        "parsing_res_list": [
            {
                "block_id": 0,
                "block_label": "text",
                "block_content": "Left: return path",
                "block_bbox": [20, 40, 120, 70],
            },
            {
                "block_id": 1,
                "block_label": "text",
                "block_content": "Right: relief path",
                "block_bbox": [220, 40, 320, 70],
            },
        ],
    }
    calls = []

    def service(self, method, url, **kwargs):
        calls.append(method)
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        if method.upper() == "POST":
            value = {"code": 0, "data": {"jobId": "layout"}}
        elif url.endswith("/layout"):
            value = {
                "code": 0,
                "data": {"state": "done", "resultUrl": {"jsonUrl": "https://assets.test/page"}},
            }
        else:
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "Left: return path\nRight: relief path",
                                "images": {},
                            },
                            "prunedResult": result,
                        }
                    ]
                }
            }
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", service)

    def document(rotation=0):
        source = tmp_path / "paths.pdf"
        with pymupdf.open() as raster, pymupdf.open() as pdf:
            pdf.new_page().insert_text((30, 30), "Independent native maintenance instructions.")
            page = raster.new_page(width=200, height=300)
            page.insert_text((10, 30), "Left: return path")
            page = pdf.new_page(width=200, height=300)
            page.insert_image(
                page.rect, stream=raster[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
            )
            page.set_rotation(rotation)
            pdf.save(source)
        return source

    return result, calls, document


def layout_readings(kb_dir, result):
    parsed = inspect_source_parse(
        kb_dir, result.source_id, version_id=result.input_version, parse_id=result.parse_id
    )
    readings = [
        read_source_evidence(
            kb_dir,
            Evidence(result.source_id, result.input_version, result.parse_id, block["id"]),
            max_chars=2000,
        )
        for block in parsed["blocks"]
    ]
    return [view for view in readings if (view.context or "").startswith("OCR layout block")]


@pytest.mark.parametrize("rotation,expected", [(0, [10, 20, 60, 35]), (90, [20, 240, 35, 290])])
def test_cloud_caption_positions_survive_import_and_source_readback(
    kb_dir, cloud_layout, model_service, rotation, expected
):
    payload, calls, document = cloud_layout
    if rotation:
        payload.update(width=600, height=400)
    result = import_document(kb_dir, document(rotation))
    assert result.knowledge_compilation == "completed", result
    caption = layout_readings(kb_dir, result)[0]
    assert caption.text == "Left: return path"
    assert caption.location == {"kind": "pdf", "page": 2, "bbox": expected}
    assert calls == ["POST", "GET", "GET"]


@pytest.mark.parametrize(
    "defect", ["missing_settings", "preprocessed", "wrong_aspect", "invalid_box"]
)
def test_unproven_cloud_coordinates_do_not_block_readable_text(
    kb_dir, cloud_layout, model_service, defect
):
    payload, calls, document = cloud_layout
    if defect == "missing_settings":
        payload.pop("model_settings")
    elif defect == "preprocessed":
        payload["model_settings"]["use_doc_preprocessor"] = True
    elif defect == "wrong_aspect":
        payload["height"] = 900
    else:
        payload["parsing_res_list"][0]["block_bbox"] = [-5, 40, 120, 70]
    result = import_document(kb_dir, document())
    assert result.knowledge_compilation == "completed", result
    left, right = layout_readings(kb_dir, result)
    assert left.text == "Left: return path"
    assert left.location == {"kind": "pdf", "page": 2}
    if defect == "invalid_box":
        assert right.location["bbox"] == [110, 20, 160, 35]
    else:
        assert "bbox" not in right.location
    assert any(
        row.get("reason", "").endswith("cloud_layout_coordinates_unverified")
        for row in result.coverage["issues"]
    )
    assert calls == ["POST", "GET", "GET"]


def test_upgraded_cloud_assembly_reuses_downloaded_job_without_remote_requests(
    kb_dir, cloud_layout, monkeypatch, model_service
):
    from openkb.ocr import assembly

    _, calls, document = cloud_layout
    first = import_document(kb_dir, document())
    previous = copy.deepcopy(source_status(kb_dir, first.source_id)["cloud_jobs"])
    old_readings = layout_readings(kb_dir, first)
    monkeypatch.setattr(assembly, "REVISION", "synthetic-next-contract")
    updated = reparse_source(kb_dir, first.source_id, version_id=first.input_version)
    assert updated.parse_id != first.parse_id
    assert layout_readings(kb_dir, first) == old_readings
    assert layout_readings(kb_dir, updated)[0].location == old_readings[0].location
    assert calls == ["POST", "GET", "GET"]
    current = source_status(kb_dir, first.source_id)["cloud_jobs"]
    assert [(job["job_id"], job["requests"], job["submissions"]) for job in current] == [
        (job["job_id"], job["requests"], job["submissions"]) for job in previous
    ]
