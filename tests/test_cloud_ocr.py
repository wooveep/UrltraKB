"""Cloud OCR is explicit, durable and never repeats an uncertain submission."""

import json

import pymupdf
import pytest
import requests
import yaml

from openkb.application.documents import import_document
from openkb.evidence import ParseStore


def cloud_settings(kb):
    path = kb / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["parsing"] = {
        "ocr": {
            "backend": "cloud",
            "cloud": {
                "endpoint": "https://ocr.example.test/api/v2/ocr/jobs",
                "model": "PaddleOCR-VL-1.6",
                "credential_env": "TEST_OCR_TOKEN",
                "limits": {
                    "seconds": 5,
                    "request_seconds": 1,
                    "poll_seconds": 0.01,
                    "max_requests": 10,
                    "max_pages": 5,
                    "max_page_bytes": 1000000,
                    "max_download_bytes": 1000000,
                },
            },
        }
    }
    path.write_text(yaml.safe_dump(settings))


def scanned_pdf(path):
    with pymupdf.open() as raster, pymupdf.open() as pdf:
        page = raster.new_page()
        page.insert_text((40, 40), "Scanned: timeout 42 seconds.")
        page = pdf.new_page()
        page.insert_image(page.rect, stream=raster[0].get_pixmap().tobytes("png"))
        pdf.save(path)


def test_shared_import_never_reposts_an_uncertain_cloud_submission(
    kb_dir, tmp_path, monkeypatch, model_service
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    source = tmp_path / "scan.pdf"
    scanned_pdf(source)
    submissions = []

    def lost_response(self, method, url, **kwargs):
        assert method.upper() == "POST" and url.startswith("https://ocr.example.test/")
        submissions.append(kwargs["files"]["file"][1].read())
        raise requests.Timeout("Synthetic response loss")

    monkeypatch.setattr(requests.Session, "request", lost_response)
    one = import_document(kb_dir, source)
    two = import_document(kb_dir, source)
    assert one.status == two.status == "unfinished"
    assert len(submissions) == 1
    assert "cloud_submission_unknown" in two.quality
    assert any(
        row["reason"] == "cloud_submission_unknown"
        for row in ParseStore(kb_dir).load(two.parse_id).quality
    )
    with pymupdf.open(stream=submissions[0], filetype="pdf") as part:
        assert part.page_count == 1
    # No credential is included in any retained source/parse/job record.
    assert all(
        "synthetic-test-token" not in p.read_text()
        for p in (kb_dir / ".openkb/source-store").rglob("*.json")
    )


@pytest.mark.parametrize("corrupt_image_once", [False, True])
def test_known_job_resumes_download_without_repeating_ocr(
    kb_dir, tmp_path, monkeypatch, model_service, corrupt_image_once
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    source = tmp_path / "scan.pdf"
    scanned_pdf(source)
    calls = []
    downloads = 0
    images = 0

    def response(value):
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(value).encode()
        result._content_consumed = True
        return result

    def service(self, method, url, **kwargs):
        nonlocal downloads, images
        calls.append((method.upper(), url))
        if method.upper() == "POST":
            return response({"code": 0, "data": {"jobId": "job-one"}})
        if url.endswith("/job-one"):
            return response(
                {
                    "code": 0,
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": "https://assets.example.test/result.jsonl"},
                    },
                }
            )
        assert "Authorization" not in kwargs.get("headers", {})
        if url.endswith("diagram.png"):
            import io

            from PIL import Image

            image = io.BytesIO()
            Image.new("RGB", (8, 8), "white").save(image, format="PNG")
            result = response({})
            images += 1
            result._content = (
                b"Temporary service error"
                if corrupt_image_once and images == 1
                else image.getvalue()
            )
            return result
        downloads += 1
        if downloads == 1:
            raise requests.ConnectionError("Synthetic download interruption")
        return response(
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "Scanned: timeout 42 seconds.\n![diagram](diagram.png)",
                                "images": {
                                    "diagram.png": "https://assets.example.test/diagram.png"
                                },
                            },
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
        )

    monkeypatch.setattr(requests.Session, "request", service)
    one = import_document(kb_dir, source)
    assert one.status == "unfinished"
    from openkb.application.source_history import source_status

    observed = source_status(kb_dir, one.source_id)
    assert observed["cloud_jobs"][0]["requests"] == 3
    assert observed["cloud_jobs"][0]["job_id"] == "job-one"
    two = import_document(kb_dir, source)
    if corrupt_image_once:
        assert two.status == "unfinished" and "cloud_required_asset_invalid" in two.quality
        two = import_document(kb_dir, source)
    assert two.status == "added", two
    assert downloads == 2 and sum(method == "POST" for method, _ in calls) == 1
    assert all(
        block.location["page"] == 1 for block in ParseStore(kb_dir).load(two.parse_id).blocks
    )

    # Installing new result assembly rules must revalidate retained raw output,
    # while preserving the paid job and the old evidence version.
    from openkb.application.source_actions import reparse_source
    from openkb.ocr import assembly

    monkeypatch.setattr(assembly, "REVISION", "synthetic-next-contract")
    before = list(calls)
    updated = reparse_source(kb_dir, two.source_id, version_id=two.input_version)
    assert updated.parse_id != two.parse_id
    assert calls == before
    assert ParseStore(kb_dir).load(two.parse_id).blocks
    assert any(block.assets for block in ParseStore(kb_dir).load(updated.parse_id).blocks)


