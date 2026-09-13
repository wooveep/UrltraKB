"""Recognize a narrow repeated-output failure without inventing replacement text."""

from html.parser import HTMLParser

from markdown_it import MarkdownIt


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def transcribed_text(text):
    """Image alt text, titles and markup cannot establish an OCR transcription."""
    parser = _VisibleText()
    parser.feed(MarkdownIt("commonmark").enable("table").render(text))
    return "".join(parser.text).strip()


def repetitive_transcription(text):
    lines = text.splitlines()
    for width in range(1, 9):
        repeats = 1
        for start in range(width, len(lines), width):
            part = lines[start : start + width]
            repeats = repeats + 1 if part == lines[start - width : start] else 1
            if repeats >= 32 and repeats * sum(len(line) + 1 for line in part) >= 1024:
                return True
    return False
