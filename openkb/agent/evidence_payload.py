"""Pure, bounded model projection of an archival source selection.

The archival readback stays untouched. Coordinates always refer to frozen source
text, while continuation is an offset in this selection's displayed content.
"""

import json
import re

from openkb.text_source import clip_text_origins

MAX_PAYLOAD_CHARS = 48000


def _clean(value):
    if isinstance(value, dict):
        return {
            k: _clean(v)
            for k, v in value.items()
            if k != "schema_version" and v is not None and v != [] and v != {}
        }
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _segments(source: dict) -> list[tuple[int, int, int, int]]:
    if "_content_segments" in source:
        return source["_content_segments"]
    cursor = 0
    result = []
    for a, b in source.get("source_spans", [[0, len(source["content"])]]):
        result.append((cursor, cursor + b - a, a, b))
        cursor += b - a
    return result


def _window(source: dict, start: int, end: int) -> dict:
    spans = [
        [a + max(start, left) - left, a + min(end, right) - left]
        for left, right, a, _ in _segments(source)
        if left < end and right > start
    ]
    origins = [
        origin
        for a, b in spans
        for origin in clip_text_origins(source.get("origin_locators", []), a, b)
    ]
    result = {
        "content": source["content"][start:end],
        "source_spans": spans,
        "unit_kind": source.get("unit_kind", "text"),
        "coverage": "complete" if start == 0 and end == len(source["content"]) else "partial",
        "continuation": end if end < len(source["content"]) else None,
    }
    for key in ("char_range", "cell_range", "block_range", "diagnostics", "assets"):
        if source.get(key):
            result[key] = source[key]
    if sheet := source.get("sheet"):
        result["sheet"] = _clean(
            {
                key: sheet[key]
                for key in ("key", "name", "state", "merged_ranges", "has_objects", "diagnostics")
                if key in sheet
            }
        )
        cells = []
        for origin in origins:
            if not (location := origin.get("sheet_cell")):
                continue
            cell = location["cell"]
            item = {"coordinate": cell["coordinate"], "span": origin["normalized_span"]}
            for key in ("formula", "cached", "hidden_row", "hidden_column"):
                if cell.get(key) is not None and cell[key] is not False:
                    item[key] = cell[key]
            for key, default in (
                ("data_type", "s"),
                ("number_format", "General"),
                ("cache_status", "not_formula"),
                ("formula_status", "not_formula"),
            ):
                if key in cell and cell[key] != default:
                    item[key] = cell[key]
            if location.get("value_complete") is False:
                item["value_complete"] = False
            cells.append(item)
        result["cells"] = cells
    else:
        result["origins"] = _clean(
            [
                {key: value for key, value in origin.items() if key not in {"csv"}}
                | (
                    {"csv": {k: v for k, v in origin["csv"].items() if k != "value"}}
                    if origin.get("csv")
                    else {}
                )
                for origin in origins
                if origin.get("kind") != "generated"
            ]
        )
    blocks = []
    for unit in source.get("units", []):
        retained = [
            [max(a, c), min(b, d)]
            for a, b in unit.get("source_spans", [])
            for c, d in spans
            if a < d and b > c
        ]
        if retained:
            blocks.append(
                _clean(
                    {
                        "ordinal": unit["ordinal"],
                        "source_spans": retained,
                        "display_context": unit.get("display_context"),
                        "headings": unit.get("headings"),
                    }
                )
            )
    if blocks:
        result["blocks"] = blocks
    return result


def project_evidence(source: dict, *, offset: int = 0, budget: int = MAX_PAYLOAD_CHARS) -> dict:
    """Paginate after serialization, at paragraph boundaries outside cell values.

    An indivisible oversized cell/paragraph returns an explicit gap; it is never
    silently shortened or represented as a complete value.
    """
    text = source["content"]
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("Continuation is outside this evidence selection")
    result = _window(source, offset, len(text))
    if len(json.dumps(result, ensure_ascii=False)) <= budget:
        return result
    protected = []
    for origin in source.get("origin_locators", []):
        if origin.get("sheet_cell") or origin.get("csv"):
            a, b = origin["normalized_span"]
            for left, right, c, d in _segments(source):
                if a < d and b > c:
                    protected.append((left + max(a, c) - c, min(right, left + b - c)))
    ends = [m.end() for m in re.finditer(r"\n\s*\n", text) if m.end() > offset]
    ends = [end for end in ends if not any(a < end < b for a, b in protected)]
    ends.append(len(text))
    low, high, best = 0, len(ends) - 1, None
    while low <= high:
        middle = (low + high) // 2
        candidate = _window(source, offset, ends[middle])
        if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    if best is None:
        return {
            "content": "",
            "coverage": "budget_exceeded",
            "continuation": None,
            "diagnostics": [
                "One cell/paragraph exceeds the model payload budget. Select a narrower "
                "explicit character range; partial cells are marked incomplete."
            ],
        }
    return best
