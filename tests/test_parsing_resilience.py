"""Regression cases for successful OCR, decorative visuals and review continuity."""

import io

import pymupdf
import pytest
from PIL import Image

from openkb.evidence import BlockDraft, ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.parsing_pdf import parse_pdf
from openkb.sources import SourceStore
from tests.docx_attachment_fixtures import docx_with_parts


def _pdf(path, *, decorative=False, icon=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        if decorative:
            page.draw_rect(page.rect, color=None, fill=(0.9, 0.9, 0.9))
            page.draw_line((20, 20), (500, 20))
        else:
            page.draw_circle((160, 180), 45)
        page.insert_text((40, 80), "Source fact: timeout is 42 seconds.")
        if icon:
            data = io.BytesIO()
            Image.new("RGB", (16, 16), "red").save(data, format="PNG")
            page.insert_image((40, 100, 56, 116), stream=data.getvalue())
        pdf.save(path)
    return path


class TextOcr:
    def __init__(self, reason=None):
        self.calls = 0
        self.reason = reason

    def page(self, document, number):
        self.calls += 1
        return [
            BlockDraft(
                "OCR fact: timeout is 42 seconds.", "paragraph", {"kind": "pdf", "page": number}
            )
        ], self.reason


def test_successful_text_ocr_keeps_original_graphics_without_requesting_ocr_again(kb_dir, tmp_path):
    source = _pdf(tmp_path / "diagram.pdf")
    store = SourceStore(kb_dir)
    ocr = TextOcr()
    blocks, quality = parse_pdf(source, store, ocr=ocr)
    assert ocr.calls == 1
    assert quality == [{"page": 1, "status": "verified", "reason": "ocr_layout_and_assets"}]
    assert any("OCR fact" in b.text for b in blocks)
    assert any("Source fact" in b.text for b in blocks)
    assert any(b.assets and "Original page" in b.text for b in blocks)
    for block in blocks:
        for asset in block.assets:
            store.asset(asset)


def test_decorative_background_border_and_small_icon_skip_ocr_but_keep_visuals(kb_dir, tmp_path):
    source = _pdf(tmp_path / "decorated.pdf", decorative=True, icon=True)
    ocr = TextOcr()
    blocks, quality = parse_pdf(source, SourceStore(kb_dir), ocr=ocr)
    assert ocr.calls == 0
    assert quality[0]["status"] == "verified"
    assert any("Source fact" in b.text for b in blocks)
    assert any(b.assets for b in blocks)


@pytest.mark.parametrize(
    "reason",
    [
        "ocr_time_budget_exhausted",
        "cloud_required_asset_missing",
        "windows_ocr_no_text",
        "cloud_output_incomplete",
    ],
)
def test_partial_ocr_keeps_native_text_and_reports_an_advisory(kb_dir, tmp_path, reason):
    source = _pdf(tmp_path / "partial.pdf")
    blocks, quality = parse_pdf(source, SourceStore(kb_dir), ocr=TextOcr(reason))
    assert quality[0]["status"] == "verified"
    assert quality[0]["reason"] == "pdf_image_ocr_notice:" + reason
    assert any("Source fact" in b.text for b in blocks)
    assert any("OCR fact" in b.text for b in blocks)


def test_vector_only_page_ocr_failure_keeps_visual_and_later_text(kb_dir, tmp_path):
    path = tmp_path / "vector-only.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().draw_circle((160, 180), 45)
        pdf.new_page().insert_text((40, 80), "Available instructions on the next page.")
        pdf.save(path)

    class BrokenOcr:
        def page(self, *args):
            raise RuntimeError("Provider failed")

    blocks, quality = parse_pdf(path, SourceStore(kb_dir), ocr=BrokenOcr())
    assert all(q["status"] == "verified" for q in quality)
    assert any("ocr_backend_failed:RuntimeError" in q["reason"] for q in quality)
    assert any(b.assets for b in blocks)
    assert any("Available instructions" in b.text for b in blocks)


@pytest.mark.parametrize("force", [False, True])
def test_optional_ocr_does_not_waive_an_independent_table_extraction_failure(
    kb_dir, tmp_path, monkeypatch, force
):
    import openkb.parsing_pdf as parser

    def broken_table(*args):
        raise RuntimeError("Native table extraction failed")

    monkeypatch.setattr(parser, "table_cells", broken_table)
    path = _pdf(tmp_path / "broken-table.pdf")
    blocks, quality = parse_pdf(
        path,
        SourceStore(kb_dir),
        ocr=TextOcr("ocr_time_budget_exhausted"),
        force_pages={1} if force else None,
    )
    assert quality[0]["status"] == "needs_review"
    assert any("Source fact" in b.text for b in blocks)


def _missing_docx(path, *, text="Retained instructions."):
    return docx_with_parts(
        path,
        f"<w:p><w:r><w:t>{text}</w:t><w:drawing>"
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        '><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="missing"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>",
        relationships='<Relationship Id="missing" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="../NULL"/>',
    )


def test_reparse_inherits_only_the_same_original_missing_image_locations(
    kb_dir, tmp_path, monkeypatch
):
    import openkb.parsing as parsing

    path = _missing_docx(tmp_path / "missing.docx")
    sources, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    with prepared_input(path) as ready:
        source = sources.intake(ready)
    one = parse_document(kb_dir, source)
    assert not parses.complete(source, one)
    parses.accept_missing_images(source, one)
    previous = parsing.package_version
    monkeypatch.setattr(parsing, "package_version", lambda name: previous(name) + ".test")
    two = parse_document(kb_dir, source, force=True)
    assert two.id != one.id
    assert parses.complete(source, two)
    assert parses.load(one.id) == one
    assert parses.accepted_missing_images(source, two)

    _missing_docx(path, text="Changed original instructions.")
    with prepared_input(path) as ready:
        changed = sources.intake(ready)
    new = parse_document(kb_dir, changed)
    assert not parses.complete(changed, new)


def test_missing_image_inheritance_rejects_new_locations_and_unrelated_failures(kb_dir, tmp_path):
    from openkb.missing_image_reviews import inherit_missing_images

    path = _missing_docx(tmp_path / "missing.docx")
    sources, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    with prepared_input(path) as ready:
        source = sources.intake(ready)
    one = parse_document(kb_dir, source)
    parses.accept_missing_images(source, one)
    drafts = [
        BlockDraft(sources.asset(b.blob).read_text(), b.kind, b.location, b.assets)
        for b in one.blocks
    ]
    extra = parses.save(
        source,
        {"test": "more-missing"},
        [
            *drafts,
            BlockDraft(
                "[Original image unavailable]", "paragraph", {"kind": "docx", "paragraph": 8}
            ),
        ],
        quality=one.quality,
    )
    inherit_missing_images(parses, source, extra)
    assert not parses.accepted_missing_images(source, extra)
    unrelated = parses.save(
        source,
        {"test": "broken-note"},
        drafts,
        quality=[*one.quality, {"status": "needs_review", "reason": "unresolved_docx_note"}],
    )
    inherit_missing_images(parses, source, unrelated)
    assert parses.accepted_missing_images(source, unrelated)
    assert not parses.complete(source, unrelated)


def test_optional_image_budget_stops_across_nested_documents_and_resets_next_run(kb_dir):
    from openkb.docx_images import read_image
    from openkb.ocr.image_session import image_ocr_scope
    from tests.test_docx_images import _png

    class ExhaustedOcr:
        calls = 0

        def page(self, *args, **kwargs):
            self.calls += 1
            return [], "ocr_time_budget_exhausted"

    backend = ExhaustedOcr()
    store = SourceStore(kb_dir)
    with image_ocr_scope(backend) as parent:
        read_image(_png("white"), store, parent)
        with image_ocr_scope(backend) as child:
            for _ in range(4):
                _, assets, _ = read_image(_png("white"), store, child)
                assert store.asset(assets[0]).read_bytes() == _png("white")
            assert child.skipped == 4
            assert len(child.notices()) == 1
        assert backend.calls == 1
    with image_ocr_scope(backend) as next_run:
        read_image(_png("white"), store, next_run)
    assert backend.calls == 2


def test_empty_optional_image_does_not_prevent_recognition_of_later_images(kb_dir):
    from openkb.docx_images import read_image
    from openkb.ocr.image_session import image_ocr_scope
    from tests.test_docx_images import _png

    class EmptyOcr:
        calls = 0

        def page(self, *args, **kwargs):
            self.calls += 1
            return [], "ocr_empty_layout_block"

    backend = EmptyOcr()
    with image_ocr_scope(backend) as session:
        for _ in range(2):
            read_image(_png("white"), SourceStore(kb_dir), session)
        assert not session.notices()
    assert backend.calls == 2


def test_optional_exhaustion_reuses_saved_image_text_without_new_ocr(kb_dir):
    from openkb.docx_images import read_image
    from openkb.ocr.image_session import image_ocr_scope
    from tests.test_docx_images import _png

    class CachedOcr:
        calls = 0
        cache_reads = 0

        def page(self, *args, **kwargs):
            self.calls += 1
            return [], "ocr_time_budget_exhausted"

        def cached_page(self, *args, **kwargs):
            self.cache_reads += 1
            return [
                BlockDraft(
                    "Previously recognized command 9473", "paragraph", {"kind": "pdf", "page": 1}
                )
            ], None

    backend = CachedOcr()
    with image_ocr_scope(backend) as session:
        read_image(_png("white"), SourceStore(kb_dir), session)
        text, _, quality = read_image(_png("black"), SourceStore(kb_dir), session)
        assert "Previously recognized command 9473" in text
        assert not quality and not session.notices()
    assert backend.calls == backend.cache_reads == 1


def test_paid_ocr_cache_remains_readable_after_the_remote_wait_budget(
    kb_dir, tmp_path, monkeypatch
):
    import json

    import requests

    from openkb.config import resolve_effective_config
    from openkb.ocr.cloud import CloudJobs
    from openkb.ocr.config import parsing_settings
    from tests.test_cloud_ocr import cloud_settings

    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    path = _pdf(tmp_path / "cached.pdf")
    store = SourceStore(kb_dir)
    with prepared_input(path) as ready:
        source = store.intake(ready)
    settings = parsing_settings(resolve_effective_config(kb_dir)[0]["parsing"]).ocr.cloud
    requests_seen = []

    def request(self, method, url, **kwargs):
        requests_seen.append(method)
        if method == "POST":
            value = {"code": 0, "data": {"jobId": "test-cache"}}
        elif url.endswith("/test-cache"):
            value = {
                "code": 0,
                "data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": "https://assets.example.test/result.jsonl"},
                },
            }
        else:
            value = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "Source fact: timeout is 42 seconds.",
                                "images": {},
                            },
                            "prunedResult": {
                                "parsing_res_list": [
                                    {
                                        "block_id": 0,
                                        "block_label": "text",
                                        "block_content": "Source fact: timeout is 42 seconds.",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(value).encode()
        result._content_consumed = True
        return result

    monkeypatch.setattr(requests.Session, "request", request)
    jobs = CloudJobs(store, source, settings)
    try:
        with pymupdf.open(path) as document:
            first = jobs.page(document, 1)
            assert first[0] and first[1] is None
            jobs.started -= 10
            second = jobs.page(document, 1)
            assert jobs.cached_page(document, 1) == first
            document[0].insert_text((40, 300), "Not in the cloud cache")
            assert jobs.cached_page(document, 1) is None
        assert second == first
        assert requests_seen == ["POST", "GET", "GET"]
    finally:
        jobs.close()
