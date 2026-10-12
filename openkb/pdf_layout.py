"""Conservative native PDF structure, without OCR or inferred cell contents."""

from dataclasses import dataclass
from html import escape
from math import isfinite

import pymupdf

PDF_LAYOUT_POLICY = "native-tables-image-order-v4"
_MAX_TABLE_SLOTS = 16384


class _TableCapacityError(ValueError):
    """Native text remains usable when its detected grid cannot be expanded."""


@dataclass
class _Table:
    bounds: pymupdf.Rect
    cells: list[pymupdf.Rect]
    positions: list[tuple[int, int, int, int]]
    rows: int
    holes: list[tuple[int, int, int]]
    text: str = ""


def _cell_bounds(span: dict, line: dict) -> pymupdf.Rect:
    bounds = pymupdf.Rect(span["bbox"])
    if line.get("dir") != (1.0, 0.0):
        return bounds
    try:
        ascender, descender, size = span["ascender"], span["descender"], span["size"]
        baseline = span["origin"][1]
        if not all(isfinite(value) for value in (ascender, descender, size, baseline)):
            return bounds
        if not (ascender > 0 >= descender and 0 < size < bounds.height):
            return bounds
        # PyMuPDF's normal bbox is line height, not glyph height. Use its
        # documented font-size rectangle locally; never toggle the global
        # small_glyph_heights setting or alter the original text/bbox.
        bottom = baseline - size * descender / (ascender - descender)
        tight = pymupdf.Rect(bounds.x0, bottom - size, bounds.x1, bottom)
        return tight if bounds.contains(tight) else bounds
    except (KeyError, TypeError, ValueError, IndexError):
        return bounds


def _geometry(table) -> _Table | None:
    bounds = pymupdf.Rect(table.bbox)
    cells = [pymupdf.Rect(value) for value in table.cells]
    if not cells or any(cell.is_empty or cell.is_infinite for cell in cells):
        return None
    xs = sorted({x for cell in cells for x in (cell.x0, cell.x1)})
    ys = sorted({y for cell in cells for y in (cell.y0, cell.y1)})
    if len(xs) < 3 or len(ys) < 3 or tuple(bounds) != (xs[0], ys[0], xs[-1], ys[-1]):
        return None
    if (len(xs) - 1) * (len(ys) - 1) > _MAX_TABLE_SLOTS:
        raise _TableCapacityError
    x_indices = {value: i for i, value in enumerate(xs)}
    y_indices = {value: i for i, value in enumerate(ys)}
    occupied: set[tuple[int, int]] = set()
    positions = []
    for cell in cells:
        left, right = x_indices[cell.x0], x_indices[cell.x1]
        top, bottom = y_indices[cell.y0], y_indices[cell.y1]
        slots = {(row, col) for row in range(top, bottom) for col in range(left, right)}
        if occupied & slots:
            return None
        occupied.update(slots)
        positions.append((top, left, bottom - top, right - left))
    by_row: list[list[int]] = [[] for _ in range(len(ys) - 1)]
    for row, column in occupied:
        by_row[row].append(column)
    holes = []
    for row, columns in enumerate(by_row):
        cursor = 0
        for column in sorted(columns):
            if cursor < column:
                holes.append((row, cursor, column - cursor))
            cursor = column + 1
        if cursor < len(xs) - 1:
            holes.append((row, cursor, len(xs) - 1 - cursor))
    return _Table(bounds, cells, positions, len(ys) - 1, holes)


def _assign(table: _Table, blocks: list[dict]) -> dict[tuple[int, int, int], int] | None:
    assigned = {}
    contents: list[dict[tuple[int, int], list[str]]] = [{} for _ in table.cells]
    for block_index, block in enumerate(blocks):
        if block["type"] != 0:
            if pymupdf.Rect(block["bbox"]).intersects(table.bounds):
                return None
            continue
        for line_index, line in enumerate(block["lines"]):
            for span_index, span in enumerate(line["spans"]):
                rect = pymupdf.Rect(span.get("_cell_bbox", span["bbox"]))
                if not span["text"] or not rect.intersects(table.bounds):
                    continue
                candidates = [i for i, cell in enumerate(table.cells) if cell.contains(rect)]
                if len(candidates) != 1:
                    if (
                        not candidates
                        and table.bounds.contains(rect)
                        and not any(cell.intersects(rect) for cell in table.cells)
                    ):
                        # A detector hole is not a native blank cell. Leave its
                        # text in place, outside the proved cell assignments.
                        continue
                    return None
                cell_index = candidates[0]
                assigned[(block_index, line_index, span_index)] = cell_index
                contents[cell_index].setdefault((block_index, line_index), []).append(span["text"])
    if not assigned:
        return None
    rows: list[list[tuple[int, str]]] = [[] for _ in range(table.rows)]
    for i in sorted(range(len(table.cells)), key=lambda i: table.positions[i]):
        row, column, height, width = table.positions[i]
        attributes = (f' rowspan="{height}"' if height > 1 else "") + (
            f' colspan="{width}"' if width > 1 else ""
        )
        text = "<br>".join(escape("".join(parts)) for parts in contents[i].values())
        rows[row].append((column, f"<td{attributes}>{text}</td>"))
    for row, column, width in table.holes:
        span = f' colspan="{width}"' if width > 1 else ""
        rows[row].append(
            (column, f'<td data-openkb-cell="unresolved"{span}>[Unresolved boundary]</td>')
        )
    note = (
        '<caption data-openkb-layout="partial">Extraction note: Native table layout is partial. '
        "Unresolved boundaries are not confirmed empty cells. Unassigned native text is "
        "retained separately; do not infer its cell membership.</caption>\n"
        if table.holes
        else ""
    )
    table.text = (
        "<table>\n"
        + note
        + "\n".join("<tr>" + "".join(cell for _, cell in sorted(row)) + "</tr>" for row in rows)
    )
    table.text += "\n</table>"
    return assigned


