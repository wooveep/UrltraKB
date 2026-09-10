"""Mammoth's document tree with original paragraph/table positions, never pages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openkb.docx_containers import ExpansionBudget
from openkb.docx_package import prepare_docx
from openkb.evidence import BlockDraft
from openkb.parsing_docx_quality import conversion_quality
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore


def parse_docx(
    path: Path,
    store: SourceStore,
    *,
    ocr=None,
    _budget=None,
    _depth=0,
    _source=None,
    _options=None,
) -> tuple[list[BlockDraft], list[dict[str, Any]]]:
    import mammoth
    from mammoth import documents as nodes

    budget = _budget or ExpansionBudget()
    prepared = prepare_docx(path.read_bytes(), store, budget, _depth)
    blocks: list[BlockDraft] = []
    quality: list[dict[str, Any]] = list(prepared.quality)
    pending_attachments: list[Any] = []
    headings: list[str] = []
    paragraph_number, table_number = 0, 0
    notes = None
    comments: dict[str, Any] = {}
    active_notes: set[tuple[str, str]] = set()

    def inline(node, assets: list[str]) -> str:
        if isinstance(node, nodes.Text):
            attachment = prepared.attachments.get(node.value)
            if attachment is not None:
                from openkb.docx_attachments import document_name

                name = document_name(attachment)
                if name is None:
                    quality.append(
                        {
                            "status": "verified",
                            "reason": "non_document_attachment_skipped:" + attachment.name,
                        }
                    )
                    return f"[Skipped non-document attachment: {attachment.name}]"
                from dataclasses import replace

                attachment = replace(attachment, name=name)
                pending_attachments.append(attachment)
                assets.append(attachment.blob)
                return f"[Embedded attachment: {attachment.name}](asset:{attachment.blob})"
            return node.value
        if isinstance(node, nodes.Tab):
            return "\t"
        if isinstance(node, nodes.Break):
            return "\n"
        if isinstance(node, nodes.NoteReference):
            identity = (node.note_type, node.note_id)
            if notes is None or identity in active_notes:
                quality.append({"status": "needs_review", "reason": "unresolved_docx_note"})
                return f"[{node.note_type} {node.note_id}: unresolved]"
            active_notes.add(identity)
            try:
                note = notes.resolve(node)
                text = "\n".join(inline(child, assets) for child in note.body)
                return f" [{node.note_type} {node.note_id}: {text}]"
            finally:
                active_notes.remove(identity)
        if isinstance(node, nodes.CommentReference):
            comment = comments.get(node.comment_id)
            if comment is None:
                quality.append({"status": "needs_review", "reason": "unresolved_docx_comment"})
                return "[unresolved comment]"
            text = "\n".join(inline(child, assets) for child in comment.body)
            return f" [Editorial comment {node.comment_id}: {text}]"
        if isinstance(node, nodes.Image):
            try:
                with node.open() as stream:
                    content = stream.read()
            except (KeyError, OSError):
                quality.append({"status": "needs_review", "reason": "docx_image_asset_missing"})
                return "[Original image unavailable]"
            digest = store.put_bytes(content)
            if digest in prepared.icons:
                assets.append(digest)
                return f"![{node.alt_text or 'Attachment icon'}](asset:{digest})"
            from openkb.docx_images import read_image

            text, images, checks = read_image(
                content, store, ocr, alt_text=node.alt_text or "Original image"
            )
            assets.extend(images)
            quality.extend(checks)
            return text
        return "".join(inline(child, assets) for child in getattr(node, "children", []))

    def visit(children, position=None, header=""):
        nonlocal paragraph_number, table_number
        for node in children:
            processing_checkpoint("parsing")
            if isinstance(node, nodes.Paragraph):
                paragraph_number += 1
                pending_attachments.clear()
                assets: list[str] = []
                text = inline(node, assets)
                style = node.style_id or node.style_name or ""
                heading = re.fullmatch(r"heading\s*([1-9])", style, re.IGNORECASE)
                if heading:
                    level = int(heading[1])
                    headings[level - 1 :] = [text]
                location = {
                    "kind": "docx",
                    "paragraph": paragraph_number,
                    "headings": list(headings),
                    **(position or {}),
                }
                if text.strip() or assets:
                    kind = "table" if position else "heading" if heading else "paragraph"
                    context = header
                    if node.numbering:
                        context += (
                            f"\nList level {node.numbering.level_index}; "
                            f"ordered={node.numbering.is_ordered}"
                        )
                    blocks.append(BlockDraft(text, kind, location, tuple(assets), context))
                    for attachment in pending_attachments:
                        from zipfile import BadZipFile

                        from openkb.docx_attachments import (
                            attachment_quality,
                            bind_blocks,
                            parse_attachment,
                        )

                        try:
                            drafts, checks = parse_attachment(
                                attachment,
                                store,
                                budget,
                                _depth + 1,
                                ocr,
                                source=_source,
                                options=_options,
                            )
                            blocks.extend(bind_blocks(drafts, attachment, location))
                            quality.extend(attachment_quality(checks, attachment))
                        except (ValueError, BadZipFile, UnicodeError):
                            quality.append(
                                {
                                    "status": "needs_review",
                                    "reason": "docx_attachment_unparsed:" + attachment.part,
                                }
                            )
            elif isinstance(node, nodes.Table):
                table_number += 1
                table = table_number
                header = (
                    " | ".join(inline(cell, []) for cell in node.children[0].children)
                    if node.children
                    else ""
                )
                for row_index, row in enumerate(node.children, 1):
                    for cell_index, cell in enumerate(row.children, 1):
                        context = (
                            f"Table {table}; header: {header}; "
                            f"colspan={cell.colspan}; rowspan={cell.rowspan}"
                        )
                        visit(
                            cell.children,
                            {"table": table, "row": row_index, "cell": cell_index},
                            context,
                        )

    def capture(document):
        nonlocal notes, comments
        notes = document.notes
        comments = {comment.comment_id: comment for comment in document.comments}
        visit(document.children)
        return document

    def image_source(image):
        try:
            with image.open() as stream:
                return {"src": "asset:" + store.put_bytes(stream.read())}
        except (KeyError, OSError):
            quality.append({"status": "needs_review", "reason": "docx_image_asset_missing"})
            return {"src": ""}

    with prepared.stream as source:
        result = mammoth.convert_to_html(
            source,
            transform_document=capture,
            convert_image=mammoth.images.img_element(image_source),
            external_file_access=False,
        )
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    for message in result.messages:
        quality.append(conversion_quality(message.message))
    return blocks, [dict(row) for row in dict.fromkeys(tuple(row.items()) for row in quality)]
