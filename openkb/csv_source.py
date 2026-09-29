"""Strict CSV values and character coordinates, rendered without business type inference."""

import csv
import html
import io
import re
import sys

from openkb.text_source import CsvCell, TextOrigin

CSV_POLICY = "csv-excel-strict-strings-v1:all-records-as-data:generated-column-labels"


def _field_spans(record: str) -> list[tuple[int, int]]:
    """Locate lexemes only; the standard-library reader owns parsing and validity."""
    body = record.removesuffix("\r\n").removesuffix("\n").removesuffix("\r")
    if not body:
        return []
    spans = []
    start = cursor = 0
    quoted = False
    while cursor < len(body):
        character = body[cursor]
        if character == '"':
            if quoted and cursor + 1 < len(body) and body[cursor + 1] == '"':
                cursor += 2
                continue
            if quoted or cursor == start:
                quoted = not quoted
        elif character == "," and not quoted:
            spans.append((start, cursor))
            start = cursor + 1
        cursor += 1
    spans.append((start, len(body)))
    return spans


def _cell_markdown(value: str) -> str:
    escaped = re.sub(r"([\\|`*_{}\[\]()])", r"\\\1", html.escape(value, quote=False))
    return re.sub(r"\r\n|\r|\n", "<br>", escaped).replace(" ", "&#32;").replace("\t", "&#9;")


def normalize_csv(original: str, offset: int) -> tuple[str, tuple[TextOrigin, ...]]:
    # Lift csv's small default field cap without clipping a single value. Parsing
    # remains bounded by the supplied source and raises normally on allocation failure.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    text = original[offset:]
    line_ends = [offset]
    for line in re.split(r"(?<=\n)|(?<=\r)(?!\n)", text):
        line_ends.append(line_ends[-1] + len(line))
    reader = csv.reader(io.StringIO(text, newline=""), dialect="excel", strict=True)
    rows = []
    previous_line = 0
    try:
        for values in reader:
            start, end = line_ends[previous_line], line_ends[reader.line_num]
            spans = _field_spans(original[start:end])
            if len(spans) != len(values):
                raise ValueError("CSV fields cannot be mapped to their original record")
            rows.append((values, spans, start, end, (previous_line + 1, reader.line_num)))
            previous_line = reader.line_num
    except csv.Error as exc:
        raise ValueError(f"CSV parse error at physical line {reader.line_num}: {exc}") from exc
    chunks: list[str] = []
    origins: list[TextOrigin] = []
    normalized = 0

    def append(value: str, a: int, b: int, kind, cell: CsvCell | None = None):
        nonlocal normalized
        chunks.append(value)
        origins.append(
            TextOrigin(
                normalized_span=(normalized, normalized + len(value)),
                original_span=(a, b),
                kind=kind,
                csv=cell,
            )
        )
        normalized += len(value)

    if offset:
        append("", 0, offset, "bom")
    width = max((len(row[0]) for row in rows), default=0)
    if rows:
        width = max(1, width)
        header = "| " + " | ".join(f"Column {i}" for i in range(1, width + 1)) + " |\n"
        header += "| " + " | ".join("---" for _ in range(width)) + " |\n"
        append(header, offset, offset, "generated")
    for ordinal, (values, spans, start, end, lines) in enumerate(rows, 1):
        append("| ", start, start, "generated")
        cursor = start
        for column, (value, (a, b)) in enumerate(zip(values, spans, strict=True), 1):
            a, b = start + a, start + b
            if a > cursor:
                append("| ", cursor, a, "csv_separator")
            append(
                _cell_markdown(value),
                a,
                b,
                "csv_cell",
                CsvCell(row=ordinal, column=column, physical_lines=lines, value=value),
            )
            append(" ", b, b, "generated")
            cursor = b
        # Do not expand ragged rows to the maximum width. Their missing fields
        # remain distinct from actual empty values, with linear artifact size.
        if not values:
            append(" ", cursor, cursor, "generated")
        append("|\n", cursor, end, "csv_separator")
    return "".join(chunks), tuple(origins)
