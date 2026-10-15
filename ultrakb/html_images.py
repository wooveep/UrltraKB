"""Locate HTML image src values without reserializing markup or other attributes."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_ATTRIBUTE = re.compile(r"""\s+([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")
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
_LITERAL_TAGS = {"pre", "code", "template"} | _RAW_TEXT_TAGS


class HTMLLiteralContext(HTMLParser):
    """Carry literal ancestry across CommonMark HTML blocks and inline tokens."""

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
        self.handle_starttag(tag, attrs)


def _attribute_sources(
    text: str, tag_names: set[str], attribute: str, *, resolve_base: bool
) -> list[tuple[int, int, str]]:
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer("\n", text))
    sources = []
    base_url = None

    class References(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nonlocal base_url
            if tag.lower() == "base" and base_url is None:
                base_url = next((value for key, value in attrs if key == "href"), None)
            if tag.lower() not in tag_names:
                return
            source = next((value for key, value in attrs if key == attribute), None)
            if source is None:
                return
            raw = self.get_starttag_text()
            matched = next(
                (
                    match
                    for match in _ATTRIBUTE.finditer(raw)
                    if match.group(1).lower() == attribute
                ),
                None,
            )
            if matched is None:
                raise ValueError(f"HTML {tag} {attribute} could not be located")
            group = next(index for index in range(2, 5) if matched.group(index) is not None)
            row, column = self.getpos()
            start, end = matched.span(group)
            base = offsets[row - 1] + column
            sources.append((base + start, base + end, source))

    parser = References(convert_charrefs=True)
    parser.feed(text)
    parser.close()
    if resolve_base:
        return [(a, b, urljoin(base_url or "", source)) for a, b, source in sources]
    return sources


def html_image_sources(text: str, *, resolve_base: bool = False) -> list[tuple[int, int, str]]:
    return _attribute_sources(text, {"img"}, "src", resolve_base=resolve_base)


def html_link_targets(text: str) -> list[tuple[int, int, str]]:
    return _attribute_sources(text, {"a", "area"}, "href", resolve_base=True)


def html_image_references(text: str, *, context: HTMLLiteralContext | None = None):
    """HTML uses its own src grammar even when indented or containing Markdown text."""
    from ultrakb.markdown_images import ImageReference

    references = []
    offsets = [0] + [match.end() for match in re.finditer("\n", text)]
    base_url = None

    class References(HTMLLiteralContext):
        def handle_starttag(self, tag, attrs):
            nonlocal base_url
            super().handle_starttag(tag, attrs)
            if self.literal:
                return
            values = {}
            for key, value in attrs:
                values.setdefault(key, value)
            if tag == "base" and base_url is None:
                base_url = values.get("href")
            if tag != "img" or values.get("src") is None:
                return
            matched = next(
                match
                for match in _ATTRIBUTE.finditer(self.get_starttag_text())
                if match.group(1).lower() == "src"
            )
            group = next(index for index in range(2, 5) if matched.group(index) is not None)
            row, column = self.getpos()
            start, end = matched.span(group)
            base = offsets[row - 1] + column
            references.append(
                ImageReference(
                    base + start,
                    base + end,
                    values["src"],
                    values.get("alt") or "",
                    values.get("title") or "",
                    True,
                )
            )

    parser = References()
    if context is not None:
        parser.literal = list(context.literal)
    parser.feed(text)
    parser.close()
    if context is not None:
        context.reset()
        context.literal = parser.literal
    return [
        ImageReference(
            ref.start, ref.end, urljoin(base_url or "", ref.source), ref.alt, ref.title, True
        )
        for ref in references
    ]
