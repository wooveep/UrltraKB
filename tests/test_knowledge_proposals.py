"""Human edits and explicit acceptance use the same durable knowledge boundary."""

import json
from types import SimpleNamespace

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401

from ultrakb.application.pages import read_page, save_page
from ultrakb.knowledge_scope import legacy_scope


def test_manual_edit_creates_a_new_snapshot_without_rewriting_the_generated_baseline(kb_dir):
    from ultrakb.unit_publication import read_head

    page = kb_dir / "wiki/concepts/fact.md"
    page.write_text("Original explanation.")
    (kb_dir / "wiki/log.md").write_text("# Previous operations\n")
    opened = read_page(kb_dir, "concepts/fact")
    assert (
        save_page(kb_dir, opened.path, "Human explanation.", version=opened.version).status
        == "saved"
    )
    first = read_head(kb_dir)
    assert first.knowledge_revision_id is not None
    assert first.generated_baselines == {}
    saved = read_page(kb_dir, "concepts/fact")
    save_page(kb_dir, saved.path, "Another human explanation.", version=saved.version)
    original_snapshot = kb_dir / ".ultrakb/knowledge/legacy/revisions" / first.knowledge_revision_id
    assert (original_snapshot / "wiki/concepts/fact.md").read_text() == "Human explanation."
    assert json.loads((original_snapshot / "manifest.json").read_text())["change_kind"] == "manual"

    from ultrakb.application.answers import save_exploration
    from ultrakb.knowledge_scope import KnowledgeScope

    history = KnowledgeScope(kb_dir, original_snapshot / "wiki")
    with pytest.raises(ValueError, match="read-only"):
        save_exploration(kb_dir, "Later question", "Later answer", scope=history)
    assert not (original_snapshot / "wiki/explorations/later-question.md").exists()
    from ultrakb.log import append_log

    log = history.wiki_dir / "log.md"
    previous = log.read_bytes()
    append_log(history.wiki_dir, "query", "Later question", scope=history)
    assert log.read_bytes() == previous


@pytest.fixture
def pending_proposal(kb_dir, live_llm):
    import pymupdf
    from publication_fixtures import publish_document

    from ultrakb.locks import atomic_write_text

    source = kb_dir / "guide.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "The recorded coolant limit is 17 degrees.")
        pdf.save(source)
    atomic_write_text(kb_dir / "wiki/summaries/guide.md", "Human explanation.")
    before = len(live_llm.calls)
    result = publish_document(
        kb_dir,
        source,
        "# Coolant limit\n\nThe recorded coolant limit is 17 degrees.",
        bundle=live_llm.credentials,
        expected_status="awaiting_confirmation",
    )
    assert result.status == "blocked"
    assert len(live_llm.calls) == before
    return result.units[0].proposal_id


def test_real_import_delivers_a_reviewable_proposal_without_replacing_manual_text(kb_dir, live_llm):
    import pymupdf

    from ultrakb.application.documents import import_document
    from ultrakb.application.proposals import accept_proposal, read_proposal
    from ultrakb.locks import atomic_write_text
    from ultrakb.source_catalog import read_source_revision

    source = kb_dir / "guide.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "The recorded coolant limit is 17 degrees.")
        pdf.save(source)
    original = source.read_bytes()
    atomic_write_text(kb_dir / "wiki/summaries/guide.md", "Human explanation.")
    before = len(live_llm.calls)
    result = import_document(
        kb_dir, source, bundle=live_llm.credentials, scope=legacy_scope(kb_dir)
    )
    assert result.status == "blocked", result.message
    assert len(live_llm.calls) > before
    assert result.model_usage["current"]["input_total"] > 0
    assert result.model_usage["current"]["output_total"] > 0
    assert read_page(kb_dir, "summaries/guide").body == "Human explanation."
    review = read_proposal(kb_dir, result.units[0].proposal_id)
    assert "-Human explanation." in review.diff
    assert "+" in review.diff and "17" in review.diff and "coolant" in review.diff.lower()
    source.unlink()
    accepted = accept_proposal(kb_dir, review.proposal_id, version=review.version)
    assert accepted.status == "added"
    revision = read_source_revision(kb_dir, accepted.source_revision_id)
    assert (kb_dir / revision.original).read_bytes() == original
    assert "17" in read_page(kb_dir, "summaries/guide").body


