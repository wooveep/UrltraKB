"""Explicit self-hosted Paddle protocols; VLM-only output never claims layout completeness."""

import base64
import json
import time
from dataclasses import replace
from pathlib import Path

from openkb.evidence import BlockDraft
from openkb.http_stream import HttpIncomplete, post_bytes
from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.cloud_result import parse_single_page
from openkb.processing import processing_checkpoint


def service_credential(kb_dir: Path, settings):
    from dotenv import dotenv_values

    from openkb import config
    from openkb.config_state import active_values

    if (captured := active_values(kb_dir)) is not None:
        return captured.get("service_credential")
    if not settings or not settings.credential_env:
        return None
    import os

    for layer in (
        dotenv_values(kb_dir / ".env"),
        os.environ,
        dotenv_values(config.GLOBAL_CONFIG_DIR / ".env"),
    ):
        if value := layer.get(settings.credential_env):
            return value
    return None


class PaddleService:
    def __init__(self, store, source, settings, *, credential_root=None):
        self.store, self.source, self.settings = store, source, settings
        self.started = time.monotonic()
        self.pages = 0
        self.key = service_credential(credential_root or store.kb_dir, settings)

    def close(self):
        pass

    def page(self, document, page, **kwargs):
        s = self.settings
        processing_checkpoint("ocr")
        remaining = s.seconds - (time.monotonic() - self.started)
        if remaining <= 0 or self.pages >= s.max_pages:
            return [], "ocr_service_budget_exhausted"
        if s.credential_env and not self.key:
            return [], "ocr_service_credentials_missing"
        self.pages += 1
        raster = document[page - 1].get_pixmap(dpi=120)
        png = raster.tobytes("png")
        if len(png) > 16_000_000:
            return [], "ocr_service_input_too_large"
        encoded = base64.b64encode(png).decode("ascii")
        if s.protocol == "pipeline":
            path = "/layout-parsing"
            body = {
                "file": encoded,
                "fileType": 1,
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useLayoutDetection": True,
                "useOcrForImageBlock": True,
                "mergeTables": False,
                "restructurePages": False,
                "returnMarkdownImages": True,
                "markdownIgnoreLabels": [],
            }
        else:
            path = "/chat/completions"
            body = {
                "model": s.model,
                "max_tokens": s.output_tokens,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "OCR:"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + encoded},
                            },
                        ],
                    }
                ],
            }
        try:
            raw = post_bytes(
                s.endpoint.rstrip("/") + path,
                body,
                seconds=max(0.001, s.seconds - (time.monotonic() - self.started)),
                max_bytes=s.output_bytes,
                headers={"Authorization": "Bearer " + self.key} if self.key else {},
            )
            value = json.loads(raw)
            runtime = {
                **s.profile(),
                "device": "service_managed_unreported",
                "model_identity": "operator_configured",
                "engine": "paddleocr",
            }
            if s.protocol == "vlm":
                choice = value["choices"][0]
                text = choice["message"]["content"]
                if not isinstance(text, str):
                    raise ValueError("invalid")
                status = "ocr_vlm_only_layout_unavailable"
                if choice.get("finish_reason") != "stop":
                    status = "ocr_output_incomplete"
                return [
                    BlockDraft(
                        text,
                        "paragraph",
                        {"kind": "pdf", "page": page},
                        context=json.dumps({"ocr": runtime, "origin": "ocr_transcription"}),
                    )
                ], status
            # Full service has its own single-image contract (page_index is removed
            # by its official API). Bind physical ownership here; never assume jobs.
            outputs = value["result"]["layoutParsingResults"]
            if len(outputs) != 1:
                raise CloudIncomplete("ocr_result_page_set_mismatch")
            output = outputs[0]
            images = output["markdown"].get("images") or {}
            assets = {name: base64.b64decode(data, validate=True) for name, data in images.items()}
            output["markdown"]["images"] = {name: name for name in images}
            blocks, reason = parse_single_page(
                json.dumps(value).encode(), page, self.store, lambda name: assets[name]
            )
            blocks = [
                replace(b, context=json.dumps({"ocr": runtime, "detail": b.context}))
                for b in blocks
            ]
            # The official pipeline response exposes no per-block EOS/usage receipt.
            # Preserve transcription but do not invent a complete generation status.
            return blocks, reason or "ocr_service_completion_unverified"
        except HttpIncomplete as exc:
            return [], "ocr_service_" + str(exc)
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            CloudIncomplete,
        ):
            return [], "ocr_service_result_unavailable"
