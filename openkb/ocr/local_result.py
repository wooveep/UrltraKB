"""Validate the pinned local raster contract before mapping OCR to PDF points."""

from __future__ import annotations

import json

from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.cloud_result import parse_single_page
from openkb.ocr.layout_coordinates import pdf_block_location


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
    if (result.get("width"), result.get("height")) != image_size or min(image_size) <= 0:
        raise CloudIncomplete("ocr_result_image_size_mismatch")
    layout, locations = result.get("parsing_res_list"), {}
    if not isinstance(layout, list):
        raise CloudIncomplete("ocr_layout_unverified")
    for block in layout:
        if not isinstance(block, dict):
            raise CloudIncomplete("ocr_result_invalid")
        location = pdf_block_location(block, pdf_page, number, image_size)
        if location is not None:
            locations[block["block_id"]] = location
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
