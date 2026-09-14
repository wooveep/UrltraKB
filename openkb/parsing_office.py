"""Original Office coordinates and assets, using the already pinned conversion dependencies."""

import io
from zipfile import ZipFile

from openkb.docx_containers import ExpansionBudget
from openkb.evidence import BlockDraft
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope


def checked_package(path):
    data = path.read_bytes()
    with ZipFile(io.BytesIO(data)) as archive:
        budget = ExpansionBudget()
        seen = set()
        for item in archive.infolist():
            if item.filename in seen or item.flag_bits & 1:
                raise ValueError("Ambiguous or encrypted Office package")
            seen.add(item.filename)
            budget.admit(item.file_size, 0)
    return io.BytesIO(data)


def parse_xlsx(path, store):
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries

    from openkb.parsing_failures import read_office_package

    book = read_office_package(
        load_workbook, checked_package(path), data_only=False, keep_links=False
    )
    blocks, quality = [], []
    try:
        with progress_scope("xlsx", len(book.worksheets), "items") as progress:
            for sheet_index, sheet in enumerate(book.worksheets, 1):
                location = {"kind": "xlsx", "sheet": sheet.title, "sheet_index": sheet_index}
                blocks.append(BlockDraft(sheet.title, "heading", location))
                tables = [(table, range_boundaries(table.ref)) for table in sheet.tables.values()]
                cells = sorted(
                    (cell for cell in sheet._cells.values() if cell.value is not None),
                    key=lambda cell: (cell.row, cell.column),
                )
                rows, first_in_column = {}, {}
                for populated in cells:
                    rows.setdefault(populated.row, []).append(populated)
                    first_in_column.setdefault(populated.column, populated)
                # openpyxl's populated cell map avoids enumerating a maliciously
                # sparse billion-cell rectangle. No filesystem paths or formulas execute.
                for cell in cells:
                    processing_checkpoint("parsing")
                    if cell.value is None:
                        continue
                    position = {**location, "cell_address": cell.coordinate}
                    context = [f"Number format: {cell.number_format}"]
                    if cell.data_type == "f":
                        context.append("Stored formula; not evaluated by the importer.")
                    for merged in sheet.merged_cells.ranges:
                        if merged.min_row <= cell.row <= merged.max_row:
                            owner = sheet.cell(merged.min_row, merged.min_col)
                            if owner.value is not None:
                                context.append(
                                    f"Merged cells crossing this row {merged}: {owner.value}"
                                )
                        if cell.coordinate in merged:
                            position["cell_range"] = str(merged)
                            context.append(
                                f"Merged range: {merged}; value belongs to its top-left cell."
                            )
                    for table, (left, top, right, bottom) in tables:
                        if left <= cell.column <= right and top <= cell.row <= bottom:
                            context.append(f"Declared table {table.displayName}: {table.ref}")
                            if table.headerRowCount and cell.row > top:
                                header = sheet.cell(top, cell.column)
                                context.append(f"Column header {header.coordinate}: {header.value}")
                    # Unmarked grids keep neighboring cell identities as context;
                    # the first row is not automatically asserted to be a header.
                    peers = [
                        f"{peer.coordinate}: {peer.value}"
                        for peer in rows[cell.row]
                        if peer.row == cell.row
                        and peer.coordinate != cell.coordinate
                        and peer.value is not None
                    ]
                    if peers:
                        context.append("Same row cells: " + " | ".join(peers))
                    first = first_in_column[cell.column]
                    if first.row < cell.row:
                        context.append(
                            "First populated cell in column (header role unconfirmed) "
                            f"{first.coordinate}: {first.value}"
                        )
                    blocks.append(
                        BlockDraft(str(cell.value), "table", position, context="\n".join(context))
                    )
                for picture in sheet._images:
                    processing_checkpoint("parsing")
                    digest = store.put_bytes(picture._data())
                    anchor = getattr(picture.anchor, "_from", None)
                    position = dict(location)
                    if anchor is not None:
                        from openpyxl.utils.cell import get_column_letter

                        position["cell_address"] = (
                            f"{get_column_letter(anchor.col + 1)}{anchor.row + 1}"
                        )
                    blocks.append(
                        BlockDraft(
                            f"![Original spreadsheet image](asset:{digest})",
                            "image",
                            position,
                            (digest,),
                        )
                    )
                if sheet._charts:
                    quality.append(
                        {
                            "status": "needs_review",
                            "reason": f"xlsx_chart_original_only:sheet:{sheet_index}",
                        }
                    )
                progress.advance()
    finally:
        book.close()
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    return blocks, quality


