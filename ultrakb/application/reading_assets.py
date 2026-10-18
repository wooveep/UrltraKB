"""Captured image bytes accompany a fixed Markdown read across the UI boundary."""

import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote, unquote, urlsplit

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.markdown_images import image_references


@dataclass(frozen=True)
class ReadingAssets:
    directory: Path
    publication_id: str
    page: str
    images: Mapping[str, bytes]

    @property
    def base_url(self) -> str:
        parent = str(PurePosixPath(self.page).parent)
        prefix = "" if parent == "." else quote(parent, safe="/") + "/"
        return f"ultrakb-knowledge://{self.publication_id}/{prefix}"

    def image(self, url: str) -> bytes | None:
        parsed = urlsplit(url)
        if parsed.scheme != "ultrakb-knowledge" or parsed.netloc != self.publication_id:
            return None
        return self.images.get(posixpath.normpath(unquote(parsed.path).lstrip("/")))


def capture_reading_assets(scope: KnowledgeScope, page: str, content: str) -> ReadingAssets:
    from .reading import read_knowledge_image

    assert scope.knowledge is not None
    images = {}
    for reference in image_references(content):
        if reference.local_path is None:
            continue
        logical = posixpath.normpath(str(PurePosixPath(page).parent / reference.local_path))
        if logical in images:
            continue
        try:
            images[logical] = read_knowledge_image(scope.kb_dir, logical, scope=scope)[0]
        except (FileNotFoundError, ValueError):
            continue  # Unknown or unsafe references cannot fall back to the live worktree.
    return ReadingAssets(
        scope.wiki_dir / PurePosixPath(page).parent,
        scope.knowledge.publication_id,
        page,
        MappingProxyType(images),
    )