def test_proposal_remains_reviewable_and_can_be_accepted_after_import_cleanup(
    kb_dir, pending_proposal
):
    from ultrakb.application.proposals import accept_proposal, read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    assert "-Human explanation." in opened.diff
    assert "+" in opened.diff and "17" in opened.diff and "coolant" in opened.diff.lower()
    accepted = accept_proposal(kb_dir, pending_proposal, version=opened.version)
    assert accepted.status == "added"
    assert "17" in read_page(kb_dir, "summaries/guide").body
    assert accept_proposal(kb_dir, pending_proposal, version=opened.version).status == "skipped"


def test_editing_after_review_keeps_the_proposal_and_current_page(kb_dir, pending_proposal):
    from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    page = read_page(kb_dir, "summaries/guide")
    save_page(kb_dir, page.path, "New human explanation.", version=page.version)
    assert accept_proposal(kb_dir, pending_proposal, version=opened.version).status == "blocked"
    assert read_page(kb_dir, page.path).body == "New human explanation."
    assert list_proposals(kb_dir)[0].proposal_id == pending_proposal


def test_proposals_are_confined_to_the_knowledge_base(kb_dir, pending_proposal, tmp_path):
    from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal
    from ultrakb.knowledge_scope import live_scope

    foreign = live_scope(tmp_path / "foreign")
    opened = read_proposal(kb_dir, pending_proposal)
    with pytest.raises(ValueError, match="different knowledge base"):
        list_proposals(kb_dir, scope=foreign)
    with pytest.raises(ValueError, match="different knowledge base"):
        read_proposal(kb_dir, pending_proposal, scope=foreign)
    with pytest.raises(ValueError, match="different knowledge base"):
        accept_proposal(kb_dir, pending_proposal, version=opened.version, scope=foreign)
    assert read_page(kb_dir, "summaries/guide").body == "Human explanation."


def test_recompile_preserves_manual_changes_as_a_new_proposal(
    kb_dir, pending_proposal, monkeypatch
):
    import asyncio

    from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal
    from ultrakb.application.recompilation import recompile_document

    proposal = read_proposal(kb_dir, pending_proposal)
    accepted = accept_proposal(kb_dir, pending_proposal, version=proposal.version)
    page = read_page(kb_dir, "summaries/guide")
    save_page(kb_dir, page.path, "Edited by the user.", version=page.version)
    result = asyncio.run(recompile_document(kb_dir, accepted.source_id))
    assert result.status == "blocked"
    assert read_page(kb_dir, page.path).body == "Edited by the user."
    assert len(list_proposals(kb_dir)) == 1


@pytest.mark.parametrize("kind", ["compile", "manual"])
def test_incomplete_candidate_cannot_be_reviewed_or_accepted(kb_dir, pending_proposal, kind):
    from ultrakb.application.proposals import read_proposal

    path = kb_dir / ".ultrakb/proposals" / pending_proposal / "proposal.json"
    data = json.loads(path.read_text())
    data["candidate_manifest"].pop("normalized_source")
    data["candidate_manifest"]["change_kind"] = kind
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="compile"):
        read_proposal(kb_dir, pending_proposal)


def test_another_source_cannot_restore_a_manually_deleted_shared_page():
    from ultrakb.unit_publication import protected_knowledge_pages

    # A baseline survives the manual deletion. An incoming candidate from a
    # different source must still be reviewed before recreating the shared page.
    generated_baselines = {"concepts/fact.md": "original-generated-hash"}
    current_pages = {}
    incoming_pages = {"concepts/fact.md": "new-source-generated-hash"}
    assert protected_knowledge_pages(current_pages, incoming_pages, generated_baselines) == {
        "concepts/fact.md"
    }
    assert (
        protected_knowledge_pages(
            current_pages, {"concepts/another.md": "new"}, generated_baselines
        )
        == set()
    )
    assert (
        protected_knowledge_pages(generated_baselines, incoming_pages, generated_baselines) == set()
    )


