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


def source_markdown(source):
    if source["content"] or not source.get("images"):
        return source["content"]
    return "\n\n".join(
        f"![Retained figure]({image['path'].removeprefix('sources/')})"
        for image in source["images"]
    )


def capture_source_assets(scope, source):
    """Read exact processing assets, never the editable Wiki projection."""
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.knowledge_inputs import image_paths

    assert scope.knowledge is not None
    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    unit = catalog.unit_revision(source["unit_revision_id"])
    if unit.source_id != source["source_id"]:
        raise ValueError("Reading assets belong to another source")
    processing = catalog.processing(unit.processing_id)
    aliases = dict(image_paths(unit, processing))
    aliases.update(image_paths(unit, processing, retained=True))
    page = source.get("source_path") or f"sources/{unit.doc_name}.md"
    if processing.source_kind == "pages" and source["content"]:
        page = "_source.md"  # Physical-page Markdown uses Wiki-root image names.
    images = {}
    for reference in image_references(source_markdown(source)):
        if reference.local_path is None:
            continue
        logical = posixpath.normpath(str(PurePosixPath(page).parent / reference.local_path))
        if logical in aliases and logical not in images:
            images[logical] = catalog.read_processed(unit.processing_id, aliases[logical])
    return ReadingAssets(
        scope.kb_dir / "wiki" / PurePosixPath(page).parent,
        source.get("publication_id") or "unit-" + unit.unit_revision_id,
        page,
        MappingProxyType(images),
    )
