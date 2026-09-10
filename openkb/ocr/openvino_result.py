"""Strict adapter for complete OpenVINO blocks, independent of the native contract."""

import json
import math

import pymupdf

from openkb.evidence import BlockDraft
from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.cloud_result import _references, _rewrite_assets, verify_image


def parse_openvino_page(value, pdf_page, number, image_size, store, download):
    try:
        result = value["result"]
        runtime = value["runtime"]
        if (
            result["contract"] != "openkb-openvino-page-v1"
            or result["physical_page"] != number
            or type(result["physical_page"]) is not int
        ):
            raise CloudIncomplete("ocr_result_page_set_mismatch")
        if (
            runtime["runtime"] != "openvino"
            or runtime["model"] != "PaddleOCR-VL-1.5"
            or set(runtime["devices"])
            != {"layout", "vision", "embedding", "language", "projection"}
            or any(not isinstance(d, str) or not d for d in runtime["devices"].values())
        ):
            raise CloudIncomplete("ocr_result_identity_mismatch")
        width, height = image_size
        if (result["width"], result["height"]) != image_size or min(image_size) <= 0:
            raise CloudIncomplete("ocr_result_image_size_mismatch")
        rows = result["blocks"]
        if not isinstance(rows, list):
            raise CloudIncomplete("ocr_layout_unverified")
        identities, orders, blocks, assets, reasons = set(), [], [], {}, []
        for name in value["assets"]:
            content = download(name)
            verify_image(content)
            assets[name] = store.put_bytes(content)
        for row in rows:
            identifier, order = row["id"], row["order"]
            if (
                type(identifier) is not int
                or identifier < 0
                or identifier in identities
                or type(order) is not int
                or order < 0
            ):
                raise CloudIncomplete("ocr_result_block_identity_invalid")
            identities.add(identifier)
            orders.append(order)
            box = row["bbox"]
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(v) not in {int, float} or not math.isfinite(v) for v in box)
                or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
            ):
                raise CloudIncomplete("ocr_result_coordinates_invalid")
            text, label, status = row["text"], row["label"], row["status"]
            if (
                not isinstance(text, str)
                or not isinstance(label, str)
                or not label
                or status not in {"completed", "empty", "length", "failed", "unfinished"}
                or type(row["tokens"]) is not int
                or row["tokens"] < 0
            ):
                raise CloudIncomplete("ocr_output_contract_unknown")
            names = row["assets"]
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise CloudIncomplete("ocr_result_assets_invalid")
            if any(name not in assets for name in set(names) | _references(text)):
                raise CloudIncomplete("ocr_required_asset_missing")
            if label in {"image", "header_image", "footer_image", "seal", "chart"} and not names:
                reasons.append("ocr_required_asset_missing")
            if status in {"length", "unfinished"}:
                reasons.append("ocr_output_incomplete")
            elif status == "failed":
                reasons.append("ocr_block_failed")
            elif status == "completed" and not text.strip():
                reasons.append("ocr_empty_layout_block")
            elif status == "empty" and text.strip():
                raise CloudIncomplete("ocr_output_contract_unknown")
            elif status == "empty":
                reasons.append("ocr_blank_or_illustration")
            text = _rewrite_assets(text, assets)
            for name in names:
                if assets[name] not in text:
                    text += f"\n![{label}](asset:{assets[name]})"
            rendered = pymupdf.Rect(
                box[0] * pdf_page.rect.width / width,
                box[1] * pdf_page.rect.height / height,
                box[2] * pdf_page.rect.width / width,
                box[3] * pdf_page.rect.height / height,
            )
            location = {
                "kind": "pdf",
                "page": number,
                "bbox": list(rendered * pdf_page.derotation_matrix),
            }
            kind = (
                "heading"
                if label in {"title", "doc_title", "paragraph_title"}
                else "table"
                if label == "table"
                else "image"
                if not row["text"].strip() and names
                else "paragraph"
            )
            if text.strip():
                blocks.append(
                    BlockDraft(
                        text,
                        kind,
                        location,
                        tuple(assets[n] for n in names),
                        json.dumps(
                            {
                                "origin": "ocr_transcription",
                                "ocr": runtime,
                                "block": identifier,
                                "label": label,
                                "status": status,
                                "tokens": row["tokens"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
        if orders != list(range(len(rows))):
            raise CloudIncomplete("ocr_result_block_order_invalid")
        return blocks, ";".join(dict.fromkeys(reasons)) or (
            None if rows else "ocr_layout_unverified"
        )
    except (ValueError, TypeError, KeyError, IndexError):
        raise CloudIncomplete("ocr_result_invalid") from None
