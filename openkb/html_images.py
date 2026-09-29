"""Locate HTML image src values without reserializing markup or other attributes."""

import re
from html.parser import HTMLParser

_ATTRIBUTE = re.compile(r"""\s+([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")


def html_image_sources(text: str) -> list[tuple[int, int, str]]:
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer("\n", text))
    sources = []

    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "img":
                return
            source = next((value for key, value in attrs if key == "src"), None)
            if source is None:
                return
            raw = self.get_starttag_text()
            matched = next(
                (match for match in _ATTRIBUTE.finditer(raw) if match.group(1).lower() == "src"),
                None,
            )
            if matched is None:
                raise ValueError("HTML image src could not be located")
            group = next(index for index in range(2, 5) if matched.group(index) is not None)
            row, column = self.getpos()
            start, end = matched.span(group)
            base = offsets[row - 1] + column
            sources.append((base + start, base + end, source))

    parser = Images(convert_charrefs=True)
    parser.feed(text)
    parser.close()
    return sources
