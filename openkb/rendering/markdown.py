"""Identify renderable content while leaving code and source documents intact."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from openkb.rendering.renderer import RenderedBlock, Renderer

_TOKENS = re.compile(
    r"(?P<fence>^[ \t]{0,3}(?P<ticks>`{3,}|~{3,})(?P<language>[^\n]*)\n"
    r"(?P<code>.*?)^(?P=ticks)[ \t]*$)"
    r"|(?P<code_span>`+[^\n]*?`+)"
    r"|(?P<display>\\\[(?P<bracket_math>.*?)\\\]|\$\$(?P<dollar_math>.*?)\$\$)"
    r"|(?P<inline>\\\((?P<paren_math>.*?)\\\)|(?<![\\$])\$(?!\s|\$)(?P<inline_math>[^\n$]*?\S)\$(?!\$))",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class RenderedMarkdown:
    markdown: str
    objects: tuple[tuple[str, RenderedBlock], ...]


def render_markdown(
    source: str, renderer: Renderer, *, dark: bool = False, scale: float = 1
) -> RenderedMarkdown:
    objects: list[tuple[str, RenderedBlock]] = []
    prefix = f"OPENKB{uuid.uuid4().hex}"

    def replace(match: re.Match[str]) -> str:
        if match["code_span"]:
            return match[0]
        if match["fence"]:
            if match["language"].strip() != "mermaid":
                return match[0]
            kind, body, display = "mermaid", match["code"], True
        else:
            kind = "math"
            body = next(
                match[name]
                for name in ("bracket_math", "dollar_math", "paren_math", "inline_math")
                if match[name] is not None
            )
            display = bool(match["display"])
        rendered = renderer.render(body, kind, display=display, dark=dark, scale=scale)
        token = f"{prefix}{len(objects)}END"
        objects.append((token, rendered))
        return f"\n\n{token}\n\n" if display else token

    markdown = _TOKENS.sub(replace, source)
    # Native links retain their KB-relative identity; no web routing involved.
    markdown = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda m: f"[{m[2] or m[1]}](openkb:{m[1]})",
        markdown,
    )
    return RenderedMarkdown(markdown, tuple(objects))
