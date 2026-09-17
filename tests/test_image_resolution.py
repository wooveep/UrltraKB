"""Small source images remain available without OCR or visual-model requests."""

import io

import httpx
import pytest
from PIL import Image, ImageDraw

from openkb.application.image_understanding import test_image_connection
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.docx_images import read_image
from openkb.evidence import BlockDraft
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from openkb.vision.session import VisionSession


@pytest.fixture(autouse=True)
def isolated_global_config(tmp_path, monkeypatch):
    from openkb import config

    root = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", root / "global.lock")


def picture(size):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((4, 4), "Status: complete", fill="black")
    data = io.BytesIO()
    image.save(data, format="PNG", dpi=(300, 300))
    return data.getvalue()


@pytest.mark.parametrize("size", [(240, 120), (300, 50)])
def test_small_docx_image_keeps_original_without_ocr(kb_dir, size):
    class Ocr:
        calls = 0

        def page(self, *args, **kwargs):
            self.calls += 1
            return [], None

    ocr = Ocr()
    data = picture(size)
    store = SourceStore(kb_dir)
    text, assets, checks = read_image(data, store, ocr, alt_text="Recovery status")
    assert ocr.calls == 0
    assert store.asset(assets[0]).read_bytes() == data
    assert "Recovery status" in text and "asset:" in text
    assert any("low_resolution" in row["reason"] for row in checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("size,region", [((240, 120), None), ((640, 480), [0, 0, 240, 120])])
async def test_small_image_or_crop_does_not_request_visual_model(kb_dir, monkeypatch, size, region):
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            image_api_key="fixture-key",
            config={
                "image_understanding": {
                    "enabled": True,
                    "supports_images": True,
                    "provider": "openai-compatible",
                    "model": "fixture-vision",
                    "endpoint": "http://127.0.0.1:8123/v1",
                }
            },
        ),
    )
    requests = []

    async def send(client, request, **kwargs):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "Red square."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    assert (await test_image_connection(kb_dir))["status"] == "ready"
    data = picture(size)
    path = kb_dir / "wiki/sources/images/status.png"
    path.write_bytes(data)
    result = await VisionSession(kb_dir).analyze(
        "sources/images/status.png", "Read the status", region
    )
    assert result["status"] == "image_low_resolution_skipped"
    assert len(requests) == 1  # Only the explicit connection-test sample.
    assert path.read_bytes() == data


@pytest.mark.parametrize("other_content", [None, "large_image", "drawing"])
def test_small_pdf_image_does_not_trigger_page_ocr_on_its_own(
    kb_dir, tmp_path, monkeypatch, other_content
):
    import pymupdf

    import openkb.parsing as parsing

    file = tmp_path / "small.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_image(pymupdf.Rect(20, 20, 260, 140), stream=picture((240, 120)))
        if other_content == "large_image":
            page.insert_image(pymupdf.Rect(20, 200, 420, 400), stream=picture((400, 200)))
        elif other_content == "drawing":
            page.draw_circle(pymupdf.Point(200, 300), 50)
        document.save(file)

    class Ocr:
        calls = 0

        def page(self, *args, **kwargs):
            self.calls += 1
            return [BlockDraft("Recognized text", "paragraph", {"kind": "pdf", "page": 1})], None

        def close(self):
            pass

    ocr = Ocr()
    monkeypatch.setattr(parsing, "create_ocr", lambda *a, **k: ocr)
    store = SourceStore(kb_dir)
    with prepared_input(file) as ready:
        source = store.intake(ready)
    parsed = parse_document(kb_dir, source)
    assert ocr.calls == (1 if other_content else 0)
    assert any(block.assets for block in parsed.blocks)
    if other_content is None:
        assert any("low_resolution" in row["reason"] for row in parsed.quality)
