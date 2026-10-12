"""Bounded structural observations of literal source HTML, without inferred headers."""

from html.parser import HTMLParser

from openkb.content_blocks import markdown_layout

MAX_TABLE_SLOTS = 16384
MAX_TABLE_CHARS = 1_000_000
_RAW_TEXT_TAGS = {
    "script",
    "style",
    "xmp",
    "iframe",
    "noembed",
    "noframes",
    "noscript",
    "textarea",
    "title",
    "plaintext",
}
_LITERAL_TAGS = {"pre", "code"} | _RAW_TEXT_TAGS
_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}
TABLE_INTERPRETATION = (
    "Rows and columns describe source layout, not counts of domain items. Repeated cell IDs "
    "refer to one merged cell in this table only. An explicit empty cell differs from an "
    "unresolved slot. Unread cell values are not supplied; read their native positions before "
    "using them. Headers are not inferred from row order."
)


class _LiteralContext(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.literal = []

    def handle_starttag(self, tag, attrs):
        if self.literal and self.literal[-1] in _RAW_TEXT_TAGS:
            return
        if tag in _LITERAL_TAGS:
            self.literal.append(tag)

    def handle_endtag(self, tag):
        if self.literal and self.literal[-1] != "plaintext" and tag == self.literal[-1]:
            self.literal.pop()

    def handle_startendtag(self, tag, attrs):
        # A self-closing flag does not close a non-void HTML literal element.
        self.handle_starttag(tag, attrs)


def plain_prose_blocks(tokens):
    """Return unchanged plain paragraphs, excluding literal HTML across blocks.

    Mixed formatting is deliberately conservative: do not reconstruct visible
    text by joining children or promote link/image destinations into prose.
    """
    context = _LiteralContext()
    for token in tokens:
        if token.type == "html_block":
            if len(token.content) > MAX_TABLE_CHARS:
                return  # Later HTML context is unknown; do not guess.
            context.feed(token.content)
        elif token.type == "inline":
            if not context.literal and all(
                child.type in {"text", "softbreak", "hardbreak"} for child in token.children or []
            ):
                yield token.content
            for child in token.children or []:
                if child.type == "html_inline":
                    context.feed(child.content)


class _Parser(_LiteralContext):
    def __init__(self, text, base):
        super().__init__()
        self.base = base
        self.text = text
        self.lines = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        self.tables = []
        self.table = self.row = self.cell = None
        self.group = 0

    def position(self):
        line, column = self.getpos()
        return self.base + self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if self.literal:
            return
        position = self.position()
        if tag == "table":
            if self.table is not None:
                raise ValueError("Nested table structure is unresolved")
            self.table = {"start": position, "rows": []}
        elif self.table is not None:
            if tag in {"thead", "tbody", "tfoot"}:
                if self.row is not None:
                    raise ValueError("A row group crosses an unfinished row")
                self.group += 1
            elif tag == "tr":
                if self.row is not None or len(self.table["rows"]) >= MAX_TABLE_SLOTS:
                    raise ValueError("Malformed or oversized table rows")
                self.row = {"start": position, "cells": [], "group": self.group}
                self.table["rows"].append(self.row)
            elif tag in {"td", "th"}:
                if self.row is None or self.cell is not None:
                    raise ValueError("Cell lacks a unique explicit row")
                attributes = dict(attrs)
                if len(attributes) != len(attrs):
                    raise ValueError("Duplicate cell attributes are unresolved")
                try:
                    height = int(attributes.get("rowspan", "1"))
                    width = int(attributes.get("colspan", "1"))
                except (ValueError, TypeError):
                    raise ValueError("Invalid table span") from None
                self.cell = {
                    "start": position,
                    "parts": [],
                    "tag": tag,
                    "height": height,
                    "width": width,
                    "kind": (
                        "unresolved"
                        if attributes.get("data-openkb-cell") == "unresolved"
                        else "cell"
                    ),
                    "header": tag == "th",
                }
                if not 1 <= self.cell["height"] <= MAX_TABLE_SLOTS or not (
                    1 <= self.cell["width"] <= MAX_TABLE_SLOTS
                ):
                    raise ValueError("Table span exceeds structural bounds")
                self.row["cells"].append(self.cell)
            elif self.cell is not None:
                if tag == "br":
                    self.cell["parts"].append("\n")
                elif tag in {"img", "svg", "video", "audio", "object", "iframe"}:
                    self.cell["nontext_content"] = True

    def handle_startendtag(self, tag, attrs):
        if self.literal:
            return
        if tag in _TABLE_TAGS | _LITERAL_TAGS:
            raise ValueError("Self-closing non-void table or literal element is unresolved")
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["parts"].append(data)

    def handle_endtag(self, tag):
        if self.literal:
            super().handle_endtag(tag)
            return
        end = self.base + self.text.index(">", self.position() - self.base) + 1
        if tag in {"td", "th"} and self.table is not None:
            if self.cell is None or self.cell["tag"] != tag:
                raise ValueError("Unmatched cell ending")
            self.cell["end"] = end
            self.cell = None
        elif tag == "tr" and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ValueError("Unfinished or unmatched table row")
            self.row["end"] = end
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.row is not None or not self.table["rows"]:
                raise ValueError("Incomplete table structure")
            self.table["end"] = end
            self.tables.append(_place(self.table))
            self.table = None
        elif tag in {"thead", "tbody", "tfoot"} and self.table is not None:
            if self.row is not None:
                raise ValueError("Unfinished row group")
            self.group += 1


def _place(table):
    rows = table["rows"]
    occupied, cells, columns = {}, [], 0
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row["cells"]:
            cell = {**cell, "value": "".join(cell["parts"])}
            cell.pop("parts")
            while (row_index, column) in occupied:
                column += 1
            height, width = cell["height"], cell["width"]
            if row_index + height > len(rows) or len(rows) * (column + width) > MAX_TABLE_SLOTS:
                raise ValueError("Table grid exceeds structural bounds")
            if any(rows[n]["group"] != row["group"] for n in range(row_index, row_index + height)):
                raise ValueError("A merged cell crosses its explicit row group")
            identity = f"r{row_index + 1}c{column + 1}"
            for y in range(row_index, row_index + height):
                for x in range(column, column + width):
                    if (y, x) in occupied:
                        raise ValueError("Overlapping merged cells are unresolved")
                    occupied[y, x] = identity
            cells.append(
                {
                    **cell,
                    "id": identity,
                    "rows": [row_index + 1, row_index + height],
                    "columns": [column + 1, column + width],
                }
            )
            column += width
            columns = max(columns, column)
    return {**table, "cells": cells, "occupied": occupied, "columns": columns}


def table_layout(text, layout=None):
    """Parse once per evidence projection; never share mutable parser state."""
    if "<table" not in text.lower():
        return []
    tokens, lines = layout or markdown_layout(text)
    tables = []
    context = _LiteralContext()
    for token in tokens:
        if token.type == "inline":
            for child in token.children or []:
                if child.type == "html_inline":
                    context.feed(child.content)
        if token.type != "html_block" or not token.map:
            continue
        start, end = (lines[n] for n in token.map)
        body = text[start:end]
        if "<table" not in body.lower():
            context.feed(body)
            continue
        try:
            if len(body) > MAX_TABLE_CHARS:
                # Do not guess the HTML context after an unparsed large block.
                context.literal = [""]
                raise ValueError("Table text exceeds structural processing bounds")
            parser = _Parser(body, start)
            parser.literal = list(context.literal)
            parser.feed(body)
            parser.close()
            if parser.table is not None:
                raise ValueError("The table boundary is incomplete")
            tables.extend(parser.tables)
            # The fragment parser already consumed any raw-text closing tag.
            # Do not retain the context parser's earlier CDATA tokenizer state.
            context.reset()
            context.literal = parser.literal
        except (ValueError, TypeError) as error:
            tables.append({"start": start, "end": end, "diagnostic": str(error)})
            if len(body) <= MAX_TABLE_CHARS:
                # A rejected grid cannot erase later literal closing tags.
                context.feed(body)
    return tables


def table_metadata(tables, start, end, coordinates):
    result = []
    for table in tables:
        if table["start"] >= end or table["end"] <= start:
            continue
        item = {
            "native_location": coordinates(table["start"], table["end"]),
            "coverage": "complete"
            if start <= table["start"] and end >= table["end"]
            else "partial",
        }
        if "diagnostic" in table:
            result.append({**item, "status": "unresolved", "diagnostic": table["diagnostic"]})
            continue
        rows = [
            n for n, row in enumerate(table["rows"]) if row["start"] < end and row["end"] > start
        ]
        row_cells = [
            {
                "row": n + 1,
                "cells": [table["occupied"].get((n, c)) for c in range(table["columns"])],
            }
            for n in rows
        ]
        visible = {
            identity for row in row_cells for identity in row["cells"] if identity is not None
        }
        cells = []
        for cell in table["cells"]:
            if cell["id"] not in visible:
                continue
            complete = start <= cell["start"] and end >= cell["end"]
            intersects = cell["start"] < end and cell["end"] > start
            observed = {key: cell[key] for key in ("id", "rows", "columns", "kind")}
            observed["native_location"] = coordinates(cell["start"], cell["end"])
            observed["value_coverage"] = (
                "complete" if complete else "partial" if intersects else "unread"
            )
            if complete and cell["kind"] != "unresolved":
                observed["value"] = cell["value"]
            for key in ("header", "nontext_content"):
                if cell.get(key):
                    observed[key] = True
            cells.append(observed)
        result.append({**item, "status": "observed", "rows": row_cells, "cells": cells})
    return result
