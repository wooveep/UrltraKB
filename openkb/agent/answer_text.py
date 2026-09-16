"""Visible answer text, excluding explicit model reasoning envelopes.

SDK history and trace events retain their provider semantics. Only displayed and
saved answers use this filter; quoted Markdown code remains literal content.
"""

import re

from markdown_it import MarkdownIt

_TOKENS = re.compile(
    r"(?P<code>```[^\n]*\n[\s\S]*?(?:```|\Z)|~~~[^\n]*\n[\s\S]*?(?:~~~|\Z)|`+[^`\n]*`+)"
    r"|(?P<tag><\s*(?P<close>/)?\s*(?P<name>think|thinking|analysis|reasoning)\s*>)",
    re.IGNORECASE,
)
_TOOL_MARKUP = re.compile(r"<[｜|]{2}DSML[｜|]{2}\s+(?:calls|invoke|parameter)(?=[\s>])")


def has_tool_markup(text: str) -> bool:
    """Recognize leaked provider control tags, preserving quoted code examples."""
    # Remove escaped pairs before parsing: rendered text loses that distinction.
    # Markdown handles code spans (including multiline spans) and block indentation.
    for token in MarkdownIt("commonmark").parse(re.sub(r"\\.", "", text)):
        for part in token.children or [token]:
            if part.type in {"text", "html_inline", "html_block"} and _TOOL_MARKUP.search(
                part.content
            ):
                return True
    return False


def visible_answer(text: str) -> str:
    """Remove tagged reasoning, including an unfinished envelope, outside code."""
    parts: list[str] = []
    stack: list[str] = []
    start = 0
    for match in _TOKENS.finditer(text):
        if not stack:
            parts.append(text[start : match.start()])
        if match.group("code"):
            if not stack:
                parts.append(match.group())
        elif match.group("close"):
            name = match.group("name").lower()
            if name in stack:
                del stack[stack.index(name) :]
        else:
            stack.append(match.group("name").lower())
        start = match.end()
    if not stack:
        parts.append(text[start:])
    # Leading spaces can be Markdown code-block indentation.
    return "".join(parts).strip("\r\n").rstrip()
