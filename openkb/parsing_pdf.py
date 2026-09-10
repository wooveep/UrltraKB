"""Physical PDF positions, native text and explicit uncertain-page ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from openkb.evidence import BlockDraft
from openkb.parsing_pdf_tables import table_cells
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore


def parse_pdf(
    path: Path, store: SourceStore, *, ocr=None, force_pages: set[int] | None = None, reuse=None
) -> tuple[list[BlockDraft], list[dict[str, Any]]]:
    blocks: list[BlockDraft] = []
    quality: list[dict[str, Any]] = []
    with (
        pymupdf.open(path) as document,
        progress_scope("pdf", document.page_count, "pages") as progress,
    ):
        if document.needs_pass:
            raise ValueError("Encrypted PDF requires an unlocked input copy")
        for number, page in enumerate(document, 1):
            processing_checkpoint("parsing")
            if number in (reuse or {}):
                previous_blocks, previous_quality = reuse[number]
                blocks.extend(previous_blocks)
                quality.append(previous_quality)
                progress.advance()
                continue
            reason = None
            verified_by = "native_text_layer"
            native = []
            try:
                cells, tables = table_cells(page, number)
            except Exception:
                cells, tables = [], []
                reason = "native_table_structure_uncertain"
            graphics = _uncovered_graphics(page, tables)
            if graphics:
                reason = "image_content_requires_ocr"
            for block in page.get_text("dict", sort=True)["blocks"]:
                location = {"kind": "pdf", "page": number, "bbox": list(block["bbox"])}
                if block["type"] == 0:
                    if any(
                        pymupdf.Rect(table).contains(pymupdf.Rect(block["bbox"]))
                        for table in tables
                    ):
                        continue
                    text = "\n".join(
                        "".join(span["text"] for span in line["spans"]) for line in block["lines"]
                    )
                    if text.strip():
                        native.append(BlockDraft(text, "paragraph", location))
                        if "\ufffd" in text or "\x00" in text:
                            reason = "unmapped_native_glyphs"
                elif block["type"] == 1:
                    image = block.get("image")
                    if not image:
                        reason = "missing_image_bytes"
                        continue
                    pixmap = pymupdf.Pixmap(image)
                    if pixmap.n > 4:
                        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
                    digest = store.put_bytes(pixmap.tobytes("png"))
                    native.append(
                        BlockDraft(
                            f"![Original image](asset:{digest})", "image", location, (digest,)
                        )
                    )
                    reason = "image_content_requires_ocr"
            native.extend(cells)
            native.sort(key=lambda block: (block.location["bbox"][1], block.location["bbox"][0]))
            if not native:
                reason = "blank_or_illustration"
            elif any(trace.get("type") == 3 for trace in page.get_texttrace()):
                reason = "invisible_text_layer"
            if number in (force_pages or set()):
                reason = "explicit_page_reprocessing"
            # A bitmap/empty/invisible text layer is not evidence of a reliable
            # extraction. Preserve the original visual for OCR or human review.
            if reason:
                digest = store.put_bytes(page.get_pixmap().tobytes("png"))
                if ocr is not None:
                    recognized, ocr_reason = ocr.page(document, number)
                    if recognized:
                        native = recognized
                    reason = ocr_reason or (None if recognized else "ocr_missing_page")
                    if not reason:
                        verified_by = "ocr_layout_and_assets"
                        if graphics and not any(block.assets for block in recognized):
                            reason = "image_content_requires_ocr"
                native.append(
                    BlockDraft(
                        f"![Original page {number}](asset:{digest})",
                        "image",
                        {"kind": "pdf", "page": number},
                        (digest,),
                    )
                )
            blocks.extend(native)
            quality.append(
                {
                    "page": number,
                    "status": "needs_review" if reason else "verified",
                    "reason": reason or verified_by,
                }
            )
            progress.advance()
    return blocks, quality


def _uncovered_graphics(page, tables):
    """Only an extracted table's unfilled grid is accounted for by text cells."""
    for path in page.get_drawings():
        grid = path.get("fill") is None and all(
            item[0] == "re"
            or (item[0] == "l" and (item[1].x == item[2].x or item[1].y == item[2].y))
            for item in path["items"]
        )
        if grid and any(
            (pymupdf.Rect(table) + (-1, -1, 1, 1)).contains(path["rect"]) for table in tables
        ):
            continue
        return True
    return False
