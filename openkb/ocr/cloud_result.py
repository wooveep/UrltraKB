"""Validate one jobs result and own every required image before checkpointing."""

from __future__ import annotations

import io
import json
import math
import re

from PIL import Image

from openkb.evidence import BlockDraft
from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.transcription_quality import repetitive_transcription


def verify_image(content: bytes) -> None:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
    except (ValueError, OSError):
        raise CloudIncomplete("cloud_required_asset_invalid") from None


def parse_single_page(raw, page, store, download, *, block_locations=None, pdf_page=None):
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        outputs = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("result"), dict):
                raise ValueError("Invalid JSONL row")
            pages = row["result"].get("layoutParsingResults")
            if not isinstance(pages, list):
                raise ValueError("Invalid page collection")
            outputs.extend(pages)
        if len(outputs) != 1 or not isinstance(outputs[0], dict):
            raise CloudIncomplete("cloud_result_page_set_mismatch")
        output = outputs[0]
        markdown = output.get("markdown")
        if not isinstance(markdown, dict) or not isinstance(markdown.get("text"), str):
            raise ValueError("Invalid page Markdown")
        text = markdown["text"]
        images = markdown.get("images", {})
        if not isinstance(images, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in images.items()
        ):
            raise ValueError("Invalid image mapping")
        pruned = output.get("prunedResult")
        layout = pruned.get("parsing_res_list") if isinstance(pruned, dict) else None
        reason = None
        if pdf_page is not None:
            from openkb.ocr.layout_coordinates import cloud_block_locations

            block_locations, reason = cloud_block_locations(pruned, pdf_page, page)
        if isinstance(layout, list):
            layout = [dict(block) if isinstance(block, dict) else block for block in layout]
            for block in layout:
                if not isinstance(block, dict) or not isinstance(block.get("block_content"), str):
                    raise ValueError("Invalid layout content")
                references = _layout_references(block, images)
                if block.get("block_label") in {"image", "figure"} and not references:
                    # A caption, empty layout item or unrelated page image
                    # cannot prove this required visual was downloaded.
                    raise CloudIncomplete("cloud_required_asset_missing")
                if block.get("block_label") in {"image", "figure"} and repetitive_transcription(
                    block["block_content"]
                ):
                    reason = "ocr_repetitive_transcription"
                    marker = "[OCR transcription omitted: repetitive output]"
                    if block["block_content"] not in text:
                        # The raw response remains retained. An unmatched page
                        # rendition cannot safely contribute a second copy.
                        text = "\n".join(f"![Original figure]({name})" for name in images)
                    else:
                        text = text.replace(block["block_content"], marker)
                    block["block_content"] = marker
                    block["transcription_pending"] = True
                for reference in sorted(references - _references(block["block_content"])):
                    block["block_content"] += f"\n![Original figure]({reference})"
        contents = [text] + (
            [block["block_content"] for block in layout] if isinstance(layout, list) else []
        )
        for content in contents:
            if any(reference not in images for reference in _references(content)):
                raise CloudIncomplete("cloud_required_asset_missing")
        assets = {}
        for name, url in images.items():
            content = download(url)
            verify_image(content)
            assets[name] = store.put_bytes(content)
        text = _rewrite_assets(text, assets)
        # The page rendition spans several layout blocks. Only individual
        # validated boxes may carry a more precise physical source position.
        location = {"kind": "pdf", "page": page}
        blocks = [BlockDraft(text, "paragraph", location, tuple(assets.values()))]
        if not text.strip():
            reason = reason or "ocr_blank_or_illustration"
        elif "\ufffd" in text or "\x00" in text:
            reason = "ocr_unmapped_glyphs"
        elif not isinstance(layout, list) or not layout:
            reason = "cloud_layout_unverified"
        else:
            identities = []
            for block in layout:
                if not isinstance(block, dict) or not isinstance(block.get("block_content"), str):
                    raise ValueError("Invalid layout content")
                identifier = block.get("block_id")
                if type(identifier) is not int or identifier < 0:
                    raise ValueError("Invalid layout identity")
                identities.append(identifier)
                if not block["block_content"].strip() and block.get("block_label") not in {
                    "image",
                    "figure",
                }:
                    reason = "ocr_empty_layout_block"
            if len(identities) != len(set(identities)):
                raise ValueError("Repeated layout block")
            # Keep every layout block as source evidence too, including text
            # omitted from the service's Markdown renderer (headers/footnotes).
            for block in layout:
                content = _rewrite_assets(block["block_content"], assets)
                if content.strip():
                    label = block.get("block_label")
                    kind = (
                        "table"
                        if label == "table"
                        else "heading"
                        if label in {"title", "doc_title", "paragraph_title"}
                        else "paragraph"
                    )
                    blocks.append(
                        BlockDraft(
                            content,
                            kind,
                            (block_locations or {}).get(block["block_id"], location),
                            tuple(
                                assets[name] for name in sorted(_references(block["block_content"]))
                            ),
                            f"OCR layout block {block['block_id']}; label={label}"
                            + (
                                "; transcription=pending"
                                if block.get("transcription_pending")
                                else ""
                            ),
                        )
                    )
        return blocks, reason
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise CloudIncomplete("cloud_result_invalid") from None


_MARKDOWN_IMAGE = re.compile(r"(!\[[^\]]*\]\()\s*(<[^>]+>|[^)\s]+)([^)]*\))")
_HTML_IMAGE = re.compile(r"(<img\b[^>]*\bsrc\s*=\s*)([\"'])(.*?)(\2)", re.IGNORECASE)


def _layout_references(block, images):
    references = _references(block["block_content"])
    if references or block.get("block_label") not in {"image", "figure"}:
        return references
    box = block.get("block_bbox")
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(v) not in {int, float} or not math.isfinite(v) for v in box)
        or not (0 <= box[0] < box[2] and 0 <= box[1] < box[3])
    ):
        return set()
    # PaddleX construct_img_path uses label + integer raster coordinates.
    # Match only that exact returned mapping key, never an adjacent page image.
    coordinates = "_".join(str(int(value)) for value in box)
    name = f"imgs/img_in_{block['block_label']}_box_{coordinates}.jpg"
    return {name} if name in images else set()


def _references(text):
    return {match.group(2).strip("<>") for match in _MARKDOWN_IMAGE.finditer(text)} | {
        match.group(3) for match in _HTML_IMAGE.finditer(text)
    }


def _rewrite_assets(text, assets):
    def markdown(match):
        return match.group(1) + "asset:" + assets[match.group(2).strip("<>")] + match.group(3)

    def html(match):
        return match.group(1) + match.group(2) + "asset:" + assets[match.group(3)] + match.group(4)

    return _HTML_IMAGE.sub(html, _MARKDOWN_IMAGE.sub(markdown, text))
