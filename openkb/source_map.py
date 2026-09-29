"""Bind normalized physical pages and their assets to an immutable input package."""

import json
from pathlib import Path

from openkb.file_state import contained_paths
from openkb.ingest_records import SourceMap
from openkb.source_pages import read_page_selection
from openkb.state import HashRegistry


def freeze_pdf_map(wiki: Path, doc_name: str, original: Path) -> SourceMap:
    from openkb.converter import get_pdf_page_count

    path = f"sources/{doc_name}.json"
    pages = json.loads((wiki / path).read_text("utf-8"))
    expected = get_pdf_page_count(original)
    selected = read_page_selection(pages)
    if selected["pages"] != expected:
        raise ValueError("Source map does not cover every physical page of the frozen PDF")
    assets = {}
    for page in selected["units"]:
        for image in page.get("images", []):
            relative = image["path"]
            target = wiki / relative
            contained_paths(wiki, [target])
            assets[relative] = HashRegistry.hash_file(target)
    return SourceMap(
        path=path, digest=HashRegistry.hash_file(wiki / path), unit_count=expected, assets=assets
    )


def read_source_map(
    wiki: Path,
    reference: SourceMap,
    doc_name: str,
    pages: str | None = None,
    *,
    chars: str | None = None,
) -> dict:
    suffix = ".content.json" if reference.unit_kind == "text" else ".json"
    if reference.path != f"sources/{doc_name}{suffix}":
        raise ValueError("Source map belongs to another processing unit")
    target = wiki / reference.path
    contained_paths(wiki, [target, *(wiki / name for name in reference.assets)])
    if HashRegistry.hash_file(target) != reference.digest:
        raise ValueError("Source map digest changed")
    raw = json.loads(target.read_text("utf-8"))
    if reference.unit_kind == "text":
        from openkb.text_source import read_text_selection

        if pages is not None:
            from openkb.source_pages import PageRangeError

            raise PageRangeError("Markdown has character positions, not physical pages")
        selected = read_text_selection(raw, chars)
        body = wiki / "sources" / f"{doc_name}.md"
        contained_paths(wiki, [body])
        if body.read_bytes().decode("utf-8") != raw["text"] or raw["assets"] != reference.assets:
            raise ValueError("Frozen text or its asset map changed")
        for path, digest in reference.assets.items():
            if HashRegistry.hash_file(wiki / path) != digest:
                raise ValueError("Source map image digest changed")
        return selected
    if chars is not None:
        from openkb.source_pages import PageRangeError

        raise PageRangeError("This source has physical pages, not frozen character positions")
    selected = read_page_selection(raw, pages)
    if selected["pages"] != reference.unit_count:
        raise ValueError("Source map physical-page coverage changed")
    if {image["path"] for page in raw for image in page.get("images", [])} != set(reference.assets):
        raise ValueError("Source map asset references changed")
    for path, digest in reference.assets.items():
        if HashRegistry.hash_file(wiki / path) != digest:
            raise ValueError("Source map image digest changed")
    return selected


def freeze_text_map(wiki: Path, doc_name: str) -> SourceMap:
    from openkb.text_source import FrozenText

    path = f"sources/{doc_name}.content.json"
    raw = FrozenText.model_validate_json((wiki / path).read_text("utf-8"))
    reference = SourceMap(
        path=path,
        digest=HashRegistry.hash_file(wiki / path),
        unit_kind="text",
        unit_count=1,
        assets=raw.assets,
    )
    read_source_map(wiki, reference, doc_name)
    return reference
