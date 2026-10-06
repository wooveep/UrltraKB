"""Validate real imported PDF identities and content in native acceptance runs."""

from __future__ import annotations

import json
from pathlib import Path


def verify_long_pdf(kb: Path, pdf: Path) -> str:
    import pymupdf

    from openkb import frontmatter
    from openkb.application.sources import source_inventory
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation
    from openkb.state import HashRegistry

    digest = HashRegistry.hash_file(pdf)
    entry = next(
        item
        for item in source_inventory(kb)
        if HashRegistry.hash_file(kb / item["original_path"]) == digest
    )
    assert entry["execution_mode"] == "segmented" and len(entry["units"]) == 1
    unit = entry["units"][0]
    revision = (
        kb / ".openkb/knowledge" / unit["view_id"] / "revisions" / unit["knowledge_revision_id"]
    )
    manifest = json.loads((revision / "manifest.json").read_text("utf-8"))
    location = IndexLocation.package(revision / "index")
    assert location.read_only and location.owner_revision == unit["knowledge_revision_id"]
    with ConDBPageIndexStorage(location) as storage:
        document = storage.get_document("default", manifest["index_ref"])
        assert document and Path(document["file_path"]).is_file()
        structure = storage.get_document_structure("default", manifest["index_ref"])
    assert isinstance(structure, list) and structure
    pages = json.loads((revision / "wiki/sources" / f"{entry['doc_name']}.json").read_text("utf-8"))
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
    summary = revision / "wiki/summaries" / f"{entry['doc_name']}.md"
    content = summary.read_text("utf-8")
    metadata = frontmatter.parse(content)
    assert metadata["doc_type"] == "pageindex"
    assert metadata["full_text"] == f"sources/{entry['doc_name']}.json"
    assert all(node["title"] in content for node in structure)
    return f"summaries/{entry['doc_name']}"
