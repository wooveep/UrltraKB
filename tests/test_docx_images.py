"""Image content, explicit missing-image decisions and native OCR cache isolation."""

import io

import pymupdf
import pytest
from PIL import Image, ImageDraw

from openkb.docx_images import read_image
from openkb.evidence import BlockDraft, ParseStore
from openkb.inputs import prepared_input
from openkb.ocr.windows import WindowsOcr
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx


def _png(color, *, size=(240, 120), text=True):
    stream = io.BytesIO()
    picture = Image.new("RGB", size, color)
    if text:
        ImageDraw.Draw(picture).text(
            (10, 10), "Restart control 9473", fill="black" if color == "white" else "white"
        )
    picture.save(stream, format="PNG")
    return stream.getvalue()


def test_docx_image_without_ocr_retains_original_and_reports_an_advisory(kb_dir):
    store = SourceStore(kb_dir)
    text, assets, quality = read_image(_png("white"), store)
    assert "asset:" in text and store.asset(assets[0]).read_bytes() == _png("white")
    assert quality[0]["status"] == "verified"
    assert quality[0]["reason"].endswith(":ocr_unavailable")


def test_native_ocr_cache_distinguishes_images_at_the_same_page_number(
    kb_dir, tmp_path, monkeypatch
):
    from openkb.ocr import windows

    profile = {
        "backend": "test-native",
        "language": "zh-Hans-CN",
        "engine_version": "test",
        "render_dpi": 144,
        "max_dimension": 10000,
    }
    monkeypatch.setattr(windows, "runtime_profile", lambda: profile)
    calls = []

    def recognize(plan, root, **kwargs):
        with Image.open(plan["input"]) as image:
            width, height = image.size
        calls.append(plan["input"])
        return {
            "info": {k: profile[k] for k in ("language", "engine_version", "max_dimension")},
            "width": width,
            "height": height,
            "lines": [
                {
                    "text": f"Recognized image {len(calls)}",
                    "words": [{"text": "Recognized", "box": [1, 1, 20, 10]}],
                }
            ],
        }

    monkeypatch.setattr(windows, "_run", recognize)
    file = tmp_path / "source.docx"
    write_docx(file, "<w:p><w:r><w:t>Source.</w:t></w:r></w:p>")
    store = SourceStore(kb_dir)
    with prepared_input(file) as ready:
        source = store.intake(ready)
    ocr = WindowsOcr(store, source)
    one = read_image(_png("white"), store, ocr)
    two = read_image(_png("black"), store, ocr)
    repeated = read_image(_png("white"), store, ocr)
    assert "Recognized image 1" in one[0]
    assert "Recognized image 2" in two[0]
    assert repeated == one and len(calls) == 2
    assert not one[2] and not two[2]
    retry = WindowsOcr(store, source, retries={1: "explicit-retry"})
    assert "Recognized image 3" in read_image(_png("white"), store, retry)[0]
    assert len(calls) == 3


def test_missing_image_permission_is_exact_and_cannot_waive_ocr(kb_dir, tmp_path):
    file = tmp_path / "source.docx"
    write_docx(file, "<w:p><w:r><w:t>Source.</w:t></w:r></w:p>")
    originals = SourceStore(kb_dir)
    with prepared_input(file) as ready:
        version = originals.intake(ready)
    store = ParseStore(kb_dir)
    blocks = [
        BlockDraft("[Original image unavailable]", "paragraph", {"kind": "docx", "paragraph": 1})
    ]
    missing = {"status": "needs_review", "reason": "docx_image_asset_missing"}
    one = store.save(version, {"parser": "test"}, blocks, quality=[missing])
    assert not store.complete(version, one)
    store.accept_missing_images(version, one)
    assert store.complete(version, one)
    changed = store.save(version, {"parser": "changed"}, blocks, quality=[missing])
    assert not store.complete(version, changed)
    with_ocr = store.save(
        version,
        {"parser": "ocr"},
        blocks,
        quality=[missing, {"status": "needs_review", "reason": "docx_image_requires_ocr"}],
    )
    store.accept_missing_images(version, with_ocr)
    assert not store.complete(version, with_ocr)
    complete = parse_document(kb_dir, version)
    with pytest.raises(ValueError, match="No matching"):
        store.accept_missing_images(version, complete)


def test_native_ocr_rejects_word_bounds_outside_the_original_image(kb_dir, tmp_path, monkeypatch):
    from openkb.ocr import windows

    profile = {"language": "en-US", "engine_version": "test", "max_dimension": 10000}
    monkeypatch.setattr(windows, "runtime_profile", lambda: profile)
    ocr = WindowsOcr(SourceStore(kb_dir), None)
    value = {
        "info": profile,
        "width": 100,
        "height": 100,
        "lines": [{"text": "Outside", "words": [{"text": "Outside", "box": [90, 90, 50, 50]}]}],
    }
    with pytest.raises(ValueError, match="word_invalid"):
        ocr._blocks(value, 1, pymupdf.Rect(0, 0, 100, 100), 100, 100, "0" * 64)


@pytest.mark.parametrize("size,text", [((16, 16), True), ((350, 43), True), ((400, 200), False)])
def test_icons_toolbars_and_flat_images_skip_ocr(kb_dir, size, text):
    class NoOcr:
        def page(self, *args, **kwargs):
            pytest.fail("Non-text candidate must not invoke OCR")

    store = SourceStore(kb_dir)
    data = _png("white", size=size, text=text)
    _, assets, quality = read_image(data, store, NoOcr())
    assert store.asset(assets[0]).read_bytes() == data
    assert quality[0]["status"] == "verified"
    assert quality[0]["reason"].startswith("docx_image_ocr_skipped:")


def test_empty_ocr_is_advisory_for_a_retained_docx_image(kb_dir):
    class EmptyOcr:
        def page(self, *args, **kwargs):
            return [], "windows_ocr_no_text_requires_review"

    store = SourceStore(kb_dir)
    _, assets, quality = read_image(_png("white"), store, EmptyOcr())
    assert assets and all(q["status"] == "verified" for q in quality)
    assert quality[0]["reason"].startswith("docx_image_ocr_notice:")


def test_missing_docx_relationship_keeps_text_and_visible_marker(kb_dir, tmp_path):
    from tests.docx_attachment_fixtures import docx_with_parts

    file = docx_with_parts(
        tmp_path / "missing.docx",
        "<w:p><w:r><w:t>Retained instructions.</w:t><w:drawing>"
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="missing"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>",
        relationships='<Relationship Id="missing" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="../NULL"/>',
    )
    originals = SourceStore(kb_dir)
    with prepared_input(file) as ready:
        source = originals.intake(ready)
    parsed = parse_document(kb_dir, source)
    text = "\n".join(originals.asset(b.blob).read_text() for b in parsed.blocks)
    assert "Retained instructions." in text and "[Original image unavailable]" in text
    parses = ParseStore(kb_dir)
    assert not parses.complete(source, parsed)
    parses.accept_missing_images(source, parsed)
    assert parses.complete(source, parsed)


def test_windows_ocr_process_is_reaped_when_cancelled(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import Mock

    from openkb.cancellation import OperationCancelled
    from openkb.ocr import windows

    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("ocr", 2), 0]
    monkeypatch.setattr(windows.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setenv("SystemRoot", str(tmp_path))

    def stop(*args):
        raise OperationCancelled()

    monkeypatch.setattr(windows, "processing_checkpoint", stop)
    with pytest.raises(OperationCancelled):
        windows._run({"action": "info"}, tmp_path)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
