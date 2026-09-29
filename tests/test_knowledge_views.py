"""Shared applicability and isolated unknown sources at the import/read boundary."""

import asyncio
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
        path = kb_dir / "versioned.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            path.write_bytes(pdf.tobytes())
        return import_document(kb_dir, path, **kwargs)

    run.calls = calls
    return run


def test_omitted_metadata_preserves_confirmed_labels(kb_dir, import_pdf):
    from openkb.application.documents import import_document
    from openkb.application.views import view_scope
    from openkb.source_catalog import read_record, read_source
    from openkb.view_records import SourceMetadata, VersionAnnotation

    metadata = SourceMetadata(
        product="Product", applicable_versions=("1",), family="install", document_revision="R1"
    )
    first = import_pdf(metadata=metadata)
    second = import_document(
        kb_dir, kb_dir / "versioned.pdf", scope=view_scope(kb_dir, first.units[0].view_id)
    )
    assert second.status == "skipped"
    assert len(import_pdf.calls) == 2
    source = read_source(kb_dir, first.source_id)
    assert (
        read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation).metadata
        == metadata
    )


@pytest.mark.parametrize(
    "first_metadata,second_metadata,second_status",
    [
        ({"product": "A"}, {"product": "B"}, "added"),
        ({"applicable_versions": ("1",)}, {"applicable_versions": ("2",)}, "blocked"),
    ],
)
def test_changed_partial_applicability_gets_its_own_view(
    kb_dir, import_pdf, first_metadata, second_metadata, second_status
):
    from openkb.view_records import SourceMetadata

    first = import_pdf(metadata=SourceMetadata(**first_metadata))
    second = import_pdf("Changed instruction.", metadata=SourceMetadata(**second_metadata))
    assert first.status == "added" and second.status == second_status
    assert first.units[0].view_id != second.units[0].view_id


def test_old_view_preserves_annotation_and_can_recompile(kb_dir, import_pdf):
    from openkb.application.recompilation import recompile_document
    from openkb.application.sources import source_inventory
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.source_catalog import read_record
    from openkb.view_records import SourceMetadata, VersionAnnotation

    first = import_pdf(
        metadata=SourceMetadata(
            product="Product", applicable_versions=("1",), family="install", document_revision="R1"
        )
    )
    old = view_scope(kb_dir, first.units[0].view_id)
    second = import_pdf(
        "Changed instruction.",
        metadata=SourceMetadata(
            product="Product",
            applicable_versions=("2",),
            family="operations",
            document_revision="R2",
        ),
    )
    current = view_scope(kb_dir, second.units[0].view_id)
    original = read_document_source(kb_dir, first.source_id, scope=old)
    item = source_inventory(kb_dir, scope=old)[0]
    annotation = read_record(kb_dir, "annotations", item["annotation_id"], VersionAnnotation)
    assert annotation.view_id == old.view_id
    assert annotation.source_revision_id == item["source_revision_id"]
    assert annotation.family_id == item["family_id"]
    result = asyncio.run(recompile_document(kb_dir, first.source_id, scope=old))
    assert result.status == "compiled"
    assert (
        read_document_source(kb_dir, first.source_id, scope=old)["content"] == original["content"]
    )
    assert (
        "Changed instruction"
        in read_document_source(kb_dir, first.source_id, scope=current)["content"]
    )


def test_failed_view_does_not_expose_a_later_views_original(kb_dir, import_pdf, monkeypatch):
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    with monkeypatch.context() as patch:
        patch.setattr(
            "litellm.completion", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        failed = import_pdf(metadata=SourceMetadata(product="Product", applicable_versions=("1",)))
    first = read_document_source(
        kb_dir, failed.source_id, scope=view_scope(kb_dir, failed.units[0].view_id)
    )
    latest = import_pdf(
        "Changed instruction.",
        metadata=SourceMetadata(product="Product", applicable_versions=("2",)),
    )
    old = read_document_source(
        kb_dir, failed.source_id, scope=view_scope(kb_dir, failed.units[0].view_id)
    )
    assert latest.status == "added"
    assert old["source_revision_id"] == first["source_revision_id"]
    assert old["original_path"] == first["original_path"]


def test_two_document_families_share_a_confirmed_product_scope(kb_dir, monkeypatch):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.application.views import list_views
    from openkb.view_records import SourceMetadata

    install = kb_dir / "install.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "A product instruction.")
        pdf.save(install)
    operate = kb_dir / "operate.pdf"
    operate.write_bytes(install.read_bytes())
    replies = iter(
        [
            {"description": "Manual", "content": "# Manual\n\nProduct instructions."},
            {"create": [], "update": [], "related": []},
        ]
        * 2
    )
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    first = import_document(
        kb_dir,
        install,
        metadata=SourceMetadata(
            product="WinStack",
            applicable_versions=("9.4",),
            family="installation",
            document_revision="R1",
        ),
    )
    second = import_document(
        kb_dir,
        operate,
        metadata=SourceMetadata(
            product="WinStack",
            applicable_versions=("9.4",),
            family="operations",
            document_revision="R2",
        ),
    )
    assert first.status == second.status == "added"
    assert first.source_id != second.source_id
    assert first.units[0].view_id == second.units[0].view_id != "legacy"
    views = [view for view in list_views(kb_dir) if view.view_id != "legacy"]
    assert len(views) == 1 and views[0].applicable_versions == ("9.4",)


@pytest.mark.parametrize(
    "other",
    [
        {"product": "Other", "applicable_versions": ("9.4",)},
        {"product": "WinStack", "applicable_versions": ("9.3", "9.4")},
        {},
    ],
)
def test_other_scopes_cannot_read_a_sources_published_body(kb_dir, monkeypatch, other):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    pdf_path = kb_dir / "source.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Only the selected view can read this.")
        pdf.save(pdf_path)
    second_path = kb_dir / "other.pdf"
    second_path.write_bytes(pdf_path.read_bytes())
    replies = iter([{"description": "Source", "content": "Known statement."}, {}] * 2)
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    first = import_document(
        kb_dir,
        pdf_path,
        metadata=SourceMetadata(
            product="WinStack",
            applicable_versions=("9.4",),
        ),
    )
    second = import_document(kb_dir, second_path, metadata=SourceMetadata(**other))
    own = view_scope(kb_dir, first.units[0].view_id)
    foreign = view_scope(kb_dir, second.units[0].view_id)
    assert first.units[0].view_id != second.units[0].view_id
    assert (
        "Only the selected view"
        in read_document_source(kb_dir, first.source_id, scope=own)["content"]
    )
    assert read_document_source(kb_dir, first.source_id, scope=foreign) is None


