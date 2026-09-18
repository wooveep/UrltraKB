"""Native text markup with original line extents and local asset ownership."""

import json
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from openkb.evidence import BlockDraft
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope


def markdown_images(text, directory):
    root = directory.resolve()
    result = {}
    for token in MarkdownIt("commonmark").enable("table").parse(text):
        for child in token.children or []:
            if child.type != "image":
                continue
            reference = child.attrGet("src")
            url = urlsplit(reference)
            if url.scheme or url.netloc or not url.path:
                continue
            path = (root / unquote(url.path)).resolve()
            if path.is_relative_to(root):
                result[reference] = path
    return result


def parse_markdown(text, source, store):
    lines = text.splitlines()
    with progress_scope("text", len(lines), "lines") as progress:
        return _markdown_blocks(lines, source, store, progress)


def _markdown_blocks(lines, source, store, progress):
    quality = []
    for reference, digest in source.assets.items():
        if digest is None:
            quality.append({"status": "needs_review", "reason": "missing_asset:" + reference})
        else:
            store.asset(digest)
    blocks = []
    offset = 0
    if lines and lines[0] == "---":
        stop = next((i for i in range(1, len(lines)) if lines[i] in {"---", "..."}), None)
        if stop is not None:
            offset = stop + 1
            blocks.append(
                BlockDraft(
                    "\n".join(lines[:offset]),
                    "metadata",
                    {
                        "kind": "text",
                        "line": 1,
                        "line_end": offset,
                    },
                )
            )
    tokens = MarkdownIt("commonmark").enable("table").parse("\n".join(lines[offset:]))
    covered, processed = 0, 0
    headings = []
    for i, token in enumerate(tokens):
        processing_checkpoint()
        if token.map is None or token.map[0] < covered:
            continue
        start, end = token.map
        covered = end
        while end > start and not lines[end + offset - 1].strip():
            end -= 1
        kind = {
            "heading_open": "heading",
            "bullet_list_open": "list",
            "ordered_list_open": "list",
            "fence": "code",
            "code_block": "code",
            "table_open": "table",
        }.get(token.type, "paragraph")
        location = {"kind": "text", "line": start + offset + 1, "line_end": end + offset}
        if kind == "heading":
            level = int(token.tag[1:])
            title = tokens[i + 1].content
            headings = [row for row in headings if row[0] < level] + [(level, title)]
            location["heading_level"] = level
        location["headings"] = [row[1] for row in headings]
        raw = "\n".join(lines[start + offset : end + offset])
        assets = []
        image_context = []
        # Literal code and metadata do not reference document images.
        if kind != "code":
            image_tokens = []
            for child in tokens[i:]:
                if child is not token and child.map and child.map[0] >= end:
                    break
                image_tokens.extend(c for c in child.children or [] if c.type == "image")
            for image in image_tokens:
                reference = image.attrGet("src")
                digest = source.assets.get(reference)
                if digest:
                    assets.append(digest)
                    raw = raw.replace("](" + reference + ")", "](asset:" + digest + ")")
                    image_context.append(
                        {"reference": reference, "asset": "asset:" + digest, "alt": image.content}
                    )
        blocks.append(
            BlockDraft(
                raw,
                kind,
                location,
                tuple(dict.fromkeys(assets)),
                context=json.dumps({"images": image_context}, ensure_ascii=False)
                if image_context
                else "",
            )
        )
        progress.advance(end + offset - processed)
        processed = end + offset
        if token.type == "fence" and not re.match(
            r"^\s{0,3}" + re.escape(token.markup[0]) + r"{" + str(len(token.markup)) + r",}\s*$",
            lines[end + offset - 1],
        ):
            quality.append({"status": "needs_review", "reason": "unclosed_code_span"})
    # Reference definitions are consumed by markdown-it but remain original
    # source metadata with their own lines and must survive the saved parse.
    occupied = {
        i for block in blocks for i in range(block.location["line"] - 1, block.location["line_end"])
    }
    for i, line in enumerate(lines):
        if i not in occupied and line.strip():
            blocks.append(
                BlockDraft(line, "metadata", {"kind": "text", "line": i + 1, "line_end": i + 1})
            )
    blocks.sort(key=lambda block: block.location["line"])
    progress.advance(len(lines) - processed)
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    return blocks, quality
