"""Render technical titles as labels, never as Wiki navigation syntax."""

import html
import re

from openkb import frontmatter


def finish_source_summary(metadata, body, name):
    if not frontmatter.parse(metadata).get("description"):
        metadata = frontmatter.set_line(metadata, "description", name)

    def label(match):
        text = html.escape(match[2], quote=False)
        for char, escaped in (("[", "&#91;"), ("]", "&#93;"), ("|", "&#124;")):
            text = text.replace(char, escaped)
        return match[1] + text + match[3]

    return metadata, re.sub(r"(?m)^(- \[\[(?:concepts|entities)/[\w-]+\|)(.*)(\]\])$", label, body)
