"""Validate real imported PDF identities and content in native acceptance runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def verify_long_pdf(kb: Path, pdf: Path) -> str:
    import pymupdf

    from openkb import frontmatter
    from openkb.state import HashRegistry

    registry = HashRegistry(kb / ".openkb/hashes.json")
    entry = registry.get_by_path(pdf.resolve().as_posix())
    assert entry is not None and entry["type"] == "long_pdf"
    with sqlite3.connect(kb / ".openkb/pageindex.db") as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        row = database.execute(
            "SELECT structure, file_path FROM documents WHERE doc_id = ?", (entry["doc_id"],)
        ).fetchone()
    assert row is not None and Path(row[1]).is_file()
    structure = json.loads(row[0])
    assert isinstance(structure, list) and structure
    pages = json.loads((kb / "wiki/sources" / f"{entry['doc_name']}.json").read_text("utf-8"))
    with pymupdf.open(pdf) as document:
        assert len(pages) == document.page_count
        assert [page["page"] for page in pages] == list(range(1, document.page_count + 1))
        for expected, actual in zip(document, pages):
            text = " ".join(expected.get_text().split())
            assert text and text in " ".join(actual["content"].split())

        def verify_nodes(nodes):
            for node in nodes:
                assert node["title"] and node["summary"] and node["text"].strip()
                assert 1 <= node["start_index"] <= node["end_index"] <= document.page_count
                verify_nodes(node.get("nodes", []))

        verify_nodes(structure)
    summary = kb / "wiki/summaries" / f"{entry['doc_name']}.md"
    content = summary.read_text("utf-8")
    metadata = frontmatter.parse(content)
    assert metadata["doc_type"] == "pageindex"
    assert metadata["full_text"] == f"sources/{entry['doc_name']}.json"
    assert all(node["title"] in content for node in structure)
    return f"summaries/{entry['doc_name']}"
