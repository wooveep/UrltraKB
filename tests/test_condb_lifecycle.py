"""Public import, proposal, revision, and deletion keep sealed ConDB evidence."""

import asyncio
import json

from openkb.application.documents import import_document
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.index_client import create_index_client
from openkb.index_location import IndexLocation
from openkb.knowledge_scope import legacy_scope

pytest_plugins = ("block_fixtures", "test_pdf_readback")


def prepare(kb):
    apply_kb_config_patch(
        kb,
        KbConfigPatchRequest(
            kb=str(kb),
            config={
                "model": "gpt-4o",
                "model_capacity": {"max_input_tokens": 1},
            },
        ),
    )
    source = kb / "greeting.md"
    source.write_text("hello world\n")
    return source


def index_for(kb, unit):
    return (
        kb / ".openkb/knowledge" / unit.view_id / "revisions" / unit.knowledge_revision_id / "index"
    )


def retained(kb, directory):
    location = IndexLocation.package(directory)
    assert location.read_only
    assert location.owner_revision == directory.parent.name
    assert not list(directory.glob("*-wal"))
    assert not (directory / "pageindex.db").exists()
    manifest = json.loads((directory.parent / "manifest.json").read_text())
    with create_index_client(location=location) as client:
        doc = client.collection().get_document(manifest["index_ref"], include_text=True)
    return doc


def test_recompile_and_source_removal_retain_original_index_without_rebuilding(kb_dir, block_model):
    from openkb.application.recompilation import recompile_document
    from openkb.application.removal import remove_document

    source = prepare(kb_dir)
    first = import_document(kb_dir, source, scope=legacy_scope(kb_dir))
    assert first.status == "added", first.message
    original_dir = index_for(kb_dir, first.units[0])
    original = retained(kb_dir, original_dir)
    database = (original_dir / "context.sqlite").read_bytes()
    calls = sum("CONTENT BLOCK STRUCTURE" in prompt for prompt in block_model)
    second = asyncio.run(recompile_document(kb_dir, first.source_id, scope=legacy_scope(kb_dir)))
    assert second.status == "compiled", second.message
    assert sum("CONTENT BLOCK STRUCTURE" in prompt for prompt in block_model) == calls
    assert retained(kb_dir, original_dir) == original
    assert (original_dir / "context.sqlite").read_bytes() == database
    result = remove_document(kb_dir, first.source_id, scope=legacy_scope(kb_dir))
    assert result.status == "removed"
    assert retained(kb_dir, original_dir) == original
    assert (original_dir / "context.sqlite").read_bytes() == database


def test_proposal_acceptance_copies_sealed_index_and_preserves_source_package(kb_dir, block_model):
    from openkb.application.proposals import accept_proposal, read_proposal

    source = prepare(kb_dir)
    (kb_dir / "wiki/summaries/greeting.md").write_text("Human explanation.\n")
    result = import_document(kb_dir, source, scope=legacy_scope(kb_dir))
    assert result.status == "blocked", result.message
    proposal_id = result.units[0].proposal_id
    package = kb_dir / ".openkb/proposals" / proposal_id / "index"
    assert IndexLocation.package(package).read_only
    original = (package / "context.sqlite").read_bytes()
    opened = read_proposal(kb_dir, proposal_id)
    accepted = accept_proposal(kb_dir, proposal_id, version=opened.version)
    assert accepted.status == "added", accepted.message
    published = index_for(kb_dir, accepted.units[0])
    assert retained(kb_dir, published)["structure"][0]["text"] == "hello world\n"
    assert (published / "context.sqlite").read_bytes() == original


def test_publication_copy_failure_keeps_old_head_and_index(kb_dir, block_model, monkeypatch):
    from openkb import index_packages
    from openkb.application.recompilation import recompile_document
    from openkb.unit_publication import read_head

    source = prepare(kb_dir)
    first = import_document(kb_dir, source, scope=legacy_scope(kb_dir))
    assert first.status == "added", first.message
    location = index_for(kb_dir, first.units[0])
    doc, head = retained(kb_dir, location), read_head(kb_dir)
    copy = index_packages.copy_index_package

    def fail(source, target, **kwargs):
        if "revisions" in target.parts:
            raise OSError("index copy interrupted")
        return copy(source, target, **kwargs)

    monkeypatch.setattr(index_packages, "copy_index_package", fail)
    result = asyncio.run(recompile_document(kb_dir, first.source_id, scope=legacy_scope(kb_dir)))
    assert result.status == "failed"
    assert read_head(kb_dir) == head
    assert retained(kb_dir, location) == doc


def test_public_pdf_import_seals_physical_pages_and_retained_images(
    kb_dir, physical_pdf, pdf_model
):
    from pathlib import Path

    from openkb.desktop.verification_documents import verify_long_pdf

    prepare(kb_dir)
    result = import_document(kb_dir, physical_pdf, scope=legacy_scope(kb_dir))
    assert result.status == "added", result.message
    assert verify_long_pdf(kb_dir, physical_pdf).startswith("summaries/")
    directory = index_for(kb_dir, result.units[0])
    manifest = json.loads((directory.parent / "manifest.json").read_text())
    original = retained(kb_dir, directory)
    physical_pdf.unlink()
    with create_index_client(location=IndexLocation.package(directory)) as reader:
        pages = reader.collection().get_page_content(manifest["index_ref"], "1-3")
        assert [page["page"] for page in pages] == [1, 2, 3]
        assert pages[1]["content"] == ""
        assert Path(pages[2]["images"][0]["path"]).is_file()
    assert retained(kb_dir, directory) == original
