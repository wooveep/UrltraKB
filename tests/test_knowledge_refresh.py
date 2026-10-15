"""Source changes retain evidence until an explicitly validated refresh replaces it."""

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401
from test_query_views import _import_rule


def test_withdrawal_marks_old_evidence_and_preserves_history(kb_dir, monkeypatch):
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.refresh import refresh_status
    from ultrakb.application.removal import preview_removal, remove_document
    from ultrakb.application.views import view_scope
    from ultrakb.documents import read_document_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    historical = view_scope(
        kb_dir, scope.view_id, historical_revision=first.units[0].knowledge_revision_id
    )
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Withdrawing a source must not call a model")
        ),
    )
    preview = preview_removal(kb_dir, first.source_id, scope=scope)
    removed = remove_document(kb_dir, first.source_id, version=preview.version, scope=scope)
    assert removed.status == "removed"
    status = refresh_status(kb_dir, scope=scope)
    reasons = status["needs_refresh"]["summaries/install.md"]
    assert reasons[0]["source_revision_id"] == first.source_revision_id
    assert reasons[0]["kind"] == "withdrawn"
    assert resolve_query_views(kb_dir, "WinStack V1 TLS?", scope=scope).views == ()
    assert (
        "TLS is required."
        in read_document_source(kb_dir, first.source_id, scope=historical)["content"]
    )
    assert removed.retained


def test_failed_update_keeps_old_body_with_pending_refresh(kb_dir, monkeypatch):
    import pymupdf

    from ultrakb.application.documents import import_document
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.refresh import refresh_status
    from ultrakb.application.views import view_scope
    from ultrakb.documents import read_document_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "TLS is forbidden.")
        (kb_dir / "install.pdf").write_bytes(pdf.tobytes())

    def fail_summary_publication(*args, **kwargs):
        raise OSError("summary storage unavailable")

    monkeypatch.setattr("ultrakb.agent.compiler._write_summary", fail_summary_publication)

    failed = import_document(
        kb_dir,
        kb_dir / "install.pdf",
    )
    scope = view_scope(kb_dir, first.units[0].view_id)
    assert failed.status == "failed"
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"]["summaries/install.md"]
    assert resolve_query_views(kb_dir, "WinStack V1 TLS?", scope=scope).views == ()
    assert "TLS is required." in read_document_source(kb_dir, first.source_id)["content"]
    import asyncio

    from ultrakb.application.refresh import refresh_knowledge_view

    before = refresh_status(kb_dir, scope=scope)
    assert asyncio.run(refresh_knowledge_view(kb_dir, scope=scope)).status == "blocked"
    assert refresh_status(kb_dir, scope=scope) == before


def test_refresh_retires_pages_without_sources_and_keeps_a_snapshot(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.pages import read_page
    from ultrakb.application.refresh import refresh_knowledge_view, refresh_status
    from ultrakb.application.removal import remove_document
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    history = view_scope(
        kb_dir, scope.view_id, historical_revision=first.units[0].knowledge_revision_id
    )
    remove_document(kb_dir, first.source_id, scope=scope)
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("No source means no model call")),
    )
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "completed"
    assert not (scope.wiki_dir / "summaries/install.md").exists()
    assert "TLS is required." in read_page(kb_dir, "summaries/install", scope=history).content
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == {}
    from ultrakb.application.refresh import list_knowledge_history

    history = list_knowledge_history(kb_dir, scope=scope)
    assert history[-1]["knowledge_revision_id"] == first.units[0].knowledge_revision_id
    assert "summaries/install.md" in history[-1]["pages"]


def test_reader_reports_pending_evidence_and_historical_status(kb_dir, monkeypatch):
    from ultrakb.application.pages import read_page
    from ultrakb.application.query_views import read_query_page, resolve_query_views
    from ultrakb.application.removal import remove_document
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    history = view_scope(
        kb_dir, scope.view_id, historical_revision=first.units[0].knowledge_revision_id
    )
    remove_document(kb_dir, first.source_id, scope=scope)
    page = read_page(kb_dir, "summaries/install", scope=scope)
    assert page.validity == "needs_refresh"
    assert page.source_revision_ids == (first.source_revision_id,)
    assert read_page(kb_dir, "summaries/install", scope=history).validity == "historical"
    selection = resolve_query_views(kb_dir, "WinStack V1", scope=history)
    assert "historical" in read_query_page(selection, "summaries/install.md", view_id=scope.view_id)


