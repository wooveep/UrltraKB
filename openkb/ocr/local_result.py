"""Validate the pinned local raster contract before mapping OCR to PDF points."""

from __future__ import annotations

import json
import math

import pymupdf

from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.cloud_result import parse_single_page


def parse_local_page(value, pdf_page, number, image_size, store, download):
    result = value.get("result")
    if not isinstance(result, dict):
        raise CloudIncomplete("ocr_result_invalid")
    # The worker passes one PNG, not a PDF. The pinned pipeline reports null
    # page fields for images; physical page ownership comes from the caller.
    if not {"page_index", "page_count"} <= result.keys() or (
        result["page_index"],
        result["page_count"],
    ) != (None, None):
        raise CloudIncomplete("ocr_result_page_set_mismatch")
    width, height = image_size
    if (result.get("width"), result.get("height")) != image_size or min(image_size) <= 0:
        raise CloudIncomplete("ocr_result_image_size_mismatch")
    layout, locations = result.get("parsing_res_list"), {}
    if not isinstance(layout, list):
        raise CloudIncomplete("ocr_layout_unverified")
    for block in layout:
        if not isinstance(block, dict):
            raise CloudIncomplete("ocr_result_invalid")
        box = block.get("block_bbox")
        if box is None:
            continue
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
        locations[block["block_id"]] = {
            "kind": "pdf",
            "page": number,
            "bbox": list(rendered * pdf_page.derotation_matrix),
        }
    payload = {
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {
                        "text": value.get("markdown"),
                        "images": {name: name for name in value["assets"]},
                    },
                    "prunedResult": result,
                }
            ]
        }
    }
    return parse_single_page(
        json.dumps(payload).encode(), number, store, download, block_locations=locations
    )
