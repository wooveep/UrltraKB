"""HTTP reads exact managed sources from one native publication."""

import pytest
from test_api import _auth, _client, _use_named_kb

from ultrakb.application.views import published_scope
from ultrakb.desktop.verification_data import retain_document
from ultrakb.storage.catalog_store import CatalogStore


def source_fixture(kb, name, doc_name, text, *, pages=None):
    retain_document(kb, name, doc_name, text, pages=pages)
    catalog = CatalogStore(kb, published_scope(kb).knowledge.identity)
    return next(
        source.source_id
        for source in catalog.source_page()
        if any(unit.doc_name == doc_name for unit in catalog.source_units(source.source_id))
    )


def test_document_source_short_md(monkeypatch, kb_dir):
    identity = source_fixture(kb_dir, "notes.md", "notes", "# Notes\n\nBody text.")
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["content"] == "# Notes\n\nBody text."
    assert payload["format"] == "markdown" and payload["pages"] is None
    assert payload["name"] == "notes.md" and payload["source_id"] == identity


def test_document_source_long_json_concatenates_pages(monkeypatch, kb_dir):
    pages = [
        {"page": i, "unit_kind": "page", "content": f"Page {i} text.", "images": []} for i in (1, 2)
    ]
    identity = source_fixture(kb_dir, "paper.pdf", "paper", "Paper", pages=pages)
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
    )
    assert response.status_code == 200, response.text
    assert (
        "Page 1 text." in response.json()["content"]
        and "Page 2 text." in response.json()["content"]
    )
    assert response.json()["source_id"] == identity


def test_document_source_uses_its_retained_processing_path(monkeypatch, kb_dir):
    identity = source_fixture(kb_dir, "report.txt", "independent-internal-name", "Converted.")
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
    )
    assert response.status_code == 200 and response.json()["content"] == "Converted."


def test_document_source_unknown_hash_404(monkeypatch, kb_dir):
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": "f" * 32}, headers=_auth()
    )
    assert response.status_code == 404


def test_document_source_without_a_processing_unit_is_not_public(monkeypatch, kb_dir):
    from focus_fixtures import unprocessed_source

    identity = unprocessed_source(kb_dir, kb_dir / "raw/unprocessed.md")
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
    )
    assert response.status_code == 404


def test_document_source_same_named_originals_keep_independent_identity(monkeypatch, kb_dir):
    short = source_fixture(kb_dir, "dup.md", "dup-md", "SHORT content")
    long = source_fixture(
        kb_dir,
        "dup.pdf",
        "dup-pdf",
        "LONG",
        pages=[
            {"page": 1, "unit_kind": "page", "content": "LONG page one", "images": []},
        ],
    )
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    for identity, wanted, other in [
        (short, "SHORT content", "LONG page one"),
        (long, "LONG page one", "SHORT content"),
    ]:
        response = client.post(
            "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
        )
        assert response.status_code == 200, response.text
        assert wanted in response.json()["content"] and other not in response.json()["content"]


@pytest.mark.parametrize("corrupt", [b"{not valid json", b'{"pages":3}'])
def test_document_source_corrupt_retained_bytes_return_a_controlled_error(
    monkeypatch, kb_dir, corrupt
):
    identity = source_fixture(
        kb_dir,
        "broken.pdf",
        "broken",
        "Retained",
        pages=[
            {"page": 1, "unit_kind": "page", "content": "Retained", "images": []},
        ],
    )
    read = CatalogStore.read_processed

    def interrupted_read(store, processing_id, path):
        data = read(store, processing_id, path)
        return corrupt if path == "pages.json" else data

    monkeypatch.setattr(CatalogStore, "read_processed", interrupted_read)
    client, kb = _client(monkeypatch), _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        "/api/v1/document/source", json={"kb": kb, "hash": identity}, headers=_auth()
    )
    assert response.status_code == 500, response.text
    assert "Could not read" in response.json()["detail"]


def test_document_source_requires_auth(monkeypatch, kb_dir):
    client = _client(monkeypatch)
    _use_named_kb(monkeypatch, kb_dir)
    response = client.post("/api/v1/document/source", json={"kb": "test-kb", "hash": "f" * 32})
    assert response.status_code == 401
