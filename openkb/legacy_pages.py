"""Read retained local page exports without claiming original PDF coordinates."""

from __future__ import annotations

import json
from pathlib import Path


def saved_pages_text(path: Path) -> str:
    pages = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pages, list) or not pages:
        raise ValueError("Saved page export must contain content entries")
    text = []
    seen = set()
    for entry in pages:
        if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
            raise ValueError("Invalid saved page content")
        page = entry.get("page")
        if type(page) is not int or page < 1 or page in seen:
            raise ValueError("Invalid saved page identity")
        seen.add(page)
        images = entry.get("images", [])
        if not isinstance(images, list) or any(
            not isinstance(image, dict) or not isinstance(image.get("path"), str)
            for image in images
        ):
            raise ValueError("Invalid saved page assets")
        text.append(f"# Saved content entry {page}\n\n{entry['content']}")
        for image in images:
            reference = image["path"]
            if not reference or any(character in reference for character in "\n\r()"):
                raise ValueError("Invalid saved image reference")
            text.append(f"![saved figure]({reference})")
    return "\n\n".join(text)
