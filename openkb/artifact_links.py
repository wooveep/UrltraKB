"""Conservative local ownership from Markdown, HTML and retained page bodies."""

import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.targets.extend(value for key, value in attrs if key in {"href", "src"} and value)


def linked_artifacts(kb_dir: Path, path: Path) -> set[Path]:
    """Snapshots keep the same link text; retain both original and local resolutions."""
    if path.suffix not in {".md", ".html", ".txt", ".json"}:
        return set()
    if path.suffix == ".json" and "sources" not in path.parts:
        return set()  # Structured owner records are validated by their catalog readers.
    bodies = []

    def strings(value):
        if isinstance(value, str):
            bodies.append(value)
        elif isinstance(value, list):
            for child in value:
                strings(child)
        elif isinstance(value, dict):
            for child in value.values():
                strings(child)

    text = path.read_text("utf-8")
    strings(json.loads(text) if path.suffix == ".json" else text)
    result = set()
    markers = (".openkb/artifacts/", ".openkb/normalized/")
    for body in bodies:
        links = _Links()
        links.feed(MarkdownIt("commonmark").render(body))
        unresolved = unquote(unescape(body))
        for target in links.targets:
            url = urlsplit(target)
            if url.scheme not in {"", "file"} or url.netloc:
                continue
            local = unquote(url.path)
            result.add((path.parent / local).resolve())
            for marker in markers:
                if marker in local:
                    # Relative links copied into history still own the original
                    # managed target even though their new base directory differs.
                    result.add((kb_dir / local[local.index(marker) :]).resolve())
                    reference = local[local.index(marker) :]
                    unresolved = re.sub(
                        re.escape(reference) + r"(?=$|[\s)\"'<>\]?#])", "", unresolved
                    )
        if any(marker in unresolved for marker in markers):
            raise ValueError("Unresolved managed artifact ownership in retained text")
    return result
