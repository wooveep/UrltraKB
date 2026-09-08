"""Visible answer text, excluding explicit model reasoning envelopes.

SDK history and trace events retain their provider semantics. Only displayed and
saved answers use this filter; quoted Markdown code remains literal content.
"""

import re

_TOKENS = re.compile(
    r"(?P<code>```[^\n]*\n[\s\S]*?(?:```|\Z)|~~~[^\n]*\n[\s\S]*?(?:~~~|\Z)|`+[^`\n]*`+)"
    r"|(?P<tag><\s*(?P<close>/)?\s*(?P<name>think|thinking|analysis|reasoning)\s*>)",
    re.IGNORECASE,
)


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
    return "".join(parts).strip()
