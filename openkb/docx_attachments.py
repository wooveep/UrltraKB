"""Import only document attachments, preserving their parent and source positions."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import PurePosixPath
from xml.etree.ElementTree import ParseError
from xml.parsers.expat import ExpatError
from zipfile import BadZipFile, ZipFile

from markitdown import (
    FileConversionException,
    MissingDependencyException,
    UnsupportedFormatException,
)
from pymupdf import FileDataError

from openkb.docx_containers import ExpansionBudget, decode_text
from openkb.docx_package import Attachment
from openkb.evidence import BlockDraft
from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.sources import SourceStore

# Only document-format/converter failures are local omissions. Cancellation,
# document deadlines, storage I/O and failed mutations must still propagate.
ATTACHMENT_CONTENT_ERRORS = (
    ValueError,
    KeyError,
    BadZipFile,
    UnicodeError,
    ParseError,
    ExpatError,
    FileConversionException,
    MissingDependencyException,
    UnsupportedFormatException,
    FileDataError,
)


class BoundOcr:
    def __init__(self, ocr, input_id):
        self.ocr, self.input_id = ocr, input_id

    def page(self, document, page):
        return self.ocr.page(document, page, input_id=self.input_id)


def document_name(attachment: Attachment) -> str | None:
    """A ZIP/script is skipped even when its payload could be read as text."""
    name = attachment.name
    if name == "embedded.docx":
        with ZipFile(io.BytesIO(attachment.content)) as archive:
            names = archive.namelist()
        extension = next(
            (
                suffix
                for member, suffix in (
                    ("word/document.xml", ".docx"),
                    ("xl/workbook.xml", ".xlsx"),
                    ("ppt/presentation.xml", ".pptx"),
                )
                if member in names
            ),
            None,
        )
        return PurePosixPath(attachment.part).stem + extension if extension else None
    return name if PurePosixPath(name).suffix.lower() in SUPPORTED_EXTENSIONS else None


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
            assets=tuple(dict.fromkeys((attachment.blob, *block.assets))),
            context=f"Embedded attachment: {attachment.name}\n{block.context}",
        )
        for block in blocks
    ]


def attachment_quality(quality, attachment):
    return [
        {
            "status": row["status"],
            "reason": "docx_attachment:"
            + attachment.part
            + ":"
            + (f"page_{row['page']}:" if "page" in row else "")
            + row["reason"],
        }
        for row in quality
    ]


def parse_attachment(
    attachment: Attachment,
    store: SourceStore,
    budget: ExpansionBudget,
    depth: int,
    ocr=None,
    *,
    source=None,
    options=None,
):
    from openkb.parsing import parse_document
    from openkb.parsing_docx import parse_docx

    name = document_name(attachment)
    if name is None:
        return [], [
            {"status": "verified", "reason": "non_document_attachment_skipped:" + attachment.name}
        ]
    budget.admit(0, depth)
    if source is not None:
        child = store.intake_attachment(
            source, part=attachment.part, name=name, content=attachment.content
        )
        parsed = parse_document(store.kb_dir, child, options=options, _budget=budget, _depth=depth)
        return [
            BlockDraft(
                store.asset(b.blob).read_text(encoding="utf-8"),
                b.kind,
                b.location,
                b.assets,
                b.context,
            )
            for b in parsed.blocks
        ], parsed.quality
    # Direct parser callers still receive document contents; only the application
    # entry point owns an immutable parent identity for importing child sources.
    path = store.asset(attachment.blob)
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".pdf":
        from openkb.parsing_pdf import parse_pdf

        return parse_pdf(path, store, ocr=BoundOcr(ocr, attachment.blob) if ocr else None)
    if suffix == ".docx":
        return parse_docx(path, store, ocr=ocr, _budget=budget, _depth=depth)
    if suffix in {".txt", ".md", ".markdown", ".csv"}:
        text = decode_text(attachment.content)
    else:
        from markitdown import MarkItDown

        text = (
            MarkItDown()
            .convert_stream(io.BytesIO(attachment.content), file_extension=suffix)
            .text_content
        )
    return [BlockDraft(text, "paragraph", {"kind": "converted", "line": 1})], (
        [] if text.strip() else [{"status": "needs_review", "reason": "empty_attachment_content"}]
    )
