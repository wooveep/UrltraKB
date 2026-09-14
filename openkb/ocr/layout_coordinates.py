"""Validate raster layout boxes and map them to unrotated PDF points."""

import math

import pymupdf

from openkb.ocr.cloud import CloudIncomplete


def pdf_block_location(block, pdf_page, number, image_size):
    box = block.get("block_bbox")
    if box is None:
        return None
    width, height = image_size
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(v) not in {int, float} or not math.isfinite(v) for v in box)
        or not (0 <= box[0] <= box[2] <= width and 0 <= box[1] <= box[3] <= height)
        or type(block.get("block_id")) is not int
    ):
        raise CloudIncomplete("ocr_result_coordinates_invalid")
    rendered = pymupdf.Rect(
        box[0] * pdf_page.rect.width / width,
        box[1] * pdf_page.rect.height / height,
        box[2] * pdf_page.rect.width / width,
        box[3] * pdf_page.rect.height / height,
    )
    return {
        "kind": "pdf",
        "page": number,
        "bbox": list(rendered * pdf_page.derotation_matrix),
        "display_bbox": list(rendered),
    }


def cloud_block_locations(pruned, pdf_page, number):
    """Trust only an explicitly unprocessed, full-page raster with valid boxes."""
    layout = pruned.get("parsing_res_list") if isinstance(pruned, dict) else None
    if not isinstance(layout, list) or not any(
        isinstance(block, dict) and "block_bbox" in block for block in layout
    ):
        return {}, None
    unknown = "cloud_layout_coordinates_unverified"
    width, height = pruned.get("width"), pruned.get("height")
    settings = pruned.get("model_settings")
    if (
        not isinstance(settings, dict)
        or settings.get("use_doc_preprocessor") is not False
        or pruned.get("doc_preprocessor_res") is not None
        or type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        # Allow at most one raster pixel of aspect-ratio rounding.
        or abs(width * pdf_page.rect.height - height * pdf_page.rect.width)
        > max(pdf_page.rect.width, pdf_page.rect.height)
    ):
        return {}, unknown
    locations, reason = {}, None
    for block in layout:
        if not isinstance(block, dict):
            continue  # The content validator still rejects malformed layout rows.
        try:
            location = pdf_block_location(block, pdf_page, number, (width, height))
        except CloudIncomplete:
            reason = unknown
        else:
            if location is not None:
                locations[block["block_id"]] = location
    return locations, reason
