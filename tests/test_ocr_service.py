"""Deployed OCR setup uses a built-in image and owns its finite HTTP wait."""

import asyncio
import json
import time

import httpx

from openkb.application.ocr_capability import check_ocr_service
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest


def service_config(kb_dir, seconds=2.0):
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "parsing": {
                    "ocr": {
                        "backend": "local",
                        "execution": "service",
                        "service": {
                            "endpoint": "http://127.0.0.1:8123/v1",
                            "protocol": "vlm",
                            "seconds": seconds,
                        },
                    }
                }
            },
        ),
    )


def test_deployed_service_check_uses_sample_and_reports_vlm_layout_limit(kb_dir, monkeypatch):
    service_config(kb_dir)
    calls = []

    async def send(client, request, **kwargs):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "OpenKB system test. The red switch controls the pump."
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    before = sorted(p.as_posix() for p in kb_dir.rglob("*"))
    result = check_ocr_service(kb_dir)
    assert result["status"] == "connected_needs_review"
    assert result["reason"] == "ocr_vlm_only_layout_unavailable"
    assert result["device"] == "service_managed_unreported"
    assert calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;")
    assert sorted(p.as_posix() for p in kb_dir.rglob("*")) == before


def test_deployed_service_deadline_closes_dripping_response(kb_dir, monkeypatch):
    service_config(kb_dir, seconds=0.15)
    closed = []

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            closed.append(True)

    async def send(client, request, **kwargs):
        return httpx.Response(200, request=request, stream=Drip())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    started = time.monotonic()
    result = check_ocr_service(kb_dir)
    assert result["status"] == "not_ready"
    assert "time_budget_exhausted" in result["reason"]
    assert time.monotonic() - started < 1.0
    assert closed == [True]
