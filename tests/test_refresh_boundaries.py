"""Public refresh boundaries reject invalid evidence and preserve deliberate exclusions."""

import asyncio
import json

import pytest
from test_query_views import _import_rule

from openkb.application.pages import read_page
from openkb.application.query_views import read_query_page, resolve_query_views
from openkb.application.refresh import (
    cleanup_unreferenced_artifacts,
    confirm_empty_source,
    read_refresh_proposal,
    refresh_knowledge_view,
)
from openkb.application.removal import remove_document
from openkb.application.views import view_scope


@pytest.mark.parametrize("operation", ["empty", "withdrawn"])
def test_inactive_input_is_not_restored_by_import_in_another_view(kb_dir, monkeypatch, operation):
    from openkb.source_catalog import read_source

    first = _import_rule(kb_dir, monkeypatch, "install", "1", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    if operation == "empty":
        confirm_empty_source(
            kb_dir,
            first.source_id,
            generation=read_source(kb_dir, first.source_id).target_generation,
        )
    else:
        remove_document(kb_dir, first.source_id)
    (kb_dir / "install.pdf").unlink()
    second = _import_rule(kb_dir, monkeypatch, "install", "2", "TLS is optional.")
    assert first.source_id == second.source_id
    assert not resolve_query_views(kb_dir, "WinStack V1 TLS?", scope=scope).views
    assert resolve_query_views(kb_dir, "WinStack V2 TLS?").views
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "completed", result.message
    assert not (scope.wiki_dir / "summaries/install.md").exists()


def test_cleanup_rejects_symbolic_link_ancestors(kb_dir):
    artifacts = kb_dir / ".openkb/artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "alias").symlink_to(kb_dir / ".openkb", target_is_directory=True)
    config = kb_dir / ".openkb/config.yaml"
    original = config.read_bytes()
    with pytest.raises(ValueError, match="symbolic link|managed"):
        cleanup_unreferenced_artifacts(kb_dir, [".openkb/artifacts/alias/config.yaml"])
    assert config.read_bytes() == original


def test_known_link_cannot_hide_another_unparsed_managed_reference(kb_dir):
    from openkb.application.pages import save_page

    paths = [f".openkb/artifacts/{letter * 64}/content.png" for letter in ("a", "b")]
    for relative in paths:
        target = kb_dir / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"referenced asset")
    (kb_dir / "wiki/concepts/manual.md").write_text("# Notes")
    save_page(
        kb_dir,
        "concepts/manual",
        (f'<img srcset="../../{paths[0]} 1x">\n\n[Known](../../{paths[1]})'),
    )
    result = cleanup_unreferenced_artifacts(kb_dir, [paths[0]])
    assert result["retained"] == [paths[0]]
    assert (kb_dir / paths[0]).is_file()