def parse_pptx(path, store):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from openkb.office_locations import TITLE_PLACEHOLDERS
    from openkb.parsing_failures import read_office_package

    presentation = read_office_package(Presentation, checked_package(path))
    blocks, quality = [], []

    def native_titles(shapes):
        for shape in shapes:
            processing_checkpoint("parsing")
            if shape.is_placeholder and shape.placeholder_format.type.name in TITLE_PLACEHOLDERS:
                yield shape
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from native_titles(shape.shapes)

    def visit(shapes, base, title_context, groups=()):
        for shape in shapes:
            processing_checkpoint("parsing")
            position = {
                **base,
                "object_id": shape.shape_id,
                "bbox": [
                    int(shape.left),
                    int(shape.top),
                    int(shape.left + shape.width),
                    int(shape.top + shape.height),
                ],
                "coordinate_unit": "emu",
                "group_ids": list(groups),
                "placeholder_type": (
                    shape.placeholder_format.type.name if shape.is_placeholder else None
                ),
            }
            context = title_context + f"\nStored object name: {shape.name}"
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                visit(shape.shapes, base, title_context, (*groups, shape.shape_id))
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                digest = store.put_bytes(shape.image.blob)
                blocks.append(
                    BlockDraft(
                        f"![Original slide image](asset:{digest})",
                        "image",
                        position,
                        (digest,),
                        context,
                    )
                )
            elif shape.has_table:
                table = shape.table
                for row_index, row in enumerate(table.rows, 1):
                    for cell_index, cell in enumerate(row.cells, 1):
                        if cell.is_spanned or not cell.text.strip():
                            continue
                        cell_context = (
                            context + f"; colspan={cell.span_width}; rowspan={cell.span_height}"
                        )
                        if table.first_row and row_index > 1:
                            cell_context += f"; column header: {table.cell(0, cell_index - 1).text}"
                        blocks.append(
                            BlockDraft(
                                cell.text,
                                "table",
                                {**position, "row": row_index, "cell": cell_index},
                                context=cell_context,
                            )
                        )
            elif shape.has_text_frame:
                for number, paragraph in enumerate(shape.text_frame.paragraphs, 1):
                    if not paragraph.text.strip():
                        continue
                    kind = (
                        "heading"
                        if position["placeholder_type"] in TITLE_PLACEHOLDERS
                        else "paragraph"
                    )
                    blocks.append(
                        BlockDraft(
                            paragraph.text,
                            kind,
                            {**position, "paragraph": number},
                            context=context + f"; list level: {paragraph.level}",
                        )
                    )
            elif shape.shape_type not in {MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.AUTO_SHAPE}:
                # Retain the OOXML object, including its original package, without
                # guessing chart/diagram semantics or following external links.
                digest = store.put_bytes(shape.element.xml.encode())
                blocks.append(
                    BlockDraft(
                        f"[Uninterpreted original slide object](asset:{digest})",
                        "paragraph",
                        position,
                        (digest,),
                        context,
                    )
                )
                quality.append(
                    {
                        "status": "needs_review",
                        "reason": f"pptx_object_original_only:slide:{base['slide']}:"
                        f"object:{shape.shape_id}",
                    }
                )

    with progress_scope("pptx", len(presentation.slides), "items") as progress:
        for slide_index, slide in enumerate(presentation.slides, 1):
            titles = list(native_titles(slide.shapes))
            title_context = (
                "\n".join(
                    f"Native {shape.placeholder_format.type.name} placeholder object "
                    f"{shape.shape_id}: {shape.text if shape.has_text_frame else ''}"
                    for shape in titles
                )
                or "No native title placeholder."
            )
            base = {
                "kind": "pptx",
                "slide": slide_index,
                "title_placeholder_count": len(titles),
                "title_object_id": titles[0].shape_id if len(titles) == 1 else None,
            }
            before = len(blocks)
            visit(
                slide.shapes,
                base,
                title_context,
            )
            if before == len(blocks):
                blocks.append(
                    BlockDraft(
                        "[Slide with no readable text]",
                        "paragraph",
                        base,
                    )
                )
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    blocks.append(
                        BlockDraft(
                            notes.text,
                            "paragraph",
                            {**base, "notes": True},
                            context=f"Speaker notes for slide {slide_index}\n{title_context}",
                        )
                    )
            progress.advance()
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    return blocks, quality
