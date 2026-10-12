"""Pending source associations remain explicitly unread during compilation."""

import json

import pytest
from test_compile_windows import compile_boundary

from openkb.application.documents import import_document
from openkb.application.reprocessing import preview_reprocessing, reprocess_source
from openkb.unit_publication import read_head


@pytest.mark.parametrize("entrypoint", ["import", "recompile"])
def test_updates_a_page_whose_linked_summary_is_pending(kb_dir, monkeypatch, entrypoint):
    import asyncio

    from openkb.application.recompilation import recompile_document, select_recompilation

    first = kb_dir / "first.txt"
    first.write_text("Model Birch requires inspection before winter operation.")
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "old-topic"}]}}, [])
    imported = import_document(kb_dir, first)
    assert imported.status == "added", imported.message

    second = kb_dir / "second.txt"
    second.write_text("Model Alder requires inspection before summer operation.")
    if entrypoint == "recompile":
        compile_boundary(monkeypatch, {}, [])
        assert import_document(kb_dir, second).status == "added"

    compile_boundary(
        monkeypatch,
        {"concepts": {"create": [{"name": "inspection"}]}},
        [],
        page_reply={
            "description": "Inspection conditions",
            "content": "# Inspection\n\nModel Birch requires inspection before winter operation.",
        },
    )
    preview = preview_reprocessing(kb_dir, imported.source_id)
    reprocessed = reprocess_source(kb_dir, imported.source_id, version=preview["version"])
    assert reprocessed.status == "added", reprocessed.message
    head = read_head(kb_dir)
    assert "summaries/first.md" in head.needs_refresh
    assert "concepts/inspection.md" not in head.needs_refresh
    historical = kb_dir / ".openkb/knowledge/legacy/revisions" / head.knowledge_revision_id
    previous = (historical / "wiki/concepts/inspection.md").read_bytes()

    requests = []
    compile_boundary(
        monkeypatch,
        {"concepts": {"update": [{"name": "inspection"}]}},
        requests,
        page_reply={
            "description": "Inspection conditions",
            "content": "Model Alder requires inspection before summer operation.",
        },
    )
    if entrypoint == "import":
        updated = import_document(kb_dir, second)
        assert updated.status == "added", updated.message
    else:
        selection = select_recompilation(kb_dir, "second")
        assert selection.status == "ready"
        updated = asyncio.run(
            recompile_document(kb_dir, selection.targets[0].file_hash, version=selection.version)
        )
        assert updated.status == "compiled", updated.message
    message = next(m for m in requests if "Update the concept page" in str(m))
    sent = json.dumps(message)
    assert "summaries/first.md" in sent
    assert '"coverage": "unread"' in str(message)
    assert "Linked source association unavailable; do not infer absence" in sent
    page = (kb_dir / "wiki/concepts/inspection.md").read_text()
    assert "Model Birch requires inspection before winter operation." in page
    assert "Model Alder requires inspection before summer operation." in page
    assert (historical / "wiki/concepts/inspection.md").read_bytes() == previous
    assert (
        read_head(kb_dir).needs_refresh["summaries/first.md"]
        == head.needs_refresh["summaries/first.md"]
    )


@pytest.mark.asyncio
async def test_long_compile_preserves_draft_when_a_linked_original_is_missing(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    original = "Model Alder requires inspection before summer operation."
    (wiki / "sources/current.json").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": original}])
    )
    page = wiki / "concepts/inspection.md"
    page.write_text(
        "---\ntype: Concept\ndescription: Inspection\n"
        "sources: [sources/retired.json]\n---\n# Inspection\n\n"
        "Model Birch requires inspection before winter operation.\n"
    )
    requests = []
    compile_boundary(
        monkeypatch,
        {"concepts": {"update": [{"name": "inspection", "reading_locations": {"pages": "1"}}]}},
        requests,
        page_reply={"description": "Inspection conditions", "content": original},
    )
    await compile_long_doc(
        "current",
        wiki / "summaries/current.md",
        "retained-index",
        kb_dir,
        "test-model",
        navigation=[],
    )
    message = next(m for m in requests if "Update the concept page" in str(m))
    sent = json.dumps(message)
    assert "sources/retired.json" in sent
    assert '"coverage": "unread"' in str(message)
    assert "Linked source association unavailable; do not infer absence" in sent
    assert original in sent
    assert "Model Birch requires inspection before winter operation." in page.read_text()
    assert original in page.read_text()