def test_refresh_rejects_candidate_with_foreign_ancestry(kb_dir, monkeypatch):
    from openkb.application.pages import save_page

    first = _import_rule(kb_dir, monkeypatch, "install", "1", "TLS is required.")
    second = _import_rule(kb_dir, monkeypatch, "operate", "2", "TLS is optional.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    save_page(kb_dir, "summaries/install", "Manual knowledge", scope=scope)
    remove_document(kb_dir, first.source_id)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "awaiting_confirmation"
    path = kb_dir / ".openkb/refresh-proposals" / result.proposal_id / "proposal.json"
    candidate = json.loads(path.read_text())
    candidate["candidate_manifest"]["base_revision_id"] = second.units[0].knowledge_revision_id
    path.write_text(json.dumps(candidate))
    with pytest.raises(ValueError):
        read_refresh_proposal(kb_dir, result.proposal_id)


def test_page_rejects_dependency_belonging_to_another_view(kb_dir, monkeypatch):
    from openkb.unit_publication import read_head

    first = _import_rule(kb_dir, monkeypatch, "install", "1", "TLS is required.")
    second = _import_rule(kb_dir, monkeypatch, "operate", "2", "TLS is optional.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    foreign = tuple(read_head(kb_dir, second.units[0].view_id).inputs.values())
    path = (
        kb_dir
        / ".openkb/knowledge"
        / scope.view_id
        / "revisions"
        / first.units[0].knowledge_revision_id
        / "manifest.json"
    )
    manifest = json.loads(path.read_text())
    manifest["page_dependencies"]["summaries/install.md"] = foreign
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_page(kb_dir, "summaries/install", scope=scope)
    with pytest.raises(ValueError):
        selection = resolve_query_views(kb_dir, "WinStack V1?", scope=scope)
        read_query_page(selection, "summaries/install.md", view_id=scope.view_id)


@pytest.mark.parametrize("path", ["./summaries/install", "summaries/../summaries/install"])
def test_page_alias_cannot_hide_pending_evidence(kb_dir, monkeypatch, path):
    first = _import_rule(kb_dir, monkeypatch, "install", "1", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    remove_document(kb_dir, first.source_id)
    page = read_page(kb_dir, path, scope=scope)
    assert page.validity == "needs_refresh"
    assert page.source_revision_ids == (first.source_revision_id,)
    assert page.refresh_reasons


def test_refresh_preserves_cancellation(kb_dir, monkeypatch):
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled

    first = _import_rule(kb_dir, monkeypatch, "install", "1", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    stopped = False
    completion = __import__("litellm").completion

    def cancel(**kwargs):
        nonlocal stopped
        stopped = True
        return completion(**kwargs)

    monkeypatch.setattr("litellm.completion", cancel)
    with pytest.raises(LockCancelled):
        asyncio.run(
            refresh_knowledge_view(
                kb_dir, scope=scope, context=ExecutionContext(cancelled=lambda: stopped)
            )
        )


def _legacy(kb_dir):
    from openkb.state import HashRegistry

    (kb_dir / "wiki/sources/old.md").write_text("TLS is required.")
    (kb_dir / "wiki/summaries/old.md").write_text("TLS is required.")
    registry = kb_dir / ".openkb/hashes.json"
    registry.write_text(
        json.dumps(
            {
                "f" * 64: {
                    "name": "old.md",
                    "doc_name": "old",
                    "type": "md",
                }
            }
        )
    )
    assert HashRegistry(registry).all_entries()


@pytest.mark.parametrize("mapped", [False, True])
def test_refresh_blocks_legacy_inputs_without_published_normalization(kb_dir, mapped):
    from openkb.application.views import map_legacy_sources

    _legacy(kb_dir)
    if mapped:
        map_legacy_sources(kb_dir)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=view_scope(kb_dir, "legacy")))
    assert result.status == "blocked"
    assert (kb_dir / "wiki/sources/old.md").read_text() == "TLS is required."
    assert (kb_dir / "wiki/summaries/old.md").is_file()


def test_mapped_legacy_withdrawal_does_not_revive_registry_fallback(kb_dir):
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.recompilation import select_recompilation
    from openkb.application.views import map_legacy_sources

    _legacy(kb_dir)
    identity = map_legacy_sources(kb_dir)["source_ids"][0]
    assert remove_document(kb_dir, identity).status == "removed"
    assert get_kb_list(kb_dir)["documents"] == []
    assert select_recompilation(kb_dir, all_docs=True).status == "empty"
    selection = resolve_query_views(kb_dir, "TLS requirements?")
    assert "TLS is required." not in read_query_page(
        selection, "summaries/old.md", view_id="legacy"
    )
    assert read_page(kb_dir, "summaries/old").validity == "needs_refresh"
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=view_scope(kb_dir, "legacy")))
    assert result.status == "blocked"


@pytest.mark.parametrize("identifier", ["old", "f" * 64, "ol"])
def test_repeating_legacy_withdrawal_cannot_delete_preserved_knowledge(kb_dir, identifier):
    from openkb.application.removal import run_remove_for_api
    from openkb.application.views import map_legacy_sources

    _legacy(kb_dir)
    identity = map_legacy_sources(kb_dir)["source_ids"][0]
    assert remove_document(kb_dir, identity).status == "removed"
    assert remove_document(kb_dir, identifier).status == "not_found"
    assert run_remove_for_api(kb_dir, identifier)["status"] == "not_found"
    assert (kb_dir / "wiki/sources/old.md").is_file()
    assert read_page(kb_dir, "summaries/old").content == "TLS is required."
