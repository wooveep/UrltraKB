"""Pending associations retain explicit unread coverage and preserve prior knowledge."""

import json

import pytest
from test_compile_windows import _page_request, _plan, _publication

from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.page_updates import PageUpdate
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.unit_publication import read_head

pytest_plugins = ("processing_fixtures",)


@pytest.mark.parametrize("entrypoint", ["import", "recompile"])
def test_updates_a_page_whose_linked_summary_is_pending(kb_dir, processing_only, entrypoint):
    from ultrakb.mutation import mutation_scope
    from ultrakb.source_catalog import read_source, write_record
    from ultrakb.source_changes import source_change_heads

    first = kb_dir / "first.txt"
    first.write_text("Model Birch requires inspection before winter operation.")
    retained = processing_only(first)
    with _publication(kb_dir, retained) as (wiki, name):
        publish_compile_results(
            wiki,
            name,
            _plan(wiki, {"create": [{"name": "old-topic"}]}),
            summary=first.read_text(),
            rewrite_summary=True,
            concept_results=[
                ("old-topic", "# Old topic\n\n" + first.read_text(), False, "Winter condition")
            ],
        )
    # Build genuine pending-page state through the production source-change plan.
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        heads = source_change_heads(kb_dir, read_source(kb_dir, retained.source_id), "updated")
        with mutation_scope(kb_dir, list(heads), operation="pending-source-associations"):
            for path, head in heads.items():
                write_record(path, head)
    with _publication(kb_dir, retained, recompile=True) as (wiki, name):
        publish_compile_results(
            wiki,
            name,
            _plan(wiki, {"create": [{"name": "inspection"}]}),
            summary="Inspection navigation.",
            concept_results=[
                (
                    "inspection",
                    "# Inspection\n\n" + first.read_text(),
                    False,
                    "Inspection conditions",
                )
            ],
        )
    head = read_head(kb_dir)
    assert "summaries/first.md" in head.needs_refresh
    assert "concepts/inspection.md" not in head.needs_refresh
    historical = kb_dir / ".ultrakb/knowledge/legacy/revisions" / head.knowledge_revision_id
    previous = (historical / "wiki/concepts/inspection.md").read_bytes()
    second = kb_dir / "second.txt"
    second.write_text("Model Alder requires inspection before summer operation.")
    incoming = processing_only(second)
    if entrypoint == "recompile":
        with _publication(kb_dir, incoming) as (wiki, name):
            publish_compile_results(
                wiki, name, _plan(wiki, {}), summary=second.read_text(), rewrite_summary=True
            )
    with _publication(kb_dir, incoming, recompile=entrypoint == "recompile") as (wiki, name):
        item = {"name": "inspection"}
        message = _page_request(
            CompileSourceWindows(wiki, name, kb_dir=kb_dir), item, action="update"
        )
        sent = json.dumps(message)
        assert "summaries/first.md" in sent
        assert '"coverage": "unread"' in str(message)
        assert "Linked source association unavailable; do not infer absence" in sent
        page = wiki / "concepts/inspection.md"
        patch = PageUpdate(page.read_text())
        updated = patch.apply(
            json.dumps(
                {
                    "base_sha256": patch.digest,
                    "description": "Inspection conditions",
                    "edits": [
                        {
                            "block_id": patch.request()["blocks"][-1]["id"],
                            "operation": "insert_after",
                            "content": "\n\n" + second.read_text(),
                        }
                    ],
                }
            ),
            current=page.read_text(),
        )
        publish_compile_results(
            wiki,
            name,
            _plan(wiki, {"update": [item]}),
            summary=second.read_text(),
            rewrite_summary=True,
            concept_results=[("inspection", updated["content"], True, updated["description"])],
        )
    page = (kb_dir / "wiki/concepts/inspection.md").read_text()
    assert "Model Birch requires inspection before winter operation." in page
    assert "Model Alder requires inspection before summer operation." in page
    assert (historical / "wiki/concepts/inspection.md").read_bytes() == previous
    assert (
        read_head(kb_dir).needs_refresh["summaries/first.md"]
        == head.needs_refresh["summaries/first.md"]
    )


def test_long_compile_preserves_draft_when_a_linked_original_is_missing(kb_dir):
    wiki = kb_dir / "wiki"
    original = "Model Alder requires inspection before summer operation."
    atomic_write_text(
        wiki / "sources/current.json",
        json.dumps([{"page": 1, "unit_kind": "page", "content": original}]),
    )
    page = wiki / "concepts/inspection.md"
    atomic_write_text(
        page,
        "---\ntype: Concept\ndescription: Inspection\n"
        "sources: [sources/retired.json]\n---\n# Inspection\n\n"
        "Model Birch requires inspection before winter operation.\n",
    )
    item = {"name": "inspection", "reading_locations": {"pages": "1"}}
    message = _page_request(CompileSourceWindows(wiki, "current"), item, action="update", long=True)
    sent = json.dumps(message)
    assert "sources/retired.json" in sent
    assert '"coverage": "unread"' in str(message)
    assert "Linked source association unavailable; do not infer absence" in sent
    assert original in sent
    patch = PageUpdate(page.read_text())
    result = patch.apply(
        json.dumps(
            {
                "base_sha256": patch.digest,
                "edits": [
                    {
                        "block_id": patch.request()["blocks"][-1]["id"],
                        "operation": "insert_after",
                        "content": "\n\n" + original,
                    }
                ],
            }
        ),
        current=page.read_text(),
    )
    publish_compile_results(
        wiki,
        "current",
        _plan(wiki, {"update": [item]}),
        summary="Inspection.",
        concept_results=[("inspection", result["content"], True, result["description"])],
        doc_type="pageindex",
        original_path="sources/current.json",
    )
    assert "Model Birch requires inspection before winter operation." in page.read_text()
    assert original in page.read_text()
