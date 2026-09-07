"""Native document markup using Markdown token boundaries for math and wiki links."""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from markdown_it import MarkdownIt

from openkb.rendering.renderer import RenderedBlock, Renderer

_INLINE = re.compile(
    r"\\\((?P<paren>.*?)\\\)|\\\[(?P<bracket>.*?)\\\]"
    r"|\$\$(?P<display>.*?)\$\$|\$(?!\s|\$)(?P<dollar>[^\n$]*?\S)\$(?![\d$])",
    re.DOTALL,
)
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


@dataclass(frozen=True)
class RenderedMarkdown:
    html: str
    objects: tuple[tuple[str, RenderedBlock], ...]


def heading_anchor(title: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", title).strip().lower())


def render_markdown(
    source: str, renderer: Renderer, *, dark: bool = False, scale: float = 1
) -> RenderedMarkdown:
    objects: list[tuple[str, RenderedBlock]] = []
    prefix = f"OPENKB{uuid.uuid4().hex}"
    markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])

    def placeholder(body: str, kind: str, display: bool) -> str:
        block = renderer.render(body, kind, display=display, dark=dark, scale=scale)
        token = f"{prefix}{len(objects)}END"
        objects.append((token, block))
        return token

    def inline(state, silent):
        # The Markdown parser invokes this only in text positions. Code spans,
        # code blocks, image targets and link destinations retain their syntax.
        match = _WIKI.match(state.src, state.pos)
        if match:
            if not silent:
                token = state.push("html_inline", "", 0)
                href = "openkb:" + quote(match[1], safe="/#")
                token.content = f'<a href="{href}">{html.escape(match[2] or match[1])}</a>'
            state.pos = match.end()
            return True
        match = _INLINE.match(state.src, state.pos)
        if not match:
            return False
        if not silent:
            display = match["display"] is not None or match["bracket"] is not None
            body = next(value for value in match.groupdict().values() if value is not None)
            state.push("text", "", 0).content = placeholder(body, "math", display)
        state.pos = match.end()
        return True

    def math_block(state, start, end, silent):
        if state.sCount[start] - state.blkIndent >= 4:
            return False
        first = state.src[state.bMarks[start] + state.tShift[start] : state.eMarks[start]]
        opening = next((item for item in (r"\[", "$$") if first.startswith(item)), None)
        if opening is None:
            return False
        closing = r"\]" if opening == r"\[" else "$$"
        for last in range(start, end):
            text = state.getLines(start, last + 1, state.blkIndent, False).strip()
            close_at = text.find(closing, len(opening))
            if close_at >= 0:
                if close_at + len(closing) != len(text):
                    return False
                if not silent:
                    token = state.push("html_block", "", 0)
                    body = text[len(opening) : -len(closing)]
                    token.content = f"<p>{placeholder(body, 'math', True)}</p>\n"
                    token.map = [start, last + 1]
                state.line = last + 1
                return True
        return False

    def fence(tokens, index, options, env):
        token = tokens[index]
        if token.info.strip() == "mermaid":
            return f"<p>{placeholder(token.content, 'mermaid', True)}</p>\n"
        return default_fence(tokens, index, options, env)

    markdown.inline.ruler.before("escape", "openkb_inline", inline)
    headings: dict[str, int] = {}

    def heading(tokens, index, options, env):
        slug = heading_anchor(tokens[index + 1].content)
        count = headings.get(slug, 0)
        headings[slug] = count + 1
        if count:
            slug += f"-{count}"
        tag = tokens[index].tag
        return f'<{tag}><a name="{html.escape(slug, quote=True)}"></a>'

    markdown.renderer.rules["heading_open"] = heading
    markdown.block.ruler.before("fence", "openkb_math", math_block)
    default_fence = markdown.renderer.rules["fence"]
    markdown.renderer.rules["fence"] = fence
    return RenderedMarkdown(markdown.render(source), tuple(objects))
