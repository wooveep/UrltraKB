"""Shared-wiki import and immutable source snapshot boundaries."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def import_pdf(kb_dir, monkeypatch):
    import pymupdf

    from openkb.application.documents import import_document

    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        reply = {"description": "Manual", "content": "Instruction."} if len(calls) % 2 else {}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.completion", complete)

    def run(text="Original instruction.", **kwargs):
        path = kb_dir / "original.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            path.write_bytes(pdf.tobytes())
        return import_document(kb_dir, path, **kwargs)

    run.calls = calls
    return run


def test_api_upload_uses_the_shared_readable_wiki(kb_dir, monkeypatch):
    import pymupdf
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.config import register_kb_alias

    register_kb_alias("view-test", kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    replies = iter([{"description": "Manual", "content": "Version nine."}, {}])
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Version nine instruction.")
        content = pdf.tobytes()
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={
                "kb": "view-test",
                "stream": "false",
            },
            files={"files": ("manual.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200
        item = response.json()["files"][0]
        selected = item["units"][0]["view_id"]
        assert selected == "legacy"
        read = client.post(
            "/api/v1/page",
            json={"kb": "view-test", "view_id": selected, "path": "summaries/manual"},
        )
        assert read.status_code == 200 and "Version nine" in read.json()["content"]
        old = client.post(
            "/api/v1/page",
            json={"kb": "view-test", "path": "summaries/manual"},
        )
        assert old.status_code == 200 and old.json()["content"] == read.json()["content"]


def test_legacy_mapping_preserves_readable_evidence_without_a_model(kb_dir):
    from openkb.application.views import map_legacy_sources, view_scope
    from openkb.documents import read_document_source

    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"old-hash": {"name": "manual.md", "doc_name": "manual", "type": "md"}})
    )
    page = kb_dir / "wiki/sources/manual.md"
    page.write_text("A mixed legacy source; its product was never recorded.")

    mapped = map_legacy_sources(kb_dir)

    assert len(mapped["source_ids"]) == 1 and not mapped["unavailable"]
    source = read_document_source(
        kb_dir, mapped["source_ids"][0], scope=view_scope(kb_dir, "legacy")
    )
    assert source["original_kind"] == "legacy_snapshot"
    assert source["content"] == page.read_text()


def test_snapshot_excludes_later_sources_and_foreign_scopes(kb_dir, import_pdf, tmp_path):
    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.unit_publication import read_head

    first = import_pdf()
    live = view_scope(kb_dir, first.units[0].view_id)
    snapshot = view_scope(
        kb_dir,
        live.view_id,
        historical_revision=read_head(kb_dir, live.view_id).knowledge_revision_id,
    )
    second_path = kb_dir / "later.pdf"
    second_path.write_bytes((kb_dir / "original.pdf").read_bytes())
    second = import_document(
        kb_dir,
        second_path,
    )
    assert len(source_inventory(kb_dir, scope=live)) == 2
    assert len(source_inventory(kb_dir, scope=snapshot)) == 1
    assert read_document_source(kb_dir, second.source_id, scope=snapshot) is None
    assert (
        read_document_source(kb_dir, first.source_id, scope=snapshot)["source_revision_id"]
        == first.source_revision_id
    )
    with pytest.raises(ValueError, match="different knowledge base"):
        source_inventory(tmp_path / "foreign", scope=live)
    with pytest.raises(ValueError, match="different knowledge base"):
        read_document_source(tmp_path / "foreign", first.source_id, scope=live)
