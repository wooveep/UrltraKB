"""Recursive embedded-document parsing with bounded expansion and source positions."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from openkb.docx_containers import ExpansionBudget, decode_text, read_member
from openkb.docx_package import Attachment
from openkb.evidence import BlockDraft
from openkb.sources import SourceStore

_TEXT = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".xml", ".ini", ".conf", ".cfg", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".py", ".sql", ".properties", ".log"}
_IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".emf", ".wmf"}


class BoundOcr:
    def __init__(self, ocr, input_id):
        self.ocr, self.input_id = ocr, input_id

    def page(self, document, page):
        return self.ocr.page(document, page, input_id=self.input_id)


def bind_blocks(blocks, attachment: Attachment, position: dict) -> list[BlockDraft]:
    return [
        replace(
            block,
            location={
                **position,
                "attachment": {
                    "part": attachment.part,
                    "name": attachment.name,
                    "blob": attachment.blob,
                    "position": block.location,
                },
            },
            assets=tuple(dict.fromkeys((attachment.container, attachment.blob, *block.assets))),
            context=f"Embedded attachment: {attachment.name}\n{block.context}",
        )
        for block in blocks
    ]


def attachment_quality(quality, attachment):
    return [
        {
            "status": row["status"],
            "reason": "docx_attachment:" + attachment.part + ":"
            + (f"page_{row['page']}:" if "page" in row else "") + row["reason"],
        }
        for row in quality
    ]


def parse_attachment(
    attachment: Attachment, store: SourceStore, budget: ExpansionBudget, depth: int, ocr=None
):
    from openkb.parsing_docx import parse_docx

    path = store.asset(attachment.blob)
    suffix = PurePosixPath(attachment.name).suffix.lower()
    content = attachment.content
    budget.admit(0, depth)
    if content.startswith(b"%PDF-"):
        from openkb.parsing_pdf import parse_pdf

        return parse_pdf(path, store, ocr=BoundOcr(ocr, attachment.blob) if ocr else None)
    if content.startswith(b"PK\x03\x04"):
        with ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if "word/document.xml" in names:
                return parse_docx(path, store, ocr=ocr, _budget=budget, _depth=depth)
            if any(name.startswith(("xl/", "ppt/")) for name in names):
                from markitdown import MarkItDown

                extension = ".xlsx" if any(name.startswith("xl/") for name in names) else ".pptx"
                text = MarkItDown().convert_stream(io.BytesIO(content), file_extension=extension).text_content
                blocks = [BlockDraft(text, "paragraph", {"kind": "converted", "line": 1})] if text.strip() else []
                # These converters do not expose complete image/embedded coverage.
                return blocks, [{"status": "needs_review", "reason": "embedded_office_visual_coverage_unverified"}]
            blocks, quality = [], []
            if len(names) != len(set(names)):
                raise ValueError("docx_duplicate_package_member")
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                relative = PurePosixPath(entry.filename)
                if relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename:
                    raise ValueError("docx_attachment_path_invalid")
                data = read_member(archive, entry.filename, budget, depth + 1)
                child = Attachment(entry.filename, relative.name, data, attachment.blob, store.put_bytes(data))
                try:
                    drafts, checks = parse_attachment(child, store, budget, depth + 1, ocr)
                    blocks.extend(bind_blocks(drafts, child, {"kind": "converted", "line": 1}))
                    quality.extend(attachment_quality(checks, child))
                except (ValueError, BadZipFile, UnicodeError):
                    blocks.append(BlockDraft(f"Unparsed attachment: {child.name}", "paragraph", {"kind": "converted", "line": 1}, (child.blob,)))
                    quality.append({"status": "needs_review", "reason": "docx_archive_member_unparsed:" + child.part})
            if not blocks:
                quality.append({"status": "needs_review", "reason": "empty_attachment_archive"})
            return blocks, quality
    if suffix in _IMAGES:
        from openkb.docx_images import read_image

        text, assets, quality = read_image(content, store, ocr)
        return [BlockDraft(text, "image", {"kind": "converted", "line": 1}, assets)], quality
    if suffix in _TEXT or not suffix:
        text = decode_text(content)
        if not text.strip():
            return [], [{"status": "needs_review", "reason": "empty_attachment_content"}]
        return [BlockDraft(text, "code" if suffix in {".sh", ".bash", ".py", ".ps1", ".sql", ".bat", ".cmd"} else "paragraph", {"kind": "text", "line": 1})], []
    return [BlockDraft(f"Unparsed attachment: {attachment.name}", "paragraph", {"kind": "converted", "line": 1}, (attachment.blob,))], [{"status": "needs_review", "reason": "docx_attachment_format_unsupported:" + suffix}]
