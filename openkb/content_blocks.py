"""Structural slices of frozen text; display aids never become original evidence."""

from bisect import bisect_right
from typing import Literal

import regex
from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import Field, model_validator

from openkb.source_records import Record

BLOCK_POLICY = "commonmark-blocks-v1:target2000:regex-2026.5.9-graphemes:no-overlap"


class DisplayContext(Record):
    kind: Literal["heading_path", "table_header", "fence_open", "fence_close"]
    text: str
    source_spans: tuple[tuple[int, int], ...] = ()


class SourceHeading(Record):
    title: str
    source_span: tuple[int, int]


class ContentBlock(Record):
    ordinal: int = Field(ge=1)
    source_spans: tuple[tuple[int, int], ...]
    overlap_spans: tuple[tuple[int, int], ...] = ()
    display_context: tuple[DisplayContext, ...] = ()
    headings: tuple[SourceHeading, ...] = ()

    @model_validator(mode="after")
    def ordered_ranges(self):
        if not self.source_spans or any(a < 0 or b < a for a, b in self.source_spans):
            raise ValueError("Content block requires ordered Unicode ranges")
        return self


def split_blocks(text: str) -> tuple[ContentBlock, ...]:
    """Pack natural Markdown blocks, cutting oversized blocks only at grapheme ends."""
    # CommonMark's line maps recognize CR/LF, not Python's broader splitlines
    # set (NEL, U+2028, U+2029, vertical tab). Keep offsets in the original bytes'
    # decoded code-point sequence even when CRLF normalizes inside the parser.
    lines = regex.split(r"(?<=\n)|(?<=\r)(?!\n)", text)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    natural: dict[int, tuple[int, Token]] = {}
    structures: list[tuple[int, int, Token]] = []
    headings = []
    for index, token in enumerate(tokens):
        if token.map:
            start, end = (offsets[line] for line in token.map)
            if token.level == 0:
                natural.setdefault(start, (end, token))
            if token.type in {"fence", "table_open"}:
                structures.append((start, end, token))
            if token.type == "heading_open":
                headings.append((start, end, int(token.tag[1:]), tokens[index + 1].content))
    starts = sorted({0, *natural, len(text)})
    # Structural boundaries and every grapheme end are deterministic coordinates
    # in the frozen text. A single giant grapheme stays intact even over target.
    graphemes = [0, *(match.end() for match in regex.finditer(r"\X", text))]
    slices = []
    cursor = 0
    for left, right in zip(starts, starts[1:]):
        if right - cursor > 2000 and left > cursor:
            slices.append((cursor, left))
            cursor = left
        while right - cursor > 2000:
            cut = graphemes[bisect_right(graphemes, cursor + 2000) - 1]
            if cut == cursor:
                cut = graphemes[bisect_right(graphemes, cursor)]
            slices.append((cursor, cut))
            cursor = cut
    if cursor < len(text) or not slices:
        slices.append((cursor, len(text)))
    result = []
    for number, (start, end) in enumerate(slices, 1):
        context = []
        path: list[tuple[int, int, int, str]] = []
        for heading in headings:
            if heading[0] >= start:
                break
            path = [item for item in path if item[2] < heading[2]] + [heading]
        for a, b, _, _ in path:
            context.append(
                DisplayContext(kind="heading_path", text=text[a:b], source_spans=((a, b),))
            )
        for a, b, token in structures:
            if token.type == "fence" and a < end and start < b:
                if a < start:
                    opening_end = offsets[token.map[0] + 1]
                    context.append(
                        DisplayContext(
                            kind="fence_open",
                            text=text[a:opening_end],
                            source_spans=((a, opening_end),),
                        )
                    )
                if end < b:
                    context.append(
                        DisplayContext(kind="fence_close", text="\n" + token.markup + "\n")
                    )
            if token.type == "table_open" and a < start < b:
                header_end = offsets[min(token.map[0] + 2, len(lines))]
                context.append(
                    DisplayContext(
                        kind="table_header",
                        text=text[a:header_end],
                        source_spans=((a, header_end),),
                    )
                )
        result.append(
            ContentBlock(
                ordinal=number,
                source_spans=((start, end),),
                display_context=tuple(context),
                headings=tuple(
                    SourceHeading(title=title, source_span=(a, b))
                    for a, b, _, title in headings
                    if start <= a < end
                ),
            )
        )
    return tuple(result)


def validate_blocks(text: str, blocks: tuple[ContentBlock, ...]) -> None:
    cursor = 0
    boundaries = {0, *(match.end() for match in regex.finditer(r"\X", text))}
    for ordinal, block in enumerate(blocks, 1):
        if block.ordinal != ordinal or block.overlap_spans:
            raise ValueError("This block policy requires contiguous ordinals without overlap")
        for start, end in block.source_spans:
            if (
                start != cursor
                or end > len(text)
                or start not in boundaries
                or end not in boundaries
            ):
                raise ValueError("Content blocks must cover the frozen text at grapheme boundaries")
            cursor = end
        for context in block.display_context:
            if context.source_spans and context.text != "".join(
                text[a:b] for a, b in context.source_spans
            ):
                raise ValueError("Display context no longer matches its source")
            if any(not 0 <= a <= b <= len(text) for a, b in context.source_spans):
                raise ValueError("Display context range is outside the source")
    if not blocks or cursor != len(text):
        raise ValueError("Content blocks leave source text uncovered")
