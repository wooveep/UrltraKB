"""Retain DOCX images and attempt OCR only for plausible text-bearing images."""

from __future__ import annotations

import io
import re

from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore, content_id


def ocr_candidate(image) -> bool:
    """Skip tiny icons, narrow toolbars and flat fills before invoking OCR.

    This is a cheap size/contrast heuristic, not a claim that every remaining
    image contains text. Empty recognition is an advisory result.
    """
    if min(image.size) <= 48 or max(image.size) < 160:
        return False
    low, high = image.convert("L").getextrema()
    return high - low >= 24


def read_image(content: bytes, store: SourceStore, ocr=None, *, alt_text="Original image"):
    original = store.put_bytes(content)
    assets = [original]
    text = f"![{alt_text}](asset:{original})"
    import pymupdf
    from PIL import Image, ImageOps, UnidentifiedImageError

    quality = []
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
                if index == 0:
                    text = f"![{alt_text}](asset:{preview})"
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
                with pymupdf.open() as document:
                    page = document.new_page(width=image.width, height=image.height)
                    page.insert_image(page.rect, stream=rendered)
                    identity = content_id(
                        {"docx_image": original, "frame": index, "rendered": preview}
                    )
                    with progress_scope("image_ocr"):
                        blocks, reason = ocr.page(document, 1, input_id=identity)
                previous_context = None
                for block in blocks:
                    assets.extend(asset for asset in block.assets if asset not in assets)
                    if block.kind == "image" and re.fullmatch(
                        r"!\[[^\]]*\]\(asset:[0-9a-f]{64}\)", block.text
                    ):
                        continue
                    if block.context != previous_context:
                        text += f"\n[Image frame {index + 1}; {block.context}]"
                        previous_context = block.context
                    text += "\n" + block.text
                if reason or not blocks:
                    quality.append(
                        {
                            "status": "verified",
                            "reason": "docx_image_ocr_notice:"
                            + original
                            + ":"
                            + (reason or "ocr_content_unverified"),
                        }
                    )
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        reason = str(exc) if str(exc).startswith("docx_") else "docx_image_render_unavailable"
        quality.append(
            {"status": "verified", "reason": "docx_image_ocr_notice:" + original + ":" + reason}
        )
    return text, tuple(assets), quality
