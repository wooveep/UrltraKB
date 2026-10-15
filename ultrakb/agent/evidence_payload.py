"""Pure, bounded model projection of an archival source selection.

The archival readback stays untouched. Coordinates always refer to frozen source
text, while continuation is an offset in this selection's displayed content.
"""

import json

from ultrakb.agent.table_evidence import TABLE_INTERPRETATION, table_metadata
from ultrakb.content_blocks import markdown_layout
from ultrakb.text_source import clip_text_origins

MAX_PAYLOAD_CHARS = 48000
FIGURE_INTERPRETATION = (
    "Image references retain original assets, occurrence locations and any original captions. "
    "Pixels have not been interpreted. An occurrence or filename does not establish visual content."
)


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
    if source.get("cell_range"):
        # Displayed cell labels and separators have no original-source span.
        for origin in source["origin_locators"]:
            a, b = origin["normalized_span"]
            prefix = origin["sheet_cell"]["cell"]["coordinate"] + ": "
            left = cursor + len(prefix)
            result.append((left, left + b - a, a, b))
            cursor = left + b - a + 2
        return result
    for a, b in source.get("source_spans", [[0, len(source["content"])]]):
        result.append((cursor, cursor + b - a, a, b))
        cursor += b - a
    return result


def _coordinates(source: dict, start: int, end: int) -> dict:
    if "_page_segments" in source:
        return {
            "page_spans": [
                {"page": page, "start": max(start, left) - left, "end": min(end, right) - left}
                for left, right, page in source["_page_segments"]
                if left < end and right > start
            ]
        }
    return {
        "source_spans": [
            [a + max(start, left) - left, a + min(end, right) - left]
            for left, right, a, _ in _segments(source)
            if left < end and right > start
        ]
    }


def image_layout(source: dict):
    """Parse references in their complete literal context once per selection."""
    from ultrakb.markdown_images import ImageReference

    if "_page_segments" in source:
        return [ImageReference(**ref) for ref in source.get("_qualified_page_images", [])]
    result = []
    for reference in source.get("_qualified_images", []):
        for left, right, native, stop in _segments(source):
            if native <= reference["start"] < reference["end"] <= stop:
                result.append(
                    ImageReference(
                        **{
                            **reference,
                            "start": left + reference["start"] - native,
                            "end": left + reference["end"] - native,
                        }
                    )
                )
                break
    return result


def _window(source: dict, start: int, end: int, layout=None, tables=None, images=None) -> dict:
    images = image_layout(source) if images is None else images
    coordinates = _coordinates(source, start, end)
    spans = coordinates.get("source_spans", [])
    origins = [
        origin
        for a, b in spans
        for origin in clip_text_origins(source.get("origin_locators", []), a, b)
    ]
    result = {
        "content": source["content"][start:end],
        **coordinates,
        "unit_kind": source.get("unit_kind", "text"),
        "coverage": "complete" if start == 0 and end == len(source["content"]) else "partial",
        "continuation": end if end < len(source["content"]) else None,
    }
    if "_page_segments" in source:
        result["units"] = [
            _page_unit(unit, result["page_spans"])
            for unit in source.get("units", [])
            if any(span["page"] == unit["page"] for span in result["page_spans"])
        ]
        result.pop("origins", None)
    context = _display_context(source, start, layout)
    if context:
        result["display_context"] = context
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
    elif "_page_segments" not in source:
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
    qualified = {
        (heading["title"], tuple(heading["source_span"]))
        for heading in source.get("_qualified_headings", [])
    }
    allowed_context = {
        "heading_path": {span for _, span in qualified},
        "table_header": {
            tuple(header["header_span"]) for header in source.get("_qualified_table_headers", [])
        },
    }
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
                        "display_context": [
                            context
                            for context in unit.get("display_context", [])
                            if context.get("kind") not in allowed_context
                            or any(
                                list(context.get("source_spans", [])) == [list(span)]
                                for span in allowed_context[context["kind"]]
                            )
                        ],
                        "headings": [
                            heading
                            for heading in unit.get("headings", [])
                            if (heading["title"], tuple(heading["source_span"])) in qualified
                        ],
                    }
                )
            )
    if blocks:
        result["blocks"] = blocks
    if "_qualified_tables" in source:
        native_tables = table_metadata(
            source["_qualified_tables"],
            start,
            end,
            lambda a, b: {"source_spans": [[a, b]]},
            spans=spans,
        )
    elif "_page_segments" in source:
        native_tables = table_metadata(
            source.get("_qualified_page_tables", []),
            start,
            end,
            lambda a, b: _coordinates(source, a, b),
        )
    else:
        # A legacy string has no recorded syntax. Preserve its text without
        # promoting literal markup to native cell evidence at any caller.
        native_tables = []
    if native_tables:
        result["native_tables"] = native_tables
        result["table_interpretation"] = TABLE_INTERPRETATION
    if any(unit.get("images") for unit in result.get("units", [])) or any(
        start <= ref.start and ref.end <= end for ref in images
    ):
        result["figure_interpretation"] = FIGURE_INTERPRETATION
    return result


def _page_unit(unit: dict, spans: list[dict]) -> dict:
    """Page inventory is not evidence that each figure belongs to an excerpt."""
    from ultrakb.source_contract import page_image_selection

    selected = [span for span in spans if span["page"] == unit["page"]]
    complete = any(span["start"] == 0 and span["end"] == len(unit["content"]) for span in selected)
    return {
        **{
            key: value
            for key, value in unit.items()
            if key
            not in {
                "content",
                "parts",
                "origin_locators",
                "images",
                "content_spans",
                "image_reference_spans",
                "slide",
            }
        },
        "text_coverage": "complete" if complete else "partial",
        **(
            {
                "slide": {
                    key: value
                    for key, value in unit["slide"].items()
                    if key not in {"body", "notes"}
                }
            }
            if unit.get("slide")
            else {}
        ),
        "structure_coverage": "recorded" if unit.get("source_structure_policy") else "unconfirmed",
        **page_image_selection(unit, selected, complete=complete),
    }


