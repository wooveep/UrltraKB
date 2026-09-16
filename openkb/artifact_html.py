"""Edit HTML text and navigable attributes at their original byte positions."""

import html
import re
from html.parser import HTMLParser

_LITERAL = {"script", "style", "pre", "code", "textarea", "template", "title"}
_ATTRIBUTE = re.compile(
    r"""(?P<name>[^\s=<>/]+)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"""
)
_MARKER = re.compile(r"\[@?evidence:([^\]\r\n]+)\]")


def rewrite_html(text, destination, marker=None):
    """Callbacks receive only links or evidence markers that are actual document content."""
    offsets = [0, *(match.end() for match in re.finditer("\n", text))]
    edits = []

    class Document(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.literal = []

        def source_offset(self):
            line, column = self.getpos()
            return offsets[line - 1] + column

        def handle_starttag(self, tag, attrs):
            if tag in _LITERAL:
                self.literal.append(tag)
            if self.literal:
                return
            field = {"a": "href", "img": "src", "source": "src"}.get(tag)
            if not field:
                return
            start = self.source_offset()
            for match in _ATTRIBUTE.finditer(self.get_starttag_text()):
                if match["name"].lower() != field:
                    continue
                group = next(key for key in ("double", "single", "bare") if match[key] is not None)
                old = html.unescape(match[group])
                new = destination(old)
                if new != old:
                    edits.append(
                        (
                            start + match.start(group),
                            start + match.end(group),
                            html.escape(new, quote=True),
                        )
                    )

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if tag in self.literal:
                self.literal = self.literal[: self.literal.index(tag)]

        def handle_data(self, data):
            if self.literal or marker is None:
                return
            for match in _MARKER.finditer(data):
                value = marker("[evidence:" + match[1] + "]")
                if value is not None:
                    start = self.source_offset()
                    edits.append((start + match.start(), start + match.end(), value))

    parser = Document()
    parser.feed(text)
    parser.close()
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text