def test_legacy_capture_can_resume_after_a_failed_transaction(kb_dir, monkeypatch):
    import asyncio

    from ultrakb.application.recompilation import recompile_document, select_recompilation
    from ultrakb.mutation import MutationSnapshot

    (kb_dir / ".ultrakb/hashes.json").write_text(json.dumps({"old": {"doc_name": "notes"}}))
    (kb_dir / "wiki/sources/notes.md").write_text("Retained evidence.")
    commit = MutationSnapshot.mark_committed

    def fail_capture(snapshot):
        if snapshot.operation == "capture-legacy-normalization":
            raise OSError("temporary disk failure")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", fail_capture)
    assert asyncio.run(recompile_document(kb_dir, "old")).status == "blocked"
    monkeypatch.setattr(MutationSnapshot, "mark_committed", commit)
    assert asyncio.run(recompile_document(kb_dir, "old")).status == "compiled"
    selection = select_recompilation(kb_dir, "notes")
    assert selection.status == "ready" and len(selection.targets) == 1


@pytest.mark.parametrize("entrypoint", ["cli", "api", "worker"])
def test_acceptance_entrypoints_use_the_saved_review(
    kb_dir, pending_proposal, monkeypatch, entrypoint
):
    from ultrakb.application.proposals import read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    if entrypoint == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        runner = CliRunner()
        shown = runner.invoke(cli, ["--kb-dir", str(kb_dir), "proposals", "show", pending_proposal])
        assert opened.version in shown.output
        accepted = runner.invoke(
            cli,
            [
                "--kb-dir",
                str(kb_dir),
                "proposals",
                "accept",
                pending_proposal,
                "--version",
                opened.version,
            ],
        )
        assert accepted.exit_code == 0, accepted.output
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app

        monkeypatch.setenv("ULTRAKB_API_TOKEN", "secret")
        monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/proposal/accept",
                json={
                    "kb": "test",
                    "proposal_id": pending_proposal,
                    "version": opened.version,
                },
                headers={"Authorization": "Bearer secret"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "added"
    else:
        from ultrakb.application.execution import ExecutionContext
        from ultrakb.runtime.requests import AcceptProposal
        from ultrakb.runtime.worker import _execute

        result = _execute(
            AcceptProposal(pending_proposal, opened.version),
            SimpleNamespace(kb_dir=str(kb_dir)),
            ExecutionContext(),
        )
        assert result.status == "completed"
    body = read_page(kb_dir, "summaries/guide").body
    assert "17" in body and "coolant" in body.lower()


def test_acceptance_resumes_a_cancelled_attempt(kb_dir, pending_proposal):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.proposals import accept_proposal, read_proposal
    from ultrakb.locks import LockCancelled
    from ultrakb.source_catalog import read_record, read_source_revision
    from ultrakb.source_records import DiscoveryIntent
    from ultrakb.unit_publication import read_unit_publication

    review = read_proposal(kb_dir, pending_proposal)
    context = ExecutionContext(
        cancelled=lambda: read_unit_publication(kb_dir, review.unit_id).status == "started"
    )
    with pytest.raises(LockCancelled):
        accept_proposal(kb_dir, pending_proposal, version=review.version, context=context)
    accepted = accept_proposal(kb_dir, pending_proposal, version=review.version)
    revision = read_source_revision(kb_dir, accepted.source_revision_id)
    intent = read_record(kb_dir, "discovery-intents", revision.discovery_intent_id, DiscoveryIntent)
    assert accepted.status == "added" and not intent.cancelled


def test_desktop_loads_the_persisted_difference(kb_dir, pending_proposal):
    import os
    import subprocess
    import sys

    pytest.importorskip("PySide6")
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys, time
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMainWindow
from ultrakb.desktop.io import LocalIO
from ultrakb.desktop.proposals import ProposalsDialog

app = QApplication([])
window = QMainWindow()
window.scope = None
window.io = LocalIO()
view = ProposalsDialog(window, Path(sys.argv[1]))
view.show()
deadline = time.monotonic() + 10
while not view.list.count() and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert view.list.count() == 1, view.status.text()
view.list.setCurrentRow(0)
assert '-Human explanation.' in view.diff.toPlainText()
assert view.accept_button.isEnabled()
view.close()
window.io.stop()
while not window.io.stopped() and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert window.io.stopped()
""",
            str(kb_dir),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True,
        timeout=20,
    )