def slice_evidence(
    source: dict, start: int, end: int, *, layout=None, tables=None, images=None
) -> dict:
    """Retain native positions and partial-value metadata for one exact subrange."""
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source["content"])
    ):
        raise ValueError("Evidence subrange is outside its original selection")
    return _window(source, start, end, layout, tables, images)


def _display_context(source: dict, offset: int, layout=None) -> list[dict]:
    if offset == 0:
        return []
    text = source["content"]
    segments = _segments(source)
    native_offset = next(
        (native + offset - left for left, right, native, _ in segments if left <= offset < right),
        None,
    )
    if native_offset is None:
        return []
    path: list[dict] = []
    context: list[dict] = []
    for heading in source.get("_qualified_headings", []):
        a, b = heading["source_span"]
        if a >= native_offset:
            break
        path = [item for item in path if item["level"] < heading["level"]] + [heading]
    for heading in path:
        a, b = heading["source_span"]
        located = next(
            (
                (left + a - native, left + b - native)
                for left, right, native, stop in segments
                if native <= a < b <= stop
            ),
            None,
        )
        if located is None:
            continue
        start, end = located
        context.append(
            {"kind": "heading_path", "text": text[start:end], **_coordinates(source, start, end)}
        )
    for table in source.get("_qualified_table_headers", []):
        a, b = table["source_span"]
        _, header = table["header_span"]
        if not a < native_offset < b:
            continue
        for left, right, native, stop in segments:
            if native <= a < header <= stop:
                start = left + a - native
                header_end = left + header - native
                context.append(
                    {
                        "kind": "table_header",
                        "text": text[start:header_end],
                        **_coordinates(source, start, header_end),
                    }
                )
    return context


def project_evidence(
    source: dict,
    *,
    offset: int = 0,
    budget: int = MAX_PAYLOAD_CHARS,
    fits=None,
    allow_partial: bool = False,
    layout=None,
    tables=None,
    images=None,
) -> dict:
    """Paginate after serialization, at paragraph boundaries outside cell values.

    By default an indivisible oversized cell/paragraph returns an explicit gap.
    Document-wide callers may opt into partial reads, with exact continuation
    and incomplete value metadata. A cached layout avoids reparsing each batch.
    """
    text = source["content"]
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("Continuation is outside this evidence selection")
    layout = layout or (markdown_layout(text) if offset else None)
    if tables is None:
        tables = []
    images = image_layout(source) if images is None else images
    result = _window(source, offset, len(text), layout, tables, images)
    fits = fits or (lambda payload: len(json.dumps(payload, ensure_ascii=False)) <= budget)
    if fits(result):
        return result
    protected = []
    for origin in source.get("origin_locators", []):
        if origin.get("sheet_cell") or origin.get("csv"):
            a, b = origin["normalized_span"]
            for left, right, c, d in _segments(source):
                if a < d and b > c:
                    protected.append((left + max(a, c) - c, min(right, left + b - c)))
    layout = layout or markdown_layout(text)
    tokens, lines = layout
    ends = sorted(
        {
            lines[token.map[1]]
            for token in tokens
            if token.map
            and (token.level == 0 or token.type == "tr_open")
            and lines[token.map[1]] > offset
        }
    )
    ends = [end for end in ends if not any(a < end < b for a, b in protected)]
    ends.append(len(text))
    low, high, best = 0, len(ends) - 1, None
    while low <= high:
        middle = (low + high) // 2
        candidate = _window(source, offset, ends[middle], layout, tables, images)
        if fits(candidate):
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    if best is None:
        if allow_partial:
            # Document-wide processing must consume indivisible paragraphs/cells too.
            # _window clips native coordinates and marks partial cell values honestly.
            low, high = offset + 1, len(text)
            while low <= high:
                middle = (low + high) // 2
                candidate = _window(source, offset, middle, layout, tables, images)
                if fits(candidate):
                    best, low = candidate, middle + 1
                else:
                    high = middle - 1
            if best is not None:
                return best
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


def preceding_evidence(
    source: dict, offset: int, max_chars: int, *, layout=None, tables=None, images=None
) -> dict:
    """Project preceding original context separately from the primary processed range."""
    if type(offset) is not int or not 0 <= offset <= len(source["content"]) or max_chars <= 0:
        raise ValueError("Invalid preceding original range")
    tokens, offsets = layout or markdown_layout(source["content"])
    floor = max(0, offset - max_chars)
    starts = [offsets[t.map[0]] for t in tokens if t.map and t.level == 0]
    start = next((n for n in starts if floor <= n < offset), floor)
    context = _window(source, start, offset, (tokens, offsets), tables, images)
    context.pop("continuation", None)
    return {**context, "primary": False}


def following_evidence(
    source: dict, offset: int, max_chars: int, *, layout=None, tables=None, images=None
) -> dict:
    """Keep an adjacent qualification visible before generating the preceding rule."""
    tokens, offsets = layout or markdown_layout(source["content"])
    ceiling = min(len(source["content"]), offset + max_chars)
    ends = [offsets[t.map[1]] for t in tokens if t.map and t.level == 0]
    end = max((n for n in ends if offset < n <= ceiling), default=ceiling)
    context = _window(source, offset, end, (tokens, offsets), tables, images)
    context.pop("continuation", None)
    return {**context, "primary": False}
