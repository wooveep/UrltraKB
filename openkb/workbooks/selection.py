"""Select disjoint physical cell ranges without expanding sparse worksheet bounds."""

from openkb.source_pages import PageRangeError


def select_cells(source: dict, specification: str | None) -> dict:
    if specification is None:
        return source
    if not source.get("sheet"):
        raise PageRangeError("Cell ranges require a worksheet source")
    from openpyxl.utils.cell import range_boundaries

    ranges = []
    for value in specification.upper().replace("$", "").split(","):
        try:
            left, top, right, bottom = range_boundaries(value.strip())
            if (
                any(type(number) is not int for number in (left, top, right, bottom))
                or not 1 <= left <= right <= 16384
                or not 1 <= top <= bottom <= 1048576
            ):
                raise ValueError("invalid bounds")
        except (ValueError, TypeError):
            raise PageRangeError("Use finite worksheet ranges such as A1:B3,D7") from None
        ranges.append((left, top, right, bottom))
    selected = []
    for origin in source.get("origin_locators", []):
        if not origin.get("sheet_cell"):
            continue
        cell = origin["sheet_cell"]["cell"]
        if any(a <= cell["column"] <= b and c <= cell["row"] <= d for a, c, b, d in ranges):
            selected.append(origin)
    text = source["content"]
    spans = [origin["normalized_span"] for origin in selected]
    return {
        **source,
        "content": "\n\n".join(
            f"{origin['sheet_cell']['cell']['coordinate']}: {text[a:b]}"
            for origin, (a, b) in zip(selected, spans)
        ),
        "origin_locators": selected,
        "source_spans": spans,
        "cell_range": specification,
        "char_range": None,
        "block_range": None,
        "units": [],
        "diagnostics": source.get("diagnostics", [])
        + ([] if selected else ["No retained cell values in the selected ranges."]),
    }
