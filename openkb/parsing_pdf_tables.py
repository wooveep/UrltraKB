"""Native ruled tables retain cell text, header context and physical coordinates."""

from __future__ import annotations

import pymupdf

from openkb.evidence import BlockDraft
from openkb.processing import processing_checkpoint


def table_cells(page, number: int, *, issues=None):
    blocks, rectangles = [], []
    found = page.find_tables(strategy="lines_strict")
    if found is None:
        raise ValueError("Native table detection unavailable")
    tables = found.tables
    rejected: set[int] = set()
    for i, table in enumerate(tables):
        for j in range(i):
            intersection = pymupdf.Rect(table.bbox) & pymupdf.Rect(tables[j].bbox)
            if not intersection.is_empty and intersection.get_area() > 0:
                rejected.update((i, j))
    if rejected and issues is not None:
        issues.append("native_table_overlap_unconfirmed")
    for table_number, table in enumerate(tables, 1):
        processing_checkpoint()
        if table_number - 1 in rejected:
            continue  # Ambiguous overlapping grids stay native text and original visuals.
        values = table.extract()
        if not any(text for row in values for text in row):
            continue  # Empty drawn rectangles do not establish a text table.
        if len(values) != table.row_count or any(len(row) != table.col_count for row in values):
            raise ValueError("Native table dimensions do not match its cells")
        headers = table.header.names
        if len(headers) != table.col_count:
            raise ValueError("Native table header does not match its columns")
        context = (
            f"Physical page {number}, table {table_number}. "
            + (
                "Detected external column labels: "
                if table.header.external
                else "First row (header role unconfirmed): "
            )
            + " | ".join(str(header or "(empty)") for header in headers)
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
