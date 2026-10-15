"""Snapshot selection pins the actual readable source bytes for one question."""

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401


def _import_rule(kb_dir, monkeypatch, name, fact):
    """Publish retained factual input for view tests without repeating compilation."""
    import pymupdf
    from publication_fixtures import publish_document

    from ultrakb.config import resolve_credential_bundle

    path = kb_dir / f"{name}.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), fact)
        pdf.save(path)
    result = publish_document(
        kb_dir,
        path,
        fact,
        bundle=resolve_credential_bundle(kb_dir),
    )
    assert result.status == "added", result.message
    return result


@pytest.fixture
def independent_documents(kb_dir, monkeypatch):
    return [
        _import_rule(kb_dir, monkeypatch, f"manual-{number}", fact)
        for number, fact in (("1", "TLS is required."), ("2", "TLS is forbidden."))
    ]


def test_query_pins_a_revision_and_reports_later_knowledge_changes(kb_dir, independent_documents):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.query_views import (
        read_query_page,
        resolve_query_views,
        selection_current,
    )
    from ultrakb.application.views import view_scope

    first, _ = independent_documents
    scope = view_scope(kb_dir, first.units[0].view_id)
    selection = resolve_query_views(kb_dir, "What is the TLS rule?", scope=scope)
    page = read_page(kb_dir, "summaries/manual-1", scope=scope)
    save_page(kb_dir, page.path, "A later manual correction.", version=page.version, scope=scope)
    assert "TLS is required." in read_query_page(
        selection, "summaries/manual-1.md", view_id=scope.view_id
    )
    assert not selection_current(selection)
