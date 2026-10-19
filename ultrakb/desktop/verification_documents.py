"""Validate real imported PDF identities and retained content in acceptance runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify_long_pdf(kb: Path, pdf: Path) -> str:
    import pymupdf

    from ultrakb import frontmatter
    from ultrakb.application.sources import source_inventory
    from ultrakb.application.views import published_scope
    from ultrakb.file_digest import hash_file
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.index_store import IndexStore

    bound = published_scope(kb).knowledge
    assert bound is not None
    catalog = CatalogStore(kb, bound.identity)
    digest = hash_file(pdf)
    entry = next(
        item
        for item in source_inventory(kb)
        if hashlib.sha256(catalog.read_original(item["source_revision_id"])).hexdigest() == digest
    )
    assert entry["execution_mode"] == "segmented" and len(entry["units"]) == 1
    unit = entry["units"][0]
    indexes = IndexStore(kb, bound.identity)
    index = next(
        revision
        for revision in bound.index_revisions()
        if indexes.revision(revision).unit_revision_id == unit["unit_revision_id"]
    )
    retained = indexes.document(index)
    structure = retained["structure"]
    assert isinstance(structure, list) and structure
    pages = json.loads(bound.read(f"sources/{entry['doc_name']}.json"))
    with pymupdf.open(pdf) as document:
        assert len(pages) == document.page_count
        assert [page["page"] for page in pages] == list(range(1, document.page_count + 1))
        for expected, actual in zip(document, pages):
            text = " ".join(expected.get_text().split())
            assert text in " ".join(actual["content"].split())

        def verify_nodes(nodes):
            for node in nodes:
                assert node["title"] and node["summary"] and node["text"].strip()
                assert 1 <= node["start_index"] <= node["end_index"] <= document.page_count
                verify_nodes(node.get("nodes", []))

        verify_nodes(structure)
    content = bound.read(f"summaries/{entry['doc_name']}.md").decode()
    metadata = frontmatter.parse(content)
    assert metadata["doc_type"] == "pageindex"
    assert metadata["full_text"] == f"sources/{entry['doc_name']}.json"
    body = frontmatter.split(content)
    assert body and body[1].strip(), "Document knowledge must have a readable body"
    return f"summaries/{entry['doc_name']}"
