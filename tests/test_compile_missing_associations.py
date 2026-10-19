"""Pending associations retain explicit unread coverage and preserve prior knowledge."""

import json

import pytest
from test_compile_windows import _page_request, _plan, _publication

from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.page_updates import PageUpdate
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text

pytest_plugins = ("processing_fixtures",)


@pytest.mark.parametrize("entrypoint", ["import", "recompile"])
def test_updates_a_page_whose_linked_summary_is_pending(kb_dir, processing_only, entrypoint):
    from processing_fixtures import source_processing_context

    from ultrakb.application.documents import import_document
    from ultrakb.application.views import published_scope
    from ultrakb.storage.knowledge_validity import publication_validity

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
    first.write_text("A changed target that is not yet published.")
    changed = import_document(kb_dir, first, context=source_processing_context())
    assert changed.status == "blocked"
    historical = published_scope(kb_dir).knowledge
    first_summary = f"summaries/{retained.doc_name}.md"
    assert first_summary in publication_validity(historical)["needs_refresh"]
    previous = historical.read("concepts/inspection.md")
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
        assert "Model Birch requires inspection before winter operation." in sent
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
    assert historical.read("concepts/inspection.md") == previous
    assert first_summary in publication_validity(published_scope(kb_dir).knowledge)["needs_refresh"]


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
