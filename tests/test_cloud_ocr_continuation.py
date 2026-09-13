"""Continue uses a new bounded OCR allowance for pages never submitted."""

import json

import pymupdf
import requests
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.application.source_history import source_status
from openkb.evidence import ParseStore
from tests.test_cloud_ocr import cloud_settings, scanned_pdf


def test_continue_submits_unstarted_page_without_resubmitting_completed_page(
    kb_dir, tmp_path, monkeypatch, model_service
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    settings_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings["parsing"]["ocr"]["cloud"]["limits"]["max_pages"] = 1
    settings_path.write_text(yaml.safe_dump(settings))
    scan = tmp_path / "one.pdf"
    scanned_pdf(scan)
    source = tmp_path / "two.pdf"
    with pymupdf.open(scan) as scanned, pymupdf.open() as pdf:
        pdf.insert_pdf(scanned)
        pdf.insert_pdf(scanned)
        pdf.save(source)
    submissions = []

    def service(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        if method.upper() == "POST":
            submissions.append(kwargs["files"]["file"][1].read())
            body = {"code": 0, "data": {"jobId": f"page-{len(submissions)}"}}
        elif "ocr.example.test" in url:
            body = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/one"},
                },
            }
        else:
            body = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "Scanned: timeout 42 seconds.", "images": {}},
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Scanned: timeout 42 seconds.",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", service)
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert len(submissions) == 1
    jobs = source_status(kb_dir, first.source_id)["cloud_jobs"]
    assert {job["state"] for job in jobs} == {"downloaded", "planned"}
    old_parse = ParseStore(kb_dir).load(first.parse_id)
    assert any("ocr_page_budget_exhausted" in row["reason"] for row in old_parse.quality)

    repeated = import_document(kb_dir, source)
    assert repeated.status == "skipped" and len(submissions) == 1
    continued = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert len(submissions) == 2, continued
    assert continued.knowledge_compilation == "completed", continued
    jobs = source_status(kb_dir, first.source_id)["cloud_jobs"]
    assert {job["state"] for job in jobs} == {"downloaded"}
    assert [job["submissions"] for job in jobs] == [1, 1]
    assert continued.parse_id != first.parse_id
    assert ParseStore(kb_dir).load(first.parse_id) == old_parse
    assert not any(
        "ocr_page_budget_exhausted" in row["reason"]
        for row in ParseStore(kb_dir).load(continued.parse_id).quality
    )
