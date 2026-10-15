"""Selectable syntax colors for Qt's deliberately small HTML/CSS subset."""

import html

from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound


def code_html(source: str, language: str = "", *, dark: bool = False) -> str:
    # Markdown owns the final line ending before the closing fence. Keeping it
    # makes QTextDocument create an extra, visibly empty code line. Remove only
    # that terminator, preserving intentional blank lines and indentation.
    source = source.removesuffix("\n")
    try:
        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = TextLexer(stripnl=False, ensurenl=False)
    style = get_style_by_name("native" if dark else "friendly")
    parts = []
    for token, value in lex(source, lexer):
        color = style.style_for_token(token)["color"]
        escaped = html.escape(value)
        parts.append(f'<span style="color: #{color}">{escaped}</span>' if color else escaped)
    return "<pre><code>" + "".join(parts) + "</code></pre>"
