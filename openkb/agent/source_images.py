"""Expose existing source figures with their text context and answer-ready wiki paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def published_images(wiki: Path, assets: set[str]) -> dict[str, dict[str, str]]:
    """Capture existing images whose published bytes match the source asset identity."""
    catalog: dict[str, dict[str, str]] = {}
    root = wiki.resolve()
    for path in sorted((wiki / "sources/images").glob("*")):
        if path.stem not in assets or path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != path.stem:
                continue
        except (OSError, RuntimeError):
            continue
        relative = path.relative_to(wiki).as_posix()
        catalog.setdefault(
            path.stem,
            {"asset": path.stem, "path": relative, "markdown": f"![原图]({relative})"},
        )
    return catalog


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
