"""Original image bytes, not recognition output, identify shared OCR work."""

import json

import pymupdf
import pytest
import requests

from openkb.evidence import BlockDraft
from openkb.inputs import prepared_input
from openkb.ocr.backend import LazyOcr
from openkb.ocr.config import OcrSettings, parsing_settings
from openkb.sources import SourceStore
from tests.test_cloud_ocr import cloud_settings, scanned_pdf
from tests.test_docx_images import _png


def sources(kb, tmp_path):
    store = SourceStore(kb)
    result = []
    for index in range(2):
        path = tmp_path / f"different-{index}.txt"
        path.write_text(f"Independent original document {index}")
        with prepared_input(path) as ready:
            result.append(store.intake(ready))
    return store, result


def test_images_share_recognition_across_different_documents(kb_dir, tmp_path, monkeypatch):
    from openkb.docx_images import read_image
    from openkb.ocr import backend

    store, versions = sources(kb_dir, tmp_path)
    calls = []

    class Engine:
        def page(self, document, page, **kwargs):
            calls.append(kwargs["input_id"])
            return [
                BlockDraft(
                    f"Variable OCR result {len(calls)}", "paragraph", {"kind": "pdf", "page": 1}
                )
            ], None

    monkeypatch.setattr(backend, "_create_backend", lambda *args, **kwargs: Engine())
    one = LazyOcr(store, versions[0], OcrSettings())
    two = LazyOcr(store, versions[1], OcrSettings())
    first = read_image(_png("white"), store, one, alt_text="Document one")
    repeated = read_image(_png("white"), store, two, alt_text="Document two")
    assert len(calls) == 1
    assert "Variable OCR result 1" in first[0] and "Variable OCR result 1" in repeated[0]
    assert "Document one" in first[0] and "Document two" in repeated[0]
    read_image(_png("black"), store, two)
    assert len(calls) == 2
    changed = LazyOcr(store, versions[1], OcrSettings(), retries={1: "explicit-refresh"})
    read_image(_png("white"), store, changed)
    assert len(calls) == 3


@pytest.mark.parametrize("uncertain", [False, True])
def test_existing_cloud_jobs_are_reused_for_identical_slices_across_sources(
    kb_dir, tmp_path, monkeypatch, uncertain
):
    from openkb.config import resolve_effective_config
    from openkb.ocr.cloud import CloudJobs

    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "test-only")
    store, versions = sources(kb_dir, tmp_path)
    config = parsing_settings(resolve_effective_config(kb_dir)[0]["parsing"]).ocr.cloud
    calls = []

    def service(self, method, url, **kwargs):
        calls.append(method)
        if uncertain:
            raise requests.Timeout("synthetic")
        value = {"code": 0, "data": {"jobId": "shared-job"}}
        if method == "GET":
            value = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/result"},
                },
            }
        if url.endswith("/result"):
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "Retained OCR output", "images": {}},
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Retained OCR output",
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
    scan = tmp_path / "scan.pdf"
    scanned_pdf(scan)
    with pymupdf.open(scan) as doc:
        first = CloudJobs(store, versions[0], config)
        result = first.page(doc, 1)
        before = list(calls)
        second = CloudJobs(store, versions[1], config)
        repeated = second.page(doc, 1)
        first.close()
        second.close()
    assert calls == before and calls.count("POST") == 1
    if uncertain:
        assert repeated == ([], "cloud_submission_unknown")
    else:
        assert repeated == result and result[0]