@pytest.mark.parametrize("image_content", ["![diagram](missing.png)", ""])
def test_required_image_omitted_from_markdown_is_still_checked(
    kb_dir, tmp_path, monkeypatch, model_service, image_content
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    source = tmp_path / "scan.pdf"
    scanned_pdf(source)

    def service(self, method, url, **kwargs):
        if method == "POST":
            body = {"code": 0, "data": {"jobId": "job-one"}}
        elif url.endswith("/job-one"):
            body = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/result.jsonl"},
                },
            }
        else:
            body = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "Recognized body", "images": {}},
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Recognized body",
                                    },
                                    {
                                        "block_id": 1,
                                        "block_label": "image",
                                        "block_content": image_content,
                                    },
                                ]
                            },
                        }
                    ]
                }
            }
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(body).encode()
        result._content_consumed = True
        return result

    monkeypatch.setattr(requests.Session, "request", service)
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert "cloud_required_asset_missing" in result.quality
    assert not model_service


def test_explicit_page_reprocessing_requires_acknowledging_unknown_submission(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import pytest

    from openkb.application.source_actions import reprocess_source_page

    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    original = tmp_path / "scan.pdf"
    scanned_pdf(original)
    posts = []

    def lost_response(self, method, url, **kwargs):
        assert method == "POST"
        posts.append(kwargs["files"]["file"][1].read())
        raise requests.Timeout("Synthetic ambiguous submission")

    monkeypatch.setattr(requests.Session, "request", lost_response)
    first = import_document(kb_dir, original)
    binding = {"version_id": first.input_version, "parse_id": first.parse_id, "page": 1}
    with pytest.raises(ValueError, match="unknown"):
        reprocess_source_page(kb_dir, first.source_id, **binding)
    assert len(posts) == 1
    second = reprocess_source_page(kb_dir, first.source_id, **binding, acknowledge_unknown=True)
    assert second.input_version == first.input_version
    assert second.parse_id != first.parse_id
    assert "cloud_submission_unknown" in second.quality
    assert (
        ParseStore(kb_dir).load(first.parse_id).quality[0]["reason"] == "cloud_submission_unknown"
    )
    assert len(posts) == 2
    import_document(kb_dir, original)
    assert len(posts) == 2  # continuation observes the new uncertain intent


def test_page_reprocessing_leaves_other_unfinished_pages_and_their_jobs_untouched(
    kb_dir, tmp_path, monkeypatch
):
    from openkb.application.source_actions import reprocess_source_page

    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    original = tmp_path / "two-pages.pdf"
    single = tmp_path / "single.pdf"
    scanned_pdf(single)
    with pymupdf.open(single) as page, pymupdf.open() as pdf:
        pdf.insert_pdf(page)
        pdf.insert_pdf(page)
        pdf.save(original)
    calls = []
    submitted = 0

    def service(self, method, url, **kwargs):
        nonlocal submitted
        calls.append((method, url))
        if method == "GET":
            raise requests.Timeout("Synthetic polling interruption")
        submitted += 1
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"code": 0, "data": {"jobId": f"job-{submitted}"}}).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "request", service)
    first = import_document(kb_dir, original)
    old = ParseStore(kb_dir).load(first.parse_id)
    assert submitted == 2
    calls.clear()
    second = reprocess_source_page(
        kb_dir, first.source_id, version_id=first.input_version, parse_id=first.parse_id, page=1
    )
    assert submitted == 3
    assert not any(url.endswith("/job-2") for _, url in calls)
    updated = ParseStore(kb_dir).load(second.parse_id)
    assert [b for b in updated.blocks if b.location["page"] == 2] == [
        b for b in old.blocks if b.location["page"] == 2
    ]


def test_stopped_page_reprocessing_invalidates_older_knowledge_proposal(
    kb_dir, tmp_path, model_service
):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source, reprocess_source_page
    from openkb.cancellation import OperationCancelled

    cloud_settings(kb_dir)
    (kb_dir / "wiki/index.md").write_text("# Human index\nKeep this page.\n")
    original = tmp_path / "native.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 40), "The retry interval is 42 seconds.")
        pdf.save(original)
    first = import_document(kb_dir, original)
    assert first.reason == "needs_acceptance"

    def stop(event):
        if event.get("stage") == "parsing":
            raise OperationCancelled()

    interrupted = reprocess_source_page(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        parse_id=first.parse_id,
        page=1,
        context=ExecutionContext(on_event=stop),
    )
    assert interrupted.status == "stopped"
    continued = continue_source(
        kb_dir, first.source_id, version_id=first.input_version, proposal_id=first.resume
    )
    assert continued.reason == "input_conflict"
    assert (kb_dir / "wiki/index.md").read_text() == "# Human index\nKeep this page.\n"
