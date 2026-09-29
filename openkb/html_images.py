"""Locate HTML image src values without reserializing markup or other attributes."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_ATTRIBUTE = re.compile(r"""\s+([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")


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


def html_image_references(text: str):
    """HTML uses its own src grammar even when indented or containing Markdown text."""
    from openkb.markdown_images import ImageReference

    return [
        ImageReference(a, b, source, "", "", True)
        for a, b, source in html_image_sources(text, resolve_base=True)
    ]
