"""Expose existing source figures with their text context and answer-ready wiki paths."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _resolve_image(target: str, source: Path, wiki: Path) -> Path | None:
    try:
        parts = urlsplit(target)
        if parts.scheme or parts.netloc:
            return None
        path = unquote(parts.path)
        for base in (source.parent, wiki, wiki / "sources"):
            candidate = (base / path).resolve()
            if (
                candidate.is_relative_to(wiki)
                and candidate.suffix.lower() in _IMAGE_SUFFIXES
                and candidate.is_file()
            ):
                return candidate
    except (OSError, ValueError):
        # A malformed or unavailable figure must not prevent reading the text.
        return None
    return None


def image_catalog(text: str, source: Path, wiki: Path) -> str:
    tokens = MarkdownIt("commonmark").enable(["table", "strikethrough"]).parse(text)
    inline = [token for token in tokens if token.type == "inline" and token.map]
    entries = []
    seen = set()
    for index, token in enumerate(inline):
        for image in token.children or []:
            if image.type != "image":
                continue
            resolved = _resolve_image(image.attrGet("src") or "", source, wiki)
            if resolved is None:
                continue
            relative = resolved.relative_to(wiki).as_posix()
            identity = (relative, token.map[0])
            if identity in seen:
                continue
            seen.add(identity)
            context = [item.content for item in inline[max(0, index - 1) : index + 2]]
            entries.append(
                {
                    "path": relative,
                    "caption": image.content,
                    "source": source.relative_to(wiki).as_posix(),
                    "line": token.map[0] + 1,
                    "adjacent_text": "\n".join(context)[:800],
                }
            )
    if not entries:
        return text
    return (
        text + "\n\nSource image catalog (wiki-relative paths for get_image and answer Markdown; "
        "adjacency records layout, not a claim about unseen image contents):\n"
        + json.dumps(entries, ensure_ascii=False)
    )
