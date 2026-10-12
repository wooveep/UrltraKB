"""Conservative native PDF structure, without OCR or inferred cell contents."""

from dataclasses import dataclass
from html import escape

import pymupdf

PDF_LAYOUT_POLICY = "native-tables-image-order-v2"


@dataclass
class _Table:
    bounds: pymupdf.Rect
    cells: list[pymupdf.Rect]
    positions: list[tuple[int, int, int, int]]
    rows: int
    text: str = ""


def _geometry(table) -> _Table | None:
    bounds = pymupdf.Rect(table.bbox)
    cells = [pymupdf.Rect(value) for value in table.cells]
    if not cells or any(cell.is_empty or cell.is_infinite for cell in cells):
        return None
    xs = sorted({x for cell in cells for x in (cell.x0, cell.x1)})
    ys = sorted({y for cell in cells for y in (cell.y0, cell.y1)})
    if len(xs) < 3 or len(ys) < 3 or tuple(bounds) != (xs[0], ys[0], xs[-1], ys[-1]):
        return None
    occupied: set[tuple[int, int]] = set()
    positions = []
    for cell in cells:
        left, right = xs.index(cell.x0), xs.index(cell.x1)
        top, bottom = ys.index(cell.y0), ys.index(cell.y1)
        slots = {(row, col) for row in range(top, bottom) for col in range(left, right)}
        if occupied & slots:
            return None
        occupied.update(slots)
        positions.append((top, left, bottom - top, right - left))
    if len(occupied) != (len(xs) - 1) * (len(ys) - 1):
        return None
    return _Table(bounds, cells, positions, len(ys) - 1)


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
                rect = pymupdf.Rect(span["bbox"])
                if not span["text"] or not rect.intersects(table.bounds):
                    continue
                candidates = [i for i, cell in enumerate(table.cells) if cell.contains(rect)]
                if len(candidates) != 1:
                    return None
                cell_index = candidates[0]
                assigned[(block_index, line_index, span_index)] = cell_index
                contents[cell_index].setdefault((block_index, line_index), []).append(span["text"])
    if not assigned:
        return None
    rows: list[list[str]] = [[] for _ in range(table.rows)]
    for i in sorted(range(len(table.cells)), key=lambda i: table.positions[i]):
        row, _, height, width = table.positions[i]
        attributes = (f' rowspan="{height}"' if height > 1 else "") + (
            f' colspan="{width}"' if width > 1 else ""
        )
        text = "<br>".join(escape("".join(parts)) for parts in contents[i].values())
        rows[row].append(f"<td{attributes}>{text}</td>")
    table.text = "<table>\n" + "\n".join("<tr>" + "".join(row) + "</tr>" for row in rows)
    table.text += "\n</table>"
    return assigned


def _table_blocks(page, blocks: list[dict]) -> list[dict]:
    try:
        tables = [
            candidate
            for table in page.find_tables(strategy="lines_strict").tables
            if (candidate := _geometry(table)) is not None
        ]
    except Exception:
        # Table detection is optional. Its failure must not discard native text.
        return blocks
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
            if block["type"] == 0 and block["bbox"][0] < bounds.x1 and block["bbox"][2] > bounds.x0
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

    Tables must tile their rectangle exactly, with each overlapping span wholly
    inside one cell. Image placement uses nearest vertically separate neighbors
    with horizontal overlap, without sorting body text or interpreting pixels.
    Ambiguous geometry retains the native content and ordering.
    """
    blocks = page.get_text("dict")["blocks"]
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
    return _place_images(_table_blocks(page, blocks))
