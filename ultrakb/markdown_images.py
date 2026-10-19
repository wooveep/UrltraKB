"""Locate actual Markdown images while leaving code and surrounding text intact."""

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import html_inline, image

from ultrakb.html_images import HTMLLiteralContext, html_image_references


@dataclass(frozen=True)
class ImageReference:
    start: int
    end: int
    source: str
    alt: str
    title: str
    html: bool = False

    def relocated(self, path: str, *, preserve_suffix: bool = False) -> str:
        target = quote(path, safe="/")
        if preserve_suffix:
            source = urlsplit(self.source)
            target += ("?" + source.query if source.query else "") + (
                "#" + source.fragment if source.fragment else ""
            )
        if self.html:
            return target
        title = self.title.replace("\\", "\\\\").replace('"', '\\"')
        suffix = f' "{title}"' if title else ""
        return f"![{self.alt}]({target}{suffix})"

    @property
    def local_path(self) -> str | None:
        parts = urlsplit(self.source)
        return unquote(parts.path) if not parts.scheme and not parts.netloc else None


def image_references(text: str) -> list[ImageReference]:
    """Use the pinned CommonMark parser to distinguish images from literal examples.

    Its inline rule records exact markup spans. Block line maps bridge removed list /
    quote prefixes back to original CRLF/codepoint coordinates; no text is serialized.
    """
    parser = MarkdownIt("commonmark")
    validate_link = parser.validateLink
    parser.validateLink = lambda url: url.startswith("data:image/") or validate_link(url)

    def positioned_image(state, silent):
        start = state.pos
        matched = image(state, silent)
        if matched and not silent:
            state.tokens[-1].meta["original_markup_span"] = (start, state.pos)
        return matched

    parser.inline.ruler.at("image", positioned_image)

    def positioned_html(state, silent):
        start = state.pos
        matched = html_inline(state, silent)
        if matched and not silent:
            state.tokens[-1].meta["original_markup_span"] = (start, state.pos)
        return matched

    parser.inline.ruler.at("html_inline", positioned_html)
    lines = [match for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if match.group()]
    result = []
    context = HTMLLiteralContext()
    for block in parser.parse(text):
        references: list[tuple[int, int, str, str, str, bool]] = []
        if block.type == "html_block":
            references.extend(
                (ref.start, ref.end, ref.source, ref.alt, ref.title, True)
                for ref in html_image_references(block.content, context=context)
            )
        for token in block.children or []:
            if token.type == "image" and not context.literal:
                start, end = token.meta["original_markup_span"]
                references.append(
                    (
                        start,
                        end,
                        token.attrGet("src") or "",
                        token.content,
                        token.attrGet("title") or "",
                        False,
                    )
                )
            elif token.type == "html_inline":
                base = token.meta["original_markup_span"][0]
                references.extend(
                    (base + ref.start, base + ref.end, ref.source, ref.alt, ref.title, True)
                    for ref in html_image_references(token.content, context=context)
                )
        if not references:
            continue
        if block.map is None:
            raise ValueError("Markdown image has no original block position")
        positions: list[int] = []
        row = block.map[0]
        for content in block.content.split("\n"):
            if row >= len(lines) and not content:
                break
            raw = lines[row].group().rstrip("\r\n").replace("\x00", "\ufffd")
            offset = raw.find(content)
            if offset < 0:
                raise ValueError("Cannot map Markdown image markup to its original line")
            positions.extend(lines[row].start() + offset + i for i in range(len(content)))
            positions.append(lines[row].start() + len(raw))
            row += 1
        for start, end, src, alt, title, html in references:
            if start == end:
                continue
            result.append(
                ImageReference(
                    positions[start],
                    positions[end - 1] + 1,
                    src,
                    alt,
                    title,
                    html,
                )
            )
    return result


def apply_image_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text