def _table_blocks(page, blocks: list[dict], notes: list[dict]) -> list[dict]:
    try:
        detected = page.find_tables(strategy="lines_strict").tables
    except Exception:
        # Table detection is optional. Its failure must not discard native text.
        return blocks
    tables = []
    for table in detected:
        try:
            candidate = _geometry(table)
        except _TableCapacityError:
            notes.append(
                {
                    "type": 0,
                    "bbox": table.bbox,
                    "lines": [
                        {
                            "spans": [
                                {
                                    "text": (
                                        "[Extraction note: Native table structure exceeds "
                                        "processing bounds in "
                                        f"region {tuple(round(value, 1) for value in table.bbox)}. "
                                        "Native text is retained; cell membership is unconfirmed.]"
                                    )
                                }
                            ]
                        }
                    ],
                }
            )
            continue
        except Exception:
            # A malformed optional detection cannot make native text unreadable.
            continue
        if candidate is not None:
            tables.append(candidate)
    owners = {}
    accepted: list[_Table] = []
    for table in tables:
        if any(other is not table and other.bounds.intersects(table.bounds) for other in tables):
            continue
        assigned = _assign(table, blocks)
        if assigned is None:
            continue
        identity = len(accepted)
        accepted.append(table)
        owners.update(dict.fromkeys(assigned, identity))
    if not accepted:
        return blocks
    result = []
    emitted = set()
    for block_index, block in enumerate(blocks):
        if block["type"] != 0:
            result.append(block)
            continue
        for line_index, line in enumerate(block["lines"]):
            spans: list[dict] = []
            for span_index, span in enumerate(line["spans"]):
                owner = owners.get((block_index, line_index, span_index))
                if owner is None:
                    spans.append(span)
                    continue
                if spans:
                    result.append(
                        {**block, "bbox": line["bbox"], "lines": [{**line, "spans": spans}]}
                    )
                    spans = []
                if owner not in emitted:
                    table = accepted[owner]
                    result.append(
                        {
                            "type": 0,
                            "bbox": tuple(table.bounds),
                            "lines": [{"spans": [{"text": table.text}]}],
                        }
                    )
                    emitted.add(owner)
            if spans:
                result.append({**block, "bbox": line["bbox"], "lines": [{**line, "spans": spans}]})
    return result


def _place_images(blocks: list[dict]) -> list[dict]:
    text: list[dict] = []
    images = []
    for block in blocks:
        if block["type"] == 1:
            images.append((len(text), block))
        elif block["type"] == 0 and block["lines"]:
            text.extend(
                {**block, "bbox": line.get("bbox", block["bbox"]), "lines": [line]}
                for line in block["lines"]
            )
        else:
            text.append(block)
    if not images:
        return blocks
    gaps: list[list[dict]] = [[] for _ in range(len(text) + 1)]
    for original, image in images:
        bounds = pymupdf.Rect(image["bbox"])
        neighbors = [
            (i, pymupdf.Rect(block["bbox"]))
            for i, block in enumerate(text)
            if block["type"] == 0
            and any(span["text"].strip() for line in block["lines"] for span in line["spans"])
            and block["bbox"][0] < bounds.x1
            and block["bbox"][2] > bounds.x0
        ]
        position = original
        if neighbors and not any(rect.intersects(bounds) for _, rect in neighbors):
            above = [(i, rect) for i, rect in neighbors if rect.y1 <= bounds.y0]
            below = [(i, rect) for i, rect in neighbors if rect.y0 >= bounds.y1]
            low, high = 0, len(text)
            if above:
                edge = max(rect.y1 for _, rect in above)
                low = max(i for i, rect in above if rect.y1 == edge) + 1
            if below:
                edge = min(rect.y0 for _, rect in below)
                high = min(i for i, rect in below if rect.y0 == edge)
            # Inconsistent neighbors cannot establish a safe insertion point.
            if low <= high:
                position = min(max(original, low), high)
        gaps[position].append(image)
    result = []
    for i, block in enumerate(text):
        result.extend(gaps[i])
        result.append(block)
    result.extend(gaps[-1])
    return result


def native_pdf_blocks(page) -> list[dict]:
    """Preserve proved native tables and place images between nearby text lines.

    Known cells must not overlap; detector holes remain explicitly unresolved.
    Assigned spans must fit one cell; unassigned text is retained separately.
    Image placement uses nearest vertically separate neighbors
    with horizontal overlap, without sorting body text or interpreting pixels.
    Ambiguous geometry retains the native content and ordering.
    """
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            for span in line["spans"]:
                span["_cell_bbox"] = tuple(_cell_bounds(span, line))
    if page.rotation:
        # Text extraction uses unrotated coordinates, while find_tables returns
        # displayed page coordinates. Compare geometry in that same frame;
        # native text, image bytes and the source PDF remain unchanged.
        for block in blocks:
            elements = [block]
            for line in block.get("lines", []):
                elements.extend([line, *line["spans"]])
            for element in elements:
                element["bbox"] = tuple(pymupdf.Rect(element["bbox"]) * page.rotation_matrix)
                if "_cell_bbox" in element:
                    element["_cell_bbox"] = tuple(
                        pymupdf.Rect(element["_cell_bbox"]) * page.rotation_matrix
                    )
    notes: list[dict] = []
    return [*_place_images(_table_blocks(page, blocks, notes)), *notes]
