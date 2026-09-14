"""Mammoth's document tree with original paragraph/table positions, never pages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openkb.docx_containers import ExpansionBudget
from openkb.docx_context import cell_excerpts
from openkb.docx_package import prepare_docx
from openkb.evidence import BlockDraft
from openkb.ocr.image_session import image_ocr_scope
from openkb.parsing_docx_quality import conversion_quality
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore, content_id


def parse_docx(
    path: Path,
    store: SourceStore,
    *,
    ocr=None,
    _budget=None,
    _depth=0,
    _source=None,
    _options=None,
    resume_ocr=False,
) -> tuple[list[BlockDraft], list[dict[str, Any]]]:
    import mammoth
    from mammoth import documents as nodes

    budget = _budget or ExpansionBudget()
    prepared = prepare_docx(path.read_bytes(), store, budget, _depth)
    outline_levels = _outline_levels(prepared.stream)
    blocks: list[BlockDraft] = []
    quality: list[dict[str, Any]] = list(prepared.quality)
    pending_attachments: list[Any] = []
    headings: list[str] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_number, table_number = 0, 0
    notes = None
    comments: dict[str, Any] = {}
    active_notes: set[tuple[str, str]] = set()
    missing_image_nodes: set[int] = set()
    progress = None

    def locate_missing_images(start, location):
        missing = [row for row in quality[start:] if row["reason"] == "docx_image_asset_missing"]
        if missing:
            quality[start:] = [row for row in quality[start:] if row not in missing]
            for row in quality[:start]:
                if row["reason"] == "docx_image_asset_missing" and row.get("location") == location:
                    row["count"] += len(missing)
                    return
            quality.append({**missing[0], "location": location, "count": len(missing)})

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
                return "\n"
            active_notes.add(identity)
            try:
                try:
                    note = notes.resolve(node)
                except KeyError:
                    quality.append({"status": "needs_review", "reason": "unresolved_docx_note"})
                    return "\n"
                text = "\n".join(inline(child, assets) for child in note.body)
                return f" [{node.note_type} {node.note_id}: {text}]"
            finally:
                active_notes.remove(identity)
        if isinstance(node, nodes.CommentReference):
            comment = comments.get(node.comment_id)
            if comment is None:
                quality.append({"status": "needs_review", "reason": "unresolved_docx_comment"})
                return "\n"
            text = "\n".join(inline(child, assets) for child in comment.body)
            return f" [Editorial comment {node.comment_id}: {text}]"
        if isinstance(node, nodes.Image):
            try:
                with node.open() as stream:
                    content = stream.read()
            except (KeyError, OSError):
                missing_image_nodes.add(id(node))
                quality.append({"status": "needs_review", "reason": "docx_image_asset_missing"})
                # Keep an omitted inline object's boundary: joining surrounding
                # runs can change words/numbers. Diagnostics are not source text.
                return "\n"
            digest = store.put_bytes(content)
            if digest in prepared.icons:
                assets.append(digest)
                return f"![{node.alt_text or 'Attachment icon'}](asset:{digest})"
            from openkb.docx_images import read_image

            text, images, checks = read_image(
                content, store, image_ocr, alt_text=node.alt_text or "Original image"
            )
            assets.extend(images)
            quality.extend(checks)
            return text
        return "".join(inline(child, assets) for child in getattr(node, "children", []))

    def visit(children, position=None, header="", context_data=None):
        nonlocal paragraph_number, table_number
        for node in children:
            processing_checkpoint("parsing")
            if isinstance(node, nodes.Paragraph):
                paragraph_number += 1
                pending_attachments.clear()
                assets: list[str] = []
                quality_start = len(quality)
                text = inline(node, assets)
                heading = re.fullmatch(
                    r"heading\s*([1-9])", node.style_id or "", re.IGNORECASE
                ) or re.fullmatch(r"heading\s*([1-9])", node.style_name or "", re.IGNORECASE)
                level = outline_levels.get(node.style_id, int(heading[1]) if heading else None)
                if level is not None:
                    heading_stack[:] = [item for item in heading_stack if item[0] < level]
                    heading_stack.append((level, text))
                    headings[:] = [title for _, title in heading_stack]
                location = {
                    "kind": "docx",
                    "paragraph": paragraph_number,
                    "headings": list(headings),
                    **({"heading_level": level} if level is not None else {}),
                    **(position or {}),
                }
                locate_missing_images(quality_start, location)
                if text.strip() or assets:
                    kind = "table" if position else "heading" if level is not None else "paragraph"
                    context = header
                    details = context_data
                    if node.numbering:
                        context += (
                            f"\nList level {node.numbering.level_index}; "
                            f"ordered={node.numbering.is_ordered}"
                        )
                        if details is not None:
                            list_level = node.numbering.level_index
                            if isinstance(list_level, str) and re.fullmatch(r"[0-9]+", list_level):
                                try:
                                    list_level = int(list_level)
                                except ValueError:
                                    list_level = None
                            if type(list_level) is int and list_level >= 0:
                                details = {
                                    **details,
                                    "structure": {
                                        **details["structure"],
                                        "list_level": list_level,
                                        "ordered": node.numbering.is_ordered,
                                    },
                                }
                            else:
                                quality.append(
                                    {"status": "needs_review", "reason": "docx_list_level_omitted"}
                                )
                    blocks.append(BlockDraft(text, kind, location, tuple(assets), context, details))
                    for attachment in pending_attachments:
                        from openkb.docx_attachments import (
                            ATTACHMENT_CONTENT_ERRORS,
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
                                resume_ocr=resume_ocr,
                            )
                            if not drafts:
                                drafts = [
                                    BlockDraft(
                                        "[Embedded document has no readable content; "
                                        "original retained]",
                                        "paragraph",
                                        {"kind": "converted", "line": 1},
                                    )
                                ]
                            blocks.extend(bind_blocks(drafts, attachment, location))
                            quality.extend(attachment_quality(checks, attachment, location))
                        except ATTACHMENT_CONTENT_ERRORS:
                            blocks.extend(
                                bind_blocks(
                                    [
                                        BlockDraft(
                                            "[Embedded document could not be parsed; "
                                            "original retained]",
                                            "paragraph",
                                            {"kind": "converted", "line": 1},
                                        )
                                    ],
                                    attachment,
                                    location,
                                )
                            )
                            quality.append(
                                {
                                    "status": "needs_review",
                                    "reason": "docx_attachment_unparsed:" + attachment.part,
                                }
                            )
                progress.advance()
            elif isinstance(node, nodes.Image):
                # VML extras can follow intervening textbox paragraphs. Their
                # nearest paragraph is not proof of original ownership.
                assets = []
                quality_start = len(quality)
                text = inline(node, assets)
                location = {"kind": "docx", **(position or {})}
                locate_missing_images(quality_start, location)
                blocks.append(
                    BlockDraft(
                        text,
                        "image",
                        location,
                        tuple(assets),
                        "Detached DOCX image; original paragraph position unavailable.",
                        context_data,
                    )
                )
                quality.append({"status": "verified", "reason": "docx_image_position_unavailable"})
            elif isinstance(node, nodes.Table):
                table_number += 1
                table = table_number
                original_rows = {
                    number: [
                        cell_excerpts(
                            cell, notes=notes, comments=comments, attachments=prepared.attachments
                        )
                        for cell in row.children
                    ]
                    for number, row in enumerate(node.children, 1)
                    if row.is_header or number == 1
                }
                # Preview source wording without running image/OCR/attachment
                # handling a second time before its physical position is known.
                header_rows = {
                    number: ["\n".join(item["text"] for item in cell) for cell in cells]
                    for number, cells in original_rows.items()
                }
                headers = [
                    (number, " | ".join(header_rows[number]))
                    for number, row in enumerate(node.children, 1)
                    if row.is_header
                ]
                first_row = " | ".join(header_rows.get(1, []))
                for row_index, row in enumerate(node.children, 1):
                    for cell_index, cell in enumerate(row.children, 1):
                        context = f"Table {table}; colspan={cell.colspan}; rowspan={cell.rowspan}"
                        declared = [text for number, text in headers if number != row_index]
                        if declared:
                            context += "; declared header: " + " | ".join(declared)
                        elif not headers and row_index > 1:
                            context += "; first row (header role unconfirmed): " + first_row
                        excerpt_rows = (
                            [number for number, _ in headers if number != row_index]
                            if headers
                            else [1]
                            if row_index > 1
                            else []
                        )
                        structure = {"table": table}
                        for key, value in (("colspan", cell.colspan), ("rowspan", cell.rowspan)):
                            if type(value) is int and value > 0:
                                structure[key] = value
                            else:
                                quality.append(
                                    {"status": "needs_review", "reason": "docx_table_span_omitted"}
                                )
                        details = {
                            "source_excerpts": [
                                {
                                    **excerpt,
                                    "row": number,
                                    "cell": column,
                                    "relation": "declared_header" if headers else "first_row",
                                }
                                for number in excerpt_rows
                                for column, excerpts in enumerate(original_rows[number], 1)
                                for excerpt in excerpts
                            ],
                            "structure": structure,
                            "reader_status": {
                                "header_role": "declared" if headers else "unconfirmed"
                            },
                        }
                        visit(
                            cell.children,
                            {"table": table, "row": row_index, "cell": cell_index},
                            context,
                            details,
                        )

    def capture(document):
        nonlocal notes, comments, progress
        notes = document.notes
        comments = {comment.comment_id: comment for comment in document.comments}

        def paragraph_count(children):
            return sum(
                1
                if isinstance(node, nodes.Paragraph)
                else paragraph_count(getattr(node, "children", []))
                for node in children
            )

        with progress_scope("docx", paragraph_count(document.children), "paragraphs") as progress:
            visit(document.children)
        if any(row["reason"] == "unresolved_docx_note" for row in quality):
            # HTML is not our evidence. Avoid a second, unused renderer resolving
            # the same missing/cyclic note after structured body capture succeeded.
            # Mammoth's document-reader diagnostics still flow into result.messages.
            return document.copy(children=[], notes=nodes.Notes({}), comments=[])
        return document

    def image_source(image):
        try:
            with image.open() as stream:
                return {"src": "asset:" + store.put_bytes(stream.read())}
        except (KeyError, OSError):
            if id(image) not in missing_image_nodes:
                quality.append({"status": "needs_review", "reason": "docx_image_asset_missing"})
            return {"src": ""}

    with image_ocr_scope(ocr) as image_ocr, prepared.stream as source:
        result = mammoth.convert_to_html(
            source,
            transform_document=capture,
            convert_image=mammoth.images.img_element(image_source),
            external_file_access=False,
        )
        if image_ocr is not None:
            quality.extend(image_ocr.notices())
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    for message in result.messages:
        quality.append(conversion_quality(message.message))
    return blocks, list({content_id(row): row for row in quality}.values())


def _outline_levels(stream):
    """Read explicit OOXML outline semantics, including inherited numeric styles."""
    from zipfile import ZipFile

    from defusedxml.ElementTree import fromstring

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with ZipFile(stream) as archive:
        if "word/styles.xml" not in archive.namelist():
            return {}
        root = fromstring(archive.read("word/styles.xml"))
    stream.seek(0)
    styles = {node.get(namespace + "styleId"): node for node in root.findall(namespace + "style")}
    result = {}
    for identity, style in styles.items():
        seen = set()
        while style is not None and len(seen) < 32:
            key = style.get(namespace + "styleId")
            if key in seen:
                break
            seen.add(key)
            outline = style.find(namespace + "pPr/" + namespace + "outlineLvl")
            if outline is not None:
                value = outline.get(namespace + "val", "")
                if value.isdecimal() and 0 <= int(value) <= 8:
                    result[identity] = int(value) + 1
                break
            base = style.find(namespace + "basedOn")
            style = styles.get(base.get(namespace + "val")) if base is not None else None
    return result
