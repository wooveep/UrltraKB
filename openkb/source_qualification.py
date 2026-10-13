"""Source roles require recorded syntax or typed payloads, not literal spelling."""

import re
from functools import lru_cache

MARKDOWN_POLICY = "markdown-unicode-preserved-v1:markdown-it-py-4.2.0-commonmark-images"
SOURCE_STRUCTURE_POLICY = "source-structure-v1"


def is_image_payload(value: dict) -> bool:
    """Only typed wire media is an image payload; source strings remain text."""
    return str(value.get("type")) in {"image_url", "input_image", "output_image", "image"} or any(
        isinstance(value.get(key), dict)
        and str(value[key].get("mime_type", value[key].get("mimeType", ""))).startswith("image/")
        for key in ("inline_data", "inlineData")
    )


def html_text_role(literal: list[str]) -> str:
    """Visible text, known hidden code, or an unresolved fallback control."""
    if any(tag in {"script", "style", "title", "template"} for tag in literal):
        return "hidden"
    if any(tag in {"iframe", "noembed", "noframes", "noscript", "textarea"} for tag in literal):
        return "unresolved"
    return "visible"


@lru_cache(maxsize=8)
def literal_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Cache only exact frozen text; line maps retain original character offsets."""
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(text, locate_html=True)
    spans = [
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in tokens
        if token.map and token.type in {"fence", "code_block"}
    ]
    # Feed only actual HTML markup to the shared literal parser. Escaped tags
    # and tags inside code examples must not open or close an HTML literal.
    markup = [char if char in "\r\n" else " " for char in text]
    cells: dict[int, tuple[str, list[int], int]] = {}
    for index, block in enumerate(tokens):
        cell = index > 0 and tokens[index - 1].type in {"th_open", "td_open"}
        html = [child for child in block.children or [] if child.type == "html_inline"]
        if not block.map or (block.type != "html_block" and not html and not cell):
            continue
        if block.type == "html_block":
            start, end = (offsets[line] for line in block.map)
            markup[start:end] = text[start:end]
            continue
        positions: list[int] = []
        row = block.map[0]
        for line in block.content.split("\n"):
            if row + 1 >= len(offsets) and not line:
                break
            raw = text[offsets[row] : offsets[row + 1]].rstrip("\r\n").replace("\x00", "\ufffd")
            if cell:
                if row not in cells:
                    # The pinned table rule removes the backslash immediately
                    # before an escaped pipe. Keep a map to the unchanged row.
                    native = [
                        i
                        for i, char in enumerate(raw)
                        if not (char == "\\" and i + 1 < len(raw) and raw[i + 1] == "|")
                    ]
                    cells[row] = ("".join(raw[i] for i in native), native, 0)
                inline, native, cursor = cells[row]
                shift = inline.find(line, cursor)
                cells[row] = (inline, native, shift + len(line))
            else:
                native = list(range(len(raw)))
                shift = raw.find(line)
            if shift < 0:
                raise ValueError("Cannot locate HTML markup in its original source line")
            positions.extend(offsets[row] + i for i in native[shift : shift + len(line)])
            positions.append(offsets[row] + len(raw))
            row += 1
        for child in html:
            start, end = child.meta["original_markup_span"]
            left, right = positions[start], positions[end - 1] + 1
            markup[left:right] = text[left:right]
    spans.extend(html_literal_spans("".join(markup)))
    return tuple(sorted(spans))


def html_literal_spans(markup: str) -> list[tuple[int, int]]:
    from markdown_it.common.html_blocks import block_names

    from openkb.html_images import HTMLLiteralContext

    offsets = [0, *(match.end() for match in re.finditer("\n", markup))]
    visible = []
    void = {
        "area",
        "base",
        "basefont",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    class LocatedLiterals(HTMLLiteralContext):
        def __init__(self):
            super().__init__()
            # Unescape would change data lengths and invalidate native offsets.
            self.convert_charrefs = False
            self.containers: list[str] = []

        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            # Native commands use angle parameters such as <vm>/<snap-name>.
            # A valid inline tag token alone does not prove a content container.
            if not self.literal and tag in block_names and tag not in void:
                self.containers.append(tag)

        def handle_endtag(self, tag):
            was_literal = bool(self.literal)
            super().handle_endtag(tag)
            if not was_literal and tag in self.containers:
                index = len(self.containers) - 1 - self.containers[::-1].index(tag)
                del self.containers[index:]

        def handle_data(self, data):
            if not self.literal and not self.containers:
                row, column = self.getpos()
                start = offsets[row - 1] + column
                visible.append((start, start + len(data)))

    parser = LocatedLiterals()
    parser.feed(markup)
    parser.close()
    # CommonMark HTML blocks may continue long after </table> or </div>.
    # Exclude actual markup and container contents, not that whole block's tail.
    spans = []
    cursor = 0
    for start, end in visible:
        if cursor < start:
            spans.append((cursor, start))
        cursor = end
    if cursor < len(markup):
        spans.append((cursor, len(markup)))
    return spans


@lru_cache(maxsize=8)
def markup_headings(text: str) -> tuple[tuple[int, int, int, str], ...]:
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(text)
    literals = literal_spans(text)
    return tuple(
        (offsets[token.map[0]], offsets[token.map[1]], int(token.tag[1:]), tokens[i + 1].content)
        for i, token in enumerate(tokens)
        if token.type == "heading_open"
        and token.level == 0
        and token.map
        # CommonMark can emit top-level tokens after a blank line inside HTML.
        # Source containers still own that text until their actual closing tag.
        and not any(start <= offsets[token.map[0]] < end for start, end in literals)
    )


def markup_source(policy: str | None) -> bool:
    from openkb.text_formats import text_normalization_policy

    return policy in {MARKDOWN_POLICY, text_normalization_policy("html")}


def qualified_headings(text: str, policy: str | None, origins=()) -> tuple:
    """Return native heading positions, preserving full-source literal context."""
    if not markup_source(policy):
        return ()
    return tuple(
        (start, end, level, title)
        for start, end, level, title in markup_headings(text)
        if policy == MARKDOWN_POLICY
        or any(
            origin.get("kind") == "html"
            and (origin.get("html") or {}).get("tag") == f"h{level}"
            and origin["normalized_span"][0] <= start < end <= origin["normalized_span"][1]
            for origin in origins
        )
    )


def source_headings(raw: dict) -> list[dict]:
    return [
        {"title": title, "source_span": [start, end], "level": level}
        for start, end, level, title in qualified_headings(
            raw["text"], raw.get("normalization_policy"), raw.get("origins", [])
        )
    ]


@lru_cache(maxsize=8)
def _markup_table_headers(text: str, *, check_literals: bool = True) -> tuple:
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(text)
    literals = literal_spans(text) if check_literals else ()
    return tuple(
        (offsets[token.map[0]], offsets[token.map[1]], offsets[token.map[0] + 2])
        for token in tokens
        if token.type == "table_open"
        and token.map
        and not any(start <= offsets[token.map[0]] < end for start, end in literals)
    )


def qualified_table_headers(text: str, policy: str | None, origins=()) -> tuple:
    """Display headers require native markup or a mapped CSV-generated header."""
    if policy == MARKDOWN_POLICY:
        return _markup_table_headers(text)
    from openkb.text_formats import text_normalization_policy

    if policy != text_normalization_policy("csv"):
        return ()
    return tuple(
        (start, end, header)
        # CSV cells are escaped by the recorded converter. Their inline token
        # spelling may differ from the source; the native mapping proves this header.
        for start, end, header in _markup_table_headers(text, check_literals=False)
        if any(
            origin.get("kind") == "generated"
            and tuple(origin["normalized_span"]) == (start, header)
            for origin in origins
        )
        and any(
            origin.get("kind") == "csv_cell"
            and origin.get("csv")
            and header <= origin["normalized_span"][0] <= origin["normalized_span"][1] <= end
            for origin in origins
        )
    )


def source_table_headers(raw: dict) -> list[dict]:
    return [
        {"source_span": [start, end], "header_span": [start, header]}
        for start, end, header in qualified_table_headers(
            raw["text"], raw.get("normalization_policy"), raw.get("origins", [])
        )
    ]


def source_images(raw: dict) -> list[dict]:
    """Qualify markup references in their complete recorded source context."""
    if not markup_source(raw.get("normalization_policy")):
        return []
    from dataclasses import asdict

    from openkb.markdown_images import image_references

    return [asdict(reference) for reference in image_references(raw["text"])]


def source_tables(raw: dict, spans: list[list[int]]) -> list[dict]:
    """Read table roles before clipping; never include unread cell values."""
    if not markup_source(raw.get("normalization_policy")):
        return []
    from openkb.source_tables import select_tables, table_layout

    return select_tables(table_layout(raw["text"]), spans)
