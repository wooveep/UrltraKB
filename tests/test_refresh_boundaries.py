"""Public refresh boundaries reject invalid evidence and preserve deliberate exclusions."""

import asyncio
import json

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401
from test_query_views import _import_rule

from ultrakb.application.pages import read_page
from ultrakb.application.query_views import read_query_page, resolve_query_views
from ultrakb.application.refresh import (
    cleanup_unreferenced_artifacts,
    confirm_empty_source,
    read_refresh_proposal,
    refresh_knowledge_view,
)
from ultrakb.application.removal import remove_document
from ultrakb.application.views import view_scope


@pytest.mark.parametrize("operation", ["empty", "withdrawn"])
def test_reimport_does_not_restore_excluded_input(kb_dir, monkeypatch, operation):
    from ultrakb.source_catalog import read_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
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
    second = _import_rule(kb_dir, monkeypatch, "install", "TLS is optional.")
    assert first.source_id == second.source_id
    from ultrakb.source_changes import effective_input

    assert not effective_input(kb_dir, "legacy", first.units[0].successful_revision_id)
    assert effective_input(kb_dir, "legacy", second.units[0].successful_revision_id)
    assert resolve_query_views(kb_dir, "What is the TLS rule?", scope=scope).has_evidence
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "completed", result.message
    assert "TLS is optional." in (scope.wiki_dir / "summaries/install.md").read_text()


def test_cleanup_rejects_symbolic_link_ancestors(kb_dir):
    artifacts = kb_dir / ".ultrakb/artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "alias").symlink_to(kb_dir / ".ultrakb", target_is_directory=True)
    config = kb_dir / ".ultrakb/config.yaml"
    original = config.read_bytes()
    with pytest.raises(ValueError, match="symbolic link|managed"):
        cleanup_unreferenced_artifacts(kb_dir, [".ultrakb/artifacts/alias/config.yaml"])
    assert config.read_bytes() == original


def test_known_link_cannot_hide_another_unparsed_managed_reference(kb_dir):
    from ultrakb.application.pages import save_page

    paths = [f".ultrakb/artifacts/{letter * 64}/content.png" for letter in ("a", "b")]
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
    from ultrakb.application.pages import save_page

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    _import_rule(kb_dir, monkeypatch, "operate", "TLS is optional.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    save_page(kb_dir, "summaries/install", "Manual knowledge", scope=scope)
    remove_document(kb_dir, first.source_id)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "awaiting_confirmation"
    path = kb_dir / ".ultrakb/refresh-proposals" / result.proposal_id / "proposal.json"
    candidate = json.loads(path.read_text())
    candidate["candidate_manifest"]["base_revision_id"] = first.units[0].knowledge_revision_id
    path.write_text(json.dumps(candidate))
    with pytest.raises(ValueError):
        read_refresh_proposal(kb_dir, result.proposal_id)


def test_readers_reject_a_source_revision_assigned_to_another_unit(kb_dir, monkeypatch):
    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    second = _import_rule(kb_dir, monkeypatch, "operate", "TLS is optional.")
    path = (
        kb_dir / ".ultrakb/catalog/unit-revisions" / f"{first.units[0].successful_revision_id}.json"
    )
    revision = json.loads(path.read_text())
    revision["source_revision_id"] = second.source_revision_id
    path.write_text(json.dumps(revision))
    with pytest.raises(ValueError, match="another source"):
        read_page(kb_dir, "summaries/install")
    with pytest.raises(ValueError, match="another source"):
        resolve_query_views(kb_dir, "What is the TLS rule?")


@pytest.mark.parametrize("path", ["./summaries/install", "summaries/../summaries/install"])
def test_page_alias_cannot_hide_pending_evidence(kb_dir, monkeypatch, path):
    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    remove_document(kb_dir, first.source_id)
    page = read_page(kb_dir, path, scope=scope)
    assert page.validity == "needs_refresh"
    assert page.source_revision_ids == (first.source_revision_id,)
    assert page.refresh_reasons


def test_refresh_preserves_cancellation(kb_dir, monkeypatch):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import LockCancelled

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
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
    from ultrakb.state import HashRegistry

    (kb_dir / "wiki/sources/old.md").write_text("TLS is required.")
    (kb_dir / "wiki/summaries/old.md").write_text("TLS is required.")
    registry = kb_dir / ".ultrakb/hashes.json"
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
    from ultrakb.application.views import map_legacy_sources

    _legacy(kb_dir)
    if mapped:
        map_legacy_sources(kb_dir)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=view_scope(kb_dir, "legacy")))
    assert result.status == "blocked"
    assert (kb_dir / "wiki/sources/old.md").read_text() == "TLS is required."
    assert (kb_dir / "wiki/summaries/old.md").is_file()


def test_mapped_legacy_withdrawal_does_not_revive_registry_fallback(kb_dir):
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.application.recompilation import select_recompilation
    from ultrakb.application.views import map_legacy_sources

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
    from ultrakb.application.removal import run_remove_for_api
    from ultrakb.application.views import map_legacy_sources

    _legacy(kb_dir)
    identity = map_legacy_sources(kb_dir)["source_ids"][0]
    assert remove_document(kb_dir, identity).status == "removed"
    assert remove_document(kb_dir, identifier).status == "not_found"
    assert run_remove_for_api(kb_dir, identifier)["status"] == "not_found"
    assert (kb_dir / "wiki/sources/old.md").is_file()
    assert read_page(kb_dir, "summaries/old").content == "TLS is required."
