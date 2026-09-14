"""Retain DOCX images and attempt OCR only for plausible text-bearing images."""

from __future__ import annotations

import io
import re
from typing import Any

from openkb.ocr.eligibility import ocr_candidate
from openkb.ocr.image_session import ImageOcrSession
from openkb.ocr.optional import recognize
from openkb.ocr.transcription_quality import transcribed_text
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore, content_id


def read_image(content: bytes, store: SourceStore, ocr=None, *, alt_text=None, relations=None):
    original = store.put_bytes(content)
    assets = [original]
    label = alt_text or "Original image"
    text = f"![{label}](asset:{original})"
    relation: dict[str, Any] = {"original_asset": original, "frames": []}
    if alt_text is not None:
        relation["source_alt"] = alt_text
    if relations is not None:
        relations.append(relation)
    import pymupdf
    from PIL import Image, ImageOps, UnidentifiedImageError

    quality: list[dict[str, Any]] = []
    transcriptions = set()
    complete_frames = 0
    try:
        with Image.open(io.BytesIO(content)) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > 4096:
                raise ValueError("docx_image_frame_budget_exceeded")
            for index in range(frames):
                processing_checkpoint("parsing")
                image.seek(index)
                if image.width * image.height > 64_000_000:
                    raise ValueError("docx_image_pixel_budget_exceeded")
                frame = ImageOps.exif_transpose(image).convert("RGBA")
                background = Image.new("RGBA", frame.size, "white")
                background.alpha_composite(frame)
                output = io.BytesIO()
                background.convert("RGB").save(output, format="PNG")
                rendered = output.getvalue()
                preview = store.put_bytes(rendered)
                if preview not in assets:
                    assets.append(preview)
                frame_relation: dict[str, Any] = {
                    "number": index + 1,
                    "asset": preview,
                    "ocr_assets": [],
                }
                relation["frames"].append(frame_relation)
                if index == 0:
                    text = f"![{label}](asset:{preview})"
                if not ocr_candidate(background):
                    quality.append(
                        {"status": "verified", "reason": "docx_image_ocr_skipped:" + original}
                    )
                    continue
                if ocr is None:
                    quality.append(
                        {
                            "status": "verified",
                            "reason": "docx_image_ocr_notice:" + original + ":ocr_unavailable",
                        }
                    )
                    continue
                if isinstance(ocr, ImageOcrSession) and ocr.skip_optional():
                    continue
                with pymupdf.open() as document:
                    page = document.new_page(width=image.width, height=image.height)
                    page.insert_image(page.rect, stream=rendered)
                    identity = content_id(
                        {"docx_image": original, "frame": index, "rendered": preview}
                    )
                    with progress_scope("image_ocr"):
                        blocks, reason = recognize(ocr, document, 1, input_id=identity)
                previous_context = None
                for block in blocks:
                    assets.extend(asset for asset in block.assets if asset not in assets)
                    frame_relation["ocr_assets"].extend(
                        asset for asset in block.assets if asset not in frame_relation["ocr_assets"]
                    )
                    if block.kind == "image" and re.fullmatch(
                        r"!\[[^\]]*\]\(asset:[0-9a-f]{64}\)", block.text
                    ):
                        continue
                    if block.context != previous_context:
                        text += f"\n[Image frame {index + 1}; {block.context}]"
                        previous_context = block.context
                    text += "\n" + block.text
                if reason == "ocr_optional_image_skipped" and isinstance(ocr, ImageOcrSession):
                    continue
                if reason or not any(transcribed_text(block.text) for block in blocks):
                    quality.append(
                        {
                            "status": "verified",
                            "reason": "docx_image_ocr_notice:"
                            + original
                            + ":"
                            + (reason or "ocr_content_unverified"),
                        }
                    )
                else:
                    complete_frames += 1
                    transcriptions.add(preview)
                    for block in blocks:
                        if (
                            block.context.startswith("OCR layout block")
                            and "transcription=pending" not in block.context
                            and transcribed_text(block.text)
                        ):
                            transcriptions.update(block.assets)
            if complete_frames == frames:
                transcriptions.add(original)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        reason = str(exc) if str(exc).startswith("docx_") else "docx_image_render_unavailable"
        quality.append(
            {"status": "verified", "reason": "docx_image_ocr_notice:" + original + ":" + reason}
        )
    if transcriptions:
        quality.append(
            {
                "status": "verified",
                "reason": "docx_image_transcription_available",
                "transcriptions": sorted(transcriptions),
            }
        )
    return text, tuple(assets), quality
