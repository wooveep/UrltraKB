"""A published reading context remains coherent while a writer changes the KB."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ultrakb.application.knowledge import list_knowledge
from ultrakb.application.reading import read_page_context


def test_public_context_keeps_catalog_body_and_links_while_writer_holds_lease(kb_dir):
    from ultrakb.application.pages import delete_page
    from ultrakb.application.views import published_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication

    atomic_write_text(kb_dir / "wiki/concepts/first.md", "# First\nOld [[concepts/second]].")
    atomic_write_text(kb_dir / "wiki/concepts/second.md", "# Second\n[[concepts/first]]")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    selected = published_scope(kb_dir)
    entered, release = threading.Event(), threading.Event()

    def writing():
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            atomic_write_text(kb_dir / "wiki/concepts/first.md", "Unpublished replacement")
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(writing)
        assert entered.wait(5)
        try:
            context = pool.submit(
                read_page_context, kb_dir, "concepts/first", scope=selected
            ).result(2)
            catalog = pool.submit(list_knowledge, kb_dir, scope=selected).result(2)
            deletion = pool.submit(
                delete_page, kb_dir, "concepts/first", dry_run=True, scope=selected
            ).result(2)
            assert context.page.body == "# First\nOld [[concepts/second]]."
            assert context.page.validity == "current"
            assert context.page.publication_id == selected.publication_id
            assert [item.path for item in context.outlinks] == ["concepts/second"]
            assert [item.path for item in context.backlinks] == ["concepts/second"]
            assert {item.title for item in catalog} == {"First", "Second"}
            assert deletion["publication_id"] == selected.publication_id
            assert deletion["version"] == context.page.version
            assert deletion["backlinks"] == ["concepts/second"]
        finally:
            release.set()
        writer.result(5)


def test_public_read_requires_explicit_writer_initialization(kb_dir):
    from ultrakb.application.views import published_scope
    from ultrakb.published_knowledge import PublicationUnavailable

    with pytest.raises(PublicationUnavailable):
        published_scope(kb_dir)
    assert not (kb_dir / ".ultrakb/published").exists()


def test_public_source_preview_and_query_use_frozen_catalog(kb_dir):
    import shutil

    from publication_fixtures import publish_document

    from ultrakb.application.document_actions import document_inventory
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.application.query_views import read_query_page, resolve_query_views
    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.documents import read_document_source

    original = kb_dir / "raw/manual.md"
    original.write_text("Original requirement: retain every receipt.\n")
    publish_document(
        kb_dir,
        original,
        "Published summary.\n",
        bundle=LlmCredentialBundle(api_key="offline-unused"),
    )
    selected = published_scope(kb_dir)
    active_database = kb_dir / ".ultrakb/context.sqlite"
    assert not active_database.exists()
    catalog = get_kb_list(kb_dir, scope=selected)
    [source] = catalog["documents"]
    shutil.rmtree(kb_dir / ".ultrakb/catalog")
    original.unlink()
    (kb_dir / "wiki/sources/manual.md").write_text("Unpublished source replacement")

    preview = read_document_source(kb_dir, source["source_id"], scope=selected)
    assert preview["content"] == "Original requirement: retain every receipt.\n"
    assert preview["validity"] == "current"
    assert preview["publication_id"] == selected.publication_id
    query = resolve_query_views(kb_dir, scope=selected)
    text = read_query_page(query, "sources/manual.md", view_id="legacy", strict=True)
    assert "Original requirement: retain every receipt." in text
    assert "status=current" in text
    [row] = document_inventory(kb_dir, scope=selected)
    assert row.actions["read"].allowed
    assert row.actions["reprocess"].allowed
    assert not active_database.exists()


def test_http_reads_pin_one_publication_and_report_a_bad_pin(kb_dir, monkeypatch):
    from test_api import _auth, _client, _use_named_kb

    from ultrakb.application.pages import save_page
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication

    atomic_write_text(kb_dir / "wiki/concepts/first.md", "Old body.\n")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    client = _client(monkeypatch)
    name = _use_named_kb(monkeypatch, kb_dir)
    listing = client.post("/api/v1/list", headers=_auth(), json={"kb": name})
    assert listing.status_code == 200, listing.text
    identity = listing.json()["publication_id"]
    save_page(kb_dir, "concepts/first", "New body.\n")
    pinned = client.post(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
            "publication_id": identity,
        },
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["publication_id"] == identity
    assert "Old body." in pinned.json()["content"]
    current = client.post(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
        },
    )
    assert "New body." in current.json()["content"]
    bad = client.post(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
            "publication_id": "a" * 32,
        },
    )
    assert bad.status_code == 409
    mismatch = client.post(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
            "publication_id": identity,
            "knowledge_revision_id": current.json()["knowledge_revision_id"],
        },
    )
    assert mismatch.status_code == 409
    graph = client.post(
        "/api/v1/graph",
        headers=_auth(),
        json={
            "kb": name,
            "publication_id": identity,
        },
    )
    assert graph.status_code == 200, graph.text
    assert graph.json()["publication_id"] == identity
    assert graph.json()["nodes"][0]["id"] == "concepts/first"
    conflict = client.put(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
            "content": "Draft body.",
            "publication_id": identity,
            "version": pinned.json()["version"],
        },
    )
    assert conflict.status_code == 409
    missing_preconditions = client.put(
        "/api/v1/page",
        headers=_auth(),
        json={
            "kb": name,
            "path": "concepts/first",
            "content": "An unbound edit.",
        },
    )
    assert missing_preconditions.status_code == 422


def test_edit_checks_public_identity_even_when_the_opened_page_has_not_changed(kb_dir):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.views import published_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication

    atomic_write_text(kb_dir / "wiki/concepts/first.md", "First body.\n")
    atomic_write_text(kb_dir / "wiki/concepts/second.md", "Second body.\n")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    selected = published_scope(kb_dir)
    opened = read_page(kb_dir, "concepts/first", scope=selected)
    assert save_page(kb_dir, "concepts/second", "Other edit.\n").status == "saved"
    result = save_page(
        kb_dir, "concepts/first", "My draft.\n", version=opened.version, scope=selected
    )
    assert result.status == "conflict"
    assert result.draft == "My draft.\n"
    assert read_page(kb_dir, "concepts/first").body == "First body.\n"


def test_http_delete_uses_pinned_confirmation_and_rejects_stale_preconditions(kb_dir, monkeypatch):
    from publication_read_inputs import publish_fixture
    from test_api import _auth, _client, _use_named_kb

    from ultrakb.application.pages import read_page, save_page

    (kb_dir / "wiki/concepts/first.md").write_text("Publication A.\n")
    (kb_dir / "wiki/concepts/second.md").write_text("See [[concepts/first]].\n")
    publish_fixture(kb_dir)
    client = _client(monkeypatch)
    name = _use_named_kb(monkeypatch, kb_dir)
    request = {"kb": name, "path": "concepts/first"}

    opened = client.post("/api/v1/page/delete", headers=_auth(), json={**request, "dry_run": True})
    assert opened.status_code == 200, opened.text
    original = opened.json()
    assert original["publication_id"]
    assert original["version"] == read_page(kb_dir, "concepts/first").version
    assert original["backlinks"] == ["concepts/second"]

    save_page(kb_dir, "concepts/first", "Publication B.\n")
    save_page(kb_dir, "concepts/second", "No remaining link.\n")
    pinned = client.post(
        "/api/v1/page/delete",
        headers=_auth(),
        json={**request, "dry_run": True, "publication_id": original["publication_id"]},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == original
    stale = client.post(
        "/api/v1/page/delete",
        headers=_auth(),
        json={
            **request,
            "publication_id": original["publication_id"],
            "version": original["version"],
        },
    )
    assert stale.status_code == 409, stale.text
    assert read_page(kb_dir, "concepts/first").body == "Publication B.\n"

    for preconditions in (
        {},
        {"publication_id": original["publication_id"]},
        {"version": original["version"]},
    ):
        missing = client.post(
            "/api/v1/page/delete", headers=_auth(), json={**request, **preconditions}
        )
        assert missing.status_code == 422, missing.text

    confirmed = client.post(
        "/api/v1/page/delete", headers=_auth(), json={**request, "dry_run": True}
    ).json()
    assert confirmed["publication_id"] != original["publication_id"]
    assert confirmed["backlinks"] == []
    fresh = client.post(
        "/api/v1/page/delete",
        headers=_auth(),
        json={
            **request,
            "publication_id": confirmed["publication_id"],
            "version": confirmed["version"],
        },
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["status"] == "deleted"
    assert not (kb_dir / "wiki/concepts/first.md").exists()


def test_http_image_uses_the_pinned_publication(kb_dir, monkeypatch):
    from test_api import _auth, _client, _use_named_kb

    from ultrakb.application.pages import save_page
    from ultrakb.application.views import published_scope
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication

    image = kb_dir / "wiki/sources/images/figure.png"
    image.write_bytes(b"old-image")
    atomic_write_text(kb_dir / "wiki/concepts/first.md", "Old figure.\n")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    identity = published_scope(kb_dir).publication_id
    image.write_bytes(b"new-image")
    save_page(kb_dir, "concepts/first", "New figure.\n")
    client = _client(monkeypatch)
    name = _use_named_kb(monkeypatch, kb_dir)
    response = client.get(
        "/api/v1/image",
        headers=_auth(),
        params={
            "kb": name,
            "path": "sources/images/figure.png",
            "publication_id": identity,
        },
    )
    assert response.status_code == 200, response.text
    assert response.content == b"old-image"
    assert response.headers["X-UltraKB-Publication-ID"] == identity
    assert response.headers["content-type"] == "image/png"


def test_public_source_preview_retains_a_failed_new_documents_normalized_body(kb_dir):
    from publication_fixtures import fail_document

    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.documents import read_document_source

    original = kb_dir / "manual.md"
    original.write_text("A retained original while knowledge generation is unfinished.\n")
    failed = fail_document(kb_dir, original, bundle=LlmCredentialBundle(api_key="offline-unused"))
    assert failed.status == "failed"
    selected = published_scope(kb_dir)
    preview = read_document_source(kb_dir, failed.source_id, scope=selected)
    assert preview is not None
    assert preview["content"] == "A retained original while knowledge generation is unfinished.\n"
    assert preview["publication_id"] == selected.publication_id
    assert preview["status"] == "failed"


def test_public_source_preview_can_read_an_older_dependency_of_a_pending_page(kb_dir):
    from publication_fixtures import publish_document

    from ultrakb.application.pages import read_page
    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.documents import read_document_source

    bundle = LlmCredentialBundle(api_key="offline-unused")
    original = kb_dir / "manual.md"
    original.write_text("Original rule: retain receipts for seven years.\n")
    first = publish_document(
        kb_dir,
        original,
        "First summary.\n",
        bundle=bundle,
        shared_body="Retain receipts for seven years.\n",
    )
    original.write_text("Replacement rule: retain receipts for nine years.\n")
    publish_document(kb_dir, original, "Replacement summary.\n", bundle=bundle)
    selected = published_scope(kb_dir)
    pending = read_page(kb_dir, "concepts/shared", scope=selected)
    assert pending.validity == "needs_refresh"
    assert pending.source_revision_ids == (first.source_revision_id,)
    preview = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id, scope=selected
    )
    assert preview is not None
    assert preview["content"] == "Original rule: retain receipts for seven years.\n"
    assert preview["publication_id"] == selected.publication_id
