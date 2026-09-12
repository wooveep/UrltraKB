"""Physical PDF positions, native text and explicit uncertain-page ownership."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pymupdf

from openkb.evidence import BlockDraft
from openkb.ocr.eligibility import decorative_path, ocr_candidate
from openkb.ocr.optional import recognize
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
            try:
                reason = None
                verified_by = "native_text_layer"
                native = []
                image_candidate = False
                try:
                    cells, tables = table_cells(page, number)
                except Exception:
                    cells, tables = [], []
                    reason = "native_table_structure_uncertain"
                graphics = _page_read(_uncovered_graphics, page, tables)
                if graphics:
                    reason = reason or "image_content_requires_ocr"
                for block in _page_read(page.get_text, "dict", sort=True)["blocks"]:
                    location = {"kind": "pdf", "page": number, "bbox": list(block["bbox"])}
                    if block["type"] == 0:
                        if any(
                            pymupdf.Rect(table).contains(pymupdf.Rect(block["bbox"]))
                            for table in tables
                        ):
                            continue
                        text = "\n".join(
                            "".join(span["text"] for span in line["spans"])
                            for line in block["lines"]
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
                        pixmap = _page_read(pymupdf.Pixmap, image)
                        if pixmap.n > 4:
                            pixmap = _page_read(pymupdf.Pixmap, pymupdf.csRGB, pixmap)
                        digest = store.put_bytes(_page_read(pixmap.tobytes, "png"))
                        native.append(
                            BlockDraft(
                                f"![Original image](asset:{digest})", "image", location, (digest,)
                            )
                        )
                        from PIL import Image

                        with Image.open(io.BytesIO(_page_read(pixmap.tobytes, "png"))) as picture:
                            image_candidate |= ocr_candidate(picture)
                native.extend(cells)
                native.sort(
                    key=lambda block: (block.location["bbox"][1], block.location["bbox"][0])
                )
                readable_text = any(
                    block.kind != "image" and block.text.strip() for block in native
                )
                if image_candidate or (native and not readable_text):
                    reason = reason or "image_content_requires_ocr"
                if not native:
                    reason = reason or "blank_or_illustration"
                elif any(trace.get("type") == 3 for trace in _page_read(page.get_texttrace)):
                    reason = "invisible_text_layer"
                optional_reasons = {None, "image_content_requires_ocr", "blank_or_illustration"}
                if number in (force_pages or set()) and reason in optional_reasons:
                    reason = "explicit_page_reprocessing"
                # A bitmap/empty/invisible text layer is not evidence of a reliable
                # extraction. Preserve the original visual for OCR or human review.
                if reason or _page_read(page.get_drawings):
                    digest = store.put_bytes(_page_read(lambda: page.get_pixmap().tobytes("png")))
                    if reason and (reason != "blank_or_illustration" or ocr is not None):
                        original_reason = reason
                        recognized, ocr_reason = recognize(ocr, document, number)
                        if recognized:
                            supplemental = bool(ocr_reason) or original_reason in {
                                "image_content_requires_ocr",
                                "explicit_page_reprocessing",
                            }
                            if supplemental:
                                native_text = {
                                    " ".join(b.text.split()) for b in native if b.kind != "image"
                                }
                                native.extend(
                                    b
                                    for b in recognized
                                    if b.kind == "image"
                                    or " ".join(b.text.split()) not in native_text
                                )
                            else:
                                native = [*recognized, *(b for b in native if b.kind == "image")]
                        reason = ocr_reason or (None if recognized else "ocr_missing_page")
                        if not reason:
                            verified_by = "ocr_layout_and_assets"
                        elif original_reason in {
                            "image_content_requires_ocr",
                            "explicit_page_reprocessing",
                        }:
                            verified_by = "pdf_image_ocr_notice:" + reason
                            if readable_text or not any(
                                part in reason
                                for part in (
                                    "output_incomplete",
                                    "block_failed",
                                    "completion_unverified",
                                    "vlm_only",
                                    "windows_ocr_sparse",
                                    "windows_ocr_no_text",
                                )
                            ):
                                reason = None
                    native.append(
                        BlockDraft(
                            f"![Original page {number}](asset:{digest})",
                            "image",
                            {"kind": "pdf", "page": number},
                            (digest,),
                            (
                                "Original visual for this physical page; associated text appears "
                                "in the same page's source blocks. OCR is supplementary; "
                                "unrecognized image text is unknown. " + verified_by
                            ),
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
            except _PageContentError as exc:
                # These are parser/document failures. OSError, cancellation and
                # execution deadlines still propagate to their owning operation.
                quality.append(
                    {
                        "page": number,
                        "status": "needs_review",
                        "reason": "pdf_page_unparsed:" + str(exc),
                    }
                )
                blocks.append(
                    BlockDraft(
                        f"[Original PDF page {number} could not be parsed; content unknown]",
                        "image",
                        {"kind": "pdf", "page": number},
                    )
                )
            progress.advance()
    return blocks, quality


def _uncovered_graphics(page, tables):
    """Only an extracted table's unfilled grid is accounted for by text cells."""
    for path in page.get_drawings():
        if decorative_path(path, page.rect):
            continue
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


class _PageContentError(Exception):
    pass


def _page_read(operation, *args, **kwargs):
    """Only the native parser boundary converts document errors to omissions."""
    try:
        return operation(*args, **kwargs)
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise _PageContentError(type(exc).__name__) from exc
