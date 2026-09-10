"""Rewrite navigable links while preserving literal Markdown examples byte for byte."""

import html
import re

from markdown_it import MarkdownIt
from markdown_it.rules_inline import image

from openkb.lint import build_norm_index, strip_ghost_wikilinks
from openkb.processing import ProcessingIncomplete


def _inline_positions(token, lines, offsets, *, cell, columns):
    """Map stripped block prefixes back to the original CR/LF-delimited lines."""
    positions = []
    for number, text in enumerate(token.content.split("\n"), token.map[0]):
        # Block indentation can contribute virtual spaces when a tab is expanded.
        body = text.lstrip(" ")
        raw = lines[number]
        # The table rule removes one backslash before each escaped pipe. Retain
        # the surviving characters' original positions, including repeated cells.
        indexes = [i for i in range(len(raw)) if not (cell and raw[i : i + 2] == "\\|")]
        line = "".join(raw[i] for i in indexes)
        column = line.find(body, columns.get(number, 0) if cell else 0)
        if column < 0:
            raise ProcessingIncomplete("topic_generation_incomplete", "generation")
        if cell:
            columns[number] = column + len(body)
        positions.extend([None] * (len(text) - len(body)))
        positions.extend(offsets[number] + indexes[column + i] for i in range(len(body)))
        positions.append(offsets[number] + len(lines[number]))
    return positions


def normalize_links(content, known_targets, assets):
    markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    environment = {}
    tokens = markdown.parse(content, environment)
    # Match CommonMark normalization without treating Unicode separators as lines.
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", content))]
    lines = re.split(r"\r\n?|\n", content.replace("\0", "\ufffd"))
    norm_index = build_norm_index(known_targets)
    prose, replacements = "", []

    def wiki_link(state, silent):
        match = re.match(r"\[\[([^\]]+)\]\]", state.src[state.pos :])
        if not match:
            return False
        if not silent:
            value, _ = strip_ghost_wikilinks(match[0], known_targets, norm_index=norm_index)
            state.push("text", "", 0).content = value
            if state.src is prose:
                replacements.append((state.pos, state.pos + len(match[0]), value))
        state.pos += len(match[0])
        return True

    def figure(state, silent):
        start = state.pos
        if not image(state, silent):
            return False
        if not silent and state.src is prose:
            token = state.tokens[-1]
            target = token.attrGet("src") or ""
            if target.startswith("asset:"):
                target = assets.get(target[6:])
            if target not in assets.values():
                raise ProcessingIncomplete("generated_asset_evidence_invalid", "generation")
            title = token.attrGet("title") or ""
            if title:
                title = ' "' + html.escape(title).replace("\\", "\\\\") + '"'
            replacements.append((start, state.pos, f"![{token.content}]({target}{title})"))
        return True

    # Inline parser dispatch naturally excludes escaped syntax and code spans.
    # Nested image-label parsing has its own source; only outer offsets are used.
    markdown.inline.ruler.before("escape", "wiki_link", wiki_link)
    markdown.inline.ruler.at("image", figure)
    edits, columns = [], {}
    for index, token in enumerate(tokens):
        if token.type != "inline" or not token.map:
            continue
        prose, replacements = token.content, []
        markdown.inline.parse(prose, markdown, environment, [])
        cell = index > 0 and tokens[index - 1].type in {"th_open", "td_open"}
        if not replacements and not cell:
            continue
        positions = _inline_positions(token, lines, offsets, cell=cell, columns=columns)
        for left, right, value in replacements:
            start, last = positions[left], positions[right - 1]
            if start is None or last is None:
                raise ProcessingIncomplete("topic_generation_incomplete", "generation")
            edits.append((start, last + 1, value.replace("|", "\\|") if cell else value))
    for start, end, value in sorted(edits, reverse=True):
        content = content[:start] + value + content[end:]
    return content