def test_conversation_rejects_a_different_view_before_model_work(kb_dir):
    import asyncio

    from openkb.agent.chat_session import ChatSession
    from openkb.application.conversations import continue_conversation
    from openkb.knowledge_scope import live_scope

    selected = live_scope(kb_dir, "a" * 32)
    selected.wiki_dir.mkdir(parents=True)
    session = ChatSession.new(kb_dir, "test-model", "en")
    session.record_turn("Which version?", "Legacy content", [])

    with pytest.raises(ValueError, match="view"):
        asyncio.run(
            continue_conversation(kb_dir, "Continue", session_id=session.id, scope=selected)
        )


def test_api_upload_exposes_an_explicit_readable_view(kb_dir, monkeypatch):
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
                "metadata": json.dumps(
                    {"product": "WinStack", "applicable_versions": ["9.4"], "family": "install"}
                ),
            },
            files={"files": ("manual.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200
        item = response.json()["files"][0]
        selected = item["units"][0]["view_id"]
        assert selected != "legacy"
        read = client.post(
            "/api/v1/page",
            json={"kb": "view-test", "view_id": selected, "path": "summaries/manual"},
        )
        assert read.status_code == 200 and "Version nine" in read.json()["content"]
        old = client.post(
            "/api/v1/page",
            json={"kb": "view-test", "view_id": "legacy", "path": "summaries/manual"},
        )
        assert old.status_code == 404


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


def test_selected_view_cannot_remove_legacy_files(kb_dir, import_pdf):
    from click.testing import CliRunner

    from openkb.application.removal import preview_removal, remove_document, run_remove_for_api
    from openkb.application.views import view_scope
    from openkb.cli import cli

    own = import_pdf()
    scope = view_scope(kb_dir, own.units[0].view_id)
    raw = kb_dir / "raw/legacy.md"
    raw.write_text("Legacy original.")
    registry = kb_dir / ".openkb/hashes.json"
    registry.write_text(
        json.dumps({"old": {"name": "legacy.md", "doc_name": "legacy", "type": "md"}})
    )
    summary = kb_dir / "wiki/summaries/legacy.md"
    summary.write_text("Legacy knowledge.")
    before = registry.read_bytes()
    assert preview_removal(kb_dir, "old", scope=scope).status == "not_found"
    assert remove_document(kb_dir, "old", scope=scope).status == "not_found"
    assert run_remove_for_api(kb_dir, "old", scope=scope)["status"] == "not_found"
    result = CliRunner().invoke(
        cli, ["--kb-dir", str(kb_dir), "--view", scope.view_id, "remove", "legacy.md", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert raw.read_text() == "Legacy original."
    assert summary.read_text() == "Legacy knowledge."
    assert registry.read_bytes() == before


def test_snapshot_excludes_later_sources_and_foreign_scopes(kb_dir, import_pdf, tmp_path):
    from openkb.application.documents import import_document
    from openkb.application.sources import source_inventory
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.unit_publication import read_head
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Product", applicable_versions=("1",))
    first = import_pdf(metadata=metadata)
    live = view_scope(kb_dir, first.units[0].view_id)
    snapshot = view_scope(
        kb_dir,
        live.view_id,
        historical_revision=read_head(kb_dir, live.view_id).knowledge_revision_id,
    )
    second_path = kb_dir / "later.pdf"
    second_path.write_bytes((kb_dir / "versioned.pdf").read_bytes())
    second = import_document(kb_dir, second_path, metadata=metadata)
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
