"""Selected refresh keeps authenticated originals and their retained references."""

import asyncio
import json

import pymupdf
import pytest
from test_selected_refresh_sdk import _import, _prepared

pytest_plugins = ("test_vendor_sdk",)


def test_selected_refresh_ignores_an_unregistered_working_copy_original(kb_dir, model_service):
    from publication_fixtures import publish_document

    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import published_scope, view_scope
    from ultrakb.locks import atomic_write_text

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "model_capacity:\n  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  retries: 0\n  max_calls: 20\n"
    )
    path = kb_dir / "problem.pdf"

    def write_pdf(text):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(path)

    write_pdf("Original problem policy allows direct access.")
    first = publish_document(
        kb_dir,
        path,
        "Original problem policy allows direct access.",
        bundle=model_service.credentials,
        shared_body="# Shared\n\nOriginal problem policy allows direct access.",
    )
    assert first.status == "added", first.message
    normal = _import(kb_dir, model_service, "normal", "UNRELATED NORMAL ORIGINAL SENTINEL.")
    write_pdf("Corrected problem policy requires approval.")
    updated = publish_document(
        kb_dir,
        path,
        "Corrected problem policy requires approval.",
        bundle=model_service.credentials,
    )
    assert updated.status == "added", updated.message
    scope = view_scope(kb_dir, first.units[0].view_id)
    normal_summary = f"summaries/{normal.units[0].doc_name}"
    normal_before = published_scope(kb_dir).knowledge.read(normal_summary)
    shadow = scope.wiki_dir / f"sources/{first.units[0].doc_name.removesuffix('.md')}.content.json"
    atomic_write_text(shadow, "UNRELATED NORMAL ORIGINAL SENTINEL.")
    count = len(model_service.requests)

    result = asyncio.run(refresh_selected_sources(kb_dir, [first.source_id], scope=scope))

    assert result.status == "completed", result.message
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert published_scope(kb_dir).knowledge.read(normal_summary) == normal_before
    assert count < len(model_service.requests) <= count + 4
    sent = json.dumps(model_service.requests[count:])
    assert "Corrected problem policy requires approval." in sent
    assert "UNRELATED NORMAL ORIGINAL SENTINEL" not in sent


@pytest.mark.parametrize("all_sources", [False, True])
def test_selected_publication_retains_originals_of_old_page_dependencies(
    kb_dir, model_service, all_sources
):
    from native_catalog_fixtures import catalog

    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import published_scope
    from ultrakb.documents import read_document_source

    sources, scope = _prepared(kb_dir, model_service)
    count = len(model_service.requests)
    result = asyncio.run(
        refresh_selected_sources(kb_dir, sources if all_sources else sources[:1], scope=scope)
    )
    assert result.status == ("completed" if all_sources else "partial"), result.message
    assert (
        (count < len(model_service.requests) <= count + 4)
        if all_sources
        else len(model_service.requests) == count
    )
    fixed = published_scope(kb_dir)
    current = set(fixed.knowledge.unit_revisions())
    dependencies = {
        unit for units in fixed.knowledge.page_input_revisions().values() for unit in units
    }
    assert dependencies - current, "The fixture must retain older page evidence."
    store = catalog(kb_dir)
    for revision_id in current | dependencies:
        target = store.unit_revision(revision_id)
        assert store.read_original(target.source_revision_id)
        original = read_document_source(
            kb_dir,
            target.source_id,
            unit_id=target.unit_id,
            unit_revision_id=target.unit_revision_id,
            scope=fixed,
        )
        assert original["content"].strip()
    assert bool(refresh_status(kb_dir, scope=scope)["needs_refresh"]) is not all_sources