def test_confirming_empty_contribution_requires_current_source(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.refresh import (
        confirm_empty_source,
        refresh_knowledge_view,
        refresh_status,
    )
    from ultrakb.application.views import view_scope
    from ultrakb.source_catalog import read_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    generation = read_source(kb_dir, first.source_id).target_generation
    result = confirm_empty_source(kb_dir, first.source_id, generation=generation)
    assert result.status == "completed"
    assert (
        refresh_status(kb_dir, scope=scope)["needs_refresh"]["summaries/install.md"][0]["kind"]
        == "empty"
    )
    assert confirm_empty_source(kb_dir, first.source_id, generation=generation).status == "conflict"
    assert asyncio.run(refresh_knowledge_view(kb_dir, scope=scope)).status == "completed"
    assert not (scope.wiki_dir / "summaries/install.md").exists()


def test_refresh_uses_only_the_remaining_sources(kb_dir, monkeypatch, live_llm):
    import asyncio

    from ultrakb.application.query_views import read_query_page, resolve_query_views
    from ultrakb.application.refresh import refresh_knowledge_view, refresh_status
    from ultrakb.application.removal import remove_document
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    second = _import_rule(
        kb_dir,
        monkeypatch,
        "operate",
        "Port 443 is open.",
    )
    scope = view_scope(kb_dir, first.units[0].view_id)
    remove_document(kb_dir, first.source_id, scope=scope)
    before = len(live_llm.calls)

    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "completed", result.message
    prompts = str([call["messages"] for call in live_llm.calls[before:]])
    assert len(live_llm.calls) > before and "TLS is required" not in prompts
    selection = resolve_query_views(kb_dir, "WinStack V1 ports?", scope=scope)
    assert "Port 443 is open" in read_query_page(
        selection, "summaries/operate.md", view_id=scope.view_id
    )
    assert selection.views[0].source_revision_ids == (second.source_revision_id,)
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == {}


def test_refresh_protects_manual_pages_until_the_saved_diff_is_accepted(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import (
        accept_refresh_proposal,
        read_refresh_proposal,
        refresh_knowledge_view,
        refresh_status,
    )
    from ultrakb.application.removal import remove_document
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    page = read_page(kb_dir, "summaries/install", scope=scope)
    save_page(
        kb_dir, page.path, "Human-reviewed connection policy.", version=page.version, scope=scope
    )
    remove_document(kb_dir, first.source_id, scope=scope)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "awaiting_confirmation"
    proposal = read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    assert "Human-reviewed connection policy" in proposal["diff"]
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"]
    accepted = accept_refresh_proposal(
        kb_dir, result.proposal_id, version=proposal["version"], scope=scope
    )
    assert accepted.status == "completed"
    assert not (scope.wiki_dir / "summaries/install.md").exists()
    assert not refresh_status(kb_dir, scope=scope)["needs_refresh"]


def test_failed_refresh_preserves_the_old_result_and_reasons(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.refresh import refresh_knowledge_view, refresh_status
    from ultrakb.application.removal import remove_document
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    _import_rule(
        kb_dir,
        monkeypatch,
        "operate",
        "Port 443 is open.",
    )
    scope = view_scope(kb_dir, first.units[0].view_id)
    remove_document(kb_dir, first.source_id, scope=scope)
    before = refresh_status(kb_dir, scope=scope)

    def fail_summary_publication(*args, **kwargs):
        raise OSError("summary storage unavailable")

    monkeypatch.setattr("ultrakb.agent.compiler._write_summary", fail_summary_publication)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    assert result.status == "failed"
    assert refresh_status(kb_dir, scope=scope) == before
    assert "TLS is required" in (scope.wiki_dir / "summaries/install.md").read_text()


def test_late_refresh_acceptance_cannot_clear_new_reasons(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import (
        accept_refresh_proposal,
        confirm_empty_source,
        read_refresh_proposal,
        refresh_knowledge_view,
        refresh_status,
    )
    from ultrakb.application.views import view_scope
    from ultrakb.source_catalog import read_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    page = read_page(kb_dir, "summaries/install", scope=scope)
    save_page(kb_dir, page.path, "Human policy", version=page.version, scope=scope)
    generation = read_source(kb_dir, first.source_id).target_generation
    confirm_empty_source(kb_dir, first.source_id, generation=generation)
    result = asyncio.run(refresh_knowledge_view(kb_dir, scope=scope))
    proposal = read_refresh_proposal(kb_dir, result.proposal_id, scope=scope)
    confirm_empty_source(kb_dir, first.source_id, generation=generation + 1)
    newer = refresh_status(kb_dir, scope=scope)
    accepted = accept_refresh_proposal(
        kb_dir, result.proposal_id, version=proposal["version"], scope=scope
    )
    assert accepted.status == "conflict"
    assert refresh_status(kb_dir, scope=scope) == newer


@pytest.mark.parametrize("entrypoint", ["cli", "api", "worker"])
def test_withdraw_and_refresh_are_available_in_each_adapter(kb_dir, monkeypatch, entrypoint):
    from ultrakb.application.refresh import refresh_status
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    scope = view_scope(kb_dir, first.units[0].view_id)
    if entrypoint == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        prefix = [
            "--kb-dir",
            str(kb_dir),
        ]
        removed = CliRunner().invoke(cli, [*prefix, "remove", first.source_id, "--yes"])
        assert removed.exit_code == 0, removed.output
        assert refresh_status(kb_dir, scope=scope)["needs_refresh"]
        refreshed = CliRunner().invoke(cli, [*prefix, "refresh", "run"])
        assert refreshed.exit_code == 0, refreshed.output
        assert '"completed"' in refreshed.output
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app
        from ultrakb.config import register_kb_alias

        register_kb_alias("refresh-test", kb_dir)
        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
        with TestClient(create_app()) as client:
            removed = client.post(
                "/api/v1/remove",
                json={
                    "kb": "refresh-test",
                    "identifier": first.source_id,
                    "view_id": scope.view_id,
                },
            )
            assert removed.status_code == 200 and removed.json()["status"] == "removed", (
                removed.text
            )
            refreshed = client.post("/api/v1/refresh", json={"kb": "refresh-test"})
            assert refreshed.status_code == 200 and refreshed.json()["status"] == "completed", (
                refreshed.text
            )
    else:
        from ultrakb.application.removal import preview_removal
        from ultrakb.runtime.requests import RefreshKnowledge, RemoveDocument
        from ultrakb.runtime.tasks import TaskManager

        preview = preview_removal(kb_dir, first.source_id, scope=scope)
        manager = TaskManager(history_dir=kb_dir / "task-history")
        try:
            task = manager.submit(
                kb_dir,
                [
                    RemoveDocument(first.source_id, preview.version, view_id=scope.view_id),
                    RefreshKnowledge(),
                ],
            )
            assert manager.wait(task, timeout=20).state == "completed"
        finally:
            manager.shutdown()
    assert not (scope.wiki_dir / "summaries/install.md").exists()
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == {}


def test_unchanged_pending_page_keeps_its_old_input_revision(kb_dir, monkeypatch):
    import pymupdf

    from ultrakb.application.documents import import_document
    from ultrakb.application.pages import read_page
    from ultrakb.application.views import view_scope

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    second = _import_rule(
        kb_dir,
        monkeypatch,
        "operate",
        "Port 443 is open.",
    )
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "TLS is optional.")
        (kb_dir / "install.pdf").write_bytes(pdf.tobytes())
    updated = import_document(
        kb_dir,
        kb_dir / "install.pdf",
    )
    assert updated.status == "added", updated.message
    page = read_page(kb_dir, "summaries/operate", scope=view_scope(kb_dir, first.units[0].view_id))
    assert page.validity == "needs_refresh"
    assert set(page.source_revision_ids) == {first.source_revision_id, second.source_revision_id}


def test_cleanup_retains_history_and_defers_when_ownership_is_unreadable(kb_dir, monkeypatch):
    from ultrakb.application.refresh import cleanup_unreferenced_artifacts
    from ultrakb.application.removal import remove_document
    from ultrakb.documents import read_document_source

    first = _import_rule(kb_dir, monkeypatch, "install", "TLS is required.")
    original = read_document_source(kb_dir, first.source_id)["original_path"]
    remove_document(kb_dir, first.source_id)
    retained = cleanup_unreferenced_artifacts(kb_dir, [original])
    assert retained["retained"] == [original]
    assert (kb_dir / original).is_file()
    orphan = ".ultrakb/artifacts/" + "d" * 64 + "/content.pdf"
    (kb_dir / orphan).parent.mkdir(parents=True)
    (kb_dir / orphan).write_bytes(b"orphan")
    broken = kb_dir / ".ultrakb/catalog/discovery-intents" / ("e" * 32 + ".json")
    broken.write_text("{broken")
    deferred = cleanup_unreferenced_artifacts(kb_dir, [orphan])
    assert deferred["retained"] == [orphan] and not deferred["complete"]
    assert (kb_dir / orphan).is_file()


def test_cleanup_does_not_guess_ownership_of_inline_managed_links(kb_dir):
    from ultrakb.application.pages import save_page
    from ultrakb.application.refresh import cleanup_unreferenced_artifacts

    artifact = ".ultrakb/artifacts/" + "f" * 64 + "/content.pdf"
    (kb_dir / artifact).parent.mkdir(parents=True)
    (kb_dir / artifact).write_bytes(b"retained manually linked evidence")
    page = kb_dir / "wiki/concepts/manual.md"
    page.write_text("# Manual evidence")
    save_page(kb_dir, "concepts/manual", f"[Original](../../{artifact})")
    result = cleanup_unreferenced_artifacts(kb_dir, [artifact])
    assert result["retained"] == [artifact]
    assert (kb_dir / artifact).is_file()
