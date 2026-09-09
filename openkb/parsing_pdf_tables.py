"""Native ruled tables retain cell text, header context and physical coordinates."""

from __future__ import annotations

from openkb.evidence import BlockDraft
from openkb.processing import processing_checkpoint


def table_cells(page, number: int):
    blocks, rectangles = [], []
    tables = page.find_tables().tables
    for table_number, table in enumerate(tables, 1):
        processing_checkpoint()
        values = table.extract()
        if not any(text for row in values for text in row):
            continue  # Empty drawn rectangles do not establish a text table.
        if len(values) != table.row_count or any(len(row) != table.col_count for row in values):
            raise ValueError("Native table dimensions do not match its cells")
        headers = table.header.names
        if len(headers) != table.col_count:
            raise ValueError("Native table header does not match its columns")
        context = f"Physical page {number}, table {table_number}. Columns: " + " | ".join(
            str(header or "(empty)") for header in headers
        )
        for row_index, (row, positions) in enumerate(zip(values, table.rows), 1):
            for column, (text, bbox) in enumerate(zip(row, positions.cells), 1):
                # A merged cell has no independent rectangle at its covered
                # positions. Preserve its real rectangle, never invent copies.
                if bbox is None:
                    if text:
                        raise ValueError("Native table text has no corresponding cell")
                    continue
                blocks.append(
                    BlockDraft(
                        text or "",
                        "table",
                        {
                            "kind": "pdf",
                            "page": number,
                            "table": table_number,
                            "row": row_index,
                            "cell": column,
                            "bbox": list(bbox),
                        },
                        context=context,
                    )
                )
        rectangles.append(table.bbox)
    return blocks, rectangles
